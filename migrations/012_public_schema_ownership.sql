-- 012_public_schema_ownership.sql
--
-- The installation database's `public` schema belongs to the operator
-- role (the app's own tables + the auth tables). The operator role must
-- OWN them, not merely be granted privileges, because:
--
--   1. a co-hosted service that connects as the operator role runs
--      UNGUARDED DDL at startup (CREATE TABLE IF NOT EXISTS /
--      ALTER TABLE ... ADD COLUMN / CREATE INDEX) on its own tables.
--      Without ownership those statements raise "must be owner of
--      table" and the service CRASH-LOOPS on restart.
--   2. The app also needs to INSERT/UPDATE those rows. As a non-owner
--      with no grants, every write returned HTTP 500 -- the web form
--      and /login were silently broken.
--
-- The correct state is DECLARATIVE and re-appliable instead of
-- implicit, which is what this migration is for.
--
-- Changing ownership requires a superuser (or being the current owner).
-- When this migration is applied by a role that can do neither, the
-- ownership changes are skipped with a note and only the grants are
-- applied -- the end state is the same, because the tables are then
-- already owned by the role that created them.
--
-- psql variables:
--   operator_role  optional  role that owns public.*  (default: harness_operator)
--   runtime_role   optional  the runtime role        (default: harness_app)
--   owned_tables   optional  comma-separated list of extra public tables to
--                            re-own (default: none). The harness's own
--                            tables (public.users, public.entity_icons) are
--                            always re-owned.
--
--   psql -v operator_role=<role> \
--         -v runtime_role=<role> \
--         -v owned_tables=app_users,contact_submissions \
--         -f migrations/012_public_schema_ownership.sql

\if :{?operator_role}
\else
  \set operator_role 'harness_operator'
\endif
\if :{?runtime_role}
\else
  \set runtime_role 'harness_app'
\endif
\if :{?owned_tables}
\else
  \set owned_tables ''
\endif

-- Can the executing role change ownership?
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper)
            THEN 'true' ELSE 'false' END AS can_change_owner \gset

\if :can_change_owner

-- The schema itself: the operator owns it, same as every other schema.
ALTER SCHEMA public OWNER TO :"operator_role";

-- The harness's own public tables plus any extra table the operator
-- wants to own (e.g. the tables of a co-hosted web app). ALTER TABLE
-- ... OWNER also re-owns the sequences owned by those tables (OWNED BY
-- columns), but they are listed explicitly as well so the intent is
-- obvious and so a table with a detached sequence still gets fixed.
SELECT format('ALTER TABLE public.%I OWNER TO %I', c.relname, :'operator_role')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relkind = 'r'
   AND (c.relname IN ('users', 'entity_icons')
        OR c.relname IN (SELECT btrim(x) FROM unnest(string_to_array(:'owned_tables', ',')) AS u(x)
                          WHERE btrim(x) <> ''))
 \gexec

SELECT format('ALTER SEQUENCE public.%I OWNER TO %I', c.relname, :'operator_role')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'S' \gexec

\else
\echo '012: the current role cannot change ownership -- skipping the ownership changes (re-run as a superuser to apply them)'
\endif

-- The harness runtime role keeps ONLY what the runtime-grants migration
-- gave it: SELECT on public.users (+ the legacy public.app_users), full
-- DML on the agent schemas. It must NOT get anything on the other
-- public tables.
GRANT USAGE ON SCHEMA public TO :"runtime_role";

SELECT CASE WHEN to_regclass('public.users') IS NOT NULL
            THEN 'true' ELSE 'false' END AS has_users \gset
\if :has_users
  GRANT SELECT ON public.users TO :"runtime_role";
\else
\echo '012: public.users does not exist -- skipped its SELECT grant'
\endif

SELECT CASE WHEN to_regclass('public.app_users') IS NOT NULL
            THEN 'true' ELSE 'false' END AS has_app_users \gset
\if :has_app_users
  GRANT SELECT ON public.app_users TO :"runtime_role";
\endif

-- Verification (expect: every row owned by the operator role; the
-- runtime role is SELECT-only on the web app's tables):
--   SELECT c.relname, pg_get_userbyid(c.relowner) AS owner,
--          has_table_privilege('<operator_role>', c.oid, 'SELECT') AS sel,
--          has_table_privilege('<operator_role>', c.oid, 'INSERT') AS ins
--     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname = 'public' AND c.relkind IN ('r','S');
