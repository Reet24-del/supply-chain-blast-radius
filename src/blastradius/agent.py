"""The agent loop. Claude picks tools; the tools run over MCP; every step streams out as an event.

The agent is an MCP client of the same server that Claude Code or Claude Desktop can use
(mcp_server.py), connected in-process. Events are plain dicts, so the web UI can draw the
dependency path from each tool result next to the answer.
"""

import json
import os
from collections.abc import AsyncIterator

import anthropic
from mcp import Client

from .mcp_server import INSTRUCTIONS, server

MODEL = os.getenv("BLAST_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("BLAST_EFFORT", "medium")
MAX_TOOL_ROUNDS = 8
MAX_TOKENS = 64000
# On a safety decline, the API re-runs the request on a fallback model (routed by category).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = (
    INSTRUCTIONS
    + """
You are answering in a web UI that draws every dependency path your tools return beside your
answer, so don't repeat long lists of paths. Lead with the number that matters (e.g. "17 of 17
repos install debug; only 1 declares it"), name the most important repos with one example path,
then say what to do next. Keep answers short. Only call record_decision or open_issue when the
user decides something or asks you to act.
"""
)


def _claude_tool(tool) -> dict:
    d = tool.model_dump(by_alias=True, exclude_none=True)
    return {
        "name": d["name"],
        "description": d.get("description", ""),
        "input_schema": d["inputSchema"],
        # Inputs stream as generated; the MCP server validates them before any tool runs.
        "eager_input_streaming": True,
    }


def _tool_payload(result) -> tuple[dict, bool]:
    """The tool's dict, from structured content or the JSON text the server sends."""
    d = result.model_dump(by_alias=True, exclude_none=True)
    is_error = bool(d.get("isError"))
    payload = d.get("structuredContent")
    if payload is None:
        text = " ".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
        try:
            payload = json.loads(text)
        except ValueError:  # e.g. an argument validation error, which is plain text
            payload = {"error": text}
    if not isinstance(payload, dict):
        payload = {"result": payload}
    return payload, is_error


class Agent:
    """One MCP session and one Claude client, shared by every chat."""

    def __init__(self) -> None:
        self.mcp = Client(server)
        self.tools: list[dict] = []
        self._claude: anthropic.AsyncAnthropic | None = None

    async def __aenter__(self) -> "Agent":
        await self.mcp.__aenter__()
        self.tools = [_claude_tool(t) for t in (await self.mcp.list_tools()).tools]
        return self

    async def __aexit__(self, *exc) -> None:
        await self.mcp.__aexit__(*exc)

    @property
    def claude(self) -> anthropic.AsyncAnthropic:
        if self._claude is None:  # created on first use, so the UI loads without credentials
            self._claude = anthropic.AsyncAnthropic()
        return self._claude

    @property
    def has_credentials(self) -> bool:
        c = self.claude
        return bool(c.api_key or c.auth_token or c.credentials)

    async def run_turn(self, history: list, user_text: str) -> AsyncIterator[dict]:
        """Append one user turn to `history` (append-only) and stream events until Claude is done."""
        if not self.has_credentials:
            yield {"type": "error", "message": "No Anthropic credentials: add ANTHROPIC_API_KEY to .env and restart."}
            return
        history.append({"role": "user", "content": user_text})
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        json_retries = 0
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            yield {"type": "status", "text": "thinking"}
            try:
                async with self.claude.beta.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM,
                    tools=self.tools,
                    messages=history,
                    thinking={"type": "adaptive"},
                    output_config={"effort": EFFORT},
                    cache_control={"type": "ephemeral"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                ) as stream:
                    async for event in stream:
                        if event.type == "text":
                            yield {"type": "text", "delta": event.text}
                        elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                            yield {"type": "tool_start", "id": event.content_block.id, "name": event.content_block.name}
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse at all. There is no tool_use_id to
                # answer, so re-issue the turn (bounded). API errors are not ValueError.
                json_retries += 1
                if json_retries > 2:
                    yield {"type": "error", "message": "The model kept producing unparseable tool input."}
                    return
                rounds -= 1
                continue
            except anthropic.AuthenticationError:
                yield {"type": "error", "message": "The Anthropic API rejected the key in ANTHROPIC_API_KEY."}
                return
            except anthropic.RateLimitError:
                yield {"type": "error", "message": "Rate limited by the Anthropic API. Try again in a minute."}
                return
            except anthropic.APIStatusError as e:
                yield {"type": "error", "message": f"Anthropic API error {e.status_code}: {e.message}"}
                return
            except anthropic.APIConnectionError:
                yield {"type": "error", "message": "Couldn't reach the Anthropic API."}
                return

            for key in usage:
                usage[key] += getattr(response.usage, key, 0) or 0
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "refusal":
                # Not appended: a refusal can cut a tool_use off mid-input.
                category = getattr(response.stop_details, "category", None)
                yield {"type": "notice", "text": f"The model declined this request ({category or 'no category'})."}
                break
            if response.stop_reason == "max_tokens" and tool_uses:
                yield {"type": "notice", "text": "A tool call was cut off at max_tokens, so it wasn't run."}
                break

            history.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if not tool_uses:
                yield {"type": "done", "model": response.model, "stop_reason": response.stop_reason, "usage": usage}
                return

            results = []
            for block in tool_uses:
                yield {"type": "tool_call", "id": block.id, "name": block.name, "input": block.input}
                if not isinstance(block.input, dict):
                    payload, is_error = {"INVALID_JSON": json.dumps(block.input)}, True
                else:
                    try:
                        payload, is_error = _tool_payload(await self.mcp.call_tool(block.name, block.input))
                    except Exception as e:  # e.g. a graph query timeout; tell the model, keep going
                        payload, is_error = {"error": str(e)}, True
                yield {"type": "tool_result", "id": block.id, "name": block.name, "result": payload, "is_error": is_error}
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(payload, separators=(",", ":"), default=str),
                        "is_error": is_error,
                    }
                )
            history.append({"role": "user", "content": results})
            yield {"type": "text", "delta": "\n\n"}

        else:
            yield {"type": "notice", "text": f"Stopped after {MAX_TOOL_ROUNDS} tool rounds."}
        yield {"type": "done", "model": MODEL, "stop_reason": "stopped", "usage": usage}
