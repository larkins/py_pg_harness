"""memory_recall tool — model calls this to search long-term memory.

Phase 13 (2026-10-15): the result list now includes `source_table`
("turns" | "memories") and `source_memory_id` so the agent knows
whether a hit is a chunk of a turn (auto-chunked from a chat) or a
chunk of a long-form memory (created via memory_write or imported).
If the hit is a memory, the LLM should consider calling
recall_memory (if it exists in future versions) to get the full
content; for now, the chunk's content is a slice of the memory.
"""
from __future__ import annotations

import time

from .base import ToolContext
from . import registry


TOOL = {
    "name": "memory_recall",
    "description": (
        "Search your long-term memory for content relevant to the "
        "query. Returns a list of memory chunks with content, source "
        "role (user/assistant), source_table (turns|memories), "
        "approximate age in days, and a relevance score. Use this when "
        "the user asks a question that might relate to prior "
        "conversation history or stored facts. Do NOT use this for "
        "the current turn's content — only past conversations. Hits "
        "with source_table='memories' are slices of long-form "
        "memories; the full memory content may be longer than the "
        "chunk returned here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The semantic search query. Be specific.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 10,
                "description": "How many chunks to return.",
            },
        },
        "required": ["query"],
    },
}


async def run(args: dict, ctx: ToolContext) -> dict:
    query = args["query"]
    top_k = int(args.get("top_k", 10))
    chunks = ctx.memory.search(query=query, top_k=top_k)
    now = time.time()
    return {
        "query": query,
        "chunks": [
            {
                "id": c.id,
                "role": c.role,
                "source_table": c.source_table,
                "source_turn_id": c.source_turn_id,
                "source_memory_id": c.source_memory_id,
                "content": c.content,
                "score": round(c.score, 4),
                "age_days": round(
                    (c.created_at.timestamp() - now) / -86400.0, 1
                ) if c.created_at else None,
                "importance": c.importance,
            }
            for c in chunks
        ],
        "count": len(chunks),
    }


registry.register({
    **TOOL,
    "run": run,
})
