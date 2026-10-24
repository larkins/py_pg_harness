"""minimax_m31_flash adapter — MiniMax-M3.1-Flash via the Anthropic-compatible endpoint.

Per the MiniMax M3.1-Flash docs (2026-10-20):
- base URL = https://api.minimax.io/anthropic  (same as M3)
- Anthropic-compatible wire format (same as M3)
- model name = 'MiniMax-M3.1-Flash'
- Auth: Authorization: Bearer <MINIMAX_API_KEY>  (same as M3)
- 1M-token context window; "frontier multimodal coding model" — agentic
  reasoning, tool use, coding, structured task execution.

M3.1-Flash-specific: tunable thinking depth via `output_config.effort`
in the request body. Accepted values: low | medium | high | xhigh | max.
- Default if omitted: max
- Cannot be disabled — sending thinking: {"type": "disabled"} or
  effort: "none" returns 400 ("model requires adaptive thinking").
- Thinking content comes back as `{"type": "thinking", "thinking": "..."}`
  content blocks. We discard them on the way back so history stays clean
  (the harness doesn't surface chain-of-thought to the user).

The `thinking` field on `_Llm` config carries the `output_config.effort`
shape — i.e. {"effort": "medium"}. The diatomic z.ai adapter reads the
same field but interprets it as z.ai's `{"type": "enabled"}` shape —
each adapter maps the dict onto its provider's wire format.

Provisioned 2026-10-20 for the agent2 M3.1-Flash test (per Mal's request).
Superseded 2026-10-21 (per Mal): ALL THREE intelligences now run this
adapter. agent1 moved off minimax_m3 (via the global llm: block) and agent3
moved off z.ai's glm_5_3_flash; all three share one MiniMax endpoint and
one API key. agent2 and agent3 set effort=medium explicitly; agent1 has no
`thinking` key and therefore uses the adapter default (max).

SSE streaming (2026-10-21, per Mal): complete() now streams by default. The
transport reassembles the same Anthropic message dict a buffered response
would have produced and hands it to the SAME parser, so tool calls, thinking
blocks, stop_reason and usage are unchanged.

Why it matters: `timeout_s` is an httpx read timeout. On a buffered POST it
bounds TOTAL generation, so any turn slower than that failed outright. Over a
stream the same value bounds the gap BETWEEN CHUNKS, so long generations now
complete as long as the model keeps emitting.

Rollback: set LLM_STREAMING=0 in .env and restart. No code change.
Tests: tests_stream_parity.py (streaming vs buffered parity),
tests_toolloop_stream.py (full tool-call round-trip).

Patterned after minimax_m3 (also Anthropic-compatible, same endpoint).
Differences:
- Different model name (MiniMax-M3.1-Flash vs MiniMax-M3)
- Adds `output_config.effort` to the request body when self.thinking
  is set; reads thinking blocks out of the response and drops them.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app.agent.tool_repair import repair_text_to_tool_calls, strip_repaired_blocks

from .base import BaseLLM, LLMResponse, Message, ToolCall

log = logging.getLogger(__name__)


class LLM(BaseLLM):
    name = "minimax/MiniMax-M3.1-Flash"
    adapter_id = "minimax_m31_flash"

    def __init__(self, config: Any) -> None:
        self.api_key = config.llm.api_key
        self.base_url = config.llm.base_url.rstrip("/")
        self.model = config.llm.model
        self.default_max_tokens = config.llm.max_tokens
        self.timeout_s = config.llm.timeout_s
        self.temperature = config.llm.temperature
        # M3.1-Flash thinking control. `self.thinking` is the dict passed
        # through to `output_config` on the wire — e.g. {"effort": "medium"}.
        # Read with getattr default None so other adapters can keep using
        # the same `_Llm.thinking` field without conflict. When None,
        # the field is omitted and MiniMax defaults to effort=max.
        self.thinking = getattr(config.llm, "thinking", None)
        # SSE streaming on by default (2026-10-21). Rolls back instantly by
        # setting LLM_STREAMING=0 in .env and restarting -- no code change.
        import os as _os
        _flag = _os.environ.get("LLM_STREAMING", "1").strip().lower()
        self.streaming = _flag not in ("0", "false", "no", "off")
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
        on_delta: Any = None,
    ) -> LLMResponse:
        """Complete a turn.

        Uses the SSE streaming path by default (2026-10-21). The practical
        win is the timeout: `timeout_s` is an httpx read timeout, so on a
        buffered POST it bounds the TOTAL generation. A long turn that took
        longer than that failed outright. Over a stream the same value bounds
        the gap BETWEEN CHUNKS, so a long generation now completes as long as
        the model keeps emitting.

        The streaming path reassembles the identical Anthropic message dict and
        hands it to the same parser the buffered path uses, so tool calls,
        thinking blocks, stop_reason mapping and usage are byte-for-byte the
        same. If the stream fails at any point it falls back to the buffered
        POST, so this is strictly an improvement, never a regression.

        `on_delta(text)` is called with each text fragment as it arrives.
        Phase 2 (forwarding those to the browser) is not wired yet; the hook
        exists so that work does not have to touch the transport again.
        """
        body = self._build_body(messages, tools, max_tokens, temperature,
                                stream=self.streaming)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self.base_url}/v1/messages"

        if not self.streaming:
            return self._complete_buffered(url, headers, body, tools)

        t0 = time.monotonic()
        # Mutable cell: a plain int cannot be rebound from the callback, and a
        # lambda would silently never increment it.
        emitted = [0]

        def _bump() -> None:
            emitted[0] += 1

        try:
            data = self._stream_to_message(url, headers, body, on_delta,
                                           on_emit=_bump)
        except Exception as e:
            # Only safe to retry if the caller has not already been shown
            # partial output -- otherwise we would duplicate it.
            #
            # A dropped upstream connection (httpx.RemoteProtocolError) is
            # worth retrying when nothing was emitted: the pooled connection
            # was poisoned, and the fallback below reuses the same client.
            # The buffered path now treats RemoteProtocolError as retryable
            # and reports an empty body clearly instead of a bare
            # JSONDecodeError.
            if emitted[0]:
                log.error("minimax_m31_flash: stream failed after %d emitted "
                          "delta(s) (%s: %s); NOT retrying to avoid duplicate output",
                          emitted[0], type(e).__name__, e)
                raise
            log.warning("minimax_m31_flash: streaming path failed (%s: %s); "
                        "falling back to buffered POST",
                        type(e).__name__, e)
            return self._complete_buffered(url, headers, body, tools)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return self._parse_anthropic_message(data, tools, latency_ms)

    def _build_body(self, messages, tools, max_tokens, temperature,
                    stream: bool = False) -> dict[str, Any]:
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
        # M3.1-Flash thinking control, passed through as `output_config`.
        # The docs only accept the `effort` shape on the Anthropic-compatible
        # protocol; a z.ai-shaped {"type": ...} block returns 400. See module
        # docstring.
        if self.thinking is not None:
            body["output_config"] = self.thinking
        if stream:
            # Required, or the endpoint returns a buffered application/json
            # body and no SSE events are ever emitted.
            body["stream"] = True
        return body

    def _complete_buffered(self, url, headers, body, tools) -> LLMResponse:
        t0 = time.monotonic()
        try:
            r = self._post_with_retry(url, headers=headers, body=body)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("minimax_m31_flash HTTP error: %s body=%s",
                      e.response.status_code, e.response.text[:500])
            raise
        except httpx.HTTPError as e:
            log.error("minimax_m31_flash transport error: %s", e)
            raise
        if not r.content:
            # A 200 with an empty body happens when the upstream connection
            # was dropped mid-flight and the pooled connection was reused.
            # r.json() would raise a bare JSONDecodeError with no context.
            raise RuntimeError(
                "minimax_m31_flash: buffered response had an empty body "
                f"(status {r.status_code}); upstream connection was dropped")
        data = r.json()
        latency_ms = int((time.monotonic() - t0) * 1000)
        return self._parse_anthropic_message(data, tools, latency_ms)

    def _stream_to_message(self, url, headers, body, on_delta, on_emit=None) -> dict:
        """Consume the Anthropic SSE stream, reassemble the message dict.

        Mirrors what a non-streamed response body would have contained:
        {"id", "type", "role", "content": [...], "stop_reason", "usage"}.
        Content blocks are accumulated by index, then emitted in arrival
        order, so `content` matches the buffered shape exactly.
        """
        blocks: dict[int, dict] = {}
        order: list[int] = []
        msg: dict[str, Any] = {"id": None, "type": "message",
                               "role": "assistant", "content": [],
                               "stop_reason": None, "usage": {}}
        with self._client.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code >= 500 or r.status_code == 429:
                # Transient -- let the caller fall back to the retrying
                # buffered path, which already knows how to back off.
                r.raise_for_status()
            r.raise_for_status()

            # If the endpoint ignores stream:true (or a proxy strips it) it
            # answers with a normal buffered JSON body. Without this guard
            # iter_lines() yields one non-"data:" line, no events are parsed,
            # and the caller silently gets an EMPTY response instead of an
            # error or a fallback. Detect and short-circuit to the plain body.
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/event-stream" not in ctype:
                data = json.loads(r.read())
                log.info("minimax_m31_flash: endpoint returned %s, not SSE; "
                         "parsing as a buffered body", ctype.split(";")[0] or "(none)")
                return data

            for raw in r.iter_lines():
                line = raw.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    log.debug("minimax_m31_flash: unparseable SSE payload")
                    continue
                et = ev.get("type")

                if et == "message_start":
                    m = ev.get("message") or {}
                    msg["id"] = m.get("id")
                    u = m.get("usage") or {}
                    if u.get("input_tokens") is not None:
                        msg["usage"]["input_tokens"] = u["input_tokens"]

                elif et == "content_block_start":
                    idx = ev.get("index", 0)
                    b = dict(ev.get("content_block") or {})
                    btype = b.get("type")
                    if btype == "text":
                        b["text"] = b.get("text", "") or ""
                    elif btype == "thinking":
                        b["thinking"] = b.get("thinking", "") or ""
                    elif btype == "tool_use":
                        b["input"] = b.get("input", {}) or {}
                        # partial_json accumulates across input_json_delta
                        # events and is parsed at content_block_stop.
                        b["_partial_json"] = ""
                    blocks[idx] = b
                    order.append(idx)

                elif et == "content_block_delta":
                    idx = ev.get("index", 0)
                    b = blocks.get(idx)
                    if b is None:
                        continue
                    d = ev.get("delta") or {}
                    dt = d.get("type")
                    if dt == "text_delta":
                        frag = d.get("text", "")
                        b["text"] = b.get("text", "") + frag
                        if on_delta and frag:
                            on_delta(frag)
                            if on_emit:
                                on_emit()
                    elif dt == "thinking_delta":
                        b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                    elif dt == "signature_delta":
                        b["signature"] = b.get("signature", "") + d.get("signature", "")
                    elif dt == "input_json_delta":
                        b["_partial_json"] = b.get("_partial_json", "") + d.get("partial_json", "")

                elif et == "content_block_stop":
                    b = blocks.get(ev.get("index", 0))
                    if b is not None and b.get("type") == "tool_use":
                        raw_json = b.pop("_partial_json", "")
                        if raw_json:
                            try:
                                b["input"] = json.loads(raw_json)
                            except json.JSONDecodeError:
                                # Mirror the buffered path: leave input as {}
                                # rather than raising, so the existing
                                # narrate-without-executing tripwire and the
                                # user-facing error still fire normally.
                                log.warning("minimax_m31_flash: streamed tool_use "
                                            "input was not valid JSON (%d chars)",
                                            len(raw_json))
                                b["input"] = {}
                        else:
                            b.pop("_partial_json", None)

                elif et == "message_delta":
                    d = ev.get("delta") or {}
                    if d.get("stop_reason"):
                        msg["stop_reason"] = d["stop_reason"]
                    u = ev.get("usage") or {}
                    for k in ("output_tokens", "input_tokens"):
                        if u.get(k) is not None:
                            msg["usage"][k] = u[k]

                elif et == "error":
                    err = ev.get("error") or {}
                    raise RuntimeError(
                        f"minimax_m31_flash stream error: "
                        f"{err.get('type')}: {err.get('message')}")

                elif et == "message_stop":
                    continue
                elif et == "ping":
                    continue

        for b in blocks.values():
            b.pop("_partial_json", None)
        msg["content"] = [blocks[i] for i in order]
        if msg["stop_reason"] is None:
            msg["stop_reason"] = "end_turn"
        return msg

    def _parse_anthropic_message(self, data: dict, tools: list[Any] | None,
                             latency_ms: int) -> LLMResponse:
        """Shared response parser.

            Used by BOTH the buffered and the SSE streaming paths so the two
            can never diverge: streaming reconstructs the same Anthropic message
            dict that a non-streamed response would have produced, then hands it
            to this exact function. Everything downstream -- tool-call repair,
            thinking-block capture, stop_reason mapping, usage, the
            narrate-without-executing tripwire -- is unchanged.
            """
        # Parse content blocks. M3.1-Flash returns a list with three
        # possible block types: thinking (drop), text (keep), tool_use
        # (keep). Stop reason drives the branch. We discard thinking
        # blocks so history stays clean and the user doesn't see the
        # chain-of-thought in /admin/last-prompt or /admin/last-emission.
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Per the Anthropic extended-thinking spec, M3.1-Flash returns
        # thinking content blocks alongside text + tool_use. We CAPTURE
        # them in LLMResponse.thinking_blocks for:
        #   1. Round-tripping back to the model in extract_assistant_content
        #      so it can maintain its reasoning state across tool calls
        #      (Anthropic docs: required for extended thinking + tools).
        #   2. Surfacing in /admin/last-emission for debugging the
        #      hallucination/fabrication failure mode where the model
        #      narrated work it didn't actually do — the thinking
        #      trail makes it visible when the model's text contradicts
        #      its own chain-of-thought. (Added 2026-10-20 per Mal.)
        thinking_blocks: list[dict] = []
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
            elif btype == "thinking":
                # Capture the full block (type + thinking text + any
                # signature). Pass-through on the wire format so future
                # providers that add their own fields still round-trip.
                thinking_blocks.append(dict(block))
            else:
                log.warning("minimax_m31_flash: unknown content block type %r", btype)

        if thinking_blocks:
            log.info(
                "minimax_m31_flash: response included %d thinking block(s) "
                "(total %d chars) — surfaced for debugging",
                len(thinking_blocks),
                sum(len(b.get("thinking", "")) for b in thinking_blocks),
            )

        # Tool-call-repair: same port as minimax_m3. When the model didn't
        # return tool_use blocks but narrated a tool call in the text,
        # try to recover it via the repair layer.
        if not tool_calls and text_parts:
            text_str = "\n".join(text_parts)
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
                log.debug("minimax_m31_flash: tool_repair raised: %s", e)
                repaired = []
            if repaired:
                # Unique id generation — same pattern as minimax_m3.
                # The previous code used hash(r.raw_text)[:8] which
                # collides when the model narrates the SAME tool call
                # twice in one response. We append a per-call index
                # `_N` to guarantee uniqueness within the response.
                # The harness treats the id as opaque, so the suffix
                # doesn't affect anything else.
                for idx, r in enumerate(repaired):
                    rid = f"repaired_{str(hash(r.raw_text))[:8]}_{idx}"
                    tool_calls.append(ToolCall(
                        id=rid,
                        name=r.name,
                        arguments=r.arguments,
                    ))
                stripped = strip_repaired_blocks(text_str, repaired)
                text_parts = [stripped] if stripped else []
                log.info(
                    "minimax_m31_flash: tool-call-repair recovered %d tool call(s) from narration: %s",
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
        # Same tripwire as minimax_m3: detect "narrate-without-execution"
        # so the operator can see when the harness prompt tweak should be
        # re-evaluated.
        if not tool_calls and text_str and re.match(
            r"^\s*(memory_recall|memory_recent|memory_write|memory_update"
            r"|memory_pin|memory_unpin|sql_query|exec_bash)\s*\(",
            text_str,
        ):
            log.warning(
                "minimax_m31_flash: response text looks like a tool call but "
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
            thinking_blocks=thinking_blocks,
        )

    def format_tool_result(self, call: ToolCall, result: Any) -> Message:
        """Anthropic expects tool results as a user message with
        `tool_result` content blocks. Same shape as minimax_m3."""
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

        We round-trip via response.raw["content"] so we preserve the
        exact blocks the model emitted. THINKING BLOCKS ARE KEPT
        (2026-10-20, per Anthropic extended-thinking spec): the model
        needs them back in the conversation to maintain its reasoning
        state across tool calls. We only strip blocks the loop never
        produced — i.e. unknown types — to keep the round-trip
        lossless for anything the harness wrote.

        Captured separately in response.thinking_blocks for the admin
        view (see LLMResponse.thinking_blocks); the round-trip here is
        the live model context, the admin view is for human inspection.
        """
        if response.raw is None:
            return []
        return list(response.raw.get("content", []))


def _to_anthropic(messages: list[Message]) -> tuple[str, list[dict]]:
    """Split messages into (system_text, rest). Strip <extras> envelope.

    Same shape as minimax_m3 — kept as a local helper rather than
    shared, so each adapter can evolve its message-translation logic
    independently as the Anthropic wire format evolves.
    """
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
    """Strip the OpenClaw <extras> envelope. Per MEMORY.md lesson 2026-08-20."""
    import re
    return re.sub(r"<extras>.*?</extras>", "", text, flags=re.DOTALL)