-- Migration 010 (2026-10-19): archived_at on <schema>.conversations
--
-- Before this migration, "start a new conversation" via the
-- /api/v1/entities/<name>/reset-conversation endpoint did:
--
--   DELETE FROM <schema>.conversations WHERE id = <conv_id>;
--   DELETE FROM <schema>.turns        WHERE conversation_id = <conv_id>;
--   DELETE FROM <schema>.memory_chunks WHERE conversation_id = <conv_id>
--                                    AND permanent = false;
--
-- That DELETED the turn rows — i.e. the LLM conversation history. The
-- intent was to "start fresh", but the data was lost forever. Per
-- Mal 2026-10-19 08:43, we need the turn data preserved for future
-- LLM retraining.
--
-- After this migration, the model is:
--
--   conversations.archived_at TIMESTAMPTZ NULL
--     - NULL       = active conversation; visible to the main session
--     - NOT NULL   = archived conversation; preserved for export, not
--                    surfaced to the main session
--
-- The "new conversation" flow is now:
--   1. Set archived_at = now() on the current conversation (preserves
--      turns + memory_chunks + the conversation row).
--   2. The next chat() call sees no non-archived conversation for
--      (agent, user) and inserts a new one. Old turns live in the
--      archived row.
--   3. The main session always serves the most recent non-archived
--      conversation. Archived conversations are reachable via the
--      new GET /api/v1/entities/<name>/conversations endpoint.
--
-- Memory chunks: still DELETE non-permanent chunks on archive
-- (matches the original intent of "free conversation-specific memory
-- from the working set"). Permanent memories (memories WHERE
-- permanent = TRUE) are unaffected. The turns themselves — which
-- are the LLM retraining source — are preserved.
--
-- Index: a partial index on (user_id, updated_at DESC) WHERE
-- archived_at IS NULL so the "find the most recent active
-- conversation" lookup is fast even after a long archive history.
--
-- Applied to every agent schema in the roster for
-- consistency, even though most agents rarely hit the limit.

\if :{?agent_schemas}
\else
  DO $missing_agent_schemas$ BEGIN
      RAISE EXCEPTION 'agent_schemas is required (pass -v agent_schemas=<a,b,c>)';
  END $missing_agent_schemas$;
\endif
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


DO $$
DECLARE
    s text;
BEGIN
    FOREACH s IN ARRAY string_to_array(current_setting('harness.agent_schemas'), ',')
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s
              AND table_name = 'conversations'
              AND column_name = 'archived_at'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.conversations
                 ADD COLUMN archived_at TIMESTAMPTZ NULL',
                s
            );
            RAISE NOTICE 'Added archived_at column to %.conversations', s;
        ELSE
            RAISE NOTICE '%.conversations already has archived_at; skipping ADD COLUMN', s;
        END IF;

        -- Partial index for the hot path: "most recent non-archived
        -- conversation for this (agent, user)". Without the WHERE
        -- clause, this index would also cover archived rows that the
        -- main session is never going to read.
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE schemaname = s
              AND tablename = 'conversations'
              AND indexname = 'conversations_active_lookup_idx'
        ) THEN
            EXECUTE format(
                'CREATE INDEX conversations_active_lookup_idx
                 ON %I.conversations (user_id, updated_at DESC)
                 WHERE archived_at IS NULL',
                s
            );
            RAISE NOTICE 'Created partial index on %.conversations (user_id, updated_at DESC) WHERE archived_at IS NULL', s;
        ELSE
            RAISE NOTICE '%.conversations already has conversations_active_lookup_idx; skipping', s;
        END IF;
    END LOOP;
END
$$;
