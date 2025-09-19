import os
import json
import asyncio
from threading import Lock
from typing import Optional, Set

from fastapi import FastAPI, Body, WebSocket, WebSocketDisconnect, Header, HTTPException, Query
from fastapi.responses import RedirectResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, conint

# ---- Configuration ----
# Defaults
DEFAULT_STATE = {"mode": 0, "brightness": 60}  # brightness 0..100 (0 => off)
# Path to JSON file for persistence (point to /data/state.json on Render with a Disk)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
# Optional API token for write access and WS connection (recommended)
API_TOKEN = os.environ.get("API_TOKEN")
# Comma-separated list of allowed origins for CORS (e.g., "https://your-frontend.vercel.app")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_state_lock = Lock()

@app.get("/", include_in_schema=False)
def root():
    # Option A: redirect to docs
    return RedirectResponse(url="/docs")
    # Option B (instead): return current state
    # return JSONResponse(load_state())

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Avoid noisy 404s from the browser
    return Response(status_code=204)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                mode = int(data.get("mode", DEFAULT_STATE["mode"]))
                brightness = int(data.get("brightness", DEFAULT_STATE["brightness"]))
                brightness = max(0, min(100, brightness))  # clamp 0..100
                return {"mode": mode, "brightness": brightness}
        except Exception:
            pass
    return DEFAULT_STATE.copy()

def save_state(state):
    # Ensure directory exists (STATE_FILE could be /data/state.json)
    dirpath = os.path.dirname(STATE_FILE)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def check_auth(authorization: Optional[str]):
    if API_TOKEN:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing/invalid Authorization")
        token = authorization.split(" ", 1)[1]
        if token != API_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid token")

class StatePatch(BaseModel):
    mode: Optional[int] = None
    brightness: Optional[conint(ge=0, le=100)] = None  # 0..100

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/state")
def get_state():
    with _state_lock:
        return load_state()


# ---- WebSocket manager ----
class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            self.active.add(websocket)
        # Push current state immediately
        await self.send_state(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            self.active.discard(websocket)

    async def send_state(self, websocket: WebSocket):
        await websocket.send_json({"type": "state", "payload": load_state()})

    async def broadcast(self, message: dict):
        dead = []
        async with self.lock:
            for ws in list(self.active):
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.active.discard(ws)

manager = ConnectionManager()

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    # Optional token check via query param
    if API_TOKEN and token != API_TOKEN:
        await websocket.close(code=1008)  # policy violation
        return
    await manager.connect(websocket)
    try:
        while True:
            # keepalive: client can send "ping" or any text; we ignore content
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

@app.post("/state")
async def patch_state(
    patch: StatePatch = Body(...),
    authorization: Optional[str] = Header(default=None),
):
    # Require token for modifications
    check_auth(authorization)
    changed = False
    with _state_lock:
        state = load_state()
        if patch.mode is not None:
            state["mode"] = int(patch.mode)
            changed = True
        if patch.brightness is not None:
            state["brightness"] = int(patch.brightness)
            changed = True
        if changed:
            save_state(state)
    if changed:
        await manager.broadcast({"type": "state", "payload": state})
    return state
