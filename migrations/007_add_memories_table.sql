-- 007_add_memories_table.sql -- memories table + memory_chunks.source_table.
--
-- The long-form memory model:
--   "The content column of the memories table is chunked and stored in
--    memory_chunks for semantic/index search like the turn.content. The
--    memory_chunk rows need a source_table column, so that the correct
--    table and rowid can be retrieved when a chunk is surfaced by
--    semantic search. This allows the agent to curate and archive long
--    form memories independent of optimal chunk length, and allows
--    retrieval and pinning of long form memories, while still allowing
--    semantic search on appropriate sized chunks."
--
-- Scope of this migration:
--   - Build the memories table in every agent schema.
--   - Add memory_chunks.source_memory_id, source_table, make
--     conversation_id nullable, change the memory_hash unique constraint
--     to be partial (per source type).
--   - DROP the orphan chunks (those with source_turn_id IS NULL) that
--     are leftovers from the old `memory_write` tool, which wrote chunks
--     directly without a source_turn_id.
--   - Do NOT backfill the orphan chunks into memories rows.
--
-- The per-agent work is driven by the `agent_schemas` list, so the same
-- migration serves any number of agents.
--
-- psql variables:
--   agent_schemas  REQUIRED  comma-separated list of agent schemas
--                             (e.g. 'agent1,agent2')
--   operator_role  optional  role that owns the tables (default: harness_operator)
--   runtime_role   optional  role that gets DML (default: harness_app)
--
-- This migration is idempotent: every operation is guarded by IF NOT
-- EXISTS / IF EXISTS, so re-running on a partially-applied DB is safe.

\if :{?operator_role}
\else
  \set operator_role 'harness_operator'
\endif
\if :{?runtime_role}
\else
  \set runtime_role 'harness_app'
\endif
\if :{?agent_schemas}
\else
  DO $missing_agent_schemas$ BEGIN
      RAISE EXCEPTION 'agent_schemas is required (pass -v agent_schemas=<a,b,c>)';
  END $missing_agent_schemas$;
\endif

-- The body of the DO block below is dollar-quoted, so psql does NOT
-- substitute variables inside it. The values are handed over through
-- session settings instead.
SET harness.agent_schemas = :'agent_schemas';

-- The pgvector / pg_trgm / citext extensions do not always live in
-- `public` — on a host where another project installed them, their types
-- and operator classes are in that project's schema. Put them on the
-- search_path so the tables below can be created.
SELECT format('SET search_path TO public, %s',
              (SELECT string_agg(DISTINCT s.nspname, ', ')
                 FROM (SELECT 'public' AS nspname
                       UNION
                       SELECT n2.nspname
                         FROM pg_extension e2
                         JOIN pg_namespace n2 ON n2.oid = e2.extnamespace
                        WHERE e2.extname IN ('vector', 'pg_trgm', 'citext')) s))
\gexec

SET harness.operator_role = :'operator_role';
SET harness.runtime_role  = :'runtime_role';

DO $do$
DECLARE
    schemas   text[] := string_to_array(current_setting('harness.agent_schemas'), ',');
    op_role   text   := COALESCE(NULLIF(current_setting('harness.operator_role'), ''), 'harness_operator');
    rt_role   text   := COALESCE(NULLIF(current_setting('harness.runtime_role'),  ''), 'harness_app');
    is_super  boolean;
    s         text;
    t         text;
    n_orphans int;
