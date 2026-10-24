-- Migration 009 (2026-10-17): cascade-delete memory_chunks when their
-- source turn is deleted.
--
-- Before this migration, <schema>.memory_chunks.source_turn_id was a
-- SOFT reference (a plain bigint column with no FK). Deleting a turn
-- from <schema>.turns left the chunk orphaned: the row stayed put with
-- source_turn_id pointing at a non-existent id, and the chunk's
-- embedding remained searchable. The matching
--   source_memory_id -> <schema>.memories(id) ON DELETE CASCADE
-- FK was already in place (added in migration 007), so memory-side
-- cleanup was automatic but turn-side was not.
--
-- Per Mal 2026-10-17: chunks should only ever originate from a turn
-- or a memory, and either parent deletion should clean up the chunks.
-- Verified pre-flight: zero orphans in any agent schema.
--
-- Applied to all three agent schemas for consistency.

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
            SELECT 1 FROM pg_constraint
            WHERE conname = 'memory_chunks_source_turn_id_fkey'
              AND connamespace = to_regnamespace(s)
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.memory_chunks
                 ADD CONSTRAINT memory_chunks_source_turn_id_fkey
                 FOREIGN KEY (source_turn_id)
                 REFERENCES %I.turns(id)
                 ON DELETE CASCADE',
                s, s
            );
            RAISE NOTICE 'Added memory_chunks -> turns cascade FK on %.memory_chunks', s;
        ELSE
            RAISE NOTICE '%.memory_chunks already has source_turn_id FK; skipping', s;
        END IF;
    END LOOP;
END
$$;
