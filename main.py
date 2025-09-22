from __future__ import annotations
import os, json, asyncio, hashlib, time, base64
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --- Config / env ---
API_TOKEN = os.getenv("API_TOKEN", "MY_SUPER_TOKEN_123")

# CORS: allow your Vercel frontend explicitly (and localhost for dev)
origins_list = [o.strip() for o in (os.getenv("CORS_ORIGINS","").split(",")) if o.strip()]
if not origins_list:
    origins_list = ["https://matrix-frontend-hh2z.vercel.app", "http://localhost:3000", "*"]

STATE_FILE = os.getenv("STATE_FILE", "/data/state.json")
IMG_FILE   = os.getenv("IMG_FILE", "/data/picture.png")

# --- Persistence helpers ---
def _load_json(path: str, fallback: dict) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return dict(fallback)

def _save_json(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)

def _etag_of_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

# --- State store ---
_state: Dict[str, Any] = {"mode": 0, "brightness": 60, "rotation": 0}

# --- WS hub ---
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

# --- models ---
class StateIn(BaseModel):
    mode: Optional[int] = Field(default=None, ge=0)
    brightness: Optional[int] = Field(default=None, ge=0, le=100)
    rotation: Optional[int] = Field(default=None)  # 0/90/180/270

def auth_ok(authorization: Optional[str]) -> bool:
    if not API_TOKEN:
        return True
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return False
    return parts[0].lower() == "bearer" and parts[1] == API_TOKEN

def current_state() -> Dict[str, Any]:
    return {
        "mode": int(_state.get("mode", 0)),
        "brightness": int(_state.get("brightness", 60)),
        "rotation": int(_state.get("rotation", 0)),
    }

async def save_and_broadcast():
    _save_json(STATE_FILE, _state)
    await hub.broadcast({"type": "state", **current_state()})

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    _state = _load_json(STATE_FILE, _state)
    # ensure /data exists on Render free disk
    try: os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    except Exception: pass
    yield
    try: _save_json(STATE_FILE, _state)
    except Exception: pass

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["ETag"],  # so frontend can read it
)

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")

@app.get("/health")
def health():
    return {"ok": True}

# --- state ---
@app.get("/state")
def get_state():
    return current_state()

@app.post("/state")
async def set_state(payload: StateIn, authorization: Optional[str] = Header(None)):
    if not auth_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    changed = False
    if payload.mode is not None and payload.mode != _state.get("mode"):
        _state["mode"] = int(payload.mode); changed = True
    if payload.brightness is not None and payload.brightness != _state.get("brightness"):
        _state["brightness"] = max(0, min(100, int(payload.brightness))); changed = True
    if payload.rotation is not None and payload.rotation != _state.get("rotation"):
        r = int(payload.rotation)
        if r not in (0,90,180,270): r = 0
        _state["rotation"] = r; changed = True

    if changed:
        await save_and_broadcast()
    return current_state()

# --- image (GET/POST) ---
@app.get("/image")
def get_image(if_none_match: Optional[str] = Header(None)):
    try:
        with open(IMG_FILE, "rb") as f:
            b = f.read()
    except Exception:
        # empty 64x64 black if not found
        b = bytes()
    etag = _etag_of_bytes(b)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    return Response(content=b, media_type="image/png", headers={"ETag": etag})

@app.post("/image")
async def post_image(
    request: Request,
    authorization: Optional[str] = Header(None),
    file: UploadFile | None = File(default=None),   # multipart support
    data_url: Optional[str] = Form(default=None),   # data URL support
):
    if not auth_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw: bytes | None = None

    # 1) multipart/form-data
    if file is not None:
        raw = await file.read()

    # 2) data URL in form field
    if raw is None and data_url:
        # expect "data:image/png;base64,...."
        if "," in data_url:
            b64 = data_url.split(",", 1)[1]
            raw = base64.b64decode(b64)

    # 3) raw body with image/png
    if raw is None:
        if request.headers.get("content-type", "").startswith("image/"):
            raw = await request.body()

    if not raw:
        raise HTTPException(status_code=422, detail="No image payload found")

    # enforce a bigger but safe limit (10 MB)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large")

    os.makedirs(os.path.dirname(IMG_FILE), exist_ok=True)
    with open(IMG_FILE, "wb") as f:
        f.write(raw)

    etag = _etag_of_bytes(raw)
    return JSONResponse({"ok": True, "etag": etag})
    
# --- WS ---
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
