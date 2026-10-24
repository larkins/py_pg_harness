-- =====================================================================
-- 015 — the canonical public.users table
-- =====================================================================
-- The harness OWNS the shared application-user table. It is the only
-- project allowed to create it; every other project (email / webmail /
-- accounts / projects) VERIFIES it with db/preflight_users.sql and fails
-- fast if it is missing or incompatible.
--
-- The DDL itself lives in ONE place — db/public_users.sql — and this
-- migration pulls it in, so there is a single source of truth to vendor
-- and checksum.
--
-- Idempotent: re-running it is a no-op. It only ever ADDS columns; it
-- never drops or rewrites.
--
-- Required psql variables (all optional, with defaults):
--   operator_role  - the PG role that owns the objects  (default: harness_operator)
--   runtime_role   - the PG role the app connects as   (default: harness_app)
--
-- Usage (tools/migrate.sh does this for you):
--   psql -d <db> -v ON_ERROR_STOP=1 \
--        -v operator_role="$APP_DB_OPERATOR_ROLE" \
--        -v runtime_role="$APP_DB_RUNTIME_ROLE" \
--        -f migrations/015_canonical_users.sql
-- =====================================================================

\set ON_ERROR_STOP on

-- Defaults for the role names, so the file is usable standalone.
\if :{?operator_role}
\else
  \set operator_role 'harness_operator'
\endif
\if :{?runtime_role}
\else
  \set runtime_role 'harness_app'
\endif

\echo ''
\echo '>> 015: canonical public.users'

-- ---------------------------------------------------------------------
-- The canonical DDL (create if absent, align if present)
-- ---------------------------------------------------------------------
\ir ../db/public_users.sql

-- ---------------------------------------------------------------------
-- Ownership + grants
-- ---------------------------------------------------------------------
-- The operator role owns the table and the sequence.
SELECT format('ALTER TABLE public.users OWNER TO %I', :'operator_role') \gexec
SELECT format('ALTER SEQUENCE public.users_id_seq OWNER TO %I', :'operator_role') \gexec

-- The runtime role may read users, create them and update them
-- (password_changed_at, role, is_active). It must NOT be able to drop
-- the table, and it never sees the mail server's external correspondents
-- (those live in the mail server's own `mailboxes` table).
SELECT format('GRANT SELECT, INSERT, UPDATE ON public.users TO %I', :'runtime_role') \gexec
SELECT format('GRANT USAGE, SELECT ON SEQUENCE public.users_id_seq TO %I', :'runtime_role') \gexec

-- Anything the operator creates later in `public` is readable by the runtime role.
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT SELECT ON TABLES TO %I', :'operator_role', :'runtime_role') \gexec

-- ---------------------------------------------------------------------
-- Guard: the canonical table must never hold an external correspondent.
-- The mail server auto-provisions a row for every external sender it
-- sees; that belongs in `mailboxes`, not here. This flag is the last
-- line of defence — the mail server's provisioning path must not write
-- here at all.
-- ---------------------------------------------------------------------
COMMENT ON TABLE public.users IS
  'Canonical application users. Owned by the harness. Humans only — external '
  'email correspondents belong in the mail server''s mailboxes table.';

\echo '>> 015: public.users ready'
\echo ''
