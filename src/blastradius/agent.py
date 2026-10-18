"""The agent loop. The model picks tools; the tools run over MCP; every step streams out as an event.

The agent is an MCP client of the same server that Claude Code or Claude Desktop can use
(mcp_server.py), connected in-process. Events are plain dicts, so the web UI can draw the
dependency path from each tool result next to the answer.

Two model providers, picked from the environment (LLM_PROVIDER, or whichever key is set):
  groq       GROQ_API_KEY, free tier, default model openai/gpt-oss-120b
  anthropic  ANTHROPIC_API_KEY, default model claude-opus-5-5
"""

import json
import os
from collections.abc import AsyncIterator

import anthropic
import groq
from mcp import Client

from .mcp_server import INSTRUCTIONS, server


def _provider() -> str:
    explicit = os.getenv("LLM_PROVIDER", "").lower()
    if explicit in ("anthropic", "groq"):
        return explicit
    return "groq" if os.getenv("GROQ_API_KEY") else "anthropic"


PROVIDER = _provider()
MODEL = os.getenv("BLAST_MODEL") or {"anthropic": "claude-opus-5-5", "groq": "openai/gpt-oss-120b"}[PROVIDER]
EFFORT = os.getenv("BLAST_EFFORT") or {"anthropic": "medium", "groq": "low"}[PROVIDER]
MAX_TOOL_ROUNDS = 8
MAX_TOKENS = 64000
# Anthropic: on a safety decline, the API re-runs the request on a fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The UI gets every row of a tool result; the model reads the first few plus a count. This keeps
# each step small enough for Groq's free tier (a few thousand tokens per minute).
MODEL_ROWS = 6

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


def _tool_spec(tool) -> dict:
    """Name, description and JSON schema of an MCP tool, without the session-owned tenant."""
    d = tool.model_dump(by_alias=True, exclude_none=True)
    schema = dict(d["inputSchema"])
    # The tenant comes from the chat session (see run_turn), never from the model.
    schema["properties"] = {k: v for k, v in schema.get("properties", {}).items() if k != "tenant"}
    if "required" in schema:
        schema["required"] = [k for k in schema["required"] if k != "tenant"]
    return {"name": d["name"], "description": d.get("description", ""), "schema": schema}


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


def _for_model(payload: dict) -> str:
    """The tool result as the model reads it: long lists cut to MODEL_ROWS, with a count."""

    def trim(value):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if isinstance(v, list) and len(v) > MODEL_ROWS:
                    out[k], out[f"{k}_not_shown"] = [trim(x) for x in v[:MODEL_ROWS]], len(v) - MODEL_ROWS
                else:
                    out[k] = trim(v)
            return out
        if isinstance(value, list):
            return [trim(x) for x in value]
        return value

    return json.dumps(trim(payload), separators=(",", ":"), default=str)


def _groq_reasoning(model: str) -> dict:
    """Reasoning settings differ by model family on Groq; tool use needs them hidden."""
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": EFFORT, "include_reasoning": False}
    if model.startswith("qwen/"):
        return {"reasoning_format": "hidden"}
    return {}


