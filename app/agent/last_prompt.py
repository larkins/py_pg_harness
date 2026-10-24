"""last_prompt — captures the most recent prompt sent to the LLM.

A single shared module-level slot, thread-safe (gunicorn sync worker
has 4 threads). The agent loop calls `set(...)` before each
adapter.complete() call; the admin route calls `get()` to return the
state as JSON.

Single-process: this is fine for our 1-worker gunicorn. For multi-worker
deploys, switch to Redis or per-worker keys.

One slot per agent_name. set() writes to _states[agent_name];
get(agent_name) reads that slot. The operator can scope the
admin route with ?agent=<name> to get the last capture for a
specific agent instead of the global 'most recent'.

Only contains the LATEST prompt per agent. The state is overwritten on every
chat. If the operator wants history, that's a follow-up (would need
to write to a per-conversation log).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any


_lock = threading.Lock()
_states: dict[str, dict[str, Any]] = {}


def _serialize_message(m: Any) -> dict[str, Any]:
    """Message -> JSON-safe dict. content may be str / dict / list."""
    role = getattr(m, "role", "user")
    content = getattr(m, "content", "")
    tool_call_id = getattr(m, "tool_call_id", None)
    out: dict[str, Any] = {"role": role, "content": _safe(content)}
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    return out


def _serialize_tool(t: Any) -> dict[str, Any]:
    """Tool (dict-style with name/description/parameters OR object-style)."""
    if isinstance(t, dict):
        return {
            "name": t.get("name"),
            "description": t.get("description"),
            "parameters": _safe(t.get("parameters")),
        }
    return {
        "name": getattr(t, "name", None),
        "description": getattr(t, "description", None),
        "parameters": _safe(getattr(t, "parameters", None)),
    }


def _safe(obj: Any) -> Any:
    """Make json.dumps()-safe. Falls back to repr() on unserializable."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def set(
    *,
    conversation_id: str,
    user_id: int | None,
    agent_name: str,
    model: str,
    adapter_id: str,
    tool_iterations: int,
    messages: list,
    tools: list,
) -> None:
    global _state
    payload = {
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "conversation_id": conversation_id,
        "user_id": user_id,
        "agent_name": agent_name,
        "model": model,
        "adapter_id": adapter_id,
        "tool_iterations": tool_iterations,
        "message_count": len(messages),
        "tool_count": len(tools),
        "messages": [_serialize_message(m) for m in messages],
        "tools": [_serialize_tool(t) for t in tools],
    }
    with _lock:
        _states[agent_name] = payload


def get(agent_name: str | None = None) -> dict[str, Any] | None:
    """Return a copy of the latest captured state.

    Args:
        agent_name: if given, return that agent's most recent capture.
                    If None, return the most recent capture across
                    ALL agents (preserved legacy behaviour for the
                    index page menu where there's no scope).

    Returns None if no matching capture exists.
    """
    with _lock:
        if agent_name is not None:
            state = _states.get(agent_name)
        else:
            # Most recent across all agents (by captured_at timestamp)
            state = max(
                _states.values(),
                key=lambda s: s.get("captured_at", 0),
                default=None,
            )
        if state is None:
            return None
        # Deep-ish copy via json round-trip (state is already
        # JSON-safe by construction).
        return json.loads(json.dumps(state))


def clear() -> None:
    """Reset the state (used in tests). Wipes all agent slots."""
    with _lock:
        _states.clear()
