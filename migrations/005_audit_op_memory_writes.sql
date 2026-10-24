-- Migration 005: expand the memory_audit_op_check CHECK constraint
-- to include the memory-write / update / delete op values that the
-- MemoryAPI has been silently trying to write since Phase 13 (2026-10-15).
-- Until now, every memory write/update/delete audit row was rejected by
-- the CHECK and silently swallowed by the `try/except: pass` in
-- memory.py:548/710/758/862 + the new delete_memory audit. So the
-- audit log has only been recording READ-ONLY operations (search,
-- pin, unpin, permanent_fetch, write_turns, list_recent).
--
-- This migration:
--   1. Drops the old CHECK constraint (per-agent schema).
--   2. Adds a new CHECK constraint that includes the new op values
--      used by write_memory, recall_memory, update_memory, delete_memory.
--
-- The op values are now: search, pin, unpin, permanent_fetch, write_turns,
-- list_recent, write_memory, recall_memory, update_memory, delete_memory.
--
-- Idempotent: safe to re-run.

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
    agent_name text;
    schemas text[] := string_to_array(current_setting('harness.agent_schemas'), ',');
BEGIN
    FOREACH agent_name IN ARRAY schemas
    LOOP
        -- Drop the old constraint if it exists
        EXECUTE format(
            'ALTER TABLE %I.memory_audit DROP CONSTRAINT IF EXISTS memory_audit_op_check',
            agent_name
        );
        -- Re-add with the expanded allowlist
        EXECUTE format(
            'ALTER TABLE %I.memory_audit ADD CONSTRAINT memory_audit_op_check '
            'CHECK (op = ANY (ARRAY['
            '''search''::text, '
            '''pin''::text, '
            '''unpin''::text, '
            '''permanent_fetch''::text, '
            '''write_turns''::text, '
            '''list_recent''::text, '
            '''write_memory''::text, '
            '''recall_memory''::text, '
            '''update_memory''::text, '
            '''delete_memory''::text'
            ']))',
            agent_name
        );
        RAISE NOTICE 'migrated %.memory_audit memory_audit_op_check', agent_name;
    END LOOP;
END $$;
