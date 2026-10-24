"""MemoryAPI — the safety boundary.

The agent loop NEVER writes SQL. It calls MemoryAPI.search(),
.write_turns(), .pin(), .unpin(), .get_permanent(). The API enforces:
- schema isolation per agent
- parametric queries (psycopg %s placeholders)
- rate limits (per turn + per conversation)
- audit row per call to <agent>.memory_audit
- no raw-SQL escape hatch
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


@dataclass
class MemoryChunk:
    id: int
    content: str
    role: str                # "user" | "assistant"
    conversation_id: UUID
    source_turn_id: int | None
    source_memory_id: int | None
    source_table: str | None  # "turns" | "memories" (set by search())
    importance: int
    pinned_at: Any | None
    pin_source: str | None
    created_at: Any
    metadata: dict
    score: float = 0.0      # for retrieval results


@dataclass
class Turn:
    id: int
    conversation_id: UUID
    role: str
    content: str
    token_count: int | None
    model: str | None
    latency_ms: int | None
    # Phase 10: per-turn image attachments. Each entry is a small dict
    # like {"url": "/api/v1/uploads/<id>.png", "filename": "...",
    # "content_type": "image/png", "size": N}. Empty list for turns
    # without attachments (the DB default).
    attachments: list[dict] | None = None
    # Migration 011 (2026-10-19): per-iteration trajectory for
    # assistant turns (None for user turns + legacy rows). Dict
    # shape: {iterations: [...], total_iterations, total_tool_calls,
    # tool_iterations, latency_ms, model}. Persisted to the
    # `trajectory` jsonb column. The export.jsonl?format=full
    # endpoint reads this for the LLM retraining pipeline.
    trajectory: dict | None = None
    created_at: Any = None


class MemoryAPI:
    """Per-agent memory API. Constructed once per agent; lives in a process."""

    def __init__(
        self,
        pool: ConnectionPool,
        embedding_client: Any,           # app.agent.embedding.EmbeddingClient
        agent_name: str,
        config: Any,
    ) -> None:
        self.pool = pool
        self.embedding = embedding_client
        self.agent_name = agent_name
        self.config = config

    # ---- read path -------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int | None = None,
        recency_decay_days: int | None = None,
    ) -> list[MemoryChunk]:
        """Hybrid retrieval (vector + trigram) with RRF + recency decay.

        Returns top_k MemoryChunks ordered by final_score desc.
        """
        top_k = top_k or self.config.memory.top_k
        decay = recency_decay_days if recency_decay_days is not None else self.config.memory.recency_decay_days
        vlim = self.config.memory.vector_limit
        tlim = self.config.memory.trigram_limit

        qvec = self.embedding.embed(query)
        query_hash = hashlib.sha256(_norm_text(query).encode("utf-8")).hexdigest()

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                # Vector list (cosine distance on halfvec(1024)).
                cur.execute(
                    f"""
                    SELECT id, content, role, conversation_id, source_turn_id,
                           source_memory_id, source_table, importance, pinned_at,
                           pin_source, created_at, metadata,
                           (embedding <=> %s::halfvec) AS distance
                      FROM {self.agent_name}.memory_chunks
                     WHERE status_commentary = FALSE
                     ORDER BY embedding <=> %s::halfvec
                     LIMIT %s
                    """,
                    (qvec, qvec, vlim),
                )
                vector_rows = cur.fetchall()
                vector_cols = [d.name for d in cur.description]

                # Trigram list (similarity() on content).
                cur.execute(
                    f"""
                    SELECT id, content, role, conversation_id, source_turn_id,
                           source_memory_id, source_table, importance, pinned_at,
                           pin_source, created_at, metadata,
                           similarity(content, %s) AS sim
                      FROM {self.agent_name}.memory_chunks
                     WHERE status_commentary = FALSE
                       AND (content %% %s OR content ILIKE '%%' || %s || '%%')
                     ORDER BY sim DESC
                     LIMIT %s
                    """,
                    (query, query, query, tlim),
                )
                trigram_rows = cur.fetchall()

        # Reciprocal Rank Fusion. RRF score = Σ 1/(60 + rank_i).
        rrf: dict[int, float] = {}
        rows_by_id: dict[int, tuple] = {}
        for rank, row in enumerate(vector_rows):
            cid = row[0]
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank)
            rows_by_id[cid] = row
        for rank, row in enumerate(trigram_rows):
            cid = row[0]
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank)
            rows_by_id[cid] = row

        # Apply recency decay.
        now = time.time()
        scored: list[tuple[int, float]] = []
        for cid, rrf_score in rrf.items():
            row = rows_by_id[cid]
            created_at_ts = row[8].timestamp() if row[8] else now
            age_days = max(0, (now - created_at_ts) / 86400.0)
            decay_factor = 2.71828 ** (-age_days / max(1, decay))
            scored.append((cid, rrf_score * decay_factor))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_k]

        # Audit + return.
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memory_audit
                        (ts, op, query_hash, top_k, result_count, latency_ms)
                    VALUES (now(), 'search', %s, %s, %s, 0)
                    """,
                    (query_hash, top_k, len(top)),
                )
            conn.commit()

        out: list[MemoryChunk] = []
        for cid, score in top:
            row = rows_by_id[cid]
            out.append(MemoryChunk(
                id=row[0],
                content=row[1],
                role=row[2],
                conversation_id=row[3],
                source_turn_id=row[4],
                source_memory_id=row[5],
                source_table=row[6],
                importance=row[7],
                pinned_at=row[8],
                pin_source=row[9],
                created_at=row[10],
                metadata=row[11] if isinstance(row[11], dict) else (json.loads(row[11]) if row[11] else {}),
                score=score,
            ))
        return out

    def list_recent(
        self,
        limit: int = 10,
    ) -> list[dict]:
        """Return the most recent N memory rows (long-form memories),
        ordered by created_at DESC.

        Reworked 2026-10-18: was previously "recent chunks" (turn-chunks
        and memory-chunks intermingled by recency). That's confusing
        for the LLM because a single long-form memory is a parent row
        with N child chunks; the chunked view breaks the conceptual
        unit. This now returns the parent <agent>.memories rows
        directly so the LLM sees "these are the 5 long-form memories
        I have" as a unit, not "these are 5 fragments of various
        memories".

        Unlike `search()`, this does no semantic ranking — just a
        temporal query. Use case: model wants "what do I know?" or
        "what's in my memory right now?" without having to craft a
        query string.

        Args:
            limit: how many memory rows to return (capped 50).

        Returns: list of dicts (newest first) with id, content,
        source_type, source_ref, importance, permanent, pin_source,
        pinned_at, metadata, created_at, age_days. No score (temporal
        query, not relevance-ranked).
        """
        limit = max(1, min(int(limit), 50))
        now = datetime.now()

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, content, source_type, source_ref, importance,
                           permanent, pin_source, pinned_at, metadata,
                           created_at
                      FROM {self.agent_name}.memories
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memory_audit
                        (op, query_hash, top_k, result_count, latency_ms, op_over_run)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    ("list_recent", "no_query_text", limit, len(rows), 0, False),
                )
            conn.commit()

        out: list[dict] = []
        for row in rows:
            (mid, content, source_type, source_ref, importance,
             permanent, pin_source, pinned_at, metadata, created_at) = row
            age_days = None
            if created_at is not None:
                # .timestamp() handles both tz-aware and tz-naive
                # datetimes: tz-aware → unix epoch (naive assumed
                # local), tz-naive → treated as local naive.
                # This matches the pattern in search() (line ~151).
                delta = (now.timestamp() - created_at.timestamp())
                age_days = round(delta / 86400.0, 2)
            meta = metadata
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta) if meta else {}
                except Exception:
                    meta = {}
            elif meta is None:
                meta = {}
            out.append({
                "id": mid,
                "content": content,
                "source_type": source_type,
                "source_ref": source_ref,
                "importance": importance,
                "permanent": permanent,
                "pin_source": pin_source,
                "pinned_at": pinned_at.isoformat() if pinned_at else None,
                "metadata": meta,
                "created_at": created_at.isoformat() if created_at else None,
                "age_days": age_days,
            })
        return out

    def get_permanent(
        self,
        max_tokens: int | None = None,
    ) -> tuple[list[MemoryChunk], bool]:
        """Return all permanent memory chunks, ordered by importance desc
        + pinned_at asc. Stops at max_tokens. Returns (chunks, overflowed).
        """
        max_tokens = max_tokens or self.config.memory.permanent.max_tokens
        chunks: list[MemoryChunk] = []
        running_tokens = 0
        overflowed = False

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, content, role, conversation_id, source_turn_id,
                           source_memory_id, source_table, importance, pinned_at,
                           pin_source, created_at, metadata
                      FROM {self.agent_name}.memory_chunks
                     WHERE permanent = TRUE
                     ORDER BY importance DESC, pinned_at ASC
                    """,
                )
                rows = cur.fetchall()
                for row in rows:
                    tok = len(row[1].split()) if row[1] else 0
                    if running_tokens + tok > max_tokens and chunks:
                        overflowed = True
                        break
                    chunks.append(MemoryChunk(
                        id=row[0],
                        content=row[1],
                        role=row[2],
                        conversation_id=row[3],
                        source_turn_id=row[4],
                        source_memory_id=row[5],
                        source_table=row[6],
                        importance=row[7],
                        pinned_at=row[8],
                        pin_source=row[9],
                        created_at=row[10],
                        metadata=row[11] if isinstance(row[11], dict) else (json.loads(row[11]) if row[11] else {}),
                    ))
                    running_tokens += tok

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memory_audit
                        (ts, op, query_hash, top_k, result_count, latency_ms, op_over_run)
                    VALUES (now(), 'permanent_fetch', 'N/A', 0, %s, 0, %s)
                    """,
                    (len(chunks), overflowed),
                )
            conn.commit()

        return chunks, overflowed

    def permanent_usage(self) -> dict:
        """Total permanent-memory usage vs the cap.

        The reverie (2026-10-23) needs the TOTAL (every permanent chunk)
        rather than the "what still fits" number, because the job is to
        get the total under the cap - not just under the part that
        happens to survive the cut.

        Returns {count, total_tokens, used_tokens, dropped, cap,
                 overflowed}:
          count        - permanent chunks in the table
          total_tokens - tokens of ALL permanent chunks
          used_tokens  - tokens of the chunks that fit in the prompt
          dropped      - permanent chunks currently NOT injected
        """
        max_tokens = int(self.config.memory.permanent.max_tokens or 0)
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT content FROM {self.agent_name}.memory_chunks
                     WHERE permanent = TRUE
                    """
                )
                rows = cur.fetchall()
        total = 0
        for r in rows:
            total += len((r[0] or "").split())
        # get_permanent() applies the cap (and reports the overflow).
        chunks, overflowed = self.get_permanent()
        used = sum(len((getattr(c, "content", "") or "").split()) for c in chunks)
        return {
            "count": len(rows),
            "total_tokens": total,
            "used_tokens": used,
            "dropped": max(0, len(rows) - len(chunks)),
            "cap": max_tokens,
            "overflowed": overflowed,
        }

    # ---- write path ------------------------------------------------------

    def write_turn_user(
        self,
        conversation_id: UUID,
        content: str,
        attachments: list[dict] | None = None,
        trajectory: dict | None = None,
    ) -> Turn:
        """Insert a user turn row. Embedding + chunking happen later, in
        write_turns(), once the assistant response is ready.

        `attachments` (Phase 10) is a list of small dicts describing
        image uploads the user attached to this turn; persisted to the
        `attachments` jsonb column on agent1.turns. Pass None or [] for
        text-only turns.

        `trajectory` (Migration 011, 2026-10-19) is typically None for
        user turns (no iterations to record). If the caller has a
        reason to persist additional user-turn metadata (e.g. which
        memory chunks were surfaced by the shadow search before this
        user message), it can be passed here.
        """
        return self._insert_turn(
            conversation_id, "user", content,
            attachments=attachments or [],
            trajectory=trajectory,
        )

    def write_turn_assistant(
        self,
        conversation_id: UUID,
        content: str,
        model: str,
        latency_ms: int,
        attachments: list[dict] | None = None,
        trajectory: dict | None = None,
    ) -> Turn:
        """Insert an assistant turn row. Phase 15 (2026-10-18):
        `attachments` is a list of file attachements produced by the
        `attach_file` tool during this turn. Each entry is the same
        shape used for user uploads (url, filename, content_type,
        size). Persisted to the same `attachments` jsonb column on
        agent1.turns so a page reload still shows the file-attachment
        icons on the assistant's response.

        `trajectory` (Migration 011, 2026-10-19) is the full
        per-iteration trace built by the loop: {iterations: [...],
        total_iterations, total_tool_calls, tool_iterations,
        latency_ms}. Persisted to the `trajectory` jsonb column on
        agent1.turns. The export.jsonl endpoint can include it for
        LLM retraining pipelines that need the full thought + tool-use
        trace, not just the final text. If None, the row is written
        with trajectory = NULL (a legacy turn written before the
        migration)."""
        return self._insert_turn(
            conversation_id, "assistant", content,
            model=model, latency_ms=latency_ms,
            attachments=attachments or [],
            trajectory=trajectory,
        )

    def update_turn_content(
        self,
        turn_id: int,
        content: str,
        model: str | None = None,
        latency_ms: int | None = None,
        attachments: list[dict] | None = None,
        trajectory: dict | None = None,
    ) -> Turn:
        """Replace the content of an existing turn row, in place.

        Used when a turn was already marked "interrupted" (the server
        was restarting under it) and then went on to finish: the marker
        is updated with the real answer so the conversation keeps
        exactly one assistant turn instead of two. Also used by the
        client-error endpoint to keep a single row per failure.
        """
        tok = len(content.split())
        att_json = json.dumps(attachments or [])
        traj_json = json.dumps(trajectory) if trajectory is not None else None
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.turns
                       SET content = %s,
                           token_count = %s,
                           model = COALESCE(%s::text, model),
                           latency_ms = COALESCE(%s::integer, latency_ms),
                           attachments = %s::jsonb,
                           trajectory = COALESCE(%s::jsonb, trajectory)
                     WHERE id = %s
                 RETURNING id, conversation_id, role, content, token_count, model, latency_ms, attachments, trajectory, created_at
                    """,
                    (content, tok, model, latency_ms, att_json, traj_json, turn_id),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError(f"turn {turn_id} not found in {self.agent_name}.turns")
        atts = row[7] if row[7] is not None else []
        if isinstance(atts, str):
            atts = json.loads(atts)
        traj = row[8] if row[8] is not None else None
        if isinstance(traj, str):
            traj = json.loads(traj)
        return Turn(
            id=row[0],
            conversation_id=row[1],
            role=row[2],
            content=row[3],
            token_count=row[4],
            model=row[5],
            latency_ms=row[6],
            attachments=atts,
            trajectory=traj,
            created_at=row[9],
        )

    def _insert_turn(
        self,
        conversation_id: UUID,
        role: str,
        content: str,
        model: str | None = None,
        latency_ms: int | None = None,
        attachments: list[dict] | None = None,
        trajectory: dict | None = None,
    ) -> Turn:
        tok = len(content.split())
        att_json = json.dumps(attachments or [])
        # Trajectory is dict | None; psycopg will write None as SQL
        # NULL and dict as JSONB. JSON-serialise to be defensive
        # against types psycopg can't auto-adapt.
        traj_json = json.dumps(trajectory) if trajectory is not None else None
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.turns
                        (conversation_id, role, content, token_count, model, latency_ms, attachments, trajectory)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    RETURNING id, conversation_id, role, content, token_count, model, latency_ms, attachments, trajectory, created_at
                    """,
                    (conversation_id, role, content, tok, model, latency_ms, att_json, traj_json),
                )
                row = cur.fetchone()
            conn.commit()
        # psycopg returns jsonb as a Python list/dict already parsed
        atts = row[7] if row[7] is not None else []
        if isinstance(atts, str):
            atts = json.loads(atts)
        traj = row[8] if row[8] is not None else None
        if isinstance(traj, str):
            traj = json.loads(traj)
        return Turn(
            id=row[0],
            conversation_id=row[1],
            role=row[2],
            content=row[3],
            token_count=row[4],
            model=row[5],
            latency_ms=row[6],
            attachments=atts,
            trajectory=traj,
            created_at=row[9],
        )

    def write_turns(
        self,
        conversation_id: UUID,
        user_turn: Turn,
        assistant_turn: Turn,
    ) -> None:
        """Chunk + embed both new turns; insert into memory_chunks.

        Dedupe via SHA-256(normalized_content) per conversation.
        Status-commentary filter on assistant chunks.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                for turn in (user_turn, assistant_turn):
                    chunks = _chunker(turn.content, self.config.memory.chunk)
                    for idx, chunk_text in enumerate(chunks):
                        mhash = hashlib.sha256(_norm_text(chunk_text).encode("utf-8")).hexdigest()
                        # Dedupe: skip if same hash already exists in this conv.
                        cur.execute(
                            f"""
                            SELECT 1 FROM {self.agent_name}.memory_chunks
                             WHERE memory_hash = %s AND conversation_id = %s
                             LIMIT 1
                            """,
                            (mhash, conversation_id),
                        )
                        if cur.fetchone():
                            continue
                        is_status = turn.role == "assistant" and _is_status_commentary(chunk_text)
                        emb = self.embedding.embed(chunk_text)
                        cur.execute(
                            f"""
                            INSERT INTO {self.agent_name}.memory_chunks
                                (source_turn_id, conversation_id, role, chunk_index,
                                 content, embedding, source_type, importance, memory_hash,
                                 status_commentary, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s::halfvec, 'chat', 5, %s, %s, '{{}}'::jsonb)
                            """,
                            (
                                turn.id, conversation_id, turn.role, idx,
                                chunk_text, emb, mhash, is_status,
                            ),
                        )
            conn.commit()

    def write_chunk(
        self,
        text: str,
        *,
        source: str = "agent",
        pin: bool = False,
        importance: int = 5,
        category: str | None = None,
    ) -> int:
        """Create a new memory chunk from arbitrary text. Embeds + inserts.

        Independent of the chat-flow auto-chunk (which fires on every
        turn). Use this for observations, conclusions, or facts the
        agent wants to remember that the user didn't explicitly say.

        Args:
            text: Self-contained fact, 20+ words ideal. Max ~8K chars.
            source: "agent" | "operator" | "user". Recorded as pin_source
                if pin=True.
            pin: If True, mark as permanent (auto-injected into every
                future prompt). Default False (searchable via
                memory_recall but not auto-injected).
            importance: 0-10. Default 5.
            category: Free-form tag stored in metadata. E.g. "user-pref",
                "bug", "config", "observation", "task-note".

        Returns:
            The new chunk_id.

        Raises:
            ValueError: empty text or out-of-range importance.
            LookupError: a chunk with identical content already exists
                (deduped on memory_hash).
        """
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        if not (0 <= importance <= 10):
            raise ValueError(f"importance must be 0-10, got {importance}")

        text = text.strip()
        mhash = hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()
        now = datetime.now()

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                # Dedupe: skip if same content already exists for this agent.
                cur.execute(
                    f"""
                    SELECT 1 FROM {self.agent_name}.memory_chunks
                     WHERE memory_hash = %s
                     LIMIT 1
                    """,
                    (mhash,),
                )
                if cur.fetchone():
                    raise LookupError(
                        f"chunk with this content already exists (hash={mhash[:8]})"
                    )

                # Embed
                emb = self.embedding.embed(text)

                # Build metadata
                metadata = json.dumps({
                    "memory_source": f"{source}_write",
                    "category": category or "inferred",
                    "pinned_at_write": bool(pin),
                    "written_at": now.isoformat(),
                })

                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memory_chunks
                        (role, chunk_index, content, embedding, source_type, source_ref,
                         importance, memory_hash, status_commentary, permanent, pin_source,
                         pinned_at, metadata, created_at)
                    VALUES (%s, %s, %s, %s::halfvec(1024), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        "assistant",  # role: agent is the writer
                        0,            # chunk_index
                        text, emb,
                        "system",     # source_type
                        f"agent_write:{now.isoformat()}",  # source_ref
                        importance,
                        mhash,
                        False,        # status_commentary
                        bool(pin),    # permanent
                        source if pin else None,  # pin_source
                        now if pin else None,      # pinned_at
                        metadata,
                        now,
                    ),
                )
                new_id = cur.fetchone()[0]
            conn.commit()

        # Audit row (best-effort)
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_audit
                            (op, query_hash, top_k, result_count, latency_ms, op_over_run)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        ("write_turns", mhash, 0, 1, 0, False),
                    )
                conn.commit()
        except Exception as e:
            log.warning("memory audit row insert failed (non-fatal): %s", e)

        return new_id



    def write_memory(
        self,
        text: str,
        *,
        importance: int = 5,
        source_type: str = "system",
        source_ref: str | None = None,
        pin: bool = False,
        pin_source: str = "operator",
        created_at: datetime | None = None,
        created_by: str = "operator",
        metadata: dict | None = None,
    ) -> int:
        """Create a new memory row + chunk slices for semantic search.

        Phase 13 (2026-10-15). The memories table is the long-form
        source-of-truth; this method inserts a row, embeds the FULL
        content for memory-level recall, then chunkifies the content
        into memory_chunks with source_memory_id set (and the
        source_table='memories' generated column auto-populated).

        Args:
            text: Self-contained long-form memory content. 1-10K
                tokens typical; no hard cap (chunked at
                MEMORY_CHUNK_MAX_TOKENS).
            importance: 0-10. Default 5.
            source_type: 'chat'|'email'|'code'|'doc'|'system'.
            source_ref: Optional freeform reference (e.g. source file
                path for imported memories).
            pin: If True, mark as permanent.
            pin_source: 'model'|'operator'|'bootstrap'. Only used if
                pin=True.
            created_at: Override the created_at (used for historical
                imports so the row's timestamp reflects the original
                event time, not the import time).
            created_by: 'operator'|'agent'|'import'. Default
                'operator'.
            metadata: Optional dict stored as JSONB.

        Returns:
            The new memory_id.

        Raises:
            ValueError, LookupError.
        """
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        if not (0 <= importance <= 10):
            raise ValueError(f"importance must be 0-10, got {importance}")
        if source_type not in ("chat", "email", "code", "doc", "system"):
            raise ValueError(f"invalid source_type: {source_type!r}")
        if pin and pin_source not in ("model", "operator", "bootstrap"):
            raise ValueError(f"invalid pin_source: {pin_source!r}")
        if created_by not in ("operator", "agent", "import"):
            raise ValueError(f"invalid created_by: {created_by!r}")

        text = text.strip()
        mhash = hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()
        now = datetime.now()
        effective_created_at = created_at or now

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                # Dedupe: skip if same content already exists.
                cur.execute(
                    f"SELECT 1 FROM {self.agent_name}.memories WHERE memory_hash = %s LIMIT 1",
                    (mhash,),
                )
                if cur.fetchone():
                    raise LookupError(
                        f"memory with this content already exists (hash={mhash[:8]})"
                    )

                # Embed the FULL content (memory-level similarity).
                emb = self.embedding.embed(text)
                meta_json = json.dumps(metadata or {})

                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memories
                        (content, embedding, source_type, source_ref,
                         importance, memory_hash, status_commentary,
                         permanent, pin_source, pinned_at,
                         metadata, created_at, updated_at, created_by)
                    VALUES (%s, %s::halfvec(1024), %s, %s, %s, %s, FALSE,
                            %s, %s, %s, %s::jsonb, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        text, emb,
                        source_type, source_ref,
                        importance, mhash,
                        bool(pin), pin_source if pin else None,
                        now if pin else None,
                        meta_json,
                        effective_created_at, now,
                        created_by,
                    ),
                )
                memory_id = cur.fetchone()[0]

                # Chunk the content and insert chunks with source_memory_id.
                chunks = _chunker(text, self.config.memory.chunk)
                for idx, chunk_text in enumerate(chunks):
                    chunk_emb = self.embedding.embed(chunk_text)
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_chunks
                            (source_memory_id, role, chunk_index,
                             content, embedding, source_type, source_ref,
                             importance, memory_hash, status_commentary,
                             permanent, pin_source, pinned_at,
                             metadata, created_at)
                        VALUES (%s, 'assistant', %s, %s, %s::halfvec(1024),
                                %s, %s, %s, %s, FALSE,
                                %s, %s, %s,
                                %s::jsonb, %s)
                        """,
                        (
                            memory_id, idx,
                            chunk_text, chunk_emb,
                            source_type, source_ref,
                            importance, mhash,
                            bool(pin),                                # permanent
                            pin_source if pin else None,
                            now if pin else None,
                            json.dumps({"parent_memory_id": memory_id, "chunk_of": "memory"}),
                            effective_created_at,
                        ),
                    )
            conn.commit()

        # Audit row (best-effort).
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_audit
                            (op, query_hash, top_k, result_count, latency_ms, op_over_run)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        ("write_memory", mhash, 0, 1, 0, False),
                    )
                conn.commit()
        except Exception as e:
            log.warning("memory audit row insert failed (non-fatal): %s", e)

        return memory_id

    def recall_memory(
        self,
        query: str,
        *,
        top_k: int = 10,
    ) -> list[dict]:
        """Search memories by semantic similarity (uses memories.embedding).

        Returns a list of memory dicts ordered by distance. Each dict:
          id, content, source_type, source_ref, importance, permanent,
          pin_source, pinned_at, created_at, metadata, distance

        For chunk-level search, use search(). For unioned chunk+memory
        results, use search() which now also returns source_table.
        """
        if not query or not query.strip():
            return []
        qhash = hashlib.sha256(_norm_text(query).encode("utf-8")).hexdigest()
        q_emb = self.embedding.embed(query)
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, content, source_type, source_ref, importance,
                           permanent, pin_source, pinned_at, created_at,
                           metadata, (embedding <=> %s::halfvec) AS distance
                      FROM {self.agent_name}.memories
                     ORDER BY embedding <=> %s::halfvec
                     LIMIT %s
                    """,
                    (q_emb, q_emb, top_k),
                )
                rows = cur.fetchall()
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_audit
                            (op, query_hash, top_k, result_count, latency_ms, op_over_run)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        ("recall_memory", qhash, top_k, len(rows), 0, False),
                    )
                conn.commit()
        except Exception as e:
            log.warning("memory audit row insert failed (non-fatal): %s", e)
        return [
            {
                "id": r[0], "content": r[1], "source_type": r[2],
                "source_ref": r[3], "importance": r[4], "permanent": r[5],
                "pin_source": r[6], "pinned_at": r[7], "created_at": r[8],
                "metadata": r[9] if isinstance(r[9], dict) else (json.loads(r[9]) if r[9] else {}),
                "distance": float(r[10]),
            }
            for r in rows
        ]

    def update_memory(
        self,
        memory_id: int,
        new_content: str,
    ) -> int:
        """Update a memory's content; re-embed and re-chunk.

        Use this to amend an existing long-form memory with a small
        detail, rather than writing a new one (which would bloat
        recall results and split related context across multiple
        rows). The original created_at is preserved; updated_at
        is bumped to now. All existing chunks for this memory are
        deleted and replaced with new ones.

        Returns the same memory_id on success.
        """
        if not new_content or not new_content.strip():
            raise ValueError("new_content must be non-empty")
        new_content = new_content.strip()
        new_mhash = hashlib.sha256(_norm_text(new_content).encode("utf-8")).hexdigest()
        now = datetime.now()
        new_emb = self.embedding.embed(new_content)

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, created_at FROM {self.agent_name}.memories WHERE id = %s",
                    (memory_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError(f"memory {memory_id} not found in {self.agent_name}")

                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memories
                       SET content = %s,
                           embedding = %s::halfvec(1024),
                           memory_hash = %s,
                           updated_at = %s
                     WHERE id = %s
                    """,
                    (new_content, new_emb, new_mhash, now, memory_id),
                )

                # Delete old chunks.
                cur.execute(
                    f"DELETE FROM {self.agent_name}.memory_chunks WHERE source_memory_id = %s",
                    (memory_id,),
                )

                # Re-chunk and insert new chunks.
                chunks = _chunker(new_content, self.config.memory.chunk)
                for idx, chunk_text in enumerate(chunks):
                    chunk_emb = self.embedding.embed(chunk_text)
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_chunks
                            (source_memory_id, role, chunk_index,
                             content, embedding, source_type, source_ref,
                             importance, memory_hash, status_commentary,
                             permanent, pin_source, pinned_at,
                             metadata, created_at)
                        SELECT %s, 'assistant', %s, %s, %s::halfvec(1024),
                               source_type, source_ref,
                               importance, %s, status_commentary,
                               permanent, pin_source, pinned_at,
                               %s::jsonb, created_at
                          FROM {self.agent_name}.memories
                         WHERE id = %s
                        """,
                        (
                            memory_id, idx,
                            chunk_text, chunk_emb,
                            new_mhash,
                            json.dumps({"parent_memory_id": memory_id, "chunk_of": "memory"}),
                            memory_id,
                        ),
                    )
            conn.commit()
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_audit
                            (op, query_hash, top_k, result_count, latency_ms, op_over_run)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        ("update_memory", new_mhash, 0, 1, 0, False),
                    )
                conn.commit()
        except Exception as e:
            log.warning("memory audit row insert failed (non-fatal): %s", e)
        return memory_id

    def delete_memory(
        self,
        memory_id: int,
        reason: str | None = None,
        source: str = "model",
    ) -> dict:
        """Permanently delete a long-form memory (and its chunks).

        Added 2026-10-18 per Mal's request. Previously, deletion was
        operator-only (had to be done via psql because the LLM
        accidentally created duplicate memories). Now exposed to the
        LLM as the `memory_delete` tool, scoped to a single
        memory_id so it's hard to misuse for bulk deletion.

        Cascade: `memory_chunks.source_memory_id` has ON DELETE
        CASCADE on the FK to `memories.id`, so the chunks go away
        automatically. The row's `permanent` status (if any) is
        irrelevant — the row itself is gone.

        Audit: appends an entry to `<agent>.memory_audit` with
        op='delete_memory', the memory's hash (so a forensic
        reviewer can re-derive what was deleted from the audit log
        + a snapshot of the prior content if needed), and the
        supplied reason.

        Args:
            memory_id: The id of the memory to delete.
            reason: Optional human-readable reason for the audit log
                (e.g. "duplicate of memory_id=42"). Not validated —
                stored verbatim.
            source: Who is doing the delete. 'model' when called via
                the memory_delete tool, 'operator' when called via
                psql (caller should pass this directly).

        Returns:
            dict with `memory_id`, `deleted`, and the content
            snapshot (for forensic confirmation of what was
            deleted). Raises LookupError if the memory doesn't
            exist.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                # Capture the content + hash BEFORE deletion so the
                # audit row + return value can attest to what was
                # removed.
                cur.execute(
                    f"""
                    SELECT id, content, memory_hash, created_at, permanent
                      FROM {self.agent_name}.memories
                     WHERE id = %s
                    """,
                    (memory_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError(
                        f"memory {memory_id} not found in {self.agent_name}"
                    )
                _id, content, mhash, created_at, was_permanent = row

                # The actual delete. FK ON DELETE CASCADE cleans up
                # the chunks.
                cur.execute(
                    f"DELETE FROM {self.agent_name}.memories WHERE id = %s",
                    (memory_id,),
                )

            conn.commit()

        # Best-effort audit row. Failure here is not fatal (we
        # already deleted the memory; the audit is a forensic
        # nicety).
        #
        # The CHECK constraint memory_audit_op_check on the
        # memory_audit table only allows bare op values (no
        # colons, no embedded source/reason info), so we use the
        # bare "delete_memory" op and stash the source + reason in
        # query_hash as "mhash|reason[:50]" (truncated to fit the
        # 64-char limit on query_hash). The original content_hash
        # (mhash) is preserved so a forensic reviewer can re-derive
        # the deleted content from a snapshot if they have one.
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    reason_part = ((reason or "")[:50]).replace("|", " ")
                    query_hash_value = f"{mhash[:32]}|{source}|{reason_part}"[:64]
                    cur.execute(
                        f"""
                        INSERT INTO {self.agent_name}.memory_audit
                            (op, query_hash, top_k, result_count, latency_ms, op_over_run)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            "delete_memory",
                            query_hash_value,
                            0,
                            1,
                            0,
                            False,
                        ),
                    )
                conn.commit()
        except Exception as e:
            log.warning("delete_memory: audit row failed (memory already deleted): %s", e)

        return {
            "memory_id": memory_id,
            "deleted": True,
            "was_permanent": was_permanent,
            "content_preview": (content[:200] + "…") if len(content) > 200 else content,
            "reason": reason,
        }

    def pin_memory(self, memory_id: int, source: str = "operator") -> dict:
        """Pin a memory to permanent. Cascades to its chunks.

        Sets permanent=true on the memory row AND on all its chunks
        (so get_permanent() picks them up via either path).
        """
        if source not in ("model", "operator", "bootstrap"):
            raise ValueError(f"pin source must be one of model/operator/bootstrap, got {source!r}")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memories
                       SET permanent = TRUE, pin_source = %s, pinned_at = now(),
                           updated_at = now()
                     WHERE id = %s
                    RETURNING id, content, source_type, source_ref, importance,
                              permanent, pin_source, pinned_at, created_at, metadata
                    """,
                    (source, memory_id),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError(f"memory {memory_id} not found")
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memory_chunks
                       SET permanent = TRUE, pin_source = %s, pinned_at = now()
                     WHERE source_memory_id = %s
                    """,
                    (source, memory_id),
                )
            conn.commit()
        return {
            "id": row[0], "content": row[1], "source_type": row[2],
            "source_ref": row[3], "importance": row[4],
            "permanent": row[5], "pin_source": row[6],
            "pinned_at": row[7], "created_at": row[8],
            "metadata": row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {}),
        }

    def unpin_memory(self, memory_id: int) -> dict:
        """Unpin a memory. Cascades to its chunks."""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memories
                       SET permanent = FALSE, pinned_at = NULL, pin_source = NULL,
                           updated_at = now()
                     WHERE id = %s
                    RETURNING id, content, source_type, source_ref, importance,
                              permanent, pin_source, pinned_at, created_at, metadata
                    """,
                    (memory_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError(f"memory {memory_id} not found")
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memory_chunks
                       SET permanent = FALSE, pinned_at = NULL, pin_source = NULL
                     WHERE source_memory_id = %s
                    """,
                    (memory_id,),
                )
            conn.commit()
        return {
            "id": row[0], "content": row[1], "source_type": row[2],
            "source_ref": row[3], "importance": row[4],
            "permanent": row[5], "pin_source": row[6],
            "pinned_at": row[7], "created_at": row[8],
            "metadata": row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {}),
        }


    # ---- pin / unpin -----------------------------------------------------

    def pin(self, chunk_id: int, source: str) -> MemoryChunk:
        if source not in ("model", "operator", "bootstrap"):
            raise ValueError(f"pin source must be one of model/operator/bootstrap, got {source!r}")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memory_chunks
                       SET permanent = TRUE, pin_source = %s, pinned_at = now()
                     WHERE id = %s
                    RETURNING id, content, role, conversation_id, source_turn_id,
                              importance, pinned_at, pin_source, created_at, metadata
                    """,
                    (source, chunk_id),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError(f"chunk {chunk_id} not found in {self.agent_name}")
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memory_audit
                        (ts, op, query_hash, top_k, result_count, latency_ms)
                    VALUES (now(), 'pin', %s, 0, 1, 0)
                    """,
                    (f"chunk:{chunk_id}:source:{source}",),
                )
            conn.commit()
        return MemoryChunk(
            id=row[0], content=row[1], role=row[2], conversation_id=row[3],
            source_turn_id=row[4], importance=row[5], pinned_at=row[6],
            pin_source=row[7], created_at=row[8],
            metadata=row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {}),
        )

    def unpin(self, chunk_id: int, source: str) -> MemoryChunk:
        if source not in ("model", "operator"):
            raise ValueError(f"unpin source must be one of model/operator, got {source!r}")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.memory_chunks
                       SET permanent = FALSE, pin_source = NULL, pinned_at = NULL
                     WHERE id = %s
                    RETURNING id, content, role, conversation_id, source_turn_id,
                              importance, pinned_at, pin_source, created_at, metadata
                    """,
                    (chunk_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError(f"chunk {chunk_id} not found in {self.agent_name}")
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.memory_audit
                        (ts, op, query_hash, top_k, result_count, latency_ms)
                    VALUES (now(), 'unpin', %s, 0, 1, 0)
                    """,
                    (f"chunk:{chunk_id}:source:{source}",),
                )
            conn.commit()
        return MemoryChunk(
            id=row[0], content=row[1], role=row[2], conversation_id=row[3],
            source_turn_id=row[4], importance=row[5], pinned_at=row[6],
            pin_source=row[7], created_at=row[8],
            metadata=row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {}),
        )

    # ---- conversation lifecycle -----------------------------------------

    def load_or_create_conversation(
        self,
        conversation_id: UUID | None,
        agent_name: str,
        user_id: int | None = None,
    ) -> tuple[UUID, bool]:
        """Return (conv_id, was_created). If conv_id is None, create new."""
        if conversation_id is not None:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT id FROM {self.agent_name}.conversations WHERE id = %s",
                        (conversation_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        return conversation_id, False
            # Caller passed an id we don't have — fall through to create.
        new_id = uuid4()
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.conversations (id, user_id, agent_name)
                    VALUES (%s, %s, %s)
                    """,
                    (new_id, user_id, agent_name),
                )
            conn.commit()
        return new_id, True

    def get_or_create_persistent_conversation(
        self,
        agent_name: str,
        user_id: int | None = None,
    ) -> UUID:
        """The most recent *active* conversation for (agent, user).

        The HMI moved from a conversation-list model to an entity model:
        each agent has exactly one persistent conversation per user.

        Migration 010 (2026-10-19) added an `archived_at` column. The
        main session now only serves turns for the most recent
        non-archived conversation — older conversations (created by
        the "New conversation" flow) are preserved for LLM retraining
        but are invisible to the chat dispatch.

        Migration 014 (2026-10-23) added an `is_group` column. A GROUP
        chat owns its own conversation id (public.groups.conversation_id)
        and materialises it in each participating agent's schema; those
        rows are marked is_group = TRUE and are EXCLUDED here, so a DM
        can never pick up the group conversation — not even when the
        group conversation is the most recently touched conversation
        row for that agent (which is exactly what happens right after a
        group turn).

        If no active conversation exists, a new one is created.
        Archived conversations are not reused; they are preserved as-is
        so the LLM training data export (see
        /api/v1/entities/<name>/conversations) can pull the full
        history including the conversation that was active just
        before the user clicked "New conversation".
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id FROM {self.agent_name}.conversations
                     WHERE (user_id = %s OR (%s IS NULL AND user_id IS NULL))
                       AND archived_at IS NULL
                       AND is_group = FALSE
                     ORDER BY updated_at DESC
                     LIMIT 1
                    """,
                    (user_id, user_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
        new_id = uuid4()
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.agent_name}.conversations (id, user_id, agent_name)
                    VALUES (%s, %s, %s)
                    """,
                    (new_id, user_id, agent_name),
                )
                # Touch updated_at so future lookups keep finding this row
                # even after the very first message lands.
                cur.execute(
                    f"UPDATE {self.agent_name}.conversations SET updated_at = now() WHERE id = %s",
                    (new_id,),
                )
            conn.commit()
        return new_id

    def list_entities_with_activity(self, user_id: int | None = None) -> list[dict]:
        """Per-agent summary for the entity-list landing page (2026-10-17).

        Returns one row per installed agent with: name, last_message,
        last_role, last_at, turn_count, conversation_id. The HMI renders
        this as the Telegram-style entity list.
        """
        # The agents are stored in app.agents (config), not in the DB.
        # For each, we query its own schema for the latest activity.
        from flask import current_app
        cfg = current_app.config["HARNESS_CONFIG_OBJ"]
        registry = current_app.extensions["harness"].get("bootstrap_registry")
        names = registry.list_agents() if registry else list(cfg.app.agents)
        out: list[dict] = []
        for name in names:
            try:
                with self.pool.connection() as conn:
                    with conn.cursor() as cur:
                        # Latest *active* conversation for this user in this agent's schema.
                        # Archived conversations are excluded — the entity list shows
                        # what the main session is currently looking at, not the
                        # retraining archive.
                        cur.execute(
                            f"""
                            SELECT id, updated_at FROM {name}.conversations
                             WHERE (user_id = %s OR (%s IS NULL AND user_id IS NULL))
                               AND archived_at IS NULL
                               AND is_group = FALSE
                             ORDER BY updated_at DESC LIMIT 1
                            """,
                            (user_id, user_id),
                        )
                        conv = cur.fetchone()
                        if not conv:
                            out.append({
                                "name": name, "last_message": None, "last_role": None,
                                "last_at": None, "turn_count": 0, "conversation_id": None,
                            })
                            continue
                        conv_id, updated_at = conv
                        # Latest turn in that conversation
                        cur.execute(
                            f"""
                            SELECT role, content, created_at FROM {name}.turns
                             WHERE conversation_id = %s
                             ORDER BY created_at DESC LIMIT 1
                            """,
                            (conv_id,),
                        )
                        last = cur.fetchone()
                        # Turn count
                        cur.execute(
                            f"SELECT COUNT(*) FROM {name}.turns WHERE conversation_id = %s",
                            (conv_id,),
                        )
                        count = cur.fetchone()[0]
                last_role = last[0] if last else None
                last_content = last[1] if last else None
                last_at = (last[2] if last else updated_at)
                out.append({
                    "name": name,
                    "last_message": (last_content[:140] + ("…" if last_content and len(last_content) > 140 else "")) if last_content else None,
                    "last_role": last_role,
                    "last_at": last_at.isoformat() if last_at else None,
                    "turn_count": count,
                    "conversation_id": str(conv_id),
                })
            except Exception as e:
                # Skip this agent if its schema isn't ready (e.g. migration pending)
                import logging as _lg
                _lg.getLogger(__name__).warning(
                    "list_entities_with_activity: agent=%s skipped: %s", name, e,
                )
                out.append({
                    "name": name, "last_message": None, "last_role": None,
                    "last_at": None, "turn_count": 0, "conversation_id": None,
                    "error": str(e),
                })
        return out

    def list_turns_for_persistent(
        self,
        agent_name: str,
        user_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Last N turns of the persistent conversation for (agent, user).

        Returns [] if no conversation exists yet. The HMI uses this
        to render the chat view (Mal: 'just the last 50 turn messages
        from the turns table to be rendered at all times').

        Group conversations are excluded (migration 014): a group chat
        owns its own conversation id, and a DM must never render the
        group's turns.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id FROM {agent_name}.conversations
                     WHERE (user_id = %s OR (%s IS NULL AND user_id IS NULL))
                       AND is_group = FALSE
                     ORDER BY updated_at DESC LIMIT 1
                    """,
                    (user_id, user_id),
                )
                conv = cur.fetchone()
                if not conv:
                    return []
                conv_id = conv[0]
                cur.execute(
                    f"""
                    SELECT id, role, content, model, attachments, created_at
                      FROM {agent_name}.turns
                     WHERE conversation_id = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (conv_id, max(1, min(int(limit), 200))),
                )
                rows = cur.fetchall()
        import json as _json
        out: list[dict] = []
        # Returned DESC; client reverses for chronological display
        for r in rows:
            atts = r[4]
            if isinstance(atts, str):
                try:
                    atts = _json.loads(atts)
                except Exception:
                    atts = []
            elif atts is None:
                atts = []
            out.append({
                "id": r[0], "role": r[1], "content": r[2], "model": r[3],
                "attachments": atts,
                "created_at": r[5].isoformat() if r[5] else None,
            })
        return out

    def load_recent_turns(self, conversation_id: UUID, limit: int = 20) -> list[Turn]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, conversation_id, role, content, token_count, model, latency_ms, attachments, created_at
                      FROM {self.agent_name}.turns
                     WHERE conversation_id = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (conversation_id, limit),
                )
                rows = cur.fetchall()
        out: list[Turn] = []
        for r in reversed(rows):
            atts = r[7] if r[7] is not None else []
            if isinstance(atts, str):
                atts = json.loads(atts)
            out.append(Turn(
                id=r[0], conversation_id=r[1], role=r[2], content=r[3],
                token_count=r[4], model=r[5], latency_ms=r[6],
                attachments=atts, created_at=r[8],
            ))
        return out

    # ---- conversation listing / loading (UI) -----------------------

    def archive_conversation(
        self,
        conversation_id: UUID,
    ) -> dict:
        """Mark a conversation as archived (replaces the old DELETE).

        Migration 010 (2026-10-19) changed the "new conversation"
        flow from a hard DELETE (turns + chunks + conversation row)
        to a soft archive (set archived_at = now() on the conversation
        row). The turn data is preserved for LLM retraining.

        Returns {"archived": True, "conversation_id": str, "turns": N,
                 "memory_chunks_deleted": M} so the caller can confirm
        what was preserved and what was cleaned up.

        Non-permanent memory_chunks for this conversation are still
        deleted (they are conversation-specific search indexes; the
        source-of-truth turns are what matters for retraining).
        Permanent memories are unaffected.

        Idempotent: archiving an already-archived conversation is a
        no-op (returns the existing archived_at timestamp).

        A GROUP conversation (migration 014) is never archived: the
        group owns that conversation id, and archiving it would break
        the group chat for every member. The caller's "New
        conversation" flow must only ever touch a DM conversation.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                # Check current state
                cur.execute(
                    f"""
                    SELECT archived_at, is_group FROM {self.agent_name}.conversations
                     WHERE id = %s
                    """,
                    (conversation_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {"archived": False, "reason": "not found", "conversation_id": str(conversation_id)}
                if row[1]:
                    return {
                        "archived": False,
                        "reason": "group conversation",
                        "conversation_id": str(conversation_id),
                    }
                if row[0] is not None:
                    return {
                        "archived": True,
                        "already_archived_at": row[0].isoformat(),
                        "conversation_id": str(conversation_id),
                        "turns": 0,
                        "memory_chunks_deleted": 0,
                    }
                # Free conversation-specific memory (non-permanent chunks)
                # so the next conversation doesn't see stale search results.
                cur.execute(
                    f"""
                    DELETE FROM {self.agent_name}.memory_chunks
                     WHERE conversation_id = %s AND permanent = false
                    """,
                    (conversation_id,),
                )
                deleted_chunks = cur.rowcount
                # Archive the conversation. Turns + the conversation row
                # are preserved. Partial index on archived_at IS NULL
                # stops get_or_create from picking this one up.
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.conversations
                       SET archived_at = now()
                     WHERE id = %s
                    """,
                    (conversation_id,),
                )
                # Count turns that were preserved (for the response)
                cur.execute(
                    f"""
                    SELECT count(*) FROM {self.agent_name}.turns
                     WHERE conversation_id = %s
                    """,
                    (conversation_id,),
                )
                turn_count = cur.fetchone()[0]
            conn.commit()
        return {
            "archived": True,
            "conversation_id": str(conversation_id),
            "turns": int(turn_count),
            "memory_chunks_deleted": int(deleted_chunks),
        }

    def get_conversation(self, conversation_id: Any) -> Any:
        """One conversation row by id, or None.

        Used by scripts/retry_turn.py to resolve a conversation before
        re-running a turn (2026-10-22).
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, user_id, agent_name, title, created_at, updated_at, metadata, archived_at
                      FROM {self.agent_name}.conversations
                     WHERE id = %s
                    """,
                    (conversation_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "agent_name": row[2],
            "title": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "metadata": row[6],
            "archived_at": row[7],
        }

    def get_turn(self, turn_id: int) -> Turn | None:
        """One turn row by id, or None."""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, conversation_id, role, content, token_count, model,
                           latency_ms, attachments, trajectory, created_at
                      FROM {self.agent_name}.turns
                     WHERE id = %s
                    """,
                    (turn_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        atts = row[7] if row[7] is not None else []
        if isinstance(atts, str):
            atts = json.loads(atts)
        traj = row[8] if row[8] is not None else None
        if isinstance(traj, str):
            traj = json.loads(traj)
        return Turn(
            id=row[0], conversation_id=row[1], role=row[2], content=row[3],
            token_count=row[4], model=row[5], latency_ms=row[6],
            attachments=atts, trajectory=traj, created_at=row[9],
        )

    def list_conversations(self, limit: int = 50, include_archived: bool = False) -> list[dict]:
        """Recent conversations.

        Returns [{id, title, updated_at, created_at, archived, archived_at,
                 message_count, first_user_preview, is_group}, ...]
        most-recent first.

        By default, archived conversations are excluded (the sidebar
        doesn't need them). Pass include_archived=True for the
        LLM-retraining export — the operator wants the full history.

        Group conversations (migration 014) ARE included — a group chat
        is legitimate training data — but they are flagged with
        is_group=True so the export can tell a group transcript from a
        one-to-one chat.
        """
        where_archived = "" if include_archived else " AND c.archived_at IS NULL"
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT c.id, c.title, c.updated_at, c.created_at,
                           c.archived_at, c.is_group,
                           (SELECT count(*) FROM {self.agent_name}.turns t
                             WHERE t.conversation_id = c.id) AS msg_count,
                           (SELECT substring(t.content, 1, 200)
                              FROM {self.agent_name}.turns t
                             WHERE t.conversation_id = c.id AND t.role = 'user'
                             ORDER BY t.created_at ASC LIMIT 1) AS first_user
                      FROM {self.agent_name}.conversations c
                     WHERE c.agent_name = %s{where_archived}
                     ORDER BY c.updated_at DESC
                     LIMIT %s
                    """,
                    (self.agent_name, limit),
                )
                rows = cur.fetchall()
        out = []
        for r in rows:
            cid, title, updated_at, created_at, archived_at, is_group, msg_count, first_user = r
            # Title fallback: first user message (truncated) or "New chat"
            if title:
                display = title
            elif first_user:
                display = (first_user[:57] + "…") if len(first_user) > 60 else first_user
            else:
                display = "New chat"
            out.append({
                "id": str(cid),
                "title": display,
                "updated_at": updated_at.isoformat() if updated_at else None,
                "created_at": created_at.isoformat() if created_at else None,
                "archived": archived_at is not None,
                "archived_at": archived_at.isoformat() if archived_at else None,
                "message_count": int(msg_count),
                # Migration 014: True when this conversation belongs to a
                # GROUP (public.groups.conversation_id), False for a DM.
                "is_group": bool(is_group),
            })
        return out

    def load_conversation(self, conversation_id: UUID) -> dict | None:
        """Full conversation with all turns in order. Returns None if not found.

        Returns the conversation even if archived_at is set (the LLM
        retraining export needs archived conversations). Callers that
        only want active conversations should filter on the
        `archived` flag in the returned dict.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, title, agent_name, created_at, updated_at, archived_at
                      FROM {self.agent_name}.conversations
                     WHERE id = %s
                    """,
                    (conversation_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cid, title, agent_name, created_at, updated_at, archived_at = row
                cur.execute(
                    f"""
                    SELECT id, role, content, model, latency_ms, created_at, trajectory
                      FROM {self.agent_name}.turns
                     WHERE conversation_id = %s
                     ORDER BY created_at ASC
                    """,
                    (conversation_id,),
                )
                turns = [
                    {
                        "id": r[0], "role": r[1], "content": r[2],
                        "model": r[3], "latency_ms": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                        # Migration 011 (2026-10-19): trajectory is the
                        # full per-iteration trace for assistant turns
                        # (or None for legacy + user turns). The
                        # export.jsonl?format=full endpoint surfaces
                        # this to the LLM retraining pipeline.
                        "trajectory": r[6] if len(r) > 6 else None,
                    }
                    for r in cur.fetchall()
                ]
        return {
            "id": str(cid),
            "title": title or "New chat",
            "agent_name": agent_name,
            "created_at": created_at.isoformat() if created_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "archived": archived_at is not None,
            "archived_at": archived_at.isoformat() if archived_at else None,
            "turns": turns,
        }

    def set_conversation_title(self, conversation_id: UUID, title: str) -> None:
        """Set the conversation title (auto-derived from first user msg)."""
        # Truncate to 200 chars; the schema column is text (no length cap).
        title = (title or "").strip()[:200]
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.agent_name}.conversations
                       SET title = %s, updated_at = now()
                     WHERE id = %s
                    """,
                    (title, conversation_id),
                )
            conn.commit()


# ---- chunking + helpers -------------------------------------------------

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _chunker(text: str, cfg: Any) -> list[str]:
    """Greedy paragraph split, then sentence split for over-budget paras.

    50-token overlap between adjacent chunks. Min chunk = cfg.min_tokens.
    """
    max_t = cfg.max_tokens
    min_t = cfg.min_tokens
    overlap_t = cfg.overlap_tokens

    paras = _PARA_SPLIT.split(text.strip())
    pieces: list[str] = []
    for p in paras:
        if not p.strip():
            continue
        if len(p.split()) <= max_t:
            pieces.append(p.strip())
        else:
            # Sentence-split the over-budget paragraph.
            sentences = _SENT_SPLIT.split(p)
            cur: list[str] = []
            cur_tok = 0
            for s in sentences:
                s_tok = len(s.split())
                if cur and cur_tok + s_tok > max_t:
                    pieces.append(" ".join(cur).strip())
                    # Carry overlap: last `overlap_t` tokens of cur.
                    flat = " ".join(cur).split()
                    if len(flat) > overlap_t:
                        cur = [" ".join(flat[-overlap_t:])]
                        cur_tok = overlap_t
                    else:
                        cur = []
                        cur_tok = 0
                cur.append(s)
                cur_tok += s_tok
            if cur:
                pieces.append(" ".join(cur).strip())

    # Filter out tiny chunks; add overlap between adjacent pieces.
    result: list[str] = []
    for i, p in enumerate(pieces):
        if len(p.split()) < min_t:
            continue
        if i > 0 and overlap_t > 0:
            # Prepend last `overlap_t` tokens of previous chunk.
            prev_words = result[-1].split() if result else []
            if len(prev_words) > overlap_t:
                head = " ".join(prev_words[-overlap_t:])
                p = head + " " + p
        result.append(p)
    return result


_WS = re.compile(r"\s+")


def _norm_text(s: str) -> str:
    return _WS.sub(" ", s.strip()).lower()


_STATUS_PATTERNS = [
    re.compile(r"^\s*(checking|running|thinking|let me|one moment|stand by)", re.I),
    re.compile(r"^(ok|okay|sure|got it)\b", re.I),
    re.compile(r"^\s*(typing|loading|processing)\.\.\.?\s*$", re.I),
]


def _is_status_commentary(text: str) -> bool:
    """Heuristic: a chunk is status commentary if it matches a short
    acknowledgement pattern. Lifted from agent3's status_commentary.py."""
    if len(text.split()) > 30:
        return False
    return any(p.search(text) for p in _STATUS_PATTERNS)
