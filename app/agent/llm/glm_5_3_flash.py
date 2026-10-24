"""glm_5_3_flash adapter — Zhipu GLM-5.3-flash via OpenAI-compatible chat completions.

Per the z.ai / bigmodel.cn API:
- Base URL = https://open.bigmodel.cn/api/paas/v4  (z.ai proxy also: https://api.z.ai/api/paas/v4)
- OpenAI-compatible wire format
- model name = "glm-5.3-flash" (also: glm-5.3-flashx for the even-faster variant)
- Auth: Authorization: Bearer <ZAI_API_KEY>
- Tool calling uses OpenAI's `tool_calls` array with `function.name` and
  `function.arguments` (stringified JSON — must json.loads())
- `finish_reason: "tool_calls"` when the model wants tools
- 11 models available in the catalog as of 2026-10-19; glm-5.3-flash is
  the "fast" tier (higher throughput, lower latency, lower cost vs glm-5.3 base)

Provisioned 2026-10-19 for the agent3 z.ai test. Used as a per-agent
adapter — agent3 gets glm-5.3-flash, agent1 + agent2 stay on minimax_m3.

Patterned after kimi_k3 (also OpenAI-compatible). Differences:
- Different base_url (open.bigmodel.cn vs api.kimi.com)
- Different model name (glm-5.3-flash vs k3)
- Reads api_key from `config.llm.api_key` like the other adapters — for
  the per-agent override path, extensions.py sets `config.llm = override_llm_config`
  for the override agent before constructing the adapter.
- z.ai accepts any temperature in [0, 2]; we DON'T force temperature=1.0
  the way kimi_k3 does. The config-supplied value is used.

z.ai-specific thinking control (added 2026-10-19):
- The model has extended thinking ("reasoning_content") that can eat all
  output tokens on complex prompts. z.ai accepts a `thinking` field in
  the request body to control this:
  - `{"type": "disabled"}` — turn off thinking entirely (model responds
    directly without a planning phase)
  - `{"type": "enabled"}` — use model-chosen thinking budget (default)
  - `{"type": "enabled", "budget": N}` — cap thinking at N tokens
- Configured via `config.llm.thinking` (read with getattr default None;
  if not set, the field is omitted from the request). Per-agent override
  possible via the standard `llm_per_agent` config block.
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
    name = "zai/glm-5.3-flash"
    adapter_id = "glm_5_3_flash"

    def __init__(self, config: Any) -> None:
        self.api_key = config.llm.api_key
        self.base_url = config.llm.base_url.rstrip("/")
        self.model = config.llm.model
        self.default_max_tokens = config.llm.max_tokens
        self.timeout_s = config.llm.timeout_s
        self.temperature = config.llm.temperature
        # z.ai-specific thinking control (added 2026-10-19).
        # Read from config.llm.thinking if present; default None (use API default).
        # Options: dict like {"type": "disabled"} or {"type": "enabled", "budget": N}.
        # Use {"type": "disabled"} to stop the model from spending all output
        # tokens on extended thinking (was causing turn failures for agent3
        # in Oct 2026 when reading Google Doc URLs).
        self.thinking = getattr(config.llm, "thinking", None)
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
        # z.ai accepts any temperature in [0, 2]; use the config value
        # (different from kimi_k3 which forces 1.0).
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "messages": oa_messages,
        }
        # z.ai thinking control (see __init__ for details)
        if self.thinking is not None:
            body["thinking"] = self.thinking
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
            log.error("glm_5_3_flash HTTP error: %s body=%s",
                      e.response.status_code, e.response.text[:500])
            raise
        except httpx.HTTPError as e:
            log.error("glm_5_3_flash transport error: %s", e)
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
                log.warning("glm_5_3_flash: bad tool args JSON: %r", args_raw)
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

        Same as kimi_k3 — OpenAI's tool_calls live at the message level,
        not in content blocks. Return the full OpenAI assistant message
        dict so the loop can append it as Message(role='assistant',
        content=<dict>) and `_to_openai` will pass it through.
        """
        if response.raw is None:
            return []
        msg = ((response.raw.get("choices") or [{}])[0]).get("message", {})
        return msg


def _to_openai(messages: list[Message]) -> list[dict]:
    """Convert our Message list to OpenAI chat.completions format.

    Identical to kimi_k3's _to_openai — duplicated here to keep the
    adapters self-contained. If we add a third OpenAI-compatible
    adapter, this is a refactor candidate (move into a shared
    `_openai_common.py`).
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
