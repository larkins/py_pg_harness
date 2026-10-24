"""memory_unpin tool — demote a long-form memory from permanent.

Unpinned memories remain in the database and are still retrievable
via memory_recall, but stop being auto-injected into every prompt
(no longer appear in get_permanent results).

Operates ONLY on long-form memories. Does NOT accept chunk_id -
that path was removed 2026-10-18; see memory_pin.py for the
rationale.
"""
from __future__ import annotations

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_unpin",
    "description": (
        "Demote a previously-pinned long-form memory. The memory "
        "stays in the database (still retrievable via "
        "memory_recall) but stops being auto-injected into every "
        "prompt. Cascades to all chunks of that memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "The id of the memory to unpin.",
            },
        },
        "required": ["memory_id"],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    memory_id = args.get("memory_id")
    if memory_id is None:
        return {"error": "memory_id is required"}

    try:
        mem = ctx.memory.unpin_memory(int(memory_id))
    except LookupError as e:
        return {"error": str(e), "memory_id": memory_id, "permanent": True}

    return {
        "memory_id": mem["id"],
        "permanent": False,
    }


registry.register({**TOOL, "run": run})
