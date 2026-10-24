-- Phase 9: seed the first admin user in public.users.
--
-- The email is a psql variable, so no install-specific address is baked
-- into the migration:
--
--   psql -v admin_email=you@example.com -f migrations/003_seed_admin_user.sql
--
-- The default is the generic placeholder admin@example.com.
--
-- public.users.password_hash is NOT NULL, so the seeded row carries a
-- placeholder hash that can never match a password (it is not a secret
-- and it is not a real credential). The operator sets the real password
-- with the create_user CLI, which hashes it and rewrites the row:
--
--   python3 tools/create_user.py \
--       --email you@example.com \
--       --password '...' \
--       --display-name 'Admin' \
--       --role admin
--
-- (create_user needs a DATABASE_URL whose role may INSERT into
-- public.users -- the operator role, not the runtime role.)
--
-- public.users must already exist (it is created by the base install /
-- the canonical users migration -- see REQUIREMENTS_AND_SPECIFICATIONS.md).
-- This migration is idempotent.

\if :{?admin_email}
\else
  \set admin_email 'admin@example.com'
\endif

-- Only seed when public.users is there (on a brand new database the
-- canonical users migration may still have to run).
SELECT CASE WHEN to_regclass('public.users') IS NOT NULL
            THEN 'true' ELSE 'false' END AS has_users \gset
\if :has_users

INSERT INTO public.users (email, password_hash, display_name, role, is_active, created_at, updated_at)
VALUES (:'admin_email',
        'pbkdf2:sha256:260000$placeholder$placeholder',
        'Admin', 'admin', TRUE, now(), now())
ON CONFLICT (email) DO NOTHING;

\else
\echo '003: public.users does not exist yet -- skipped the admin seed (run the canonical users migration first)'
\endif
