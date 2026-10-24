"""BaseLLM Protocol + LLMResponse + Message + ToolCall data types.

The agent loop, MemoryAPI, and tool framework are written once and never
change when a new model is added. Adding a model = writing one new
adapter module that satisfies BaseLLM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass
class Message:
    """One message in the conversation stream.

    role: 'system' | 'user' | 'assistant' | 'tool'
    content: list of blocks (type-specific — adapters normalize)
    tool_call_id: when role='tool', this is the originating tool call's
                 provider-assigned id. Anthropic stores it in the content
                 block; OpenAI uses a top-level field. Adapters normalize.
    """
    role: str
    content: Any   # str for simple messages; list of blocks for tool results
    tool_call_id: str | None = None


@dataclass
class ToolCall:
    """A model-issued tool invocation request."""
    id: str                # provider-assigned id
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Normalized response from a BaseLLM.complete() call.

    The loop only branches on response.text (final answer) vs
    response.tool_calls (continue the loop). raw is for logging/audit.

    thinking_blocks (2026-10-20): per the Anthropic extended-thinking
    spec, models like MiniMax-M3.1-Flash return a separate sequence
    of "thinking" content blocks alongside text + tool_use. The agent
    loop does NOT branch on these (the model has already done its
    internal reasoning), but the harness captures them so:
      1. They round-trip back to the model in extract_assistant_content
         — Anthropic's spec requires this for the model to maintain
         its reasoning state across tool calls.
      2. They are persisted in turns.trajectory (per-iter thought
         stream) for the /admin/last-emission debug view.
    Other adapters (M3, kimi_k3, glm_5_3_flash) leave this as the
    default empty list — they don't speak Anthropic-style thinking.
    """
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"        # "stop" | "tool_use" | "length" | "error"
    raw: Any = None
    usage: dict = field(default_factory=dict)   # input_tokens, output_tokens, latency_ms
    thinking_blocks: list[dict] = field(default_factory=list)


@runtime_checkable
class BaseLLM(Protocol):
    """Adapter contract — every LLM module implements this.

    v0: complete() is sync (httpx.Client). Future versions may move to
    async once the LLM client can be hosted on a long-lived event loop
    outside the Flask worker.
    """

    name: str                  # human-readable, e.g. "minimax/MiniMax-M3"
    adapter_id: str            # module name, e.g. "minimax_m3"

    def __init__(self, config: Any) -> None: ...

    def complete(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,        # list[Tool]
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> LLMResponse: ...

    def format_tool_result(self, call: ToolCall, result: Any) -> Message:
        """Render a tool result in the format this model expects
        (Anthropic tool_result block, OpenAI tool message, etc.)."""
        ...

    def _post_with_retry(
        self,
        url: str,
        headers: dict,
        body: dict,
        *,
        max_retries: int = 3,
    ) -> Any:
        """POST with retry-on-5xx + 429 + transient network errors.

        Returns the httpx.Response on success. Raises the LAST exception
        (HTTPStatusError, etc.) if all retries are exhausted.

        4xx errors (except 429) propagate immediately — those are auth,
        bad-request, etc., where retry won't help. 5xx (server overload)
        and 429 (rate limit) get exponential backoff. Transient network
        errors (ConnectError, ReadTimeout, RemoteProtocolError, etc.)
        also retry. RemoteProtocolError was added 2026-10-21: MiniMax
        dropped the HTTP/2 connection mid-stream on a long turn, which
        is unrecoverable for that call but fine for the next one.

        Backoff schedule (default max_retries=3 → 4 attempts total):
            attempt 0 → no delay (first try)
            attempt 1 → 1s
            attempt 2 → 2s
            attempt 3 → 4s
        Plus honours `Retry-After` if the server sends it (in seconds
        or HTTP-date format).

        Added 2026-10-20 after agent2 hit a 529 from MiniMax mid-iter-3
        with 6 tool calls already executed; the loop had no retry path
        and surfaced the bare HTTPStatusError to the user.
        """
        import time
        import httpx

        last_exc: Exception | None = None
        attempts = max_retries + 1
        for attempt in range(attempts):
            try:
                r = self._client.post(url, headers=headers, json=body)
                # 5xx + 429 are transient; everything else is final.
                if r.status_code >= 500 or r.status_code == 429:
                    if attempt < max_retries:
                        # Honour server-suggested backoff when present.
                        retry_after = r.headers.get("retry-after") if r.headers else None
                        try:
                            delay = float(retry_after) if retry_after else None
                        except ValueError:
                            delay = None
                        if delay is None:
                            delay = min(2 ** attempt, 8)
                        log.warning(
                            "%s: HTTP %d on attempt %d/%d; retrying in %.1fs",
                            getattr(self, "name", self.__class__.__name__),
                            r.status_code,
                            attempt + 1,
                            attempts,
                            delay,
                        )
                        time.sleep(delay)
                        continue
                    # Final attempt failed; raise.
                    r.raise_for_status()
                return r
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.ConnectTimeout,
                    httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt < max_retries:
                    delay = min(2 ** attempt, 8)
                    log.warning(
                        "%s: transient network error on attempt %d/%d (%s); retrying in %.1fs",
                        getattr(self, "name", self.__class__.__name__),
                        attempt + 1,
                        attempts,
                        type(e).__name__,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise
        # Defensive — shouldn't get here.
        if last_exc:
            raise last_exc
        raise RuntimeError("retry loop exited without result")

    def extract_assistant_content(self, response: LLMResponse) -> Any:
        """Extract the original assistant message content from the response
        (including tool_use blocks). The loop needs to add this back to
        the conversation history so the model sees its own previous turns."""
        ...

    def close(self) -> None: ...
