-- =====================================================================
-- CANONICAL public.users  —  version 0.4
-- =====================================================================
-- The shared application-user table for one installation root.
--
-- OWNERSHIP: the HARNESS owns this table. It creates it (fresh mode) or
-- verifies it (existing mode). NO OTHER PROJECT MAY CREATE IT — that
-- silent second `users` table is the root cause of the identity
-- split this file exists to end.
--
-- Other projects (email / webmail / accounts / projects) must run
-- db/preflight_users.sql first, which VERIFIES this table and fails
-- loudly if it is missing or incompatible. They never create it.
--
-- v0.3 (approved 2026-10-14) + the three additive deltas:
--   role_scope   JSONB  - per-agent authorisation scope (the harness uses it)
--   is_local     BOOLEAN- the mail server's inbound routing needs it
--   password_changed_at  - already in v0.3
--
-- Deliberately BIGSERIAL (not GENERATED ... AS IDENTITY): it matches what
-- is already deployed, and it allows an explicit id insert — which the
-- mail server's migration needs to preserve its existing int4 ids.
--
-- Idempotent: safe to re-run. Only ever ADDs; never drops or rewrites.
-- =====================================================================

-- citext carries the case-insensitive email. It is an extension, so it
-- must exist before the table does. Creating it needs a role with the
-- right privilege: the owner of the database can, otherwise the
-- installer pre-creates it as a superuser. Idempotent either way.
CREATE EXTENSION IF NOT EXISTS citext;

-- citext may live outside `public` (extensions are not always installed
-- there), so put the extension's schema on the search_path before the
-- table below uses the CITEXT type.
SELECT format('SET search_path TO public, %s',
              (SELECT string_agg(DISTINCT s.nspname, ', ')
                 FROM (SELECT 'public' AS nspname
                       UNION
                       SELECT n2.nspname
                         FROM pg_extension e2
                         JOIN pg_namespace n2 ON n2.oid = e2.extnamespace
                        WHERE e2.extname IN ('citext', 'vector', 'pg_trgm')) s))
\gexec

CREATE TABLE IF NOT EXISTS public.users (
    id                  BIGSERIAL PRIMARY KEY,
    email               CITEXT UNIQUE NOT NULL,
    password_hash       TEXT NOT NULL,
    display_name        TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL DEFAULT 'member'
                            CHECK (role IN ('admin','member','viewer')),
    role_scope          JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    is_local            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-insensitive uniqueness, independent of the citext extension.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx
    ON public.users (lower(email::text));

-- ---------------------------------------------------------------------
-- Alignment for a table that already exists (v0.3 deployments).
-- Additive only: a column that is already there is left alone.
-- ---------------------------------------------------------------------
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS role_scope JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS is_local   BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT '';
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- The role check constraint, added only if the table has no such check.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname = 'users_role_check'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_role_check
            CHECK (role IN ('admin','member','viewer'));
    END IF;
END $$;
