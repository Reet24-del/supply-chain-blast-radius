"""Chat UI and streaming API.

    uv run blast-web     # http://localhost:8000

POST /api/chat streams newline-delimited JSON events from agent.run_turn, so the page can
draw each tool's dependency paths next to the answer while Claude is still writing it.
"""

import asyncio
import json
import os
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from . import queries as Q
from .agent import EFFORT, MODEL, Agent
from .db import rows

STATIC = Path(__file__).parent / "web"
MAX_SESSIONS = 200
MAX_MESSAGES = 80  # history is append-only, so a long chat ends instead of being trimmed

# Every question spends Anthropic API credits, so a public deployment needs limits:
# a sliding window per visitor, and a daily cap across everyone as the hard ceiling.
VISITOR_LIMIT = int(os.getenv("RATE_LIMIT_PER_VISITOR", "15"))
VISITOR_WINDOW_S = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
DAILY_LIMIT = int(os.getenv("DAILY_QUESTION_LIMIT", "400"))

agent = Agent()
sessions: OrderedDict[str, list] = OrderedDict()
locks: dict[str, asyncio.Lock] = {}
visits: dict[str, deque] = {}
daily = {"day": None, "count": 0}


def _rate_limit(visitor: str) -> None:
    """Raise 429 if this visitor, or everyone today, has asked too many questions."""
    now = time.monotonic()
    today = datetime.now(timezone.utc).date()
    if daily["day"] != today:
        daily.update(day=today, count=0)
        visits.clear()  # also keeps the per-visitor table from growing forever
    if daily["count"] >= DAILY_LIMIT:
        raise HTTPException(429, "The live demo has used today's question budget. Run it locally (see the README).")
    seen = visits.setdefault(visitor, deque())
    while seen and now - seen[0] > VISITOR_WINDOW_S:
        seen.popleft()
    if len(seen) >= VISITOR_LIMIT:
        wait = int(VISITOR_WINDOW_S - (now - seen[0])) + 1
        raise HTTPException(429, f"That's {VISITOR_LIMIT} questions in {VISITOR_WINDOW_S // 60} minutes. Try again in {wait}s.")
    seen.append(now)
    daily["count"] += 1


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
    # An empty or unreachable graph is reported, not raised: the service itself is up, and a
    # failing health check would make the host restart it in a loop.
    try:
        nodes = {r["label"]: r["n"] for r in rows(Q.COUNT_NODES)}
        edges = {r["type"]: r["n"] for r in rows(Q.COUNT_EDGES)}
        graph_error = None
    except RedisError as e:
        nodes, edges, graph_error = {}, {}, str(e)
    return {
        "nodes": nodes,
        "edges": edges,
        "graph_error": graph_error,
        "model": MODEL,
        "effort": EFFORT,
        "tools": len(agent.tools),
        "llm_ready": agent.has_credentials,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    _rate_limit(request.client.host if request.client else "unknown")
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

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        # Behind a host's proxy, take the visitor's address from X-Forwarded-For. With "*" a
        # visitor could spoof it to dodge the per-visitor limit; the daily cap still holds.
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
