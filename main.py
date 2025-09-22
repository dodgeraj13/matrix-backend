from __future__ import annotations
import os, json, asyncio, hashlib
from typing import Optional, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, UploadFile, File, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

API_TOKEN = os.getenv("API_TOKEN", "MY_SUPER_TOKEN_123")
CORS_ORIGINS = [o.strip() for o in (os.getenv("CORS_ORIGINS","").split(",")) if o.strip()] or ["*"]

STATE_FILE = os.getenv("STATE_FILE")                 # e.g. /data/state.json
REDIS_URL  = os.getenv("REDIS_URL")                  # optional
REDIS_KEY  = os.getenv("REDIS_KEY", "matrix:state")
IMAGE_FILE = os.getenv("IMAGE_FILE", "/data/image.png")
IMAGE_KEY  = os.getenv("IMAGE_KEY", "matrix:image")  # redis key for raw PNG (optional)
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024  # 10 MB default

# ------------ Persistence ------------
class StateStore:
    def __init__(self):
        self._mem: Dict[str, Any] = {"mode": 0, "brightness": 60, "rotation": 0}
        self._mode: str = "memory"
        self._r = None
        if REDIS_URL:
            try:
                from redis import Redis
                self._r = Redis.from_url(REDIS_URL, decode_responses=True)
                self._r.ping()
                self._mode = "redis"
            except Exception as e:
                print(f"[store] REDIS_URL unusable: {e}; falling back.")
                self._r = None
        if self._mode != "redis" and STATE_FILE:
            self._mode = "file"
        print(f"[store] mode = {self._mode}")

    def load(self) -> Dict[str, Any]:
        try:
            if self._mode == "redis" and self._r:
                val = self._r.get(REDIS_KEY)
                if val: self._mem = json.loads(val)
            elif self._mode == "file" and STATE_FILE and os.path.exists(STATE_FILE):
                with open(STATE_FILE,"r") as f:
                    self._mem = json.load(f)
        except Exception as e:
            print(f"[store] load error: {e}")
        return self._mem.copy()

    def save(self, state: Dict[str, Any]) -> None:
        s = {
            "mode": int(state.get("mode", 0)),
            "brightness": max(0, min(100, int(state.get("brightness", 60)))),
            "rotation": int(state.get("rotation", 0)) % 360
        }
        try:
            if self._mode == "redis" and self._r:
                self._r.set(REDIS_KEY, json.dumps(s))
            elif self._mode == "file" and STATE_FILE:
                os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
                with open(STATE_FILE,"w") as f:
                    json.dump(s, f)
            self._mem = s
        except Exception as e:
            print(f"[store] save error: {e}")
            self._mem = s

store = StateStore()
_state = store.load()

# image cache / etag
_image_etag: Optional[str] = None

def _compute_etag(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            h = hashlib.md5()
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

def _load_etag_from_disk():
    global _image_etag
    if os.path.exists(IMAGE_FILE):
        _image_etag = _compute_etag(IMAGE_FILE)

# ------------ WS Hub ------------
class Hub:
    def __init__(self):
        self.active: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def register(self, ws: WebSocket):
        async with self.lock:
            self.active.add(ws)

    async def unregister(self, ws: WebSocket):
        async with self.lock:
            self.active.discard(ws)

    async def broadcast(self, message: Dict[str, Any]):
        dead = []
        async with self.lock:
            for ws in list(self.active):
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.active.discard(ws)

hub = Hub()

# ------------ Models ------------
class StateIn(BaseModel):
    mode: Optional[int] = Field(default=None, ge=0)
    brightness: Optional[int] = Field(default=None, ge=0, le=100)
    rotation: Optional[int] = Field(default=None)

# ------------ App / CORS ------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    _state = store.load()
    _load_etag_from_disk()
    yield
    try: store.save(_state)
    except Exception: pass

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["Authorization","Content-Type","*"],
    expose_headers=["ETag"],  # so frontend can read ETag
)

# ------------ Helpers ------------
def auth_ok(authorization: Optional[str]) -> bool:
    if not API_TOKEN: return True
    if not authorization: return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2: return False
    scheme, token = parts
    return scheme.lower() == "bearer" and token == API_TOKEN

def current_state() -> Dict[str, Any]:
    return {
        "mode": int(_state.get("mode", 0)),
        "brightness": int(_state.get("brightness", 60)),
        "rotation": int(_state.get("rotation", 0)),
    }

async def save_and_broadcast():
    store.save(_state)
    await hub.broadcast({"type": "state", **current_state()})

# ------------ HTTP routes ------------
@app.get("/", include_in_schema=False)
def root(): return RedirectResponse("/docs")

@app.get("/health")
def health(): return JSONResponse({"ok": True})

@app.get("/state")
def get_state(): return JSONResponse(current_state())

@app.post("/state")
async def set_state(payload: StateIn, authorization: Optional[str] = Header(None)):
    if not auth_ok(authorization): raise HTTPException(401, "Unauthorized")
    changed = False
    if payload.mode is not None and payload.mode != _state.get("mode"):
        _state["mode"] = int(payload.mode); changed = True
    if payload.brightness is not None and payload.brightness != _state.get("brightness"):
        _state["brightness"] = max(0, min(100, int(payload.brightness))); changed = True
    if payload.rotation is not None and payload.rotation != _state.get("rotation"):
        _state["rotation"] = int(payload.rotation) % 360; changed = True
    if changed: await save_and_broadcast()
    return JSONResponse(current_state())

# --- Image: GET returns PNG with ETag; POST accepts multipart up to MAX_UPLOAD ---
@app.get("/image")
def get_image(request: Request):
    global _image_etag
    if not os.path.exists(IMAGE_FILE):
        return Response(status_code=404)
    etag = _image_etag or _compute_etag(IMAGE_FILE)
    inm = request.headers.get("if-none-match")
    headers = {"ETag": etag} if etag else {}
    if etag and inm == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(IMAGE_FILE, media_type="image/png", headers=headers)

@app.post("/image")
async def post_image(file: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    if not auth_ok(authorization): raise HTTPException(401, "Unauthorized")
    if not file.filename.lower().endswith(".png"):
        raise HTTPException(400, "Only .png is accepted")

    os.makedirs(os.path.dirname(IMAGE_FILE), exist_ok=True)

    # stream to disk, enforce limit
    size = 0
    with open(IMAGE_FILE, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                try: os.remove(IMAGE_FILE)
                except Exception: pass
                raise HTTPException(413, f"File too large (> {MAX_UPLOAD//1024//1024} MB)")
            out.write(chunk)

    # refresh ETag
    global _image_etag
    _image_etag = _compute_etag(IMAGE_FILE)

    # Optional: broadcast a hint so frontends know image changed
    await hub.broadcast({"type":"image","etag": _image_etag})

    return JSONResponse({"ok": True, "size": size, "etag": _image_etag})

@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    await hub.register(ws)
    try:
        await ws.send_json({"type":"state", **current_state()})
        while True:
            await ws.receive_text()  # keep alive; we don't require client messages
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.unregister(ws)
