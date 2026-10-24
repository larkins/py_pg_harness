"""last_emission — captures the FULL thought stream from a chat.

A "thought stream" is the iteration-by-iteration trace of the
agent's tool-use loop: the assistant's text (or null when it
chose to call a tool), the tool calls it requested, and the
results the tools returned. The complementary module
`last_prompt` only captures the prompt that went INTO one LLM
call; this module captures what CAME OUT plus the loop shape.

Use case: when an agent gives a surprising answer, the operator
opens /admin/last-emission to see exactly which tools it called
and what each tool returned. The "Last raw prompt" view is
useful for "what did the model see" (system prompt + history +
bootstrap + permanent memories + the user message); this view
is useful for "what did the model do" (text + tool calls +
results + final answer).

Per-agent slots (matching last_prompt's pattern): one dict
keyed by agent_name. Overwritten on every chat. The admin
route accepts ?agent=<name> to scope to one agent.

Also captures (2026-10-20):
- `attachments` — the list of file attachments produced by the
  attach_file tool (same shape as user uploads: url, filename,
  content_type, size). Mirrors the `attachments` JSONB column on
  <agent>.turns.
- `trajectory` — the full per-iteration trace dict (same shape
  as the `trajectory` JSONB column on <agent>.turns, Migration
  011). Including `max_tool_iterations` when known so the admin
  view can detect "model fabricated the canned failure text"
  — the message says "40 tool iterations" while the configured
  cap is 500 (see harness memory 2026-10-20 agent2 turn 279).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any


_lock = threading.Lock()
_states: dict[str, dict[str, Any]] = {}


def _safe(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def set(
    *,
    agent_name: str,
    user_text: str,
    conversation_id: str,
    user_id: int | None,
    model: str,
    adapter_id: str,
    iterations: list[dict],
    final_text: str | None,
    retrieved_chunk_ids: list[int],
    tool_iterations: int,
    latency_ms: int,
    attachments: list[dict] | None = None,
    max_iters: int | None = None,
) -> None:
    total_tool_calls = sum(len(it.get("tool_calls") or []) for it in iterations)
    # Mirror the shape of turns.trajectory (Migration 011, 2026-10-19)
    # so the admin /admin/last-emission view can render the same JSONB
    # that gets persisted — without forcing the operator to query
    # Postgres. `max_tool_iterations` is included when known so the
    # view can detect the "model fabricated the canned failure text"
    # failure mode (the message says "40 tool iterations" while the
    # configured cap is 500, see harness memory 2026-10-20).
    trajectory = {
        "iterations": iterations,
        "total_iterations": len(iterations),
        "total_tool_calls": total_tool_calls,
        "tool_iterations": tool_iterations,
        "latency_ms": latency_ms,
        "model": model,
    }
    if max_iters is not None:
        trajectory["max_tool_iterations"] = max_iters
    payload = {
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "agent_name": agent_name,
        "user_text": user_text,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "model": model,
        "adapter_id": adapter_id,
        "iterations": iterations,
        "final_text": final_text,
        "retrieved_chunk_ids": retrieved_chunk_ids,
        "total_iterations": len(iterations),
        "total_tool_calls": total_tool_calls,
        "tool_iterations": tool_iterations,
        "latency_ms": latency_ms,
        "trajectory": trajectory,
        "attachments": list(attachments or []),
    }
    if max_iters is not None:
        payload["max_tool_iterations"] = max_iters
    with _lock:
        _states[agent_name] = payload


def get(agent_name: str | None = None) -> dict[str, Any] | None:
    with _lock:
        if agent_name is not None:
            state = _states.get(agent_name)
        else:
            state = max(
                _states.values(),
                key=lambda s: s.get("captured_at", 0),
                default=None,
            )
        if state is None:
            return None
        return json.loads(json.dumps(state))


def clear() -> None:
    with _lock:
        _states.clear()