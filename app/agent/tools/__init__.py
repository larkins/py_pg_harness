"""Tool framework package.

Importing this package triggers each tool module's side-effect
`registry.register(...)` call, populating TOOL_REGISTRY.

Tool roster (Phase 15.3, 2026-10-18):
  7 memory tools (in-DB): memory_recall, memory_recent, memory_pin,
    memory_unpin, memory_write, memory_update, memory_delete
  3 operational tools: sql_query, exec_bash, attach_file

Phase 13 (memories table) added memory_update (amend a long-form
memory in place) and refactored memory_write to write a memory row
(instead of a chunk directly), memory_pin/unpin to accept either
memory_id or chunk_id, and memory_recall to surface the source_table
so the LLM knows if a hit is a turn-chunk or a memory-chunk.

Phase 15 (2026-10-18) added attach_file — copy a file under the
service user's home directory into the harness uploads dir so the
chat UI can render a file-attachment icon (paperclip) on the
assistant's response.

Phase 15.2 (2026-10-18) fixed the tool-call-repair branch in
app/agent/loop.py — when the model narrated a tool call and the
repair layer synthesized tool_use blocks with a `repaired_…` id,
the loop was appending a text-only assistant message and the API
returned 400 with "tool result's tool id ... not found (2013)".
3-way branch now: real tool_use → append as-is; synthesized →
build synthetic content with proper tool_use blocks; pure text →
append as-is.

Phase 15.3 (2026-10-18):
  - memory_delete — LLM-callable hard delete (was operator-only).
    Cascades to chunks via the existing FK ON DELETE CASCADE.
    Audited in <agent>.memory_audit with op='delete_memory'.
  - memory_recent rework — was "recent chunks" (turn-chunks and
    memory-chunks intermingled by recency); now returns the parent
    <agent>.memories rows directly. The chunked view broke the
    conceptual unit (a long-form memory is a parent row with N
    child chunks). Drops the `role` parameter (no semantic
    equivalent on the memories table).
"""

from . import (  # noqa: F401
    memory_recall, memory_recent, memory_pin, memory_unpin, memory_write,
    memory_update, memory_delete, sql_query, exec_bash, attach_file,
)
from .registry import all_tools, get, names, register
from .base import ToolContext

__all__ = ["all_tools", "get", "names", "register", "ToolContext"]
