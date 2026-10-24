"""memory_update tool — amend an existing long-form memory.

Phase 13 (2026-10-15). Use this to add a small detail or correct an
inaccuracy in an existing memory, rather than writing a new memory
(which would bloat recall results). The original created_at is
preserved; updated_at is bumped; all chunks are re-generated.
"""
from __future__ import annotations

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_update",
    "description": (
        "Amend an existing long-form memory with a new version of its "
        "content. The memory's id stays the same, the original "
        "created_at is preserved, and all chunks are re-generated "
        "from the new content. Use this when a memory is mostly "
        "correct but needs a small detail added, fixed, or removed. "
        "Don't use this to write a new memory — for that, use "
        "memory_write. Don't use this to delete a memory (use "
        "memory_recall to find it, then ask the operator to delete it "
        "directly via the new memory_delete tool (added 2026-10-18) — but "
        "now reachable as `memory_delete`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": (
                    "The id of the memory to amend. Get this from "
                    "memory_recall or recall_memory results."
                ),
            },
            "new_content": {
                "type": "string",
                "description": (
                    "The new full content of the memory. Must be "
                    "self-contained; the old content is replaced "
                    "entirely (not merged)."
                ),
            },
        },
        "required": ["memory_id", "new_content"],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    memory_id = args.get("memory_id")
    new_content = str(args.get("new_content", "")).strip()

    if memory_id is None:
        return {"error": "memory_id is required"}
    if not new_content:
        return {"error": "new_content is required and must be non-empty"}
    if len(new_content) > 32000:
        return {"error": f"new_content too long ({len(new_content)} chars; max 32000)"}

    try:
        updated_id = ctx.memory.update_memory(
            memory_id=int(memory_id),
            new_content=new_content,
        )
    except LookupError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"update failed: {type(e).__name__}: {e}"}

    return {
        "memory_id": updated_id,
        "bytes": len(new_content),
        "note": "memory amended; chunks re-generated; original created_at preserved",
    }


registry.register({
    **TOOL,
    "run": run,
})
