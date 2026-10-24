-- Canonical per-agent schema.
--
-- ONE file for every agent. tools/migrate.sh applies it once per
-- configured agent (the roster comes from HARNESS_AGENTS, else from the
-- `app.agents` list in config.yaml, else from HARNESS_DEFAULT_AGENT).
--
-- Nothing about a particular install is baked in -- the agent name, the
-- owning (operator) role and the runtime role are all psql variables:
--
--   psql -v agent_name=<agent> \
--         -v operator_role=<operator role> \
--         -v runtime_role=<runtime role> \
--         -f migrations/agents/agent_schema.sql
--
--   agent_name     REQUIRED  the agent (and its schema) to create.
--   operator_role  optional  role that owns the schema and the tables.
--                            default: harness_operator
--   runtime_role   optional  role the app connects as; gets DML.
--                            default: harness_app
--
-- See REQUIREMENTS_AND_SPECIFICATIONS.md §6.2 for the column-level spec.

-- Optional variables: fall back to the documented defaults.
\if :{?operator_role}
\else
  \set operator_role 'harness_operator'
\endif
\if :{?runtime_role}
\else
  \set runtime_role 'harness_app'
\endif

-- pgvector provides the `halfvec` type the memory_chunks table uses.
-- It is an extension, so it must exist before the table does. The
-- database owner can create it; on a cluster where the extension is not
-- available, fail with an actionable message rather than a bare
-- "type halfvec does not exist".
-- The extensions this schema needs: pgvector (the halfvec type), pg_trgm
-- (the trigram index) and citext (the shared users table's email).
-- They are checked, not created: creating an extension needs a superuser,
-- and the installer is the privileged step.
DO $require_extensions$
DECLARE
    ext text;
BEGIN
    FOREACH ext IN ARRAY ARRAY['vector', 'pg_trgm', 'citext'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = ext) THEN
            IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = ext) THEN
                RAISE EXCEPTION
                    'the % extension is not available on this server -- install the '
                    'postgresql-*-pgvector / postgresql-contrib package first', ext;
            END IF;
            RAISE EXCEPTION
                'the % extension is not installed in this database -- the installer '
                'creates it (it needs a superuser); run CREATE EXTENSION IF NOT EXISTS %; '
                'as a superuser, then re-run', ext, ext;
        END IF;
    END LOOP;
END
$require_extensions$;

-- The search_path is resolved dynamically: the pgvector/pg_trgm/citext
-- extensions do not always live in `public` (on one host they are in the
-- mail server's schema), and their types and operator classes must be on
-- the path or the tables below cannot be created.
--
-- The agent name is not optional -- fail loudly rather than create a
-- schema with a placeholder name.
\if :{?agent_name}
\else
  DO $missing_agent_name$ BEGIN
      RAISE EXCEPTION 'agent_name is required (pass -v agent_name=<name>)';
  END $missing_agent_name$;
\endif

-- Create the schema with a dynamic name, owned by the operator role.
SELECT format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I',
              :'agent_name',
              COALESCE(NULLIF(:'operator_role', ''), 'harness_operator')) \gexec

-- Everything below is unqualified, so it lands in the agent schema.
-- `public` stays on the search path so the shared extensions resolve
-- (pgvector's halfvec, pg_trgm's gin_trgm_ops) and so the FK target
-- public.users is reachable.
SELECT format('SET search_path TO %I, %s',
              :'agent_name',
              (SELECT string_agg(DISTINCT s.nspname, ', ')
                 FROM (SELECT 'public' AS nspname
                       UNION
                       SELECT n2.nspname
                         FROM pg_extension e2
                         JOIN pg_namespace n2 ON n2.oid = e2.extnamespace
                        WHERE e2.extname IN ('vector', 'pg_trgm', 'citext')) s))
\gexec


-- Conversations
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    agent_name      TEXT NOT NULL DEFAULT :'agent_name',
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Migration 014 (2026-10-23): True when this conversation belongs
    -- to a GROUP (the group's own conversation id, shared by every
    -- agent in that group). A DM conversation (this agent + this user)
    -- is always False, and every DM lookup path filters on it, so a DM
    -- can never pick up a group conversation.
    is_group        BOOLEAN NOT NULL DEFAULT FALSE
);

-- Turns (one row per user or assistant message)
CREATE TABLE IF NOT EXISTS turns (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    token_count     INTEGER,
    model           TEXT,
    latency_ms      INTEGER,
    -- Phase 10: per-turn image attachments (list of {url, filename,
    -- content_type, size}). Defaulted to an empty array so legacy
    -- inserts without the column work unchanged. See migration 005
    -- for the live-DB equivalent of adding this to an existing
    -- install.
    attachments     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turns_conv_idx ON turns (conversation_id, created_at);

-- Memory chunks
CREATE TABLE IF NOT EXISTS memory_chunks (
    id                BIGSERIAL PRIMARY KEY,
    source_turn_id    BIGINT REFERENCES turns(id) ON DELETE SET NULL,
    conversation_id   UUID REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
    chunk_index       INTEGER NOT NULL DEFAULT 0,
    content           TEXT NOT NULL,
    content_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding         halfvec(1024),
    source_type       TEXT NOT NULL DEFAULT 'chat' CHECK (source_type IN ('chat','email','code','doc','system')),
    source_ref        TEXT,
    importance        SMALLINT NOT NULL DEFAULT 5 CHECK (importance BETWEEN 0 AND 10),
    memory_hash       TEXT NOT NULL,
    status_commentary BOOLEAN NOT NULL DEFAULT FALSE,
    permanent         BOOLEAN NOT NULL DEFAULT FALSE,
    pin_source        TEXT,
    pinned_at         TIMESTAMPTZ,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (memory_hash, conversation_id)
);

-- Vector index — HNSW for incremental insert + good recall at <1M rows.
CREATE INDEX IF NOT EXISTS memory_chunks_emb_hnsw_idx
    ON memory_chunks USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Trigram index for fuzzy text search.
CREATE INDEX IF NOT EXISTS memory_chunks_content_trgm_idx
    ON memory_chunks USING gin (content gin_trgm_ops);

-- Full-text search (English) on the generated tsvector column.
CREATE INDEX IF NOT EXISTS memory_chunks_tsv_idx
    ON memory_chunks USING gin (content_tsv);

-- Partial index — small/fast for the every-prompt permanent-memory fetch.
CREATE INDEX IF NOT EXISTS memory_chunks_permanent_idx
    ON memory_chunks (permanent, pinned_at DESC)
    WHERE permanent = TRUE;

-- Audit table for every MemoryAPI call (safety boundary observability).
CREATE TABLE IF NOT EXISTS memory_audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    op              TEXT NOT NULL CHECK (op IN ('search','pin','unpin','permanent_fetch','write_turns')),
    query_hash      TEXT NOT NULL,
    top_k           INTEGER NOT NULL DEFAULT 0,
    result_count    INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    op_over_run     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS memory_audit_ts_idx ON memory_audit (ts DESC);
CREATE INDEX IF NOT EXISTS memory_audit_op_idx ON memory_audit (op, ts DESC);

-- Runtime role gets DML on all tables in the agent schema. Future tables
-- created by the operator role get DML via the default privileges.
-- Sequences (e.g. turns_id_seq) need USAGE for BIGSERIAL inserts.
GRANT USAGE ON SCHEMA :"agent_name" TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA :"agent_name" TO :"runtime_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA :"agent_name" TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"operator_role" IN SCHEMA :"agent_name"
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"operator_role" IN SCHEMA :"agent_name"
    GRANT USAGE, SELECT ON SEQUENCES TO :"runtime_role";
