"""Chat UI and streaming API.

    uv run blast-web     # http://localhost:8000

POST /api/chat streams newline-delimited JSON events from agent.run_turn, so the page can
draw each tool's dependency paths next to the answer while Claude is still writing it.
"""

import asyncio
import json
import os
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import queries as Q
from .agent import EFFORT, MODEL, Agent
from .db import rows

STATIC = Path(__file__).parent / "web"
MAX_SESSIONS = 200
MAX_MESSAGES = 80  # history is append-only, so a long chat ends instead of being trimmed

agent = Agent()
sessions: OrderedDict[str, list] = OrderedDict()
locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with agent:
        yield


app = FastAPI(title="Supply-chain blast radius", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


def _session(session_id: str | None) -> tuple[str, list]:
    sid = session_id if session_id in sessions else uuid.uuid4().hex
    history = sessions.setdefault(sid, [])
    sessions.move_to_end(sid)
    while len(sessions) > MAX_SESSIONS:
        old, _ = sessions.popitem(last=False)
        locks.pop(old, None)
    return sid, history


@app.get("/api/health")
def health() -> dict:
    nodes = {r["label"]: r["n"] for r in rows(Q.COUNT_NODES)}
    edges = {r["type"]: r["n"] for r in rows(Q.COUNT_EDGES)}
    return {
        "nodes": nodes,
        "edges": edges,
        "model": MODEL,
        "effort": EFFORT,
        "tools": len(agent.tools),
        "llm_ready": agent.has_credentials,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    sid, history = _session(req.session_id)
    lock = locks.setdefault(sid, asyncio.Lock())

    async def events():
        yield json.dumps({"type": "session", "session_id": sid}) + "\n"
        if len(history) >= MAX_MESSAGES:
            yield json.dumps({"type": "error", "message": "This chat is full. Start a new chat."}) + "\n"
            return
        async with lock:
            async for event in agent.run_turn(history, req.message):
                yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
