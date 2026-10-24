-- 014_conversations_is_group.sql (2026-10-23)
--
-- Per Mal 2026-10-23 07:25:
--   "the conversation run in the group chat is a separate conversation id
--    associated with that group (not with any single agent) and you need
--    to make sure that the agent DM conversations with a user doesn't
--    accidentally load the group conversation id, even though it may be
--    the most recent conversation id involving that agent. Add a is_group
--    boolean to the conversation table if you need to keep the code as
--    clean as possible."
--
-- Where the group conversation lives
-- ---------------------------------
-- public.groups.conversation_id OWNS the group conversation id. It is
-- not owned by any single agent. The id is materialised as a row in
-- each participating agent's <agent>.conversations table so that:
--   * the agent's own turns have a conversation to hang off (memory
--     chunks, the retraining export and the interrupted-turn guard all
--     key off conversation_id), and
--   * <agent>.turns keeps its REFERENCES ... conversations(id) contract
--     on a fresh install (the live DB does not carry that FK, but the
--     canonical agent schema files do).
-- Those rows are marked is_group = TRUE, so every DM lookup path can
-- exclude them with one extra predicate instead of relying on
-- "group rows happen to have user_id IS NULL".
--
-- DM conversations are per user
-- ------------------------------
-- <agent>.conversations.user_id holds the DM user's id, and
-- get_or_create_persistent_conversation() filters on it, so two users
-- never share a DM conversation with the same agent. That behaviour is
-- unchanged by this migration; the is_group filter just makes the
-- group/DM split explicit instead of implicit.
--
-- Idempotent + safe on a fresh install: tools/migrate.sh applies
-- migrations/*.sql BEFORE migrations/agents/*.sql, so the agent schemas
-- may not exist yet. The DO block skips the ones that don't.
--
-- Run as the postgres superuser (the operator role cannot ALTER the
-- agent schemas' tables):
--   psql -d peristyle -f migrations/014_conversations_is_group.sql

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
    FOREACH s IN ARRAY string_to_array(current_setting('harness.agent_schemas'), ',') LOOP
        IF to_regclass(s || '.conversations') IS NULL THEN
            RAISE NOTICE 'skipping %: schema not installed yet', s;
            CONTINUE;
        END IF;

        -- The flag.
        EXECUTE format(
            'ALTER TABLE %I.conversations
                ADD COLUMN IF NOT EXISTS is_group BOOLEAN NOT NULL DEFAULT FALSE', s);

        -- Mark the group conversations that already exist
        -- (metadata->>'kind' = 'group' is what
        -- GroupStore.ensure_agent_conversation writes; the group
        -- conversation id is also in public.groups, so the second
        -- condition catches rows whose metadata was lost).
        EXECUTE format(
            'UPDATE %I.conversations SET is_group = TRUE
              WHERE metadata->>''kind'' = ''group''
                 OR id IN (SELECT conversation_id FROM public.groups)', s);

        -- The DM lookup index. The hot DM path
        -- (get_or_create_persistent_conversation) looks up "the most
        -- recent ACTIVE conversation for (agent, user)"; the partial
        -- index now excludes group conversations, so the lookup only
        -- ever scans DM rows.
        EXECUTE format('DROP INDEX IF EXISTS %I.conversations_active_lookup_idx', s);
        EXECUTE format(
            'CREATE INDEX conversations_active_lookup_idx
                 ON %I.conversations (user_id, updated_at DESC)
               WHERE archived_at IS NULL AND is_group = FALSE', s);
    END LOOP;
END $$;

-- --- verification ----------------------------------------------------------
-- Expect: 0 group conversations in the DM lookup for any user, and the
-- group conversation id present in public.groups flagged is_group=true
-- in every agent schema that took part.
--   SELECT c.user_id, count(*) FROM <agent>.conversations c
--    WHERE c.is_group AND c.user_id IS NOT NULL GROUP BY 1;   -- expect 0 rows
--   SELECT id, is_group, metadata FROM <agent>.conversations
--    WHERE id = (SELECT conversation_id FROM public.groups LIMIT 1);
