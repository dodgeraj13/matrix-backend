from __future__ import annotations

import os
import json
import asyncio
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager


# ---------------- Env / Config ----------------
API_TOKEN   = os.getenv("API_TOKEN", "MY_SUPER_TOKEN_123")
STATE_FILE  = os.getenv("STATE_FILE")   # e.g. /data/state.json (requires Render disk)
REDIS_URL   = os.getenv("REDIS_URL")    # e.g. rediss://:password@host:port
REDIS_KEY   = os.getenv("REDIS_KEY", "matrix:state")
CORS_ORIGINS_ENV = os.getenv("CORS_ORIGINS", "")

# ---------------- Persistence ----------------
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
                print(f"[store] REDIS_URL set but unusable: {e}; falling back.")
                self._r = None

        if self._mode != "redis" and STATE_FILE:
            self._mode = "file"

        print(f"[store] mode = {self._mode}")

    def load(self) -> Dict[str, Any]:
        try:
            if self._mode == "redis" and self._r:
                val = self._r.get(REDIS_KEY)
                if val:
                    self._mem = json.loads(val)
            elif self._mode == "file" and STATE_FILE:
                if os.path.exists(STATE_FILE):
                    with open(STATE_FILE, "r") as f:
                        self._mem = json.load(f)
        except Exception as e:
            print(f"[store] load error: {e}")
        return self._mem.copy()

    def save(self, state: Dict[str, Any]) -> None:
        s = {
            "mode": int(state.get("mode", 0)),
            "brightness": max(0, min(100, int(state.get("brightness", 60)))),
            "rotation": int(state.get("rotation", 0)) % 360,
        }
        try:
            if self._mode == "redis" and self._r:
                self._r.set(REDIS_KEY, json.dumps(s))
            elif self._mode == "file" and STATE_FILE:
                os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
                with open(STATE_FILE, "w") as f:
                    json.dump(s, f)
            self._mem = s
        except Exception as e:
            print(f"[store] save error: {e}")
            self._mem = s


store = StateStore()
_state = store.load()


# ---------------- WebSocket Hub ----------------
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


# ---------------- Models ----------------
class StateIn(BaseModel):
    mode: Optional[int] = Field(default=None, ge=0)
    brightness: Optional[int] = Field(default=None, ge=0, le=100)
    rotation: Optional[int] = Field(default=None)  # 0, 90, 180, 270, etc.


# ---------------- Lifespan ----------------
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

# ---------------- CORS (single middleware) ----------------
origins_list = [o.strip() for o in CORS_ORIGINS_ENV.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins_list,  # e.g. ["https://matrix-frontend-xxx.vercel.app", "http://localhost:3000"]
    allow_credentials=False,     # keep False if using "*"
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "*"],
)
print("CORS allow_origins =", origins_list)


# ---------------- Helpers ----------------
def auth_ok(authorization: Optional[str]) -> bool:
    if not API_TOKEN:
        return True
    if not authorization:
        return False
    try:
        scheme, token = authorization.split(" ", 1)
    except ValueError:
        return False
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


# ---------------- Routes ----------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return PlainTextResponse("", status_code=204)

@app.get("/health")
def health():
    return JSONResponse({"ok": True})

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
        _state["brightness"] = max(0, min(100, int(payload.brightness))); changed = True
    if payload.rotation is not None and payload.rotation != _state.get("rotation"):
        _state["rotation"] = int(payload.rotation) % 360; changed = True

    if changed:
        await save_and_broadcast()
    return JSONResponse(current_state())

@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    await hub.register(ws)
    try:
        # Send current state immediately so clients (Pi/FE) sync on connect
        await ws.send_json({"type": "state", **current_state()})
        while True:
            # We don't require messages from clients; keep socket open
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.unregister(ws)
