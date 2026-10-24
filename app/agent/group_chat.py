"""Group chat orchestration — @mentions drive the turns.

Added 2026-10-23 (per Mal): "in the group chat, agents will only respond
if there is an @<agent_name>, but multiple agents can be mentioned in one
message (so eg. '@agent1 @agent3, please update your git repos' -> agent1 and
agent3 would both have a turn)".

How a group message is handled:

  1. The message is appended to public.group_messages (the canonical
     transcript) as a human message.
  2. The mentions are parsed. Only agents in the group's roster
     (public.group_agents) count; anything else is reported back as an
     unknown mention.
  3. Each mentioned agent takes a turn, in mention order. An agent's
     prompt is: [older group transcript] + [this message] + [what the
     other mentioned agents already said in this same message]. So the
     second agent sees the first agent's answer, which is what makes a
     group chat a group chat.
  4. Each agent's reply is appended to the transcript (author_type
     = 'agent'). The agent's own copy of the turns also lands in its
     own <agent>.turns table, via the normal agent loop -- so agent
     memory, the retraining export and the interrupted-turn guard all
     keep working unchanged.

The turns run SEQUENTIALLY on purpose: the transcript is the shared
state, and a sequential run keeps the order deterministic and lets each
agent read the previous agent's reply.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

log = logging.getLogger(__name__)

# An @mention is an @ followed by a word. The negative lookbehind keeps
# email addresses (me@example.com) and @@ from matching.
MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9_\-]{0,39})")

# How many transcript entries of history each agent sees.
MAX_CONTEXT_ENTRIES = 40


def parse_mentions(text: str, allowed_agents: Iterable[str]) -> tuple[list[str], list[str]]:
    """Parse @mentions out of a message.

    Returns (matched, unknown):
      matched  — agent names (from `allowed_agents`) that were mentioned,
                 in the order they first appear, de-duplicated.
      unknown  — @tokens that look like mentions but are not in the
                 group's roster (typos, agents from another group, ...).
                 The caller surfaces these so the user gets feedback
                 instead of silence.

    "@all" is expanded to every allowed agent.
    """
    allowed = [a for a in (allowed_agents or []) if a]
    lower_map = {a.lower(): a for a in allowed}
    matched: list[str] = []
    unknown: list[str] = []
    for raw in MENTION_RE.findall(text or ""):
        token = raw.lower()
        if token == "all":
            for agent in allowed:
                if agent not in matched:
                    matched.append(agent)
            continue
        agent = lower_map.get(token)
        if agent:
            if agent not in matched:
                matched.append(agent)
        elif raw not in unknown:
            unknown.append(raw)
    return matched, unknown


def render_entry(entry: dict, self_agent: str) -> dict:
    """Render one transcript entry as an LLM message.

    The agent's own past replies are rendered as `assistant` messages
    (that IS its own output); everything else is rendered as a `user`
    message with the speaker in front, so the model can tell who said
    what:
        [the operator] please update the repos
        [agent1] done, repos updated
    """
    author_type = entry.get("author_type")
    author = entry.get("author_name") or ""
    content = entry.get("content") or ""
    if author_type == "agent":
        if author == self_agent:
            return {"role": "assistant", "content": content}
        return {"role": "user", "content": f"[{author}] {content}"}
    if author_type == "system":
        return {"role": "user", "content": f"[system] {content}"}
    return {"role": "user", "content": f"[{author or 'user'}] {content}"}


def build_context(
    messages: list[dict],
    current_message_id: int,
    self_agent: str,
    max_entries: int = MAX_CONTEXT_ENTRIES,
) -> tuple[list[dict], list[dict]]:
    """Split the transcript around the message that is being answered.

    Returns (before, after):
      before — the older part of the conversation (everything that
               happened before this message).
      after  — what has already been said FOR this message, i.e. the
               replies of the agents that already took their turn.

    The caller puts `before` and the fresh user message in the prompt,
    and `after` after it.
    """
    before: list[dict] = []
    after: list[dict] = []
    for m in messages:
        mid = m.get("id")
        if mid == current_message_id:
            continue
        if mid is not None and current_message_id is not None and mid < current_message_id:
            before.append(m)
        else:
            after.append(m)
    before = before[-max_entries:]
    after = after[-max_entries:]
    return (
        [render_entry(m, self_agent) for m in before],
        [render_entry(m, self_agent) for m in after],
    )


def group_notice(text: str) -> dict:
    """A system line in the transcript."""
    return {"type": "notice", "message": text}


def run_group_message(
    *,
    store: Any,
    group: Any,
    user: Any,
    user_text: str,
    attachments: list[dict] | None = None,
    request_id: str | None = None,
    allowed_agents: list[str] | None = None,
    loop: Any = None,
    mention_limit: int = 200,
) -> Iterable[dict]:
    """Handle one message in a group. Yields event dicts.

    Event types (the client renders them):
      group_message  — a transcript row was written (human or agent)
      agent_start    — an agent is about to take its turn
      agent_done     — the agent's turn finished
      notice         — a human-readable note (no agent mentioned, ...)
      plus every event the agent loop emits (iter_start / delta /
      iter_text / tool_start / tool_end / nudge / final / error), each
      tagged with the `agent` it belongs to.
    """
    from flask import current_app

    if loop is None:
        loop = current_app.extensions["harness"]["loop"]

    agents = list(allowed_agents or [])
    matched, unknown = parse_mentions(user_text, agents)

    # 1. the human message goes into the transcript first, so it survives
    #    even if every agent turn fails.
    message_id = store.add_message(
        group.id,
        author_type="user",
        content=user_text,
        author_id=getattr(user, "id", None),
        author_name=getattr(user, "display_name", "") or getattr(user, "email", ""),
        attachments=attachments or [],
        mentions=matched,
    )
    yield {
        "type": "group_message",
        "message": {
            "id": message_id,
            "author_type": "user",
            "author_id": getattr(user, "id", None),
            "author_name": getattr(user, "display_name", "") or getattr(user, "email", ""),
            "content": user_text,
            "attachments": attachments or [],
            "mentions": matched,
        },
    }

    if unknown:
        yield group_notice(
            "Ignored unknown mention(s): " + ", ".join("@" + u for u in unknown)
        )

    if not matched:
        yield group_notice(
            "No agent mentioned — use @<agent> to get one to answer "
            "(available: " + ", ".join("@" + a for a in agents) + ")."
        )
        return

    # 2. each mentioned agent takes a turn, in mention order.
    for agent in matched:
        yield {"type": "agent_start", "agent": agent}
        try:
            store.ensure_agent_conversation(agent, group.conversation_id, group.name)
        except Exception as exc:
            log.exception("ensure_agent_conversation failed: agent=%s group=%s", agent, group.id)
            yield {"type": "error", "agent": agent,
                   "message": f"could not open the conversation for {agent}: {exc}"}
            continue

        # The transcript is re-read per agent so the next agent sees the
        # previous agent's reply.
        messages = store.list_messages(group.id, limit=200)
        before, after = build_context(messages, message_id, agent)

        final_text = ""
        failed = False
        for ev in loop.handle_chat_streaming(
            user_text=user_text,
            agent_name=agent,
            user_id=getattr(user, "id", None),
            attachments=attachments or [],
            request_id=request_id,
            conversation_id=group.conversation_id,
            history_override=before,
            trailing_override=after,
            skip_auto_title=True,
        ):
            if isinstance(ev, dict):
                ev = dict(ev)
                ev["agent"] = agent
                if ev.get("type") == "final":
                    resp = ev.get("response")
                    if resp is not None:
                        final_text = getattr(resp, "assistant", "") or ""
            yield ev
            if isinstance(ev, dict) and ev.get("type") == "error":
                failed = True

        if final_text:
            row_id = store.add_message(
                group.id,
                author_type="agent",
                content=final_text,
                author_id=None,
                author_name=agent,
                attachments=[],
                mentions=[agent],
            )
            yield {
                "type": "group_message",
                "message": {
                    "id": row_id,
                    "author_type": "agent",
                    "author_id": None,
                    "author_name": agent,
                    "content": final_text,
                    "attachments": [],
                    "mentions": [agent],
                },
            }
        elif failed:
            yield group_notice(f"{agent} did not answer — see the error above.")
        yield {"type": "agent_done", "agent": agent}
