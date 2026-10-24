-- 005_turn_attachments.sql
-- Phase 10: per-turn image attachments for the harness chat UI.
--
-- Adds an `attachments` jsonb column to <agent>.turns, defaulting to an
-- empty array. Each entry is a small object like:
--   {"url": "/api/v1/uploads/<id>.png", "filename": "screenshot.png",
--    "content_type": "image/png", "size": 12345}
--
-- The column is nullable-but-defaulted so existing rows continue to work
-- without a backfill; new rows will always carry the empty array.
--
-- Applied to EVERY agent schema in the roster.
--
-- psql variables:
--   agent_schemas  REQUIRED  comma-separated list of agent schemas
--
-- Idempotent: re-running it on an already-migrated DB is a no-op.

\set ON_ERROR_STOP on

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


DO $do$
DECLARE
    s       text;
    schemas text[] := string_to_array(current_setting('harness.agent_schemas'), ',');
BEGIN
    FOREACH s IN ARRAY schemas LOOP
        IF to_regclass(format('%I.turns', s)) IS NULL THEN
            RAISE NOTICE '%.turns does not exist; skipping', s;
            CONTINUE;
        END IF;
        EXECUTE format(
            'ALTER TABLE %I.turns '
            'ADD COLUMN IF NOT EXISTS attachments JSONB NOT NULL DEFAULT ''[]''::jsonb',
            s
        );
        RAISE NOTICE 'ensured %.turns.attachments', s;
    END LOOP;
END
$do$;

-- A GIN index would help if we ever start querying *into* the attachments
-- JSONB (e.g. "show me all turns that attached a PNG"); not needed for
-- the current use case (we just SELECT/RETURN the column), so skip it.