BEGIN
    SELECT rolsuper INTO is_super FROM pg_roles WHERE rolname = current_user;

    FOREACH s IN ARRAY schemas LOOP
        s := btrim(s);
        CONTINUE WHEN s = '';

        -- ============================================================
        -- STEP 1: Drop orphan chunks (no source_turn_id)
        -- ============================================================

        EXECUTE format('SELECT count(*) FROM %I.memory_chunks WHERE source_turn_id IS NULL', s)
            INTO n_orphans;
        RAISE NOTICE '%: orphan chunks (no source_turn_id): %', s, n_orphans;

        EXECUTE format('DELETE FROM %I.memory_chunks WHERE source_turn_id IS NULL', s);

        -- ============================================================
        -- STEP 2: Create the memories table
        -- ============================================================
        -- One per agent, owned by the operator role. Same shape as the
        -- chunk table's metadata columns (role/importance/permanent/etc.)
        -- so the API surface is symmetric. The memories table is the
        -- long-form source; chunks are slices of it.

        EXECUTE format($ddl$
CREATE TABLE IF NOT EXISTS %I.memories (
    id                BIGSERIAL PRIMARY KEY,
    content           TEXT NOT NULL,
    content_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding         halfvec(1024),
    source_type       TEXT NOT NULL DEFAULT 'system'
                          CHECK (source_type IN ('chat','email','code','doc','system')),
    source_ref        TEXT,
    importance        SMALLINT NOT NULL DEFAULT 5 CHECK (importance BETWEEN 0 AND 10),
    memory_hash       TEXT NOT NULL,
    status_commentary BOOLEAN NOT NULL DEFAULT FALSE,
    permanent         BOOLEAN NOT NULL DEFAULT FALSE,
    pin_source        TEXT CHECK (pin_source IS NULL OR pin_source IN ('model','operator','bootstrap')),
    pinned_at         TIMESTAMPTZ,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by        TEXT NOT NULL DEFAULT 'operator'
                          CHECK (created_by IN ('operator','agent','import'))
)$ddl$, s);

        -- Re-own the new table to the operator role. Without this the
        -- table owner is the role that ran the migration (the postgres
        -- superuser in that case), and the runtime role can only get
        -- the privileges explicitly GRANTed below. By re-owning to the
        -- operator role, that role has implicit full DML on its own
        -- tables, which means the per-agent schema design works as
        -- documented.
        EXECUTE format('ALTER TABLE %I.memories OWNER TO %I', s, op_role);

        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I.memories (memory_hash)',
                       s || '_memories_memory_hash_key', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memories (permanent, pinned_at DESC) WHERE permanent = TRUE',
                       s || '_memories_permanent_idx', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memories (created_at DESC)',
                       s || '_memories_created_at_idx', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memories USING gin (content_tsv)',
                       s || '_memories_tsv_idx', s);

        -- ============================================================
        -- STEP 3: Update memory_chunks
        -- ============================================================

        -- 3a. Make conversation_id nullable. (Memory-derived chunks have
        -- no conversation.)
        EXECUTE format('ALTER TABLE %I.memory_chunks ALTER COLUMN conversation_id DROP NOT NULL', s);

        -- 3b. Add source_memory_id (nullable, FK to memories, cascade delete).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s AND table_name = 'memory_chunks'
              AND column_name = 'source_memory_id'
        ) THEN
            EXECUTE format('ALTER TABLE %I.memory_chunks ADD COLUMN source_memory_id BIGINT REFERENCES %I.memories(id) ON DELETE CASCADE', s, s);
        END IF;

        -- 3c. Add the source_table GENERATED column. Stays in sync with
        -- the two source FK columns; the CHECK constraint enforces that
        -- exactly one of the two is set.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s AND table_name = 'memory_chunks'
              AND column_name = 'source_table'
        ) THEN
            EXECUTE format($ddl$
ALTER TABLE %I.memory_chunks
    ADD COLUMN source_table TEXT GENERATED ALWAYS AS (
        CASE
            WHEN source_turn_id IS NOT NULL THEN 'turns'
            WHEN source_memory_id IS NOT NULL THEN 'memories'
        END
    ) STORED$ddl$, s);
            EXECUTE format($ddl$
ALTER TABLE %I.memory_chunks
    ADD CONSTRAINT memory_chunks_source_oneof_chk
    CHECK (
        (source_turn_id IS NOT NULL AND source_memory_id IS NULL)
     OR (source_turn_id IS NULL AND source_memory_id IS NOT NULL)
    )$ddl$, s);
        END IF;

        -- 3d. Drop the old (memory_hash, conversation_id) unique
        -- constraint and replace it with partial unique indexes per
        -- source type. The old constraint was UNIQUE on the pair; we
        -- need uniqueness per source.
        EXECUTE format('ALTER TABLE %I.memory_chunks DROP CONSTRAINT IF EXISTS memory_chunks_memory_hash_conversation_id_key', s);

        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I.memory_chunks (memory_hash, conversation_id) WHERE source_turn_id IS NOT NULL',
                       s || '_memory_chunks_hash_turn_key', s);

        -- NOTE: A unique index on (memory_hash, source_memory_id) is
        -- intentionally NOT created. All chunks of a single memory share
        -- the same memory_hash (it's the hash of the whole content, not
        -- the chunk). The natural uniqueness is via (source_memory_id,
        -- chunk_index), which is implicit since chunk_index is set
        -- per-memory. For turn-derived chunks, the unique index on
        -- (memory_hash, conversation_id) covers dedup.

        -- 3e. Index on source_memory_id (the new FK column) and on
        -- source_table (for the common filter "chunks from memories" /
        -- "chunks from turns").
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memory_chunks (source_memory_id) WHERE source_memory_id IS NOT NULL',
                       s || '_memory_chunks_source_memory_idx', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memory_chunks (source_table)',
                       s || '_memory_chunks_source_table_idx', s);

        -- ============================================================
        -- STEP 4: HNSW index on memories.embedding
        -- ============================================================
        -- Separate from the per-agent CREATE TABLE because the index
        -- needs halfvec_cosine_ops to exist (from pgvector 0.5+).
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memories USING hnsw (embedding halfvec_cosine_ops) WITH (m=''16'', ef_construction=''64'')',
                       s || '_memories_emb_hnsw_idx', s);

        -- ============================================================
        -- STEP 5: Grant sequence usage + DML on the new memories tables
        -- ============================================================

        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', s, rt_role);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON %I.memories TO %I', s, rt_role);

        -- Also fix: tables that were restored from another database can
        -- end up owned by the superuser. Re-own them to the operator
        -- role so the per-agent schema design holds (the runtime role
        -- still has DML via the GRANTs above). Changing ownership needs
        -- a superuser or the current owner, so it is skipped for any
        -- other role.
        IF is_super OR current_user = op_role THEN
            FOREACH t IN ARRAY ARRAY['conversations','turns','memory_chunks','memory_audit','memories'] LOOP
                IF to_regclass(format('%I.%I', s, t)) IS NOT NULL THEN
                    EXECUTE format('ALTER TABLE %I.%I OWNER TO %I', s, t, op_role);
                END IF;
            END LOOP;
        ELSE
            RAISE NOTICE '%: skipping the ownership changes (current role is neither a superuser nor the operator role)', s;
        END IF;
    END LOOP;
END
$do$;
