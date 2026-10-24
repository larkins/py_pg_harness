"""memory_recent tool — list the most recent N long-form memories.

Reworked 2026-10-18: was previously "recent chunks" (turn-chunks and
memory-chunks intermingled by recency). A single long-form memory
is a parent row with N child chunks; the chunked view broke the
conceptual unit. This now returns the parent <agent>.memories rows
directly so the LLM sees "these are the N long-form memories I have"
as a unit, not "these are N fragments of various memories".

This is a TEMPORAL query (no semantic ranking). For semantic
content search, use memory_recall instead. For mutating a specific
memory, use memory_update (amend) or memory_delete (remove).
"""
from __future__ import annotations

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_recent",
    "description": (
        "List the most recent N long-form memories in chronological order "
        "(newest first). Unlike memory_recall, this does NOT do semantic "
        "search — use it when you want recent context regardless of topic. "
        "Returns the parent memory rows directly (one per long-form memory, "
        "not per chunk), so the LLM can reason about memories as whole "
        "units. Examples: the user asks 'what did we discuss yesterday?', "
        "'list the last 10 memories', 'what's in memory right now?'. "
        "Returns up to `limit` memories (default 10, max 50) with id, "
        "content, source_type, importance, permanent, pin_source, "
        "pinned_at, metadata, created_at, age_days."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "How many of the most recent memory rows to return.",
            },
        },
        "required": [],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    limit = int(args.get("limit", 10))
    memories = ctx.memory.list_recent(limit=limit)
    return {
        "count": len(memories),
        "limit": limit,
        "memories": memories,
    }


registry.register({
    **TOOL,
    "run": run,
})
