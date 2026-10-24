"""memory_write tool — write a new long-form memory.

Phase 13 (2026-10-15). Creates a row in the per-agent `memories` table
and chunks it for semantic search. Independent of the chat-flow auto-
chunk (which fires on every turn and only captures the user's and the
assistant's text).

Use this for observations, conclusions, or facts the agent wants to
remember that the user didn't explicitly say. The memory is un-pinned
by default (searchable via memory_recall but not auto-injected into
every prompt). Set pin=true for high-priority facts that should be in
every future prompt. Idempotent on content: a second call with the
same text returns 'duplicate: true' instead of creating a second row.

To AMEND an existing memory with a small detail, use `memory_update`
instead — it preserves the original created_at, keeps the memory_id
stable, and re-chunks the content.
"""
from __future__ import annotations

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_write",
    "description": (
        "Write a new long-form memory to the agent's memory database, "
        "INDEPENDENT of the chat-flow auto-chunks (which fire on every "
        "turn and only capture the user's and the assistant's text). "
        "Use this for observations, conclusions, or facts the agent "
        "wants to remember that the user didn't explicitly say — e.g. "
        "'I just discovered that X is true', 'the operator told me Y "
        "offhand', 'I notice that the conversation pattern Z is "
        "happening'. The memory is un-pinned by default (searchable "
        "via memory_recall but not auto-injected into every prompt). "
        "Set pin=true for high-priority facts that should be in every "
        "future prompt. Idempotent on content: a second call with the "
        "same text returns 'duplicate: true' instead of creating a "
        "second row. To amend an existing memory with a small detail, "
        "use `memory_update(memory_id, new_content)` instead of "
        "writing a new row."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The long-form text to remember. Self-contained, "
                    "ideally 20+ words. Will be embedded AND chunked "
                    "for semantic search. Max ~32K chars (no hard cap, "
                    "but large values are slow to embed)."
                ),
            },
            "pin": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, mark as permanent (auto-injected into "
                    "future prompts). Use sparingly — every pinned "
                    "memory adds to every prompt's cost."
                ),
            },
            "importance": {
                "type": "integer",
                "default": 5,
                "minimum": 0,
                "maximum": 10,
                "description": (
                    "Importance 0-10. Default 5. Higher = more likely "
                    "to be surfaced in permanent injection ordering."
                ),
            },
            "source_type": {
                "type": "string",
                "enum": ["chat", "email", "code", "doc", "system"],
                "default": "system",
                "description": (
                    "Semantic source-type tag. Default 'system' for "
                    "agent-written memories. Use 'chat' for chat-"
                    "derived (rare; usually those are auto-chunked), "
                    "'email' for email-derived, 'doc' for document-"
                    "derived, 'code' for code-derived."
                ),
            },
        },
        "required": ["text"],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    text = str(args.get("text", "")).strip()
    pin = bool(args.get("pin", False))
    importance = int(args.get("importance", 5))
    source_type = args.get("source_type") or "system"

    if not text:
        return {"error": "text is required and must be non-empty"}
    if len(text) > 32000:
        return {"error": f"text too long ({len(text)} chars; max 32000)"}
    if not (0 <= importance <= 10):
        return {"error": f"importance must be 0-10, got {importance}"}

    try:
        new_id = ctx.memory.write_memory(
            text=text,
            importance=importance,
            source_type=source_type,
            pin=pin,
            pin_source="model" if pin else "operator",
            created_by="agent",
        )
    except LookupError as e:
        return {
            "error": str(e),
            "duplicate": True,
            "note": "a memory with identical content already exists; recall it via memory_recall or amend with memory_update",
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"write failed: {type(e).__name__}: {e}"}

    return {
        "memory_id": new_id,
        "bytes": len(text),
        "pinned": pin,
        "importance": importance,
        "source_type": source_type,
    }


registry.register({
    **TOOL,
    "run": run,
})
