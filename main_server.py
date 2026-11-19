import hmac
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import anyio
from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP

API_TOKEN = os.environ["MCP_TOKEN"]          # fail at startup if missing
ALLOWED_ORIGINS = {"https://yourapp.com"}    # only matters for browser clients


# =============== shared business logic ===============
NOTES: dict[str, str] = {}  # use a real DB in production

async def add_numbers(a: float, b: float) -> float:
    return a + b

async def get_weather_info(city: str) -> str:
    return f"It's sunny in {city}, 30°C"

def _blocking_lookup(key: str) -> str:
    return f"result for {key}"  # example of blocking work


# =============== MCP tools ===============
mcp = FastMCP("MyTools", stateless_http=True, json_response=True)
'''
@mcp.tool()
async def add(a: float, b: float) -> float:
    """Add two numbers."""
    return await add_numbers(a, b)

@mcp.tool()
async def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return await get_weather_info(city)

@mcp.tool()
async def save_note(title: str, content: str) -> str:
    """Save a note."""
    NOTES[title] = content
    return f"Saved '{title}'"

@mcp.tool()
async def get_note(title: str) -> str:
    """Read a note by title."""
    return NOTES.get(title, "Not found")

@mcp.tool()
async def lookup(key: str) -> str:
    """Example of running blocking code safely."""
    return await anyio.to_thread.run_sync(_blocking_lookup, key)

'''
# =============== FastAPI app ===============
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():   # required for MCP
        yield

app = FastAPI(title="REST + MCP + WebSocket", lifespan=lifespan)


# =============== REST routes ===============
@app.get("/health")
def health():
    return {"status": "ok"}

api = APIRouter(prefix="/api", tags=["api"])

class NoteIn(BaseModel):
    title: str
    content: str

@api.get("/add")
async def rest_add(a: float, b: float):
    return {"result": await add_numbers(a, b)}

@api.get("/weather/{city}")
async def rest_weather(city: str):
    return {"weather": await get_weather_info(city)}

@api.post("/notes", status_code=201)
async def create_note(note: NoteIn):
    NOTES[note.title] = note.content
    return {"saved": note.title}

@api.get("/notes/{title}")
async def read_note(title: str):
    if title not in NOTES:
        raise HTTPException(404, "Note not found")
    return {"content": NOTES[title]}

@api.delete("/notes/{title}", status_code=204)
async def delete_note(title: str):
    NOTES.pop(title, None)

app.include_router(api)


# =============== WebSocket echo channel ===============
# NOTE: must be registered BEFORE the catch-all mount("/") below.
@app.websocket("/ws")
async def my_ws(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text(f"echo: {msg}")
    except WebSocketDisconnect:
        pass


app.mount("/", mcp.streamable_http_app())


# =============== auth middleware (covers REST, MCP and WebSocket) ===============
def _token_ok(supplied: str | None) -> bool:
    return supplied is not None and hmac.compare_digest(supplied, API_TOKEN)


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or scope["path"] == "/health":
            return await self.app(scope, receive, send)

        is_ws = scope["type"] == "websocket"
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}

        origin = headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return await self._deny(send, is_ws, 403, "Forbidden origin")

        auth = headers.get("authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else None

        # Browsers can't set headers on WebSocket connections, so also
        # accept ?token=... for WS only (note: URLs may end up in logs).
        if supplied is None and is_ws:
            qs = parse_qs(scope.get("query_string", b"").decode())
            supplied = (qs.get("token") or [None])[0]

        if not _token_ok(supplied):
            return await self._deny(send, is_ws, 401, "Unauthorized")

        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, is_ws, status, msg):
        if is_ws:
            # Closing before accept -> client sees an HTTP 403 handshake rejection
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": f'{{"error":"{msg}"}}'.encode()})

app.add_middleware(SecurityMiddleware)