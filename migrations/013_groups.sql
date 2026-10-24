-- 013_groups.sql -- group chats for the harness.
--
-- "implement a new group table and a create group chat feature for admin
-- users. They can create a group, with any users from the public.users
-- table. Any user added to a group will see it appear as another row in
-- the landing dash that lists the current agents. And any of the
-- available agents."
--
-- So a group is BOTH a set of human members (from public.users) AND a
-- set of agents (from the harness's installed agent roster). The group
-- has its own conversation; in that conversation an agent only replies
-- when the message mentions it with @<agent_name>. Several agents can
-- be mentioned in one message -- each mentioned agent takes a turn, in
-- mention order, and can see what the other agents already said.
--
-- Schema notes:
--   * public.groups.conversation_id is the ONE conversation id shared by
--     every agent that participates in the group. Each participating
--     agent gets a row with that same id in its own <agent>.conversations
--     table, so the agent's turns (and therefore its memory chunks and
--     the retraining export) stay in that agent's own schema -- the
--     cross-agent view is assembled from public.group_messages.
--   * public.group_messages is the canonical transcript: one row per
--     message, with an explicit author (human / agent / system). This is
--     what the group chat UI renders and what each agent sees as its
--     context.
--
-- psql variables:
--   operator_role  optional  role that owns the tables (default: harness_operator)
--   runtime_role   optional  the runtime role       (default: harness_app)
--
--   psql -v operator_role=<role> -v runtime_role=<role> \
--         -f migrations/013_groups.sql
--
-- The ownership statements need a superuser (or the operator role
-- itself, when it already owns the tables).

\if :{?operator_role}
\else
  \set operator_role 'harness_operator'
\endif
\if :{?runtime_role}
\else
  \set runtime_role 'harness_app'
\endif

-- --- groups ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.groups (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    -- The shared conversation id for this group (one conversation, many agents).
    conversation_id UUID NOT NULL DEFAULT gen_random_uuid(),
    created_by      BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at     TIMESTAMPTZ,
    CONSTRAINT groups_name_len CHECK (char_length(btrim(name)) BETWEEN 1 AND 120)
);

-- --- group members (humans) ----------------------------------------------
CREATE TABLE IF NOT EXISTS public.group_members (
    group_id  UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'member',
    added_by  BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id),
    CONSTRAINT group_members_role_check CHECK (role IN ('owner', 'member'))
);

CREATE INDEX IF NOT EXISTS group_members_user_idx
    ON public.group_members (user_id);

-- --- group agents (the roster a member can @mention) ----------------------
CREATE TABLE IF NOT EXISTS public.group_agents (
    group_id   UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, agent_name)
);

-- --- group transcript -----------------------------------------------------
CREATE TABLE IF NOT EXISTS public.group_messages (
    id          BIGSERIAL PRIMARY KEY,
    group_id    UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
    author_type TEXT NOT NULL,
    author_id   BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    author_name TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
    mentions    TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT group_messages_author_type_check
        CHECK (author_type IN ('user', 'agent', 'system'))
);

CREATE INDEX IF NOT EXISTS group_messages_group_idx
    ON public.group_messages (group_id, id);

-- --- ownership ------------------------------------------------------------
-- The operator role owns every public.* table (a co-hosted service runs
-- unguarded DDL at startup as the operator role).
ALTER TABLE public.groups           OWNER TO :"operator_role";
ALTER TABLE public.group_members    OWNER TO :"operator_role";
ALTER TABLE public.group_agents     OWNER TO :"operator_role";
ALTER TABLE public.group_messages   OWNER TO :"operator_role";
ALTER SEQUENCE public.group_messages_id_seq OWNER TO :"operator_role";

-- Re-runnable: make created_by nullable + SET NULL (a deleted user must
-- not block the deletion, and the group itself survives).
ALTER TABLE public.groups ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE public.groups DROP CONSTRAINT IF EXISTS groups_created_by_fkey;
ALTER TABLE public.groups
    ADD CONSTRAINT groups_created_by_fkey
    FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

-- --- grants ---------------------------------------------------------------
-- The runtime role is the role the app connects as. It needs DML on the
-- group tables and nothing else.
GRANT USAGE ON SCHEMA public TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.groups, public.group_members, public.group_agents, public.group_messages
    TO :"runtime_role";
GRANT USAGE, SELECT ON SEQUENCE public.group_messages_id_seq TO :"runtime_role";

-- --- verification ---------------------------------------------------------
--   SELECT c.relname, pg_get_userbyid(c.relowner) AS owner,
--          has_table_privilege('<runtime_role>', c.oid, 'SELECT')  AS sel,
--          has_table_privilege('<runtime_role>', c.oid, 'INSERT')  AS ins,
--          has_table_privilege('<runtime_role>', c.oid, 'UPDATE')  AS upd,
--          has_table_privilege('<runtime_role>', c.oid, 'DELETE')  AS del
--     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname = 'public' AND c.relname LIKE 'group%';
