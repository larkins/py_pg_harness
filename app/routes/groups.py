"""Group chat API — /api/v1/groups/*.

Added 2026-10-23 (per Mal): "implement a new group table and a create
group chat feature for admin users. They can create a group, with any
users from the public.users table. Any user added to a group will see it
appear as another row in the landing dash that lists the current agents.
And any of the available agents."

So:
  * only admins create groups (POST /api/v1/groups),
  * members (humans from public.users) + agents make up the group,
  * members see the group on the landing page and can chat in it,
  * in the group chat an agent only answers when it is @mentioned,
    and several agents can be mentioned in one message.

Access rules:
  * any logged-in user can list the groups they are a member of,
  * only members can read a group's transcript or post to it,
  * only admins create groups, change membership or the agent roster.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from queue import Empty, Queue

from flask import Blueprint, current_app, g, jsonify, request
from uuid import UUID

from app.auth import current_user, login_required_json

log = logging.getLogger(__name__)

bp = Blueprint("groups", __name__, url_prefix="/api/v1")


def _store():
    """The shared GroupStore (built once at app startup)."""
    return current_app.extensions["harness"]["group_store"]


def _is_admin(user) -> bool:
    return bool(user) and getattr(user, "role", "member") == "admin"


def _require_admin():
    """Return (user, error_response). error_response is None when OK."""
    user = current_user()
    if user is None:
        return None, (jsonify({"error": "login required", "code": "auth"}), 401)
    if not _is_admin(user):
        return None, (jsonify({"error": "admin role required"}), 403)
    return user, None


def _require_member(group_id: str):
    """Return (user, group, error_response)."""
    user = current_user()
    if user is None:
        return None, None, (jsonify({"error": "login required", "code": "auth"}), 401)
    store = _store()
    group = store.get(group_id)
    if group is None:
        return None, None, (jsonify({"error": "group not found"}), 404)
    if not store.is_member(group_id, user.id):
        return None, None, (jsonify({"error": "you are not a member of this group"}), 403)
    group.my_role = store.membership(group_id, user.id)
    return user, group, None


def _installed_agents() -> list[str]:
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    registry = current_app.extensions["harness"].get("bootstrap_registry")
    if registry is not None:
        return list(registry.list_agents())
    return list(cfg.app.agents or ())


# --- roster helpers -------------------------------------------------------

@bp.route("/agents", methods=["GET"])
@login_required_json
def agents_list():
    """The installed agent roster (used by the new-group dialog)."""
    return jsonify({"agents": _installed_agents()}), 200


@bp.route("/admin/users", methods=["GET"])
@login_required_json
def admin_users():
    """Every user in public.users — the pool a group can be built from.

    Admin only: it is the same table the harness authenticates against,
    so it holds email addresses. Ordinary members never need it.
    """
    user, err = _require_admin()
    if err:
        return err
    return jsonify({"users": _store().list_selectable_users()}), 200


# --- groups ---------------------------------------------------------------

@bp.route("/groups", methods=["GET"])
@login_required_json
def groups_list():
    """The groups the current user is a member of (landing page rows)."""
    user = current_user()
    groups = _store().list_for_user(user.id)
    return jsonify({
        "groups": [g.to_dict() for g in groups],
        "is_admin": _is_admin(user),
    }), 200


@bp.route("/groups", methods=["POST"])
@login_required_json
def groups_create():
    """Create a group (admin only).

    Body: {"name": "...", "description": "...", "member_ids": [1, 2],
           "agent_names": ["agent1", "agent3"]}

    The creator is always a member (role=owner). If no agent roster is
    given, every installed agent is added.
    """
    user, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    description = (data.get("description") or "").strip()

    member_ids = data.get("member_ids") or []
    if not isinstance(member_ids, list):
        return jsonify({"error": "member_ids must be a list"}), 400
    try:
        member_ids = [int(m) for m in member_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "member_ids must be a list of user ids"}), 400

    agent_names = data.get("agent_names") or []
    if not isinstance(agent_names, list):
        return jsonify({"error": "agent_names must be a list"}), 400
    installed = _installed_agents()
    agent_names = [str(a) for a in agent_names if str(a) in installed]
    if not agent_names:
        # No explicit roster -> every installed agent can be mentioned.
        agent_names = installed

    # Only real, active users can be added to a group.
    store = _store()
    known = {u["id"] for u in store.list_selectable_users() if u["is_active"]}
    valid_members = [m for m in member_ids if m in known]

    try:
        group = store.create_group(
            name=name,
            description=description,
            member_ids=valid_members,
            agent_names=agent_names,
            created_by=user.id,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"group": group.to_dict()}), 201


@bp.route("/groups/<uuid:group_id>", methods=["GET"])
@login_required_json
def group_get(group_id: UUID):
    """One group with its members + agent roster (members only)."""
    _, group, err = _require_member(str(group_id))
    if err:
        return err
    return jsonify({"group": group.to_dict()}), 200


@bp.route("/groups/<uuid:group_id>", methods=["DELETE"])
@login_required_json
def group_archive(group_id: UUID):
    """Archive a group (admin only). The rows stay for the record."""
    user, err = _require_admin()
    if err:
        return err
    ok = _store().archive(str(group_id))
    if not ok:
        return jsonify({"error": "group not found (or already archived)"}), 404
    return jsonify({"ok": True}), 200


@bp.route("/groups/<uuid:group_id>/members", methods=["POST"])
@login_required_json
def group_add_members(group_id: UUID):
    """Add users to a group (admin only). Body: {"member_ids": [...]}"""
    user, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    member_ids = data.get("member_ids") or []
    if not isinstance(member_ids, list):
        return jsonify({"error": "member_ids must be a list"}), 400
    try:
        member_ids = [int(m) for m in member_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "member_ids must be a list of user ids"}), 400
    store = _store()
    known = {u["id"] for u in store.list_selectable_users() if u["is_active"]}
    valid = [m for m in member_ids if m in known]
    added = store.add_members(str(group_id), valid, user.id)
    group = store.get(str(group_id))
    return jsonify({"added": added, "group": group.to_dict() if group else None}), 200


@bp.route("/groups/<uuid:group_id>/members/<int:user_id>", methods=["DELETE"])
@login_required_json
def group_remove_member(group_id: UUID, user_id: int):
    """Remove a user from a group (admin only)."""
    user, err = _require_admin()
    if err:
        return err
    store = _store()
    if not store.remove_member(str(group_id), user_id):
        return jsonify({"error": "user is not a member of this group"}), 404
    group = store.get(str(group_id))
    return jsonify({"ok": True, "group": group.to_dict() if group else None}), 200


@bp.route("/groups/<uuid:group_id>/agents", methods=["PUT"])
@login_required_json
def group_set_agents(group_id: UUID):
    """Replace the group's agent roster (admin only).

    Body: {"agent_names": ["agent1", "agent3"]}
    """
    user, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    agent_names = data.get("agent_names") or []
    if not isinstance(agent_names, list):
        return jsonify({"error": "agent_names must be a list"}), 400
    installed = _installed_agents()
    names = [str(a) for a in agent_names if str(a) in installed]
    store = _store()
    if store.get(str(group_id)) is None:
        return jsonify({"error": "group not found"}), 404
    store.set_agents(str(group_id), names)
    group = store.get(str(group_id))
    return jsonify({"group": group.to_dict()}), 200


@bp.route("/groups/<uuid:group_id>/messages", methods=["GET"])
@login_required_json
def group_messages(group_id: UUID):
    """The group transcript, oldest first."""
    _, group, err = _require_member(str(group_id))
    if err:
        return err
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    messages = _store().list_messages(group.id, limit=limit)
    return jsonify({"group_id": group.id, "messages": messages}), 200


@bp.route("/groups/<uuid:group_id>/chat", methods=["POST"])
@login_required_json
def group_chat(group_id: UUID):
    """Post a message to a group and run the mentioned agents.

    Body: {"message": "@agent1 @agent3, please update your git repos"}

    Only the @mentioned agents take a turn, in mention order. Each one
    sees the group transcript plus what the other mentioned agents
    already said for this same message.

    Streams NDJSON when the client asks for it (application/x-ndjson),
    the same way the 1:1 entity chat does; otherwise the whole thing is
    buffered and returned as JSON.
    """
    user, group, err = _require_member(str(group_id))
    if err:
        return err
    data = request.get_json(silent=True) or {}
    user_text = (data.get("message") or "").strip()
    raw_attachments = data.get("attachments") or []
    if not isinstance(raw_attachments, list):
        return jsonify({"error": "attachments must be a list"}), 400
    if not user_text and not raw_attachments:
        return jsonify({"error": "message is required"}), 400
    attachments: list[dict] = []
    for a in raw_attachments:
        if not isinstance(a, dict):
            return jsonify({"error": "each attachment must be an object"}), 400
        if not a.get("url") or not a.get("filename"):
            return jsonify({"error": "each attachment needs url and filename"}), 400
        attachments.append({
            "url": str(a["url"])[:512],
            "filename": str(a["filename"])[:256],
            "content_type": str(a.get("content_type", ""))[:128],
            "size": int(a.get("size", 0) or 0),
        })

    request_id = getattr(g, "request_id", None) or uuid.uuid4().hex[:12]
    store = _store()
    agents = group.agents or _installed_agents()

    from app.agent import group_chat as gc

    def _runner():
        return gc.run_group_message(
            store=store,
            group=group,
            user=user,
            user_text=user_text,
            attachments=attachments,
            request_id=request_id,
            allowed_agents=agents,
        )

    accept = (request.headers.get("Accept") or "").lower()
    wants_stream = "application/x-ndjson" in accept

    if not wants_stream:
        # Buffered path: run every turn, collect the replies.
        replies = []
        notices = []
        for ev in _runner():
            if ev.get("type") == "group_message" and ev.get("message", {}).get("author_type") == "agent":
                replies.append(ev["message"])
            elif ev.get("type") == "notice":
                notices.append(ev.get("message"))
        return jsonify({
            "group_id": group.id,
            "replies": replies,
            "notices": notices,
        }), 200

    # Streaming path. The agent loop runs in a worker thread (it can take
    # minutes); the generator drains a queue and emits a keepalive
    # comment every 30s so Cloudflare's 125s edge timeout doesn't cut
    # the connection.
    from flask import Response, stream_with_context

    KEEPALIVE_INTERVAL = 30
    q: Queue = Queue()
    done = threading.Event()
    app_obj = current_app._get_current_object()

    def pump():
        try:
            with app_obj.app_context():
                for ev in _runner():
                    q.put(("event", ev))
        except Exception as exc:   # noqa: BLE001 - surfaced to the client
            log.exception("group chat failed: group=%s", group.id)
            q.put(("error", exc))
        finally:
            done.set()

    def _generate_ndjson():
        t = threading.Thread(target=pump, daemon=True, name="group-chat")
        t.start()
        while True:
            try:
                kind, payload = q.get(timeout=KEEPALIVE_INTERVAL)
            except Empty:
                yield "# keepalive\n"
                continue
            if kind == "error":
                yield json.dumps({
                    "type": "error",
                    "message": f"{type(payload).__name__}: {payload}",
                }) + "\n"
                break
            event = payload
            if isinstance(event, dict):
                event.setdefault("request_id", request_id)
                if event.get("type") == "final":
                    # The loop's final event carries a ChatResponse
                    # object; flatten it the same way the 1:1 entity
                    # chat does (otherwise json.dumps turns it into a
                    # useless repr string).
                    resp = event.get("response")
                    if resp is not None and hasattr(resp, "__dict__"):
                        event["response"] = {
                            "conversation_id": str(getattr(resp, "conversation_id", "")),
                            "assistant": getattr(resp, "assistant", ""),
                            "model": getattr(resp, "model", ""),
                            "latency_ms": getattr(resp, "latency_ms", 0),
                            "tool_iterations": getattr(resp, "tool_iterations", 0),
                            "retrieved_chunk_ids": list(getattr(resp, "retrieved_chunk_ids", []) or []),
                            "attachments": list(getattr(resp, "attachments", None) or []),
                        }
            yield json.dumps(event, default=str) + "\n"
            if event.get("type") == "error":
                break
            if done.is_set() and q.empty():
                break

    return Response(
        stream_with_context(_generate_ndjson()),
        content_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
