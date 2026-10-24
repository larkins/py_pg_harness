"""memory_delete tool — permanently delete a long-form memory.

Added 2026-10-18 per Mal's request. Previously, deletion was
operator-only (had to be done via psql because the LLM
accidentally created duplicate memories). Now exposed to the LLM
as a regular tool, scoped to a single memory_id so it's hard to
misuse for bulk deletion.

Use this when:
  - a memory was created by mistake (e.g. a duplicate of an
    existing memory)
  - the content is wrong AND the operator has decided the right
    fix is to remove it (not amend via memory_update)
  - a memory is no longer relevant and the LLM has been told to
    prune old context

For amending (most common case), use memory_update — it preserves
the memory_id and created_at, just changes the content. For
duplicates where the LLM created the same content twice with a
different memory_id, use this tool to remove the duplicate.

Hard delete (no soft-delete / archive): the row and its chunks
go away permanently. The audit row in <agent>.memory_audit
records the deletion with the prior content_hash for forensic
review, but the content itself is not preserved.
"""
from __future__ import annotations

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_delete",
    "description": (
        "Permanently delete a single long-form memory by id. Use this "
        "when you've accidentally created a duplicate memory, when a "
        "memory's content is wrong and the operator has decided to "
        "remove it (vs amend via memory_update), or when a memory is "
        "no longer relevant. The memory_id and ALL its chunks are "
        "removed (chunks auto-cleanup via FK ON DELETE CASCADE). The "
        "deletion is audited in <agent>.memory_audit with the prior "
        "content hash. To amend a memory's content while keeping its "
        "id and created_at, use memory_update instead — that is the "
        "safer default and you should reach for it first. Do NOT use "
        "this in a loop to delete many memories — there is no bulk "
        "delete by design; ask the operator to run psql if you need "
        "to remove more than one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": (
                    "The id of the memory to delete. Get this from "
                    "memory_recall or memory_recent."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Short justification for the audit log "
                    "(e.g. 'duplicate of memory_id=42 with same content'). "
                    "Optional but strongly recommended — without it the "
                    "audit row will just show 'no reason given'."
                ),
            },
        },
        "required": ["memory_id"],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    memory_id = args.get("memory_id")
    reason = (args.get("reason") or "").strip() or None

    if memory_id is None:
        return {"error": "memory_id is required"}

    try:
        result = ctx.memory.delete_memory(
            memory_id=int(memory_id),
            reason=reason,
            source="model",
        )
    except LookupError as e:
        return {
            "error": str(e),
            "memory_id": memory_id,
            "deleted": False,
        }
    except Exception as e:
        return {
            "error": f"delete failed: {type(e).__name__}: {e}",
            "memory_id": memory_id,
            "deleted": False,
        }

    return result


registry.register({
    **TOOL,
    "run": run,
})
