-- Phase 14 (2026-10-17): entity_icons table for per-agent avatars.
--
-- Per Mal 2026-10-17: the burger menu on each entity chat page should
-- let the user upload/replace an icon. Stored on the filesystem under
-- app/static/entity-icons/ (or wherever the deployment stores uploads),
-- with this table tracking the metadata + filename so the API can
-- serve the right file.
--
-- The icon is optional — if no row exists, the HMI falls back to a
-- colored letter placeholder.

CREATE TABLE IF NOT EXISTS public.entity_icons (
    entity_name  TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    mime         TEXT NOT NULL,
    size_bytes   BIGINT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by   INT REFERENCES public.users(id) ON DELETE SET NULL
);

-- Grants: harness_app needs read/write on this table. Done.
