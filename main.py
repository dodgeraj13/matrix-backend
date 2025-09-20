import json
import os
from typing import Dict, List

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    Depends,
    HTTPException,
    status,
    Header,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# --------------------------
# Config / ENV
# --------------------------
API_TOKEN = (os.getenv("API_TOKEN", "") or "").strip()
STATE_FILE = os.getenv("STATE_FILE", "/tmp/state.json")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

VALID_ROTATIONS = {0, 90, 180, 270}
DEFAULT_STATE = {"mode": 0, "brightness": 60, "rotation": 0}

# --------------------------
# Models
# --------------------------
class State(BaseModel):
    mode: int = Field(0, ge=0, le=5)            # 0..5 (0=Idle, 1=MLB, 2=Music, 3=Clock, 4=Weather, 5=Picture)
    brightness: int = Field(60, ge=0, le=100)   # 0..100
    rotation: int = Field(0)                    # 0,90,180,270

    @field_validator("rotation")
    @classmethod
    def _rot_ok(cls, v: int) -> int:
        if v not in VALID_ROTATIONS:
            raise ValueError("rotation must be one of 0, 90, 180, 270")
        return v

# --------------------------
# App + CORS
# --------------------------
app = FastAPI(title="Matrix Backend", version="1.2")

allowed_origins = [o.strip() for o in CORS_ORIGINS.split(",")] if CORS_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# State persistence
# --------------------------
_state: Dict = DEFAULT_STATE.copy()

def load_state():
    global _state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            merged = DEFAULT_STATE.copy()
            for k in DEFAULT_STATE.keys():
                if k in data:
                    merged[k] = data[k]
            _state = merged
        else:
            save_state(_state)
    except Exception:
        _state = DEFAULT_STATE.copy()

def save_state(s: Dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

load_state()

# --------------------------
# Auth
# --------------------------
def _require_token(auth_header: str | None):
    # If API token is unset on server, allow (no auth in dev)
    if not API_TOKEN:
        return
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    tok = auth_header.split(" ", 1)[1].strip()
    if tok != API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad token")

def require_auth(
    authorization: str | None = Header(default=None),
    token: str | None = None,  # optional ?token=... fallback (handy for debugging)
):
    auth_val = authorization
    if (not auth_val) and token:
        auth_val = f"Bearer {token}"
    _require_token(auth_val)

# --------------------------
# WebSocket manager
# --------------------------
class WSManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: Dict):
        living = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
                living.append(ws)
            except Exception:
                pass
        self.active = living

manager = WSManager()

# --------------------------
# Routes
# --------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    return {"message": "Matrix backend running. See /docs", "endpoints": ["/state", "/ws", "/health"]}

@app.get("/state", response_model=State)
def get_state():
    return _state

@app.post("/state", response_model=State, dependencies=[Depends(require_auth)])
async def set_state(new: State):
    global _state
    _state = {"mode": new.mode, "brightness": new.brightness, "rotation": new.rotation}
    save_state(_state)
    await manager.broadcast({"type": "state", **_state})
    return _state

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # send current state on connect
        await websocket.send_json({"type": "state", **_state})
        while True:
            # keep connection; messages from clients are optional
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

# -------- Optional diag endpoint (remove later if you like)
@app.get("/_authcheck")
def authcheck(authorization: str | None = Header(default=None), token: str | None = None):
    api_set = bool(API_TOKEN)
    provided = authorization or (f"Bearer {token}" if token else None)
    has_bearer = bool(provided and provided.startswith("Bearer "))
    same = False
    if has_bearer and api_set:
        same = (provided.split(" ",1)[1].strip() == API_TOKEN)
    return {
        "api_token_set": api_set,
        "got_header": authorization is not None,
        "got_token_param": token is not None,
        "has_bearer_prefix": has_bearer,
        "match": same,
    }
