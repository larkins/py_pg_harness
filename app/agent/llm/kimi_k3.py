"""kimi_k3 adapter — Kimi K3 via OpenAI-compatible chat completions.

Per the Kimi Coding API:
- Base URL = https://api.kimi.com/coding/v1
- OpenAI-compatible wire format
- model name = "k3"
- Auth: Authorization: Bearer KIMI_API_KEY
- Tool calling uses OpenAI's `tool_calls` array with `function.name` and
  `function.arguments` (stringified JSON — must json.loads())
- `finish_reason: "tool_calls"` when the model wants tools
- Includes a `reasoning_content` field for chain-of-thought; we discard
  it on the way back to keep history clean

Phase 2 §19 open question 11 (Kimi API base URL) — resolved:
  base URL https://api.kimi.com/coding/v1, model "k3".
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .base import BaseLLM, LLMResponse, Message, ToolCall

log = logging.getLogger(__name__)


class LLM(BaseLLM):
    name = "kimi/Kimi-K3"
    adapter_id = "kimi_k3"

    def __init__(self, config: Any) -> None:
        self.api_key = config.llm.api_key
        self.base_url = config.llm.base_url.rstrip("/")
        self.model = config.llm.model
        self.default_max_tokens = config.llm.max_tokens
        self.timeout_s = config.llm.timeout_s
        self.temperature = config.llm.temperature
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.timeout_s),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
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
        oa_messages = _to_openai(messages)
        # Per Kimi API quirk (discovered 2026-10-08): only temperature=1
        # is accepted. Override whatever the config / caller specified.
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": 1.0,
            "messages": oa_messages,
        }
        if tools:
            body["tools"] = [_to_openai_tool(t) for t in tools]

        t0 = time.monotonic()
        try:
            r = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("kimi_k3 HTTP error: %s body=%s",
                      e.response.status_code, e.response.text[:500])
            raise
        except httpx.HTTPError as e:
            log.error("kimi_k3 transport error: %s", e)
            raise

        data = r.json()
        latency_ms = int((time.monotonic() - t0) * 1000)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        text = message.get("content") or None
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            # OpenAI wire: arguments is a STRING of JSON, not a parsed dict.
            args_raw = fn.get("arguments", "{}")
            try:
                args_dict = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                log.warning("kimi_k3: bad tool args JSON: %r", args_raw)
                args_dict = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args_dict,
            ))

        usage = data.get("usage", {})
        sr_norm = {
            "stop": "stop",
            "tool_calls": "tool_use",
            "length": "length",
        }.get(finish_reason, "stop")

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=sr_norm,
            raw=data,
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "latency_ms": latency_ms,
            },
        )

    def format_tool_result(self, call: ToolCall, result: Any) -> Message:
        """OpenAI expects tool results as `role: 'tool'` messages with
        `tool_call_id` linking back to the originating call."""
        if isinstance(result, str):
            content_str = result
        else:
            content_str = json.dumps(result, default=str)
        return Message(
            role="tool",
            content=content_str,
            tool_call_id=call.id,
        )

    def extract_assistant_content(self, response: LLMResponse) -> Any:
        """Reconstruct the assistant message for round-tripping.

        OpenAI doesn't have 'tool_use' content blocks — the tool_calls
        live at the message level (response.raw.choices[0].message).
        We return the full OpenAI assistant message dict so the loop
        can append it as Message(role='assistant', content=<dict>) and
        `_to_openai` will recognise and pass it through unchanged.
        """
        if response.raw is None:
            return []
        msg = ((response.raw.get("choices") or [{}])[0]).get("message", {})
        return msg


def _to_openai(messages: list[Message]) -> list[dict]:
    """Convert our Message list to OpenAI chat.completions format.

    - All system messages collapse into a single leading system message.
    - User messages pass through.
    - tool messages use the dedicated `role: 'tool'` shape with
      `tool_call_id` linking back.
    - assistant messages that come from `extract_assistant_content` carry
      a pre-shaped OpenAI message dict as their content. We pass these
      through so the model sees its own prior tool_calls.
    """
    out: list[dict] = []
    sys_acc: list[str] = []

    for m in messages:
        if m.role == "system":
            sys_acc.append(m.content if isinstance(m.content, str) else str(m.content))
            continue

        if m.role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.tool_call_id or "",
                "content": m.content if isinstance(m.content, str) else json.dumps(m.content),
            })
            continue

        # Assistant / user: handle pre-shaped dict from extract_assistant_content
        if isinstance(m.content, dict) and "role" in m.content:
            out.append(m.content)
            continue

        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
        else:
            out.append({"role": m.role, "content": str(m.content)})

    if sys_acc:
        out.insert(0, {"role": "system", "content": "\n\n".join(sys_acc)})
    return out


def _to_openai_tool(t: Any) -> dict:
    """Convert our Tool dict/object to OpenAI's function-calling format."""
    if isinstance(t, dict):
        return {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }
