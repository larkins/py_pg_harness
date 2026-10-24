-- Phase 12: Grants for the harness runtime role.
--
-- The agent schemas need full DML from the runtime role (it is the only
-- role the app connects as). public.users needs SELECT for the auth
-- login flow.
--
-- Other public tables (e.g. the tables of a co-hosted web app) are NOT
-- granted to the runtime role -- it has no business touching them.
--
-- Future tables: ALTER DEFAULT PRIVILEGES for the operator role in the
-- agent schemas (DML to the runtime role) is set by the per-agent
-- schema installer (migrations/agents/agent_schema.sql).
--
-- psql variables:
--   agent_schemas  REQUIRED  comma-separated list of agent schemas
--                             (e.g. 'agent1,agent2')
--   runtime_role   optional  the runtime role  (default: harness_app)
--   operator_role  optional  the operator role (default: harness_operator)
--
--   psql -v agent_schemas=agent1,agent2 \
--         -v runtime_role=harness_app \
--         -v operator_role=harness_operator \
--         -f migrations/006_install_runtime_grants.sql

\if :{?runtime_role}
\else
  \set runtime_role 'harness_app'
\endif
\if :{?operator_role}
\else
  \set operator_role 'harness_operator'
\endif
\if :{?agent_schemas}
\else
  DO $missing_agent_schemas$ BEGIN
      RAISE EXCEPTION 'agent_schemas is required (pass -v agent_schemas=<a,b,c>)';
  END $missing_agent_schemas$;
\endif

-- Agent schema grants.
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', btrim(s), :'runtime_role')
  FROM unnest(string_to_array(:'agent_schemas', ',')) AS t(s)
 WHERE btrim(s) <> '' \gexec

SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
              btrim(s), :'runtime_role')
  FROM unnest(string_to_array(:'agent_schemas', ',')) AS t(s)
 WHERE btrim(s) <> '' \gexec

SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I',
              btrim(s), :'runtime_role')
  FROM unnest(string_to_array(:'agent_schemas', ',')) AS t(s)
 WHERE btrim(s) <> '' \gexec

-- Auth: the harness login() reads from public.users.
GRANT USAGE ON SCHEMA public TO :"runtime_role";
GRANT SELECT ON public.users TO :"runtime_role";

-- Legacy/alternate auth table: some installs keep a public.app_users
-- table next to public.users. Grant on it only when it exists.
SELECT CASE WHEN to_regclass('public.app_users') IS NOT NULL
            THEN 'true' ELSE 'false' END AS has_app_users \gset
\if :has_app_users
  GRANT SELECT ON public.app_users TO :"runtime_role";
\endif
