-- =====================================================================
-- PREFLIGHT: is the canonical public.users present and compatible?
-- =====================================================================
-- Run this BEFORE installing any project that references public.users
-- (email / webmail / accounts / projects).
--
-- This script VERIFIES. It never creates. If the table is missing or
-- incompatible it RAISES — the install must fail fast rather than
-- silently create a second, divergent users table.
--
-- Usage:
--   psql -d <db> -v ON_ERROR_STOP=1 -f db/preflight_users.sql
--
-- Exit 0 = compatible. Non-zero = do not install.
-- =====================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
    v_missing_cols TEXT[] := '{}';
    v_id_type      TEXT;
    v_cols         TEXT;
BEGIN
    ------------------------------------------------------------------
    -- 1. the table must exist
    ------------------------------------------------------------------
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION
            'public.users is missing — install the harness first (it owns the shared users table). '
            'Do NOT create a local users table: a second users table is what caused the identity split.';
    END IF;

    ------------------------------------------------------------------
    -- 2. the id must be BIGINT (an int4 id cannot carry a FK from the
    --    canonical shape, and the sequence must be the table's own)
    ------------------------------------------------------------------
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO v_id_type
      FROM pg_attribute a
     WHERE a.attrelid = 'public.users'::regclass
       AND a.attname = 'id';

    IF v_id_type IS DISTINCT FROM 'bigint' THEN
        RAISE EXCEPTION
            'public.users.id is %, expected bigint — the canonical table uses BIGSERIAL. '
            'Migrate the id column before installing (see the harness public.users migration).',
            coalesce(v_id_type, '<missing>');
    END IF;

    ------------------------------------------------------------------
    -- 3. every column this project depends on must be present
    ------------------------------------------------------------------
    SELECT string_agg(c.column_name, ', ' ORDER BY c.column_name)
      INTO v_cols
      FROM information_schema.columns c
     WHERE c.table_schema = 'public'
       AND c.table_name   = 'users';

    SELECT array_agg(required.column_name ORDER BY required.column_name)
      INTO v_missing_cols
      FROM (VALUES ('id'),('email'),('password_hash'),('display_name'),('role'),
                   ('role_scope'),('is_active'),('is_local'),
                   ('created_at'),('updated_at'),('password_changed_at')) AS required(column_name)
     WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns c
         WHERE c.table_schema = 'public' AND c.table_name = 'users'
           AND c.column_name = required.column_name);

    IF v_missing_cols IS NOT NULL THEN
        RAISE EXCEPTION
            'public.users is missing column(s): % — the canonical table is version 0.4. '
            'Apply the harness public.users migration (migrations/015_canonical_users.sql) to align it.',
            array_to_string(v_missing_cols, ', ');
    END IF;

    ------------------------------------------------------------------
    -- 4. the email must be unique (citext or a lower() index)
    ------------------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
          JOIN pg_class c ON c.oid = i.indexrelid
         WHERE i.indrelid = 'public.users'::regclass AND i.indisunique
    ) THEN
        RAISE EXCEPTION 'public.users has no unique index — email uniqueness is required.';
    END IF;
END $$;

-- ----------------------------------------------------------------------
-- Summary (informational; the DO block above is what fails the install)
-- ----------------------------------------------------------------------
\echo ''
\echo 'preflight: public.users — OK'
SELECT
    (SELECT count(*) FROM public.users)                                  AS user_rows,
    (SELECT count(*) FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users')               AS columns,
    (SELECT string_agg(DISTINCT role, ', ') FROM public.users)          AS roles_in_use,
    (SELECT count(*) FROM public.users WHERE NOT is_active)             AS deactivated,
    (SELECT count(*) FROM public.users WHERE NOT is_local)              AS not_local;
\echo ''
\echo 'Reminder: the mail server keeps external correspondents in its own'
\echo 'mailboxes table. public.users must only ever contain humans.'
\echo ''
