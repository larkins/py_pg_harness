-- Migration 011 (2026-10-19): trajectory JSONB on <schema>.turns
--
-- Before this migration, the `turns` table stored only the FINAL
-- assistant text per turn (`turns.content`). The loop builds a full
-- per-iteration trace (assistant_text, tool_calls, tool_results,
-- nudge flags) per turn, but that trace was only captured in the
-- volatile `last_emission` module — never persisted. For LLM
-- retraining, the final text alone is the "thin" representation;
-- the trajectory teaches tool-use + reasoning patterns.
--
-- Per Mal 2026-10-19 09:16: add a `trajectory JSONB` column to
-- <schema>.turns. Format:
--
--   {
--     "iterations": [
--       {"iteration": 1, "finish_reason": "tool_use",
--        "assistant_text": "Let me check the data first...",
--        "tool_calls": [{"name": "sql_query", "arguments": {...}}],
--        "tool_results": [{"name": "sql_query", "result": "..."}],
--        "_nudged": false},
--       ...
--     ],
--     "total_iterations": 4,
--     "total_tool_calls": 6,
--     "tool_iterations": 4,
--     "latency_ms": 45086
--   }
--
-- Initial capacity: 1 turn with a 4-iter trajectory is ~5-15 KB
-- JSON. For long trajectories (rare today, but possible as the
-- model gets more complex), size can hit 50-100 KB. The 8KB TOAST
-- threshold means anything beyond inline-storage gets compressed +
-- out-of-line, which is fine.
--
-- If trajectory size becomes problematic (DB bloat, slow backups,
-- or a single conversation's trajectory exceeds say 1 MB), the
-- documented fallback is to externalize to per-conversation JSONL
-- files in pie/<agent>/trajectories/ and store only the filename
-- in this column. See docs/FUTURE_TRAJECTORY_EXTERNALIZATION.md
-- for the spec.
--
-- User turns: trajectory is NULL (no signal there). Assistant
-- turns: dict with the full per-iteration trace.
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
              AND table_name = 'turns'
              AND column_name = 'trajectory'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.turns
                 ADD COLUMN trajectory JSONB NULL',
                s
            );
            RAISE NOTICE 'Added trajectory column to %.turns', s;
        ELSE
            RAISE NOTICE '%.turns already has trajectory; skipping', s;
        END IF;
    END LOOP;
END
$$;
