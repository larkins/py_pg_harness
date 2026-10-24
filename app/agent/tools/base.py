"""Tool Protocol + ToolContext.

A tool is a model-callable function. The agent loop never sees a tool's
implementation — only the registered Tool object. Adding a tool = writing
a new module that registers itself on import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable
from uuid import UUID


@dataclass
class ToolContext:
    """Per-tool-call context. Carries agent + conversation + memory handle."""
    agent_name: str
    conversation_id: UUID
    user_id: int | None
    memory: Any        # MemoryAPI (typed loosely to avoid circular import)
    # Files the current turn must not touch (2026-10-23). The reverie
    # turn sets this to the protected identity files (SOUL.md), which
    # may only be edited with the user's explicit permission. Empty
    # tuple = nothing protected.
    protected_paths: tuple[str, ...] = ()


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict   # JSON schema for input

    async def run(self, args: dict, ctx: ToolContext) -> dict: ...


# A simpler type for "thing that has the tool fields" (registry accepts both
# Protocol instances and dataclass-style objects).
ToolLike = Any
