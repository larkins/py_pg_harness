-- Phase N (v0.8.6): add 'list_recent' to the memory_audit op CHECK constraint.
--
-- The original constraint was inline in the per-agent schema, so it was
-- auto-named 'memory_audit_op_check' by Postgres. v0.8.6 introduces a new
-- memory_recent tool whose API.list_recent() inserts audit rows with
-- op='list_recent'; without this migration those inserts violate the
-- constraint.
--
-- Applied to EVERY agent schema in the roster, so an install with any
-- number of agents (and any names) works.
--
-- psql variables:
--   agent_schemas  REQUIRED  comma-separated list of agent schemas
--
-- Idempotent: drops the existing constraint by name and re-adds with the
-- new value list. Safe to re-run.

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
        IF to_regclass(format('%I.memory_audit', s)) IS NULL THEN
            RAISE NOTICE '%.memory_audit does not exist; skipping', s;
            CONTINUE;
        END IF;
        EXECUTE format(
            'ALTER TABLE %I.memory_audit DROP CONSTRAINT IF EXISTS memory_audit_op_check',
            s
        );
        EXECUTE format(
            'ALTER TABLE %I.memory_audit ADD CONSTRAINT memory_audit_op_check '
            'CHECK (op IN (''search'',''pin'',''unpin'',''permanent_fetch'','
            '''write_turns'',''list_recent''))',
            s
        );
        RAISE NOTICE 'migrated %.memory_audit memory_audit_op_check', s;
    END LOOP;
END
$do$;
