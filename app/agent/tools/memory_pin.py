"""memory_pin tool — promote a long-form memory to permanent.

Pinned memories are injected into every prompt as standing context
(see MemoryAPI.get_permanent). Use this when a fact must be
remembered across all future conversations.

The tool operates ONLY on long-form memories (rows in
<agent>.memories). It does NOT accept chunk_id - that path was
removed 2026-10-18 because pinning a single turn-chunk was a
confused operation (the chunk is just one fragment of an
ephemeral turn, not a useful standing instruction). If you want
a turn-chunk turned into a permanent long-form memory, use
memory_write with the text of the chunk.

The DB cascade is handled inside pin_memory(): setting the
memory's `permanent = TRUE` causes get_permanent() to surface
all chunks of that memory, so a single pin propagates to every
chunk of the long-form memory automatically.
"""
from __future__ import annotations

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_pin",
    "description": (
        "Promote a long-form memory to permanent status. The memory "
        "and all its chunks are then injected into every prompt as "
        "standing context. Use this when the user or the conversation "
        "establishes a fact that must be remembered across all future "
        "conversations - e.g. the user's name, a long-running project, "
        "a standing instruction. Do NOT pin ephemeral facts, recent "
        "conversation details, or anything that should naturally age "
        "out of memory. To create a NEW permanent memory from scratch, "
        "use memory_write with pin=true; to amend an existing memory, "
        "use memory_update; to demote later, use memory_unpin."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": (
                    "The id of the memory to pin (returned by "
                    "memory_recall where source_table='memories'). "
                    "Pinning cascades to all chunks of that memory."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Short justification for why this should be permanent.",
            },
        },
        "required": ["memory_id", "reason"],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    reason = str(args.get("reason", ""))
    if not reason:
        return {"error": "reason is required (audit trail)"}

    memory_id = args.get("memory_id")
    if memory_id is None:
        return {"error": "memory_id is required"}

    try:
        mem = ctx.memory.pin_memory(int(memory_id), source="model")
    except LookupError as e:
        return {"error": str(e), "memory_id": memory_id, "permanent": False}

    return {
        "memory_id": mem["id"],
        "permanent": True,
        "pin_source": mem["pin_source"],
        "pinned_at": mem["pinned_at"].isoformat() if mem["pinned_at"] else None,
        "reason": reason,
    }


registry.register({**TOOL, "run": run})
