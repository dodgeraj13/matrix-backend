# backend: main.py (replace the whole file)
cat > main.py <<'PY'
from __future__ import annotations
import os, json, asyncio
from typing import Optional, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

API_TOKEN   = os.getenv("API_TOKEN", "MY_SUPER_TOKEN_123")
CORS_ORIGINS = [o.strip() for o in (os.getenv("CORS_ORIGINS","").split(",")) if o.strip()] or ["*"]

STATE_FILE  = os.getenv("STATE_FILE")      # optional file persistence
REDIS_URL   = os.getenv("REDIS_URL")       # use Upstash / Redis here
REDIS_KEY   = os.getenv("REDIS_KEY", "matrix:state")
REDIS_IMG   = os.getenv("REDIS_IMG", "matrix:image")
REDIS_IMG_REV = os.getenv("REDIS_IMG_REV", "matrix:image_rev")

# ---------- Persistence ----------
class StateStore:
    def __init__(self):
        self._mem: Dict[str, Any] = {"mode":0, "brightness":60, "rotation":0}
        self._mode = "memory"
        self._r = None
        if REDIS_URL:
            try:
                from redis import Redis
                self._r = Redis.from_url(REDIS_URL, decode_responses=False)  # bytes for image
                self._r.ping()
                self._mode = "redis"
            except Exception as e:
                print(f"[store] REDIS_URL set but unusable: {e}; falling back.")
                self._r = None
        if self._mode != "redis" and STATE_FILE:
            self._mode = "file"
        print(f"[store] mode = {self._mode}")

    def load(self) -> Dict[str, Any]:
        try:
            if self._mode == "redis" and self._r:
                b = self._r.get(REDIS_KEY)
                if b:
                    self._mem = json.loads(b.decode("utf-8"))
            elif self._mode == "file" and STATE_FILE and os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    self._mem = json.load(f)
        except Exception as e:
            print(f"[store] load error: {e}")
        return self._mem.copy()

    def save(self, s: Dict[str,Any]) -> None:
        s = {
            "mode": int(s.get("mode",0)),
            "brightness": max(0, min(100, int(s.get("brightness",60)))),
            "rotation": int(s.get("rotation",0)) % 360,
        }
        self._mem = s
        try:
            if self._mode == "redis" and self._r:
                self._r.set(REDIS_KEY, json.dumps(s).encode("utf-8"))
            elif self._mode == "file" and STATE_FILE:
                os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
                with open(STATE_FILE, "w") as f:
                    json.dump(s, f)
        except Exception as e:
            print(f"[store] save error: {e}")

    # image ops
    def save_image(self, png_bytes: bytes) -> int:
        if self._mode == "redis" and self._r:
            pipe = self._r.pipeline()
            pipe.set(REDIS_IMG, png_bytes)
            pipe.incr(REDIS_IMG_REV)
            _, new_rev = pipe.execute()
            return int(new_rev)
        # file or mem fallback
        if STATE_FILE:
            base = os.path.dirname(STATE_FILE)
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "picture.png"), "wb") as f:
                f.write(png_bytes)
            # emulate rev
            rev_path = os.path.join(base, "picture.rev")
            rev = 1
            if os.path.exists(rev_path):
                try:
                    rev = int(open(rev_path).read().strip()) + 1
                except: pass
            open(rev_path,"w").write(str(rev))
            return rev
        # memory
        self._img = png_bytes
        self._img_rev = getattr(self, "_img_rev", 0) + 1
        return self._img_rev

    def get_image(self) -> Optional[bytes]:
        if self._mode == "redis" and self._r:
            return self._r.get(REDIS_IMG)
        if STATE_FILE:
            p = os.path.join(os.path.dirname(STATE_FILE), "picture.png")
            if os.path.exists(p):
                return open(p,"rb").read()
            return None
        # memory
        return getattr(self, "_img", None)

store = StateStore()
_state = store.load()

# ---------- WS Hub ----------
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

    async def broadcast(self, message: Dict[str,Any]):
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

# ---------- Models ----------
class StateIn(BaseModel):
    mode: Optional[int] = Field(default=None, ge=0)
    brightness: Optional[int] = Field(default=None, ge=0, le=100)
    rotation: Optional[int] = Field(default=None)

# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    _state = store.load()
    yield
    try:
        store.save(_state)
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["Authorization","Content-Type","*"],
)

# ---------- Helpers ----------
def auth_ok(authorization: Optional[str]) -> bool:
    if not API_TOKEN:
        return True
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return False
    return parts[0].lower() == "bearer" and parts[1] == API_TOKEN

def current_state() -> Dict[str,Any]:
    return {
        "mode": int(_state.get("mode",0)),
        "brightness": int(_state.get("brightness",60)),
        "rotation": int(_state.get("rotation",0)),
    }

async def save_and_broadcast():
    store.save(_state)
    await hub.broadcast({"type":"state", **current_state()})

# ---------- Routes ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")

@app.get("/health")
def health():
    return JSONResponse({"ok":True})

@app.get("/state")
def get_state():
    return JSONResponse(current_state())

@app.post("/state")
async def set_state(payload: StateIn, authorization: Optional[str] = Header(None)):
    if not auth_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    changed = False
    if payload.mode is not None and payload.mode != _state.get("mode"):
        _state["mode"] = int(payload.mode); changed = True
    if payload.brightness is not None and payload.brightness != _state.get("brightness"):
        _state["brightness"] = max(0,min(100,int(payload.brightness))); changed = True
    if payload.rotation is not None and payload.rotation != _state.get("rotation"):
        _state["rotation"] = int(payload.rotation) % 360; changed = True
    if changed:
        await save_and_broadcast()
    return JSONResponse(current_state())

@app.post("/image")
async def upload_image(
    request: Request,
    authorization: Optional[str] = Header(None),
    content_type: Optional[str] = Header(None),
    file: UploadFile = File(None)
):
    if not auth_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Accept either raw PNG body or multipart form (file)
    body: bytes
    if file is not None:
        if (file.content_type or "").lower() != "image/png":
            raise HTTPException(status_code=415, detail="Only image/png allowed.")
        body = await file.read()
    else:
        body = await request.body()
        if not body or ("image/png" not in (content_type or "").lower()):
            raise HTTPException(status_code=415, detail="Send raw PNG with Content-Type: image/png or use multipart 'file'.")

    if len(body) > 200_000:
        raise HTTPException(status_code=413, detail="Image too large (limit ~200KB).")

    rev = store.save_image(body)
    # Let Pis know there's a new image to pull
    await hub.broadcast({"type":"image", "rev": int(rev)})
    return JSONResponse({"ok":True, "rev": int(rev)})

@app.get("/image")
def get_image():
    data = store.get_image()
    if not data:
        return PlainTextResponse("No image", status_code=404)
    return Response(content=data, media_type="image/png")

@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    await hub.register(ws)
    try:
        await ws.send_json({"type":"state", **current_state()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.unregister(ws)
PY
