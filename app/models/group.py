"""Group chat store — persistence for public.groups & friends.

Added 2026-10-23 (per Mal): group chats. An admin creates a group from
the landing page; the group has

  * members  — humans picked from public.users (the canonical user table
    of the peristyle DB), and
  * agents   — members of the harness's installed agent roster (agent1,
    agent2, agent3, ...) who can be @mentioned in the group's chat.

The canonical transcript is public.group_messages: one row per message
with an explicit author (human / agent / system). Every agent that
takes a turn in the group ALSO writes its own copy of the turns into
its own <agent>.turns table (via the normal agent loop), so agent
memory + the retraining export keep working unchanged.

The group has ONE conversation id (public.groups.conversation_id). It
is reused as the conversation id in each participating agent's schema,
which is how the agent loop writes its turns without a new conversation
per agent.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Agent names end up in SQL (the <agent>.conversations table name), so
# they are validated against this pattern before they are interpolated.
# The installed agent roster is a config value, but the name still
# travels through the DB (public.group_agents), so it is checked here
# too -- defence in depth for a table name.
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass
class Group:
    """One group chat."""

    id: str
    name: str
    description: str
    conversation_id: str
    created_by: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    # Populated by the store when it loads the group.
    members: list[dict] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    last_message: str | None = None
    last_at: datetime | None = None
    message_count: int = 0
    member_count: int = 0
    my_role: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "conversation_id": self.conversation_id,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "members": self.members,
            "agents": self.agents,
            "last_message": self.last_message,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "message_count": self.message_count,
            "member_count": self.member_count,
            "my_role": self.my_role,
        }


class GroupStore:
    """DB access for groups. Uses the harness's shared psycopg pool."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    # --- read helpers ----------------------------------------------------

    @staticmethod
    def _row_to_group(row: dict) -> Group:
        return Group(
            id=str(row["id"]),
            name=row["name"],
            description=row.get("description") or "",
            conversation_id=str(row["conversation_id"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row.get("archived_at"),
            last_message=row.get("last_message"),
            last_at=row.get("last_at"),
            message_count=int(row.get("message_count") or 0),
            member_count=int(row.get("member_count") or 0),
            my_role=row.get("my_role"),
        )
    def _fetch_members(self, group_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.id, u.email, u.display_name, u.role,
                           u.is_active, gm.role AS group_role, gm.joined_at
                      FROM public.group_members gm
                      JOIN public.users u ON u.id = gm.user_id
                     WHERE gm.group_id = %s
                     ORDER BY u.display_name, u.id
                    """,
                    (group_id,),
                )
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "user_id": r[0],
                "email": r[1],
                "display_name": r[2],
                "role": r[3],
                "is_active": r[4],
                "group_role": r[5],
                "joined_at": r[6].isoformat() if r[6] else None,
            })
        return out

    def _fetch_agents(self, group_id: str) -> list[str]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT agent_name FROM public.group_agents WHERE group_id = %s ORDER BY agent_name",
                    (group_id,),
                )
                return [r[0] for r in cur.fetchall()]

    def get(self, group_id: str, with_detail: bool = True) -> Group | None:
        """Load one group (without members/agents unless with_detail)."""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT g.id, g.name, g.description, g.conversation_id,
                           g.created_by, g.created_at, g.updated_at, g.archived_at,
                           (SELECT content FROM public.group_messages gm
                             WHERE gm.group_id = g.id ORDER BY gm.id DESC LIMIT 1) AS last_message,
                           (SELECT created_at FROM public.group_messages gm
                             WHERE gm.group_id = g.id ORDER BY gm.id DESC LIMIT 1) AS last_at,
                           (SELECT COUNT(*) FROM public.group_messages gm
                             WHERE gm.group_id = g.id) AS message_count,
                           (SELECT COUNT(*) FROM public.group_members gm2
                             WHERE gm2.group_id = g.id) AS member_count
                      FROM public.groups g
                     WHERE g.id = %s
                    """,
                    (group_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                names = [
                    "id", "name", "description", "conversation_id", "created_by",
                    "created_at", "updated_at", "archived_at", "last_message",
                    "last_at", "message_count", "member_count",
                ]
                group = self._row_to_group(dict(zip(names, row)))
        if with_detail:
            group.members = self._fetch_members(group.id)
            group.agents = self._fetch_agents(group.id)
        return group

    def list_for_user(self, user_id: int, include_archived: bool = False) -> list[Group]:
        """Every group the given user is a member of, most recent first."""
        sql = """
            SELECT g.id, g.name, g.description, g.conversation_id,
                   g.created_by, g.created_at, g.updated_at, g.archived_at,
                   (SELECT content FROM public.group_messages gm
                     WHERE gm.group_id = g.id ORDER BY gm.id DESC LIMIT 1) AS last_message,
                   (SELECT created_at FROM public.group_messages gm
                     WHERE gm.group_id = g.id ORDER BY gm.id DESC LIMIT 1) AS last_at,
                   (SELECT COUNT(*) FROM public.group_messages gm
                     WHERE gm.group_id = g.id) AS message_count,
                   (SELECT COUNT(*) FROM public.group_members gm2
                     WHERE gm2.group_id = g.id) AS member_count,
                   (SELECT array_agg(ga.agent_name ORDER BY ga.agent_name)
                      FROM public.group_agents ga WHERE ga.group_id = g.id) AS agents,
                   gm.role AS my_role
              FROM public.groups g
              JOIN public.group_members gm ON gm.group_id = g.id AND gm.user_id = %s
             WHERE (%s = TRUE OR g.archived_at IS NULL)
             ORDER BY COALESCE(
                        (SELECT created_at FROM public.group_messages gm3
                          WHERE gm3.group_id = g.id ORDER BY gm3.id DESC LIMIT 1),
                        g.created_at) DESC
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (user_id, include_archived))
                rows = cur.fetchall()
        names = [
            "id", "name", "description", "conversation_id", "created_by",
            "created_at", "updated_at", "archived_at", "last_message",
            "last_at", "message_count", "member_count", "agents", "my_role",
        ]
        groups = []
        for r in rows:
            group = self._row_to_group(dict(zip(names, r)))
            group.agents = list(r[12] or [])
            groups.append(group)
        return groups

    def membership(self, group_id: str, user_id: int) -> str | None:
        """The user's role in the group ('owner' / 'member') or None."""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM public.group_members WHERE group_id = %s AND user_id = %s",
                    (group_id, user_id),
                )
                row = cur.fetchone()
        return row[0] if row else None

    def is_member(self, group_id: str, user_id: int) -> bool:
        return self.membership(group_id, user_id) is not None

    # --- write helpers ----------------------------------------------------

    def create_group(
        self,
        *,
        name: str,
        description: str = "",
        member_ids: Iterable[int] | None = None,
        agent_names: Iterable[str] | None = None,
        created_by: int,
    ) -> Group:
        """Create a group. The creator is always added as 'owner'."""
        name = (name or "").strip()
        if not name:
            raise ValueError("group name is required")
        if len(name) > 120:
            raise ValueError("group name must be 120 characters or fewer")
        description = (description or "").strip()[:2000]
        group_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        member_ids = list(dict.fromkeys(int(m) for m in (member_ids or [])))
        agent_names = list(dict.fromkeys(str(a) for a in (agent_names or [])))
        if created_by not in member_ids:
            member_ids.insert(0, created_by)
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.groups
                        (id, name, description, conversation_id, created_by)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (group_id, name, description, conversation_id, created_by),
                )
                cur.fetchone()
                for uid in member_ids:
                    cur.execute(
                        """
                        INSERT INTO public.group_members (group_id, user_id, role, added_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (group_id, user_id) DO NOTHING
                        """,
                        (group_id, uid, "owner" if uid == created_by else "member", created_by),
                    )
                for agent in agent_names:
                    cur.execute(
                        """
                        INSERT INTO public.group_agents (group_id, agent_name)
                        VALUES (%s, %s)
                        ON CONFLICT (group_id, agent_name) DO NOTHING
                        """,
                        (group_id, agent),
                    )
            conn.commit()
        log.info("group created: id=%s name=%r members=%d agents=%s",
                 group_id, name, len(member_ids), agent_names)
        return self.get(group_id)

    def add_members(self, group_id: str, user_ids: Iterable[int], added_by: int) -> list[int]:
        """Add users to a group. Returns the ids that were actually added."""
        added: list[int] = []
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                for uid in dict.fromkeys(int(u) for u in (user_ids or [])):
                    cur.execute(
                        """
                        INSERT INTO public.group_members (group_id, user_id, role, added_by)
                        VALUES (%s, %s, 'member', %s)
                        ON CONFLICT (group_id, user_id) DO NOTHING
                        RETURNING user_id
                        """,
                        (group_id, uid, added_by),
                    )
                    if cur.fetchone() is not None:
                        added.append(uid)
                cur.execute(
                    "UPDATE public.groups SET updated_at = now() WHERE id = %s",
                    (group_id,),
                )
            conn.commit()
        return added

    def remove_member(self, group_id: str, user_id: int) -> bool:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.group_members WHERE group_id = %s AND user_id = %s",
                    (group_id, user_id),
                )
                deleted = cur.rowcount > 0
                cur.execute(
                    "UPDATE public.groups SET updated_at = now() WHERE id = %s",
                    (group_id,),
                )
            conn.commit()
        return deleted

    def set_agents(self, group_id: str, agent_names: Iterable[str]) -> list[str]:
        """Replace the group's agent roster."""
        names = list(dict.fromkeys(str(a) for a in (agent_names or [])))
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.group_agents WHERE group_id = %s", (group_id,))
                for agent in names:
                    cur.execute(
                        "INSERT INTO public.group_agents (group_id, agent_name) VALUES (%s, %s)",
                        (group_id, agent),
                    )
                cur.execute(
                    "UPDATE public.groups SET updated_at = now() WHERE id = %s",
                    (group_id,),
                )
            conn.commit()
        return names

    def archive(self, group_id: str) -> bool:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.groups SET archived_at = now(), updated_at = now() "
                    "WHERE id = %s AND archived_at IS NULL",
                    (group_id,),
                )
                ok = cur.rowcount > 0
            conn.commit()
        return ok

    # --- transcript --------------------------------------------------------

    def add_message(
        self,
        group_id: str,
        *,
        author_type: str,
        content: str,
        author_id: int | None = None,
        author_name: str = "",
        attachments: list[dict] | None = None,
        mentions: list[str] | None = None,
    ) -> int:
        """Append a message to the group transcript. Returns the new row id."""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.group_messages
                        (group_id, author_type, author_id, author_name,
                         content, attachments, mentions)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id
                    """,
                    (
                        group_id,
                        author_type,
                        author_id,
                        author_name or "",
                        content,
                        json.dumps(attachments or []),
                        list(mentions or []),
                    ),
                )
                row = cur.fetchone()
                cur.execute(
                    "UPDATE public.groups SET updated_at = now() WHERE id = %s",
                    (group_id,),
                )
            conn.commit()
        return int(row[0])

    def list_messages(self, group_id: str, limit: int = 50) -> list[dict]:
        """The last N messages, oldest first (the order the UI renders)."""
        limit = max(1, min(int(limit), 500))
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, author_type, author_id, author_name, content,
                           attachments, mentions, created_at
                      FROM public.group_messages
                     WHERE group_id = %s
                     ORDER BY id DESC
                     LIMIT %s
                    """,
                    (group_id, limit),
                )
                rows = cur.fetchall()
        out = []
        for r in reversed(rows):
            atts = r[5] if isinstance(r[5], list) else (json.loads(r[5]) if r[5] else [])
            out.append({
                "id": r[0],
                "author_type": r[1],
                "author_id": r[2],
                "author_name": r[3],
                "content": r[4],
                "attachments": atts,
                "mentions": list(r[6] or []),
                "created_at": r[7].isoformat() if r[7] else None,
            })
        return out

    # --- users -------------------------------------------------------------

    def list_selectable_users(self) -> list[dict]:
        """Every user in public.users that can be added to a group.

        Used by the "new group" dialog (admin only). Inactive users are
        listed too but flagged, so an admin can still see who exists.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, display_name, role, is_active
                      FROM public.users
                     ORDER BY display_name, id
                    """
                )
                rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "email": r[1],
                "display_name": r[2],
                "role": r[3],
                "is_active": r[4],
            }
            for r in rows
        ]

    # --- agent-side conversation ------------------------------------------

    def ensure_agent_conversation(self, agent_name: str, conversation_id: str, title: str) -> None:
        """Make sure the agent has a conversation row for this group.

        The group has ONE conversation id shared by every participating
        agent. Each agent gets its own row in <agent>.conversations with
        that id, so the agent loop can write its turns into its own
        schema (and therefore its own memory + retraining corpus).

        user_id is NULL on purpose: a group conversation belongs to the
        group, not to one person -- otherwise the 1:1 entity list would
        start showing group chatter as if it were a private chat.

        is_group = TRUE (migration 014) is the flag the DM lookup paths
        filter on: a DM must never pick up this conversation, even
        though it is the most recently touched conversation row for
        this agent.
        """
        if not _AGENT_NAME_RE.match(agent_name or ""):
            raise ValueError(f"invalid agent name: {agent_name!r}")
        metadata = json.dumps({"kind": "group", "conversation_id": conversation_id})
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {agent_name}.conversations
                        (id, user_id, agent_name, title, metadata, is_group)
                    VALUES (%s, NULL, %s, %s, %s::jsonb, TRUE)
                    ON CONFLICT (id) DO UPDATE
                        SET title = EXCLUDED.title,
                            metadata = EXCLUDED.metadata,
                            is_group = TRUE
                    """,
                    (conversation_id, agent_name, title, metadata),
                )
            conn.commit()