class Agent:
    """One MCP session and one model client, shared by every chat."""

    def __init__(self) -> None:
        self.mcp = Client(server)
        self.specs: list[dict] = []
        self._claude: anthropic.AsyncAnthropic | None = None
        self._groq: groq.AsyncGroq | None = None

    async def __aenter__(self) -> "Agent":
        await self.mcp.__aenter__()
        self.specs = [_tool_spec(t) for t in (await self.mcp.list_tools()).tools]
        return self

    async def __aexit__(self, *exc) -> None:
        await self.mcp.__aexit__(*exc)

    @property
    def tools(self) -> list[dict]:
        """Tool definitions in the active provider's format."""
        if PROVIDER == "groq":
            return [
                {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["schema"]}}
                for s in self.specs
            ]
        # Inputs stream as generated; the MCP server validates them before any tool runs.
        return [
            {"name": s["name"], "description": s["description"], "input_schema": s["schema"], "eager_input_streaming": True}
            for s in self.specs
        ]

    # Clients are created on first use, so the UI loads without credentials.
    @property
    def claude(self) -> anthropic.AsyncAnthropic:
        if self._claude is None:
            self._claude = anthropic.AsyncAnthropic()
        return self._claude

    @property
    def groq(self) -> groq.AsyncGroq:
        if self._groq is None:
            self._groq = groq.AsyncGroq(max_retries=3)  # retries wait out free-tier rate limits
        return self._groq

    @property
    def has_credentials(self) -> bool:
        if PROVIDER == "groq":
            return bool(os.getenv("GROQ_API_KEY"))
        c = self.claude
        return bool(c.api_key or c.auth_token or c.credentials)

    async def _call_tool(self, name: str, args, tenant: str) -> tuple[dict, bool]:
        if not isinstance(args, dict):
            return {"INVALID_JSON": json.dumps(args)}, True
        try:
            return _tool_payload(await self.mcp.call_tool(name, {**args, "tenant": tenant}))
        except Exception as e:  # e.g. a graph query timeout; tell the model, keep going
            return {"error": str(e)}, True

    async def run_turn(self, history: list, user_text: str, tenant: str = "") -> AsyncIterator[dict]:
        """Append one user turn to `history` (append-only) and stream events until the model is done.

        Every tool call runs against `tenant`'s graph, whatever the model put in its input.
        """
        if not self.has_credentials:
            key = "GROQ_API_KEY" if PROVIDER == "groq" else "ANTHROPIC_API_KEY"
            yield {"type": "error", "message": f"No API key: add {key} to .env and restart."}
            return
        turn = self._groq_turn if PROVIDER == "groq" else self._claude_turn
        async for event in turn(history, user_text, tenant):
            yield event

    async def _claude_turn(self, history: list, user_text: str, tenant: str) -> AsyncIterator[dict]:
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
                payload, is_error = await self._call_tool(block.name, block.input, tenant)
                yield {"type": "tool_result", "id": block.id, "name": block.name, "result": payload, "is_error": is_error}
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": _for_model(payload), "is_error": is_error}
                )
            history.append({"role": "user", "content": results})
            yield {"type": "text", "delta": "\n\n"}

        else:
            yield {"type": "notice", "text": f"Stopped after {MAX_TOOL_ROUNDS} tool rounds."}
        yield {"type": "done", "model": MODEL, "stop_reason": "stopped", "usage": usage}

    async def _groq_turn(self, history: list, user_text: str, tenant: str) -> AsyncIterator[dict]:
        history.append({"role": "user", "content": user_text})
        usage = {"input_tokens": 0, "output_tokens": 0}
        failed_generations = 0
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            yield {"type": "status", "text": "thinking"}
            text, calls, finish = [], {}, None
            try:
                stream = await self.groq.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": SYSTEM}, *history],
                    tools=self.tools,
                    tool_choice="auto",
                    stream=True,
                    max_completion_tokens=8192,
                    **_groq_reasoning(MODEL),
                )
                async for chunk in stream:
                    x_groq = getattr(chunk, "x_groq", None)
                    if x_groq is not None and getattr(x_groq, "usage", None):
                        usage["input_tokens"] += x_groq.usage.prompt_tokens or 0
                        usage["output_tokens"] += x_groq.usage.completion_tokens or 0
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.delta.content:
                        text.append(choice.delta.content)
                        yield {"type": "text", "delta": choice.delta.content}
                    # Tool calls arrive in pieces keyed by index: id and name once, arguments as text.
                    for part in choice.delta.tool_calls or []:
                        if part.index not in calls:  # the id is fixed here, so UI and history agree on it
                            calls[part.index] = {"id": part.id or f"call_{rounds}_{part.index}", "name": "", "arguments": ""}
                        call = calls[part.index]
                        if part.function and part.function.name and not call["name"]:
                            call["name"] = part.function.name
                            yield {"type": "tool_start", "id": call["id"], "name": call["name"]}
                        if part.function and part.function.arguments:
                            call["arguments"] += part.function.arguments
                    if choice.finish_reason:
                        finish = choice.finish_reason
            except groq.BadRequestError as e:
                # Groq rejects a turn whose tool call the model garbled ("tool_use_failed"):
                # nothing was appended, so ask again (bounded).
                if "tool_use_failed" in str(e.body) and failed_generations < 2:
                    failed_generations += 1
                    rounds -= 1
                    continue
                yield {"type": "error", "message": f"Groq API error: {e.message}"}
                return
            except groq.AuthenticationError:
                yield {"type": "error", "message": "Groq rejected the key in GROQ_API_KEY."}
                return
            except groq.RateLimitError:
                yield {"type": "error", "message": "Groq's free-tier limit is used up for now. Try again in a minute."}
                return
            except groq.APIStatusError as e:
                yield {"type": "error", "message": f"Groq API error {e.status_code}: {e.message}"}
                return
            except groq.APIConnectionError:
                yield {"type": "error", "message": "Couldn't reach the Groq API."}
                return

            ordered = [calls[i] for i in sorted(calls)]
            if finish == "length" and ordered:
                yield {"type": "notice", "text": "A tool call was cut off at the length limit, so it wasn't run."}
                break

            reply = "".join(text)
            if not ordered:
                history.append({"role": "assistant", "content": reply})
                yield {"type": "done", "model": MODEL, "stop_reason": finish, "usage": usage}
                return

            history.append(
                {
                    "role": "assistant",
                    "content": reply or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in ordered
                    ],
                }
            )
            for call in ordered:
                try:
                    args = json.loads(call["arguments"] or "{}")
                except ValueError:
                    args = call["arguments"]  # _call_tool reports it as invalid JSON
                yield {"type": "tool_call", "id": call["id"], "name": call["name"], "input": args}
                payload, is_error = await self._call_tool(call["name"], args, tenant)
                yield {"type": "tool_result", "id": call["id"], "name": call["name"], "result": payload, "is_error": is_error}
                history.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": _for_model(payload)})
            yield {"type": "text", "delta": "\n\n"}

        else:
            yield {"type": "notice", "text": f"Stopped after {MAX_TOOL_ROUNDS} tool rounds."}
        yield {"type": "done", "model": MODEL, "stop_reason": "stopped", "usage": usage}
