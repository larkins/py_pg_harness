"""minimax_m3 adapter — MiniMax-M3 via the Anthropic-compatible endpoint.

Per MEMORY.md:
- base URL = https://api.minimax.io/anthropic
- DO NOT use the /v1 OpenAI-compatible endpoint (it garbles history)
- Strip <extras> envelope from message history (lesson 2026-08-20)

Quirks discovered during Phase 2 (2026-10-08):
- Header is case-sensitive: 'X-Api-Key' (uppercase) works, 'x-api-key' returns 401.
  Per docs the canonical header is 'x-api-key', but the MiniMax backend
  rejects lowercase. Use 'Authorization: Bearer' as the more-compatible fallback.
- Both 'X-Api-Key' and 'Authorization: Bearer' authenticate; we use Bearer.
- Response uses the standard Anthropic messages format with `content` array
  of blocks (text / tool_use). model name = 'MiniMax-M3'.

Phase 2 v0.1 uses SYNC httpx. The async version had event-loop-binding
issues when paired with `asyncio.run()` per request — each fresh event
loop broke the AsyncClient's pooled connections. Sync is simpler for v0
(single Flask worker, low throughput). Future versions may revisit
async if throughput demands it.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.agent.tool_repair import repair_text_to_tool_calls, strip_repaired_blocks

from .base import BaseLLM, LLMResponse, Message, ToolCall

log = logging.getLogger(__name__)


class LLM(BaseLLM):
    name = "minimax/MiniMax-M3"
    adapter_id = "minimax_m3"

    def __init__(self, config: Any) -> None:
        self.api_key = config.llm.api_key
        self.base_url = config.llm.base_url.rstrip("/")
        self.model = config.llm.model
        self.default_max_tokens = config.llm.max_tokens
        self.timeout_s = config.llm.timeout_s
        self.temperature = config.llm.temperature
        # Single shared client. HTTP/2 keeps a persistent connection to
        # the MiniMax endpoint — saves ~1s per call vs fresh connections.
        self._client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(self.timeout_s),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    def close(self) -> None:
        self._client.close()

    def complete(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        # Convert our generic Message list to Anthropic's expected format.
        system_text, anth_messages = _to_anthropic(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "messages": anth_messages,
        }
        if system_text:
            body["system"] = system_text
        if tools:
            body["tools"] = [_to_anthropic_tool(t) for t in tools]

        t0 = time.monotonic()
        try:
            r = self._post_with_retry(
                f"{self.base_url}/v1/messages",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                body=body,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("minimax_m3 HTTP error: %s body=%s",
                      e.response.status_code, e.response.text[:500])
            raise
        except httpx.HTTPError as e:
            log.error("minimax_m3 transport error: %s", e)
            raise

        data = r.json()
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Parse content blocks. Anthropic returns a list; we look for
        # text + tool_use blocks. Stop reason drives the branch.
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}) or {},
                ))
            else:
                log.warning("minimax_m3: unknown content block type %r", btype)

        # Tool-call-repair: when the model didn't return tool_use blocks
        # but narrated a tool call in the text (e.g. as markdown prose
        # with backticks), try to recover it via the repair layer.
        # This is a port of OpenClaw's tool-call-repair subsystem
        # (dist/payload-C252NdEX.js). Best-effort: failures are
        # silently dropped (the text stays as-is, the loop sees no
        # tool calls and exits normally).
        if not tool_calls and text_parts:
            text_str = "\n".join(text_parts)
            # Build tool_schemas map from the tools we were given.
            # Used by the Python parser to map positional args.
            schemas: dict[str, list[str]] = {}
            if tools:
                for t in tools:
                    if isinstance(t, dict):
                        name = t.get("name")
                        params = t.get("parameters", {}).get("properties", {}) if isinstance(t.get("parameters"), dict) else {}
                        if name and params:
                            schemas[name] = list(params.keys())
            try:
                repaired = repair_text_to_tool_calls(
                    text_str,
                    allowed_tool_names=set(schemas.keys()) if schemas else set(),
                    tool_schemas=schemas if schemas else None,
                )
            except Exception as e:
                log.debug("minimax_m3: tool_repair raised: %s", e)
                repaired = []
            if repaired:
                # Merge the recovered tool calls into the response.
                #
                # ID generation MUST be unique within a single
                # response — the API rejects requests with duplicate
                # tool_use ids ("duplicate tool_call id: ... (2013)").
                # The previous code used hash(r.raw_text)[:8] which
                # collides when the model narrates the SAME tool call
                # twice in one response (common pattern: the model
                # includes a tool call once, then re-narrates the
                # same call as a "retry hint" or just because the
                # output has the same pattern twice). We now append a
                # per-call index `_N` to guarantee uniqueness within
                # the response. The harness's agent loop treats the
                # id as opaque (it only uses it for format_tool_result
                # pairing), so the suffix doesn't affect anything else.
                for idx, r in enumerate(repaired):
                    rid = f"repaired_{str(hash(r.raw_text))[:8]}_{idx}"
                    tool_calls.append(ToolCall(
                        id=rid,
                        name=r.name,
                        arguments=r.arguments,
                    ))
                # Strip the matched raw_text from the visible text so
                # the user doesn't see the tool call syntax.
                stripped = strip_repaired_blocks(text_str, repaired)
                text_parts = [stripped] if stripped else []
                log.info(
                    "minimax_m3: tool-call-repair recovered %d tool call(s) from narration: %s",
                    len(repaired),
                    [r.name for r in repaired],
                )

        sr = data.get("stop_reason", "end_turn")
        finish_reason = {
            "end_turn": "stop",
            "tool_use": "tool_use",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }.get(sr, "stop")

        usage = data.get("usage", {})
        text_str = "\n".join(text_parts) if text_parts else None
        # Tripwire: the model is "narrating" tool calls in prose rather than
        # returning tool_use content blocks. Anthropic's content array has
        # separate blocks for text and tool_use; if the model writes
        # "memory_recall(...)" inline, the system has no way to detect the
        # intent and the agent loop exits with no real tool call. Warn so
        # the operator can see when this happens (and the prompt tweak
        # can be evaluated over time).
        if not tool_calls and text_str and re.match(
            r"^\s*(memory_recall|memory_recent|memory_write|memory_update"
            r"|memory_pin|memory_unpin|sql_query|exec_bash)\s*\(",
            text_str,
        ):
            log.warning(
                "minimax_m3: response text looks like a tool call but "
                "tool_calls is empty (model is narrating instead of "
                "returning tool_use blocks): %r",
                text_str[:200],
            )
        return LLMResponse(
            text=text_str,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=data,
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "latency_ms": latency_ms,
            },
        )

    def format_tool_result(self, call: ToolCall, result: Any) -> Message:
        """Anthropic expects tool results as a user message with
        `tool_result` content blocks."""
        import json
        if isinstance(result, str):
            content_str = result
        else:
            content_str = json.dumps(result, default=str)
        return Message(
            role="user",
            content=[{
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": content_str,
            }],
        )

    def extract_assistant_content(self, response: LLMResponse) -> list[dict]:
        """Reconstruct the assistant content blocks from response.raw.

        Anthropic expects the conversation to include the original assistant
        message (with tool_use blocks) alongside the user/tool_result
        message that follows. We round-trip via response.raw["content"]
        so we preserve the exact blocks (text, tool_use) the model emitted.
        """
        if response.raw is None:
            return []
        return response.raw.get("content", [])


def _to_anthropic(messages: list[Message]) -> tuple[str, list[dict]]:
    """Split messages into (system_text, rest). Strip <extras> envelope."""
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.role == "system":
            text = m.content if isinstance(m.content, str) else str(m.content)
            text = _strip_extras(text)
            system_parts.append(text)
            continue
        if isinstance(m.content, str):
            content = _strip_extras(m.content)
            rest.append({"role": m.role, "content": content})
        elif isinstance(m.content, list):
            rest.append({"role": m.role, "content": m.content})
        else:
            rest.append({"role": m.role, "content": str(m.content)})
    return "\n\n".join(system_parts), rest


def _to_anthropic_tool(t: Any) -> dict:
    """Convert our Tool-like object to Anthropic's tool schema.

    Tool has: name, description, parameters (JSON schema).
    Anthropic wants: name, description, input_schema.
    Accepts both object form and dict form (we register tools as dicts).
    """
    if isinstance(t, dict):
        return {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.parameters,
    }


def _strip_extras(text: str) -> str:
    """Strip the OpenClaw <extras> envelope that leaks into non-Anthropic
    providers via the anthropic-messages API. Per MEMORY.md lesson
    2026-08-20."""
    import re
    return re.sub(r"<extras>.*?</extras>", "", text, flags=re.DOTALL)
