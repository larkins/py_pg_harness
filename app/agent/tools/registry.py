"""TOOL_REGISTRY — collects all tools via import side-effects.

Each tool module calls `registry.register(my_tool)` at import time. The
agent loop reads TOOL_REGISTRY to get the list of tools to pass to the
LLM adapter.
"""
from __future__ import annotations

from typing import Any

_REGISTRY: dict[str, Any] = {}


def register(tool: Any) -> None:
    """Register a tool. Idempotent (last write wins)."""
    name = tool["name"] if isinstance(tool, dict) else tool.name
    _REGISTRY[name] = tool


def get(name: str) -> Any | None:
    return _REGISTRY.get(name)


def all_tools() -> list[Any]:
    return list(_REGISTRY.values())


def names() -> list[str]:
    return list(_REGISTRY.keys())
