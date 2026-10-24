"""API routes — /api/v1/chat, /api/v1/memories/* (debug), /api/v1/conversations/*.

Phase 9: protected routes require login (via @login_required_json).
Phase 10: image uploads via /api/v1/uploads (multipart) and serve
          via GET /api/v1/uploads/<id>.<ext>. Files land in
          HARNESS_UPLOAD_DIR with a UUID-prefixed name; the
          original filename is kept only in the JSONB
          `agent1.turns.attachments` metadata.
"""
from __future__ import annotations

import logging
import os, secrets
import threading
import uuid
from datetime import datetime, timezone
from queue import Empty, Queue
from pathlib import Path
from uuid import UUID

from psycopg import sql
from flask import Blueprint, current_app, g, jsonify, request, send_file, session

log = logging.getLogger(__name__)

bp = Blueprint("api", __name__, url_prefix="/api/v1")

from app.auth import login_required_json, current_user  # noqa: E402
from app.agent import turn_guard  # noqa: E402


# Phase 10: image upload allowlist. Keep tight — anything outside
# this list gets a 415. SVG is intentionally excluded (script payload
# risk); raw camera formats (heic/heif) are allowed.
_ALLOWED_MIME = frozenset({
    # images (Phase 10)
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
    # documents (added 2026-10-19 per Mal's request)
    "application/pdf",
    "text/csv",
    "text/html",
    # OOXML / ODF
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
})
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
    # documents (added 2026-10-19)
    "application/pdf": "pdf",
    "text/csv": "csv",
    "text/html": "html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.oasis.opendocument.text": "odt",
}

# Phase 15 (2026-10-18): broader extension allowlist for the
# /uploads/<id>.<ext> serve endpoint. The attach_file tool
# (app/agent/tools/attach_file.py) writes files to the same
# uploads dir with extensions from this list, so the serve
# route must accept them too. Same auth-gated path, same
# `<32-hex>.<ext>` filename shape.
#
# Note on dangerous content types: the serve endpoint forces
# `as_attachment=True` for ANY non-image extension below, so
# `.html` / `.svg` / `.js` are downloaded rather than rendered
# in the browser. That's the XSS-defence layer: even if a
# malicious user tricks the LLM into attaching an HTML file,
# the browser treats it as a download, not a same-origin
# document that can read cookies.
_SERVE_ALLOWED_EXTS = frozenset({
    # image (upload route, Phase 10)
    "jpg", "jpeg", "png", "gif", "webp", "heic", "heif",
    # text-ish (attach_file, Phase 15)
    "txt", "md", "log", "csv", "tsv",
    "json", "yaml", "yml", "toml", "ini", "conf", "env",
    "xml", "html", "htm", "css", "scss", "less",
    "sql", "graphql",
    # code
    "py", "sh", "bash", "zsh", "fish",
    "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "rb", "go", "rs", "java", "kt", "swift",
    "c", "h", "cpp", "cxx", "hpp", "cc",
    "cs", "php", "pl", "lua", "r",
    "dockerfile", "makefile", "cmake",
    # images that attach_file can write (svg/standard)
    "svg",
    # documents (added 2026-10-19: docx, odt)
    "pdf", "docx", "odt",
    # archives
    "zip", "tar", "gz", "tgz", "bz2", "xz",
    # data
    "parquet", "sqlite", "db",
    # misc
    "diff", "patch", "ipynb",
})
# Map extension -> safe content type. Anything that could be
# rendered by the browser (html, htm, svg, js, xml) is forced
# to a text/* or octet-stream type so the browser doesn't
# execute it. The `as_attachment=True` on the response is the
# belt-and-braces (also forces download instead of render).
_SERVE_EXT_TO_MIME = {
    "html": "text/plain",
    "htm":  "text/plain",
    "svg":  "image/svg+xml",  # still download-only via as_attachment
    "js":   "text/plain",
    "mjs":  "text/plain",
    "cjs":  "text/plain",
    "css":  "text/css",
    "xml":  "application/xml",
    "json": "application/json",
    "yaml": "text/plain",
    "yml":  "text/plain",
    "toml": "text/plain",
    "ini":  "text/plain",
    "conf": "text/plain",
    "env":  "text/plain",
    "txt":  "text/plain",
    "md":   "text/markdown",
    "log":  "text/plain",
    "csv":  "text/csv",
    "tsv":  "text/tab-separated-values",
    "sql":  "text/plain",
    "graphql": "text/plain",
    "py":   "text/x-python",
    "sh":   "text/x-shellscript",
    "bash": "text/x-shellscript",
    "zsh":  "text/x-shellscript",
    "fish": "text/x-shellscript",
    "ts":   "text/plain",
    "tsx":  "text/plain",
    "jsx":  "text/plain",
    "rb":   "text/plain",
    "go":   "text/plain",
    "rs":   "text/plain",
    "java": "text/plain",
    "kt":   "text/plain",
    "swift": "text/plain",
    "c":    "text/x-c",
    "h":    "text/x-c",
    "cpp":  "text/x-c++",
    "cxx":  "text/x-c++",
    "hpp":  "text/x-c++",
    "cc":   "text/x-c++",
    "cs":   "text/plain",
    "php":  "text/plain",
    "pl":   "text/plain",
    "lua":  "text/plain",
    "r":    "text/plain",
    "scss": "text/x-scss",
    "less": "text/x-less",
    "dockerfile": "text/plain",
    "makefile":  "text/x-makefile",
    "cmake":     "text/x-cmake",
    "pdf":  "application/pdf",
    "zip":  "application/zip",
    "tar":  "application/x-tar",
    "gz":   "application/gzip",
    "tgz":  "application/gzip",
    "bz2":  "application/x-bzip2",
    "xz":   "application/x-xz",
    "parquet": "application/octet-stream",
    "sqlite":  "application/octet-stream",
    "db":      "application/octet-stream",
    "diff":    "text/x-diff",
    "patch":   "text/x-diff",
    "ipynb":   "application/x-ipynb+json",
}


def _lim(limit_str: str):
    """Look up the limiter + apply the given rate string. Falls back to no
    limit if the limiter wasn't installed (e.g. tests)."""
    lim = current_app.extensions.get("limiter")
    if lim is None:
        from functools import wraps
        class _N:
            def limit(self, *a, **kw):
                def deco(f): return f
                return deco
        return _N().limit(limit_str)
    return lim.limit(limit_str)


@bp.route("/chat", methods=["POST"])
@login_required_json
def chat():
    """One-shot chat. Requires login.

    Request:
        {"agent": "agent1", "conversation_id": "uuid-or-null", "message": "..."}
    Response:
        {"conversation_id": "...", "assistant": "...", "model": "...",
         "latency_ms": N, "tool_iterations": M, "retrieved_chunk_ids": [...]}
    """
    user = current_user()
    user_id = user.id if user else None

    data = request.get_json(silent=True) or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "message is required"}), 400

    # Entity-based refactor (2026-10-17): conversation_id is no longer
    # accepted from the client. The agent+user pair has exactly one
    # persistent conversation; we look it up server-side.

    # Agent dispatch order (multi-agent since 2026-10-15):
    # 1. session["active_agent_name"] (set by the HMI top-bar toggle)
    # 2. data["agent"] (one-shot override per-request)
    # 3. config default_agent (HARNESS_DEFAULT_AGENT)
    # 4. first entry in config.app.agents (last-resort fallback)
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    session_agent = session.get("active_agent_name")
    body_agent = data.get("agent")
    default_agent = cfg.app.default_agent
    fallback_first = cfg.app.agents[0] if cfg.app.agents else default_agent
    agent_name = session_agent or body_agent or default_agent or fallback_first
    # Validate the chosen agent is in the registry; if not, fall back to default
    registry = current_app.extensions["harness"].get("bootstrap_registry")
    if registry is not None and agent_name not in registry.list_agents():
        log.warning(
            "agent dispatch: requested agent=%s not in registry %s; falling back to %s",
            agent_name, registry.list_agents(), default_agent,
        )
        agent_name = default_agent
        session["active_agent_name"] = default_agent

    ext = current_app.extensions["harness"]
    loop = ext["loop"]
    # Phase 10: per-turn image attachments. Optional; validated upstream
    # by POST /api/v1/uploads which assigns the storage URL. We just
    # pass the list through to the turn row.
    raw_attachments = data.get("attachments") or []
    if not isinstance(raw_attachments, list):
        return jsonify({"error": "attachments must be a list"}), 400
    attachments: list[dict] = []
    for a in raw_attachments:
        if not isinstance(a, dict):
            return jsonify({"error": "each attachment must be an object"}), 400
        if not a.get("url") or not a.get("filename"):
            return jsonify({"error": "each attachment needs url and filename"}), 400
        # Constrain to known fields to keep the row tidy.
        attachments.append({
            "url": str(a["url"])[:512],
            "filename": str(a["filename"])[:256],
            "content_type": str(a.get("content_type", ""))[:128],
            "size": int(a.get("size", 0) or 0),
        })
    response = loop.handle_chat(
        user_text=user_text,
        agent_name=agent_name,
        user_id=user_id,
        attachments=attachments,
    )
    return jsonify({
        "conversation_id": str(response.conversation_id),
        "assistant": response.assistant,
        "model": response.model,
        "latency_ms": response.latency_ms,
        "tool_iterations": response.tool_iterations,
        "retrieved_chunk_ids": response.retrieved_chunk_ids,
    }), 200


# Apply chat-specific rate limit by re-wrapping the view function.
@bp.route("/internal/timer", methods=["POST"])
def internal_timer():
    """Service-to-service chat trigger. Auth via X-Harness-Token header.

    Used by systemd --user timer services (e.g. create_timer skill) to
    wake a harness agent main session with a prompt. Runs the chat in
    a background thread and returns 202 with a request_id; the chat
    appears as a new conversation in the HMI under user_id=3 (the operator).

    Body (JSON):
        {"agent": "agent2", "message": "..."}

    Required headers:
        X-Harness-Token: matches HARNESS_INTERNAL_TOKEN env var

    Response:
        202: {"status": "triggered", "conversation_id": "...", "request_id": "..."}
        401: bad/missing token
        400: missing agent or message
        500: dispatch failure
    """
    # 1. Token check
    expected = os.environ.get("HARNESS_INTERNAL_TOKEN", "")
    presented = request.headers.get("X-Harness-Token", "")
    if not expected or not presented or presented != expected:
        log.warning(
            "internal/timer: rejected request from %s (token %s)",
            request.remote_addr, "present" if presented else "missing",
        )
        return jsonify({"error": "invalid or missing X-Harness-Token"}), 401

    # 2. Body parse
    data = request.get_json(silent=True) or {}
    agent_name = (data.get("agent") or "").strip()
    message = (data.get("message") or "").strip()
    if not agent_name:
        return jsonify({"error": "agent is required"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400

    # 3. Validate agent
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    registry = current_app.extensions["harness"].get("bootstrap_registry")
    available = registry.list_agents() if registry is not None else list(cfg.app.agents)
    if agent_name not in available:
        return jsonify({
            "error": f"unknown agent: {agent_name!r}",
            "available": available,
        }), 400

    # 4. Create a fresh conversation (each timer fire is its own convo
    #    so the HMI's conversation list is navigable). user_id=3 is
    #    the operator (the admin who sets up the timers).
    ext = current_app.extensions["harness"]
    loop = ext["loop"]
    user_id = 3
    request_id = uuid.uuid4().hex[:12]

    def _run_chat() -> None:
        """Background thread target: run handle_chat, log result."""
        try:
            response = loop.handle_chat(
                user_text=message,
                agent_name=agent_name,
                user_id=user_id,
                attachments=None,
            )
            log.info(
                "internal/timer: completed request_id=%s conversation_id=%s agent=%s latency_ms=%d",
                request_id, response.conversation_id, agent_name, response.latency_ms,
            )
        except Exception as e:
            log.exception(
                "internal/timer: FAILED request_id=%s agent=%s err=%s",
                request_id, agent_name, e,
            )

    thread = threading.Thread(target=_run_chat, name=f"timer-{request_id}", daemon=True)
    thread.start()

    log.info(
        "internal/timer: triggered request_id=%s agent=%s user_id=%s message_len=%d",
        request_id, agent_name, user_id, len(message),
    )
    return jsonify({
        "status": "triggered",
        "request_id": request_id,
        "agent": agent_name,
    }), 202


# --- Nightly reverie (2026-10-23) -----------------------------------------

@bp.route("/internal/reverie", methods=["POST"])
def internal_reverie():
    """Fire the nightly reverie turn(s). Auth via X-Harness-Token header.

    The reverie is a REAL agent turn (unlike a heartbeat): it runs the
    agent with its last 50 turns of history, asks it to clean up its own
    context budget (permanent memories + identity files) down to the
    configured target percentages, and to finish with reflection +
    proposals for the user. The turn is captured in the turns table like
    any other turn, so tomorrow's conversation has context of what
    tonight's reverie changed.

    It only fires for agents that had real activity in the window
    (config.reverie.activity_window_hours) - otherwise the turn history
    would fill up with no-op reveries. `force: true` overrides that
    check (operator use).

    Body (JSON):
        {"agent": "agent1", "force": false}

    Required headers:
        X-Harness-Token: matches HARNESS_INTERNAL_TOKEN env var

    Response:
        202: {"status": "triggered", "triggered": [...], "skipped": [...]}
        401: bad/missing token
        409: reverie disabled in config
        400: unknown agent
    """
    expected = os.environ.get("HARNESS_INTERNAL_TOKEN", "")
    presented = request.headers.get("X-Harness-Token", "")
    if not expected or not presented or presented != expected:
        log.warning(
            "internal/reverie: rejected request from %s (token %s)",
            request.remote_addr, "present" if presented else "missing",
        )
        return jsonify({"error": "invalid or missing X-Harness-Token"}), 401

    from app.agent import reverie as _reverie

    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    if not cfg.reverie.enabled:
        return jsonify({
            "status": "disabled",
            "error": "reverie is disabled in config.yaml (reverie.enabled)",
        }), 409

    data = request.get_json(silent=True) or {}
    only_agent = (data.get("agent") or "").strip() or None
    force = bool(data.get("force", False))

    registry = current_app.extensions["harness"].get("bootstrap_registry")
    installed = list(registry.list_agents()) if registry is not None else list(cfg.app.agents)
    if only_agent:
        if only_agent not in installed:
            return jsonify({"error": f"unknown agent: {only_agent!r}",
                            "available": installed}), 400
        agents = [only_agent]
    elif cfg.reverie.agents:
        agents = [a for a in cfg.reverie.agents if a in installed]
    else:
        agents = installed

    ext = current_app.extensions["harness"]
    pool = ext["pool"]
    loop = ext["loop"]

    # The user the reverie runs as. The turn lands in that user's DM
    # conversation with the agent, so it shows up in the HMI and
    # tomorrow's conversation continues from it.
    user_id = _reverie.resolve_reverie_user_id(pool, cfg.reverie.user_email)
    if user_id is None:
        return jsonify({
            "status": "error",
            "error": (
                "could not resolve the reverie user "
                f"(reverie.user_email={cfg.reverie.user_email!r})"
            ),
        }), 409

    triggered: list[dict] = []
    skipped: list[dict] = []
    for agent in agents:
        # Don't pile a reverie on top of a turn that is already running
        # for that agent (a chat in progress, or a reverie from an
        # earlier fire). The turn registry is in-process, so this only
        # sees turns running in THIS app process.
        if turn_guard.find(agent_name=agent) is not None:
            skipped.append({
                "agent": agent,
                "reason": "a turn is already in flight for this agent",
            })
            continue
        plan = _reverie.plan_reverie(
            pool=pool, cfg=cfg, agent_name=agent, force=force,
        )
        if not plan["should_run"]:
            skipped.append({"agent": agent, "reason": plan["reason"]})
            continue
        triggered.append({"agent": agent, "reason": plan["reason"]})

    if not triggered:
        log.info("internal/reverie: nothing to do (skipped=%s)", skipped)
        return jsonify({"status": "skipped", "triggered": [], "skipped": skipped}), 202

    request_ids: list[dict] = []
    # The worker thread has no Flask context of its own, so capture the
    # app object HERE (in the request) and push the context inside the
    # thread. Evaluating `current_app` inside the thread is the classic
    # "Working outside of application context" trap.
    app_obj = current_app._get_current_object()
    for item in triggered:
        agent = item["agent"]
        request_id = uuid.uuid4().hex[:12]

        def _run_reverie(agent_name: str = agent, rid: str = request_id) -> None:
            try:
                with app_obj.app_context():
                    bootstrap = ext["bootstrap_registry"].get(agent_name)
                    memory = ext["memory_registry"][agent_name]
                    brief = _reverie.build_brief_with_agent_file(
                        agent_name, cfg, bootstrap, memory,
                    )
                    log.info("internal/reverie: running agent=%s rid=%s brief=%d bytes",
                             agent_name, rid, len(brief))
                    response = loop.handle_chat(
                        user_text=brief,
                        agent_name=agent_name,
                        user_id=user_id,
                        attachments=None,
                        request_id=rid,
                        # The turn is marked so the nightly activity
                        # check does not count it as real activity.
                        user_turn_trajectory=_reverie.reverie_trajectory(
                            agent_name, datetime.now(timezone.utc),
                        ),
                        # SOUL.md and friends: user permission only.
                        protected_paths=tuple(cfg.reverie.protected_files or ()),
                        # A reverie is a focused session: cap the tool
                        # iterations if the config asks for it.
                        max_tool_iterations=(cfg.reverie.max_tool_iterations or None),
                        # The scope rule goes in the SYSTEM prompt.
                        system_suffix=_reverie.REVERIE_MODE,
                    )
                    log.info(
                        "internal/reverie: done agent=%s rid=%s conv=%s latency_ms=%s",
                        agent_name, rid, getattr(response, "conversation_id", "?"),
                        getattr(response, "latency_ms", "?"),
                    )
                    # The reverie edits the identity files on disk, but
                    # the harness caches them in the BootstrapLoader at
                    # startup -- without a reload the agent would keep
                    # being served the OLD files until the next restart
                    # (agent1's reverie spotted this itself and proposed
                    # it as a fix). Re-read them now.
                    try:
                        loader = ext["bootstrap_registry"].get(agent_name)
                        if loader is not None:
                            loader.reload()
                            log.info(
                                "internal/reverie: reloaded identity files for agent=%s",
                                agent_name,
                            )
                    except Exception as exc:
                        # reload() raises BootstrapOverflowError if an
                        # edit pushed a file over its cap. That is the
                        # operator's problem to see, not a crash.
                        log.error(
                            "internal/reverie: identity reload failed for agent=%s: %s: %s",
                            agent_name, type(exc).__name__, exc,
                        )
            except Exception as exc:   # noqa: BLE001 - logged, not fatal
                log.exception("internal/reverie FAILED agent=%s rid=%s", agent_name, rid)

        threading.Thread(
            target=_run_reverie, name=f"reverie-{agent}-{request_id}", daemon=True
        ).start()
        request_ids.append({"agent": agent, "request_id": request_id})

    log.info("internal/reverie: triggered=%s skipped=%s", request_ids, skipped)
    return jsonify({
        "status": "triggered",
        "triggered": request_ids,
        "skipped": skipped,
        "user_id": user_id,
    }), 202


@bp.route("/internal/reverie/preview", methods=["GET"])
def internal_reverie_preview():
    """Show the reverie brief + the fire/skip decision WITHOUT running it.

    Operator/debug aid: lets you see exactly what the agent would be
    asked to do tonight (the numbers, the targets, the protected files)
    before letting it loose. Same token auth as the other internal
    routes.
    """
    expected = os.environ.get("HARNESS_INTERNAL_TOKEN", "")
    presented = request.headers.get("X-Harness-Token", "")
    if not expected or not presented or presented != expected:
        return jsonify({"error": "invalid or missing X-Harness-Token"}), 401

    from app.agent import reverie as _reverie

    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    ext = current_app.extensions["harness"]
    pool = ext["pool"]
    registry = ext["bootstrap_registry"]
    agent_name = (request.args.get("agent") or "").strip() or cfg.app.default_agent
    if agent_name not in registry.list_agents():
        return jsonify({"error": f"unknown agent: {agent_name!r}",
                        "available": registry.list_agents()}), 400

    plan = _reverie.plan_reverie(
        pool=pool, cfg=cfg, agent_name=agent_name,
        force=bool(request.args.get("force", "")),
    )
    bootstrap = registry.get(agent_name)
    memory = ext["memory_registry"][agent_name]
    brief = _reverie.build_brief_with_agent_file(agent_name, cfg, bootstrap, memory)
    return jsonify({
        "agent": agent_name,
        "enabled": cfg.reverie.enabled,
        "plan": plan,
        "brief": brief,
    }), 200




def _apply_route_rate_limits(app):
    """Walk the api blueprint and apply per-route rate limits based on
    a `rate_limit` attribute on the view function (set by decorators
    below) or a hard-coded map."""
    from .security import _route_rate_limit  # type: ignore
    cfg = app.config.get("HARNESS_CONFIG_OBJ")
    if cfg is None:
        return
    chat_lim = cfg.security.rate_limit.chat
    mem_lim = cfg.security.rate_limit.memory
    rate_map = {
        "chat": chat_lim,
        "memories_search": mem_lim,
        "memory_pin": mem_lim,
    }
    # Use Flask-Limiter's shared_limit keyed on a tag.
    lim = app.extensions.get("limiter")
    if lim is None:
        return
    # Apply via shared_limit; tag-based so multiple routes share.
    # This is a best-effort: the chat route already has @_lim(None) above
    # which is a no-op; we explicitly add the chat limit here.
    for view_name, view_func in app.view_functions.items():
        if view_name == "api.chat":
            lim.limit(chat_lim)(view_func)
        elif view_name in ("api.memories_search", "api.memory_pin"):
            lim.limit(mem_lim)(view_func)


@bp.route("/agent", methods=["GET", "POST"])
@login_required_json
def set_agent():
    """List available agents (GET) or set the active agent for this session (POST).

    GET response:
        {"agents": ["agent1", "agent2"], "active": "agent1", "default": "agent1"}
    POST body: {"agent": "agent2"}  -> sets session["active_agent_name"]
    POST response: {"active": "agent2"} on success, 400 on unknown agent.
    """
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    registry = current_app.extensions["harness"].get("bootstrap_registry")
    available = registry.list_agents() if registry is not None else list(cfg.app.agents)

    if request.method == "GET":
        active = session.get("active_agent_name") or cfg.app.default_agent
        return jsonify({
            "agents": available,
            "active": active,
            "default": cfg.app.default_agent,
        }), 200

    # POST: set the active agent
    data = request.get_json(silent=True) or {}
    requested = (data.get("agent") or "").strip()
    if not requested:
        return jsonify({"error": "agent is required"}), 400
    if requested not in available:
        return jsonify({
            "error": f"unknown agent: {requested!r}",
            "available": available,
        }), 400
    session["active_agent_name"] = requested
    # Mark the session as modified so Flask persists it
    session.modified = True
    log.info("agent dispatch: session active_agent_name=%s (user=%s)", requested, current_user())
    return jsonify({"active": requested}), 200


@bp.route("/conversations", methods=["GET"])
@login_required_json
def _agent_schema() -> str:
    """The schema of the agent this request is for.

    Each agent owns a schema named after it, so the agent name IS the
    schema. The name is taken from the same dispatch order the chat uses
    (session -> request body -> config default) and is validated against
    the bootstrap registry, so it can never carry SQL into a query.
    """
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    session_agent = session.get("active_agent_name")
    body_agent = (request.get_json(silent=True) or {}).get("agent")
    fallback_first = cfg.app.agents[0] if cfg.app.agents else cfg.app.default_agent
    agent_name = session_agent or body_agent or cfg.app.default_agent or fallback_first
    registry = current_app.extensions["harness"].get("bootstrap_registry")
    if registry is not None and agent_name not in registry.list_agents():
        agent_name = cfg.app.default_agent
    return agent_name


def conversations_list():
    """List recent conversations (newest first, default 50).

    Legacy endpoint (pre-entity-model). Group conversations are
    excluded (migration 014): this list is a one-to-one chat list, and
    a group chat owns its own conversation id.
    """
    limit = min(int(request.args.get("limit", 50)), 200)
    ext = current_app.extensions["harness"]
    pool = ext["pool"]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(t.id) AS message_count
                  FROM {} c
                  LEFT JOIN {} t ON t.conversation_id = c.id
                 WHERE c.is_group = FALSE
                 GROUP BY c.id
                 ORDER BY c.updated_at DESC
                 LIMIT %s
                """).format(
                    sql.Identifier(schema, "conversations"),
                    sql.Identifier(schema, "turns"),
                ),
                (limit,),
            )
            rows = cur.fetchall()
    return jsonify({
        "conversations": [
            {
                "id": str(r[0]),
                "title": r[1] or "(untitled)",
                "created_at": r[2].isoformat() if r[2] else None,
                "updated_at": r[3].isoformat() if r[3] else None,
                "message_count": r[4],
            }
            for r in rows
        ],
    }), 200


@bp.route("/conversations/<uuid:conv_id>", methods=["GET"])
@login_required_json
def conversation_get(conv_id: UUID):
    """Load a conversation's turn history.

    Query params:
        limit (int, optional): if set, return only the *last* N turns
            (in DESC order; client reverses for chronological display).
            Capped at 200. Without limit, all turns are returned in
            ASC order (back-compat).
    """
    ext = current_app.extensions["harness"]
    pool = ext["pool"]
    schema = _agent_schema()
    limit_raw = request.args.get("limit")
    limit = None
    if limit_raw is not None:
        try:
            limit = max(1, min(int(limit_raw), 200))
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT id, title, created_at, updated_at FROM {} WHERE id = %s"
                ).format(sql.Identifier(schema, "conversations")),
                (conv_id,),
            )
            conv = cur.fetchone()
            if not conv:
                return jsonify({"error": "conversation not found"}), 404
            if limit is None:
                cur.execute(
                    sql.SQL("""
                    SELECT id, role, content, model, attachments, created_at
                      FROM {}
                     WHERE conversation_id = %s
                     ORDER BY created_at ASC
                    """).format(sql.Identifier(schema, "turns")),
                    (conv_id,),
                )
            else:
                cur.execute(
                    sql.SQL("""
                    SELECT id, role, content, model, attachments, created_at
                      FROM {}
                     WHERE conversation_id = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """).format(sql.Identifier(schema, "turns")),
                    (conv_id, limit),
                )
            turns = cur.fetchall()
    if not conv[1] and turns:
        for t in turns:
            if t[1] == "user":
                title = t[2][:60] + ("…" if len(t[2]) > 60 else "")
                break
    else:
        title = conv[1]
    import json as _json
    def _atts(v):
        if v is None:
            return []
        if isinstance(v, (list, dict)):
            return v
        return _json.loads(v)
    return jsonify({
        "id": str(conv[0]),
        "title": title,
        "created_at": conv[2].isoformat() if conv[2] else None,
        "updated_at": conv[3].isoformat() if conv[3] else None,
        "turns": [
            {
                "id": t[0], "role": t[1], "content": t[2], "model": t[3],
                "attachments": _atts(t[4]),
                "created_at": t[5].isoformat() if t[5] else None,
            }
            for t in turns
        ],
    }), 200


@bp.route("/uploads", methods=["POST"])
@login_required_json
def upload_image():
    """Phase 10: accept a single image upload for chat attachment.

    Multipart form: field `file` (the image). Returns:

        {"url": "/api/v1/uploads/<uuid>.<ext>",
         "filename": "<original-name>", "content_type": "image/...",
         "size": <bytes>}

    Validation:
      - Content-Type must be image/* and in the allowlist
        (jpeg/png/gif/webp/heic/heif; svg excluded)
      - Size <= HARNESS_MAX_UPLOAD_BYTES (default 10 MiB; the
        Flask MAX_CONTENT_LENGTH enforces the upper bound before
        we get here)
      - Filename and MIME are stored verbatim; the on-disk file
        name is `<uuid>.<ext>` to keep URLs unguessable.
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "no file provided"}), 400

    ct = (f.mimetype or "").lower()
    if ct not in _ALLOWED_MIME:
        return jsonify({
            "error": f"unsupported content type: {ct or 'unknown'}",
            "allowed": sorted(_ALLOWED_MIME),
        }), 415

    ext = _MIME_TO_EXT[ct]
    upload_dir = Path(current_app.config["HARNESS_UPLOAD_DIR"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Defence in depth: also enforce per-request size here in case
    # MAX_CONTENT_LENGTH isn't set (e.g. tests). Read into memory
    # up to the cap, then write.
    max_bytes = int(current_app.config.get("HARNESS_MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
    blob = f.read(max_bytes + 1)
    if len(blob) > max_bytes:
        return jsonify({
            "error": f"file too large (max {max_bytes} bytes)",
        }), 413
    if len(blob) == 0:
        return jsonify({"error": "empty file"}), 400

    file_id = uuid.uuid4().hex
    on_disk = upload_dir / f"{file_id}.{ext}"
    on_disk.write_bytes(blob)
    # Restore the read position so request.files is consistent if
    # anything downstream reads it (defensive — we don't expect
    # that here, but Flask's stream semantics are subtle).
    try:
        f.stream.seek(0)
    except Exception:  # noqa: BLE001
        pass

    return jsonify({
        "url": f"/api/v1/uploads/{file_id}.{ext}",
        "filename": f.filename,
        "content_type": ct,
        "size": len(blob),
    }), 201


@bp.route("/uploads/<path:file_id>", methods=["GET"])
@login_required_json
def serve_upload(file_id: str):
    """Phase 10: serve a previously uploaded attachment image.

    The path segment is `<uuid>.<ext>` — the format the upload
    endpoint returned. We re-validate the extension against the
    allowlist so a malicious client can't fetch arbitrary paths
    out of HARNESS_UPLOAD_DIR (defence against path traversal
    even though Flask's `<path:>` converter doesn't decode ..).
    """
    # Reject anything that's not <32-hex>.<allowed-ext>.
    if "/" in file_id or ".." in file_id or "\\" in file_id:
        return jsonify({"error": "bad path"}), 400
    if "." not in file_id:
        return jsonify({"error": "missing extension"}), 400
    stem, ext = file_id.rsplit(".", 1)
    if not (len(stem) == 32 and all(c in "0123456789abcdef" for c in stem)):
        return jsonify({"error": "bad file id"}), 400
    if ext not in _SERVE_ALLOWED_EXTS:
        return jsonify({"error": "bad extension"}), 400

    upload_dir = Path(current_app.config["HARNESS_UPLOAD_DIR"])
    target = (upload_dir / file_id).resolve()
    # Defence in depth: ensure the resolved path is still inside
    # the upload dir (catches symlink games too).
    try:
        target.relative_to(upload_dir.resolve())
    except ValueError:
        return jsonify({"error": "bad path"}), 400
    if not target.is_file():
        return jsonify({"error": "not found"}), 404

    # Pick the right content type. We never trust the original
    # upload's content_type — it's recomputed from the extension
    # here, with the XSS-defence rule (html/svg/etc. are forced
    # to text/plain or attachment-only).
    is_image_ext = ext in _MIME_TO_EXT.values()
    mime = _SERVE_EXT_TO_MIME.get(ext) or (
        # Fallback: if it's a known image extension, use the
        # canonical image mime; otherwise octet-stream.
        {v: k for k, v in _MIME_TO_EXT.items()}.get(ext, "application/octet-stream")
    )
    return send_file(
        str(target),
        mimetype=mime,
        # Images render inline (the chat UI does <img src=...>);
        # everything else downloads. as_attachment=True forces
        # Content-Disposition: attachment and skips in-place
        # rendering, which is the XSS-defence layer for html/
        # svg/js etc.
        as_attachment=not is_image_ext,
        # Use the original filename where possible (the chat UI
        # sets this via the `download` attribute on the chip; the
        # server side just needs a sensible default).
        download_name=file_id,
        max_age=3600,
    )


@bp.route("/memories/search", methods=["GET"])
@login_required_json
def memories_search():
    """Debug: hybrid retrieval without going through the model."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q is required"}), 400
    top_k = int(request.args.get("top_k", 10))
    ext = current_app.extensions["harness"]
    chunks = ext["memory"].search(q, top_k=top_k)
    return jsonify({
        "query": q,
        "chunks": [
            {
                "id": c.id, "role": c.role, "content": c.content,
                "score": round(c.score, 4), "importance": c.importance,
            }
            for c in chunks
        ],
        "count": len(chunks),
    }), 200


def _require_admin():
    """Return the current user if they're an admin, else None + a 403 response."""
    from flask import jsonify as _jsonify
    u = current_user()
    if u is None:
        return None, (_jsonify({"code": "auth", "error": "login required"}), 401)
    if getattr(u, "role", "member") != "admin":
        return None, (_jsonify({"code": "forbidden", "error": "admin role required"}), 403)
    return u, None


@bp.route("/admin/last-emission", methods=["GET"])
@login_required_json
def admin_last_emission():
    """JSON form of the most recent thought stream (the companion
    to admin_last_prompt, which shows the prompt the model saw).

    Returns the iteration-by-iteration stream: text + tool calls +
    tool results + final text. Accepts ?agent=<name>.
    """
    from app.agent import last_emission as _le
    _user, err = _require_admin()
    if err is not None:
        return err
    agent_name = request.args.get("agent") or None
    state = _le.get(agent_name=agent_name)
    if state is None:
        return jsonify({
            "error": "no emission captured yet" + (f" for agent {agent_name!r}" if agent_name else ""),
            "hint": "send a chat request first, then re-fetch",
        }), 404
    return jsonify(state), 200


@bp.route("/admin/last-prompt", methods=["GET"])
@login_required_json
def admin_last_prompt():
    """Return the raw prompt + tool definitions from the most recent
    LLM call. Operator-only: role=admin. Useful for debugging what
    the model actually saw on the last chat (bootstrap, permanent
    memory, history, tool definitions, current user message).

    Captures the LATEST iteration of the tool-use loop, so the response
    reflects the prompt that produced the assistant's text (or the
    last attempt before max_tool_iterations).
    """
    from app.agent import last_prompt as _lp
    _user, err = _require_admin()
    if err is not None:
        return err
    agent_name = request.args.get("agent") or None
    state = _lp.get(agent_name=agent_name)
    if state is None:
        return jsonify({
            "error": "no prompt captured yet" + (f" for agent {agent_name!r}" if agent_name else ""),
            "hint": "send a chat request first, then re-fetch",
        }), 404
    return jsonify(state), 200

@bp.route("/admin/exec-audit", methods=["GET"])
@login_required_json
def admin_exec_audit():
    """Tail the exec_audit.jsonl log. Operator-only: role=admin.
    Phase 2 (Mal 2026-10-14): lets Mal/Grove grep recent exec_bash
    calls without shell access to the harness. Default returns the
    last 50 entries; ?limit=N raises it (max 1000). Optional
    ?outcome= filters by outcome (ok|blocked|confirm_required|error|timeout).
    """
    import json
    from pathlib import Path
    _user, err = _require_admin()
    if err is not None:
        return err
    log_path = Path(__file__).resolve().parents[2] / "logs" / "exec_audit.jsonl"
    if not log_path.is_file():
        return jsonify({"count": 0, "entries": [], "hint": "exec_audit.jsonl does not exist yet"}), 200
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    if limit < 1:
        limit = 50
    if limit > 1000:
        limit = 1000
    outcome_filter = request.args.get("outcome")
    entries = []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if outcome_filter and d.get("outcome") != outcome_filter:
                continue
            entries.append(d)
            if len(entries) >= limit:
                break
    except (OSError, PermissionError) as e:
        return jsonify({"error": f"failed to read exec_audit.jsonl: {e}"}), 500
    return jsonify({"count": len(entries), "limit": limit, "entries": entries}), 200



@bp.route("/memories/<int:chunk_id>/pin", methods=["PUT"])
@login_required_json
def memory_pin(chunk_id: int):
    """Operator-side pin. Body: {"permanent": true|false, "source": "operator"}."""
    data = request.get_json(silent=True) or {}
    want_permanent = bool(data.get("permanent", True))
    source = data.get("source", "operator")
    if source != "operator":
        return jsonify({"error": "this endpoint requires source=operator"}), 400

    ext = current_app.extensions["harness"]
    if want_permanent:
        chunk = ext["memory"].pin(chunk_id, source="operator")
    else:
        chunk = ext["memory"].unpin(chunk_id, source="operator")
    return jsonify({
        "chunk_id": chunk.id, "permanent": chunk.pin_source is not None,
    }), 200



# --- Entity-based refactor (2026-10-17) -----------------------------------
# These endpoints replace the older /chat + /conversations pair. Each
# entity (agent) has exactly one persistent conversation per user; the
# frontend never sees a conversation_id. The last 50 turns are always
# rendered and always included in the LLM prompt.

@bp.route("/entities", methods=["GET"])
@login_required_json
def entities_list():
    """List all installed agents with their last-turn info + per-agent icon.

    Used by the landing page (Telegram-style entity list). icon_url is
    null when no custom icon has been uploaded.
    """
    user = current_user()
    user_id = user.id if user else None
    ext = current_app.extensions["harness"]
    # Use the default-agent memory to call the cross-agent helper
    memory = ext["memory"]
    entities = memory.list_entities_with_activity(user_id=user_id)
    # Annotate each entity with icon_url if the user uploaded one
    pool = ext["pool"]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT entity_name FROM public.entity_icons")
            have_icons = {row[0] for row in cur.fetchall()}
    for e in entities:
        if e["name"] in have_icons:
            e["icon_url"] = f"/api/v1/entities/{e["name"]}/icon"
        else:
            e["icon_url"] = None
    return jsonify({"entities": entities}), 200


@bp.route("/entities/<name>/turns", methods=["GET"])
@login_required_json
def entity_turns(name: str):
    """Last N turns of the entity's persistent conversation.

    Query params:
        limit (int, default 50, max 200): how many of the most recent
            turns to return (DESC order; client reverses for display).
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    user = current_user()
    user_id = user.id if user else None
    ext = current_app.extensions["harness"]
    memory = ext["memory"]
    turns = memory.list_turns_for_persistent(name, user_id=user_id, limit=limit)
    return jsonify({"name": name, "turns": turns}), 200


@bp.route("/entities/<name>/chat", methods=["POST"])
@login_required_json
def entity_chat(name: str):
    """Send a message to an entity. Always appends to the persistent
    conversation; the last 50 turns are included in the LLM prompt.
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    data = request.get_json(silent=True) or {}
    user_text = (data.get("message") or "").strip()
    raw_attachments = data.get("attachments") or []
    if not isinstance(raw_attachments, list):
        return jsonify({"error": "attachments must be a list"}), 400
    # Allow empty message when attachments are provided (Phase 10).
    # The frontend supports sending a bare image, and the loop's
    # _content_with_attachments_vision builds a proper content
    # block for the model so empty-text + image is a real turn.
    if not user_text and not raw_attachments:
        return jsonify({"error": "message is required"}), 400
    user = current_user()
    user_id = user.id if user else None
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
    # Correlate this turn with the request that started it. The client
    # echoes it back when the stream dies, so the failure can be
    # matched to the turn it belongs to (and de-duplicated).
    request_id = getattr(g, "request_id", None) or uuid.uuid4().hex[:12]
    # Validate active agent in session matches the URL (one entity per page)
    session["active_agent_name"] = name
    ext = current_app.extensions["harness"]
    loop = ext["loop"]

    # Streaming path (NDJSON): when the client sends
    # `Accept: application/x-ndjson`, yield events as the loop runs
    # so long-running turns don't get killed by Cloudflare's 125s
    # origin read timeout (the 524 you saw on 2026-10-17 20:05).
    # Back-compat: any other Accept header gets the original buffered
    # JSON response, so the admin /last-emission and existing test
    # suite still work unchanged.
    accept = (request.headers.get("Accept") or "").lower()
    wants_stream = "application/x-ndjson" in accept

    if wants_stream:
        from flask import Response, stream_with_context
        import json as _json

        # Phase 16.4 (2026-10-20): keepalive yields to prevent
        # Cloudflare's 125s edge timeout from dropping long-running
        # streams. When the iter loop is quiet (e.g., model
        # synthesising the final response after a long tool chain),
        # there's no event to send, Cloudflare closes the connection,
        # and the JS client shows "stream ended without a final event".
        # The keepalive loop runs the agent loop in a daemon thread
        # that pumps events into a queue; the generator drains the
        # queue with a 30s timeout, emitting a comment keepalive line
        # on each timeout. Comment lines are skipped by the JS
        # client's JSON.parse fallthrough (logged as 'bad NDJSON line'
        # — no UI noise).
        def _generate_ndjson():
            KEEPALIVE_INTERVAL = 30  # seconds — well under CF 125s edge timeout
            q = Queue()
            done = threading.Event()

            # 2026-10-22: the loop runs here in a DAEMON THREAD, which
            # has no Flask app context. Push one for the duration of the
            # turn so any context-dependent code in the loop or its tools
            # (e.g. the vision path reading HARNESS_UPLOAD_DIR) works.
            # Belt and braces with the env-var fallback in
            # loop._resolve_upload_dir(): this covers any OTHER
            # current_app use we have not found yet.
            app_obj = current_app._get_current_object()

            def pump():
                state = None
                try:
                    with app_obj.app_context():
                        for ev in loop.handle_chat_streaming(
                            user_text=user_text,
                            agent_name=name,
                            user_id=user_id,
                            attachments=attachments,
                            request_id=request_id,
                        ):
                            # Buffer the event server-side (2026-10-22) so a
                            # browser that refreshes mid-turn can rebuild the
                            # live bubble from GET /entities/<name>/turns/active
                            # instead of staring at an empty chat.
                            if state is None:
                                state = turn_guard.find(
                                    agent_name=name, user_id=user_id,
                                    request_id=request_id,
                                )
                            if state is not None and isinstance(ev, dict):
                                state.push_event(ev)
                            q.put(("event", ev))
                except Exception as exc:
                    q.put(("error", exc))
                finally:
                    done.set()

            t = threading.Thread(target=pump, daemon=True)
            t.start()
            try:
                while True:
                    try:
                        kind, payload = q.get(timeout=KEEPALIVE_INTERVAL)
                    except Empty:
                        yield "# keepalive\n"
                        continue
                    if kind == "error":
                        log.exception("entity_chat streaming failed: %s", payload)
                        yield _json.dumps({"type": "error", "message": f"{type(payload).__name__}: {payload}"}) + "\n"
                        break
                    event = payload
                    if isinstance(event, dict):
                        event.setdefault("request_id", request_id)
                    if event.get("type") == "final":
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
                    yield _json.dumps(event, default=str) + "\n"
                    if event.get("type") == "error":
                        # A fatal error was surfaced to the client. Stop the
                        # stream -- otherwise the keepalive timer keeps the
                        # connection open indefinitely with no further work
                        # happening, and the client waits for an event that
                        # will never come.
                        log.warning("entity_chat: closing stream after error event")
                        break
                    if done.is_set() and q.empty():
                        break
            finally:
                pass


        return Response(
        stream_with_context(_generate_ndjson()),
        content_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if any
        },
    )

    # Non-streaming back-compat path (unchanged from pre-streaming era).
    response = loop.handle_chat(
        user_text=user_text,
        agent_name=name,
        user_id=user_id,
        attachments=attachments,
        request_id=request_id,
    )
    return jsonify({
        "assistant": response.assistant,
        "model": response.model,
        "latency_ms": response.latency_ms,
        "tool_iterations": response.tool_iterations,
        "retrieved_chunk_ids": response.retrieved_chunk_ids,
        # Phase 15 (2026-10-18): file attachments produced by
        # the attach_file tool. Same shape as user-side
        # attachments (url, filename, content_type, size).
        "attachments": list(getattr(response, "attachments", None) or []),
        # Migration 011 (2026-10-19): per-iteration trajectory for the
        # LLM retraining pipeline. None for user turns + legacy rows.
        "trajectory": getattr(response, "trajectory", None),
    }), 200


def _agent_exists(name: str) -> bool:
    ext = current_app.extensions["harness"]
    registry = ext.get("bootstrap_registry")
    if registry is not None:
        return name in registry.list_agents()
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    return name in (cfg.app.agents or ())



# --- Entity icons (Phase 14, 2026-10-17) --------------------------------
# Each agent can have a custom icon (avatar). Stored as a file under
# app/static/entity-icons/, with public.entity_icons tracking the
# metadata so the GET endpoint knows the right Content-Type.

ICON_DIR = Path(__file__).resolve().parent.parent / "static" / "entity-icons"
ALLOWED_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
MAX_ICON_BYTES = 2 * 1024 * 1024  # 2 MiB


def _safe_name(name: str) -> str:
    # Entity names are typically simple identifiers (agent1, agent2, agent3).
    # Keep them as-is for now; if the registry ever allows weirder names
    # we can hash here. Reject anything that smells like a path traversal.
    if ".." in name or "/" in name or "\\" in name or not name:
        raise ValueError("invalid entity name")
    return name


@bp.route("/entities/<name>/turns/active", methods=["GET"])
@login_required_json
def entity_turn_active(name: str):
    """The turn currently running for this user + entity, if any.

    2026-10-22: the streaming bubble in the web chat is client-side DOM.
    If the browser refreshes (or the connection drops) the bubble is
    gone even though the turn keeps running server-side, and there was
    nothing to re-attach to -- the chat looked empty for 40+ minutes
    while Evie was still working.

    This endpoint exposes the in-flight turn's buffered NDJSON events so
    the UI can rebuild the bubble (iteration count, tool calls, partial
    text) after a refresh, and poll until the turn finishes.
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    user = current_user()
    user_id = user.id if user else None
    state = turn_guard.find(agent_name=name, user_id=user_id)
    if state is None:
        return jsonify({"active": False}), 200
    snap = state.buffer_snapshot()
    snap["active"] = True
    return jsonify(snap), 200


@bp.route("/entities/<name>/turns/error", methods=["POST"])
@login_required_json
def entity_turn_error(name: str):
    """Persist a client-side stream failure as an assistant turn.

    2026-10-22: when the NDJSON stream died without a final event the
    browser rendered a ⚠️ bubble, but that bubble lived only in the DOM
    -- the next refresh made the failure look like silence again, which
    is how Mal lost an hour to "why didn't Evie answer?". This endpoint
    writes the failure into the turns table so it survives a refresh and
    shows up in the conversation history.

    Idempotent: if the conversation already has an assistant turn after
    the last user turn -- the server persisted a partial turn, the
    SIGTERM handler wrote an interrupted marker, or the turn finished
    after the client gave up -- this is a no-op. One failure, one row.
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()[:500]
    if not message:
        return jsonify({"error": "message is required"}), 400
    request_id = str(data.get("request_id") or "").strip()[:64] or None
    user = current_user()
    user_id = user.id if user else None
    ext = current_app.extensions["harness"]
    memory = ext.get("memory_registry", {}).get(name) or ext["memory"]
    conv_id = memory.get_or_create_persistent_conversation(name, user_id=user_id)
    recent = memory.load_recent_turns(conv_id, limit=10)
    last_user_idx = None
    for i in range(len(recent) - 1, -1, -1):
        if recent[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return jsonify({"error": "no user turn to attach the error to"}), 400
    if any(t.role == "assistant" for t in recent[last_user_idx + 1:]):
        # Already answered -- partial turn, interrupted marker, or the
        # real answer. Don't add a second assistant turn.
        return jsonify({"persisted": False, "reason": "already answered"}), 200
    turn = memory.write_turn_assistant(
        conv_id, f"⚠️ {message}",
        model="client-error",
        latency_ms=0,
        attachments=[],
        trajectory={
            "client_error": True,
            "message": message,
            "request_id": request_id,
        },
    )
    log.info(
        "client stream error persisted: conv=%s agent=%s turn_id=%s request_id=%s",
        conv_id, name, turn.id, request_id,
    )
    return jsonify({"persisted": True, "turn_id": turn.id}), 201


@bp.route("/entities/<name>/icon", methods=["GET"])
def entity_icon_get(name: str):
    """Serve the entity's icon image, or 404 if none is set.

    Public (no auth) so the <img> tag in the HMI can load the icon
    without credentials — same approach as /api/v1/uploads/<file_id>.
    """
    try:
        name = _safe_name(name)
    except ValueError:
        return jsonify({"error": "invalid entity name"}), 400
    ext = current_app.extensions["harness"]
    pool = ext["pool"]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT filename, mime FROM public.entity_icons WHERE entity_name = %s",
                (name,),
            )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "no icon"}), 404
    filename, mime = row[0], row[1]
    path = ICON_DIR / filename
    if not path.exists():
        # Metadata row exists but the file is missing — log + 404
        log.warning("entity_icon_get: file missing for %s: %s", name, path)
        return jsonify({"error": "icon file missing"}), 404
    # Manual response so we can set the right Content-Type and cache headers
    from flask import send_file
    resp = send_file(str(path), mimetype=mime, max_age=300)
    return resp


@bp.route("/entities/<name>/icon", methods=["PUT"])
@login_required_json
def entity_icon_put(name: str):
    """Upload (or replace) the entity's icon. Body: multipart with file field."""
    try:
        name = _safe_name(name)
    except ValueError:
        return jsonify({"error": "invalid entity name"}), 400
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    if "file" not in request.files:
        return jsonify({"error": "missing 'file' field in multipart form"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    mime = (f.mimetype or "").lower()
    if mime not in ALLOWED_MIMES:
        return jsonify({"error": f"unsupported mime {mime!r}; allowed: {sorted(ALLOWED_MIMES)}"}), 400
    # Read into memory (we cap at 2 MiB). werkzeug's FileStorage.read() is
    # bounded by MAX_CONTENT_LENGTH already, so this is just a guard.
    data = f.read()
    if len(data) > MAX_ICON_BYTES:
        return jsonify({"error": f"icon too large ({len(data)} bytes > {MAX_ICON_BYTES})"}), 413
    ext_map = {
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
        "image/webp": "webp", "image/svg+xml": "svg",
    }
    ext = ext_map[mime]
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    new_filename = f"{name}-{secrets.token_hex(4)}.{ext}"
    new_path = ICON_DIR / new_filename
    # Remove any previous file for this entity
    user = current_user()
    user_id = user.id if user else None
    ext_obj = current_app.extensions["harness"]
    pool = ext_obj["pool"]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT filename FROM public.entity_icons WHERE entity_name = %s",
                (name,),
            )
            old = cur.fetchone()
            if old:
                try:
                    (ICON_DIR / old[0]).unlink()
                except FileNotFoundError:
                    pass
            cur.execute(
                """
                INSERT INTO public.entity_icons (entity_name, filename, mime, size_bytes, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (entity_name) DO UPDATE
                    SET filename = EXCLUDED.filename,
                        mime = EXCLUDED.mime,
                        size_bytes = EXCLUDED.size_bytes,
                        updated_at = EXCLUDED.updated_at,
                        updated_by = EXCLUDED.updated_by
                """,
                (name, new_filename, mime, len(data), user_id),
            )
        conn.commit()
    new_path.write_bytes(data)
    return jsonify({
        "name": name,
        "icon_url": f"/api/v1/entities/{name}/icon",
        "filename": new_filename,
        "mime": mime,
        "size_bytes": len(data),
    }), 200


@bp.route("/entities/<name>/icon", methods=["DELETE"])
@login_required_json
def entity_icon_delete(name: str):
    """Remove the entity's icon. Falls back to the letter placeholder."""
    try:
        name = _safe_name(name)
    except ValueError:
        return jsonify({"error": "invalid entity name"}), 400
    ext = current_app.extensions["harness"]
    pool = ext["pool"]
    removed_file = None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT filename FROM public.entity_icons WHERE entity_name = %s",
                (name,),
            )
            row = cur.fetchone()
            if row:
                removed_file = row[0]
                cur.execute("DELETE FROM public.entity_icons WHERE entity_name = %s", (name,))
        conn.commit()
    if removed_file:
        try:
            (ICON_DIR / removed_file).unlink()
        except FileNotFoundError:
            pass
    return jsonify({"name": name, "removed": bool(removed_file)}), 200


@bp.route("/entities/<name>/reset-conversation", methods=["POST"])
@login_required_json
def entity_reset_conversation(name: str):
    """Start a new conversation for (entity, user). Archives the old one.

    Before migration 010 (2026-10-19) this endpoint did:
      DELETE FROM <schema>.conversations WHERE id = <conv>;
      DELETE FROM <schema>.turns        WHERE conversation_id = <conv>;
      DELETE FROM <schema>.memory_chunks WHERE conversation_id = <conv>
                                       AND permanent = false;

    That destroyed the turn data (LLM conversation history), so
    once you clicked "New conversation" the prior conversation was
    gone forever. Per Mal 2026-10-19 08:43 we need the turn data
    preserved for future LLM retraining.

    Now the flow is:
      1. Set archived_at = now() on the current conversation
         (turns + conversation row preserved).
      2. Free non-permanent memory_chunks for the conversation
         so the next conversation's recall isn't polluted.
      3. Permanent memories (memories WHERE permanent = TRUE) are
         unaffected.
      4. The next chat() call sees no non-archived conversation for
         (agent, user) and inserts a new one.

    The archived conversation is reachable via the new
    GET /api/v1/entities/<name>/conversations?include_archived=true
    and GET /api/v1/entities/<name>/conversations/<id> endpoints
    (for the LLM retraining export).
    """
    try:
        name = _safe_name(name)
    except ValueError:
        return jsonify({"error": "invalid entity name"}), 400
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400

    user = current_user()
    user_id = user.id if user else None

    ext = current_app.extensions["harness"]
    memory = ext["memory"]

    conv_id = memory.get_or_create_persistent_conversation(
        agent_name=name, user_id=user_id,
    )
    result = memory.archive_conversation(conv_id)
    if not result.get("archived"):
        return jsonify({
            "ok": False,
            "error": result.get("reason", "archive failed"),
        }), 400
    return jsonify({
        "ok": True,
        "archived": True,
        "conversation_id": str(conv_id),
        "turns_preserved": result.get("turns", 0),
        "memory_chunks_freed": result.get("memory_chunks_deleted", 0),
    }), 200


@bp.route("/entities/<name>/conversations", methods=["GET"])
@login_required_json
def entity_conversations(name: str):
    """List conversations for an entity (LLM retraining export).

    Query params:
        include_archived (bool, default true): include archived
            conversations in the list. The default is true here (the
            main use case is the retraining export — operator wants
            everything). The active-conversation list the sidebar
            uses goes through a different code path.
        limit (int, default 100, max 1000): max conversations to return.
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    include_archived = request.args.get("include_archived", "true").lower() != "false"

    ext = current_app.extensions["harness"]
    memory = ext["memory"]
    convs = memory.list_conversations(limit=limit, include_archived=include_archived)
    return jsonify({
        "name": name,
        "include_archived": include_archived,
        "count": len(convs),
        "conversations": convs,
    }), 200


@bp.route("/entities/<name>/conversations/<uuid:conv_id>", methods=["GET"])
@login_required_json
def entity_conversation_get(name: str, conv_id):
    """Full conversation (all turns in order).

    Returns archived conversations too (the LLM retraining export
    needs the full history). The caller can filter on the `archived`
    flag in the response if they only want the active one.
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    ext = current_app.extensions["harness"]
    memory = ext["memory"]
    conv = memory.load_conversation(conv_id)
    if conv is None:
        return jsonify({"error": f"conversation not found: {conv_id}"}), 404
    return jsonify(conv), 200


@bp.route("/entities/<name>/conversations/<uuid:conv_id>/export.jsonl", methods=["GET"])
@login_required_json
def entity_conversation_export_jsonl(name: str, conv_id):
    """Export a single conversation as JSONL (LLM training format).

    Query params:
        format: "thin" (default) or "full".
            - thin: one line per turn with {role, content, model,
              created_at, turn_id}. The legacy / LLM-SFT baseline
              format. Works for any conversation, even legacy ones
              that pre-date Migration 011.
            - full: same as thin + the per-iteration trajectory
              ({iterations: [...], total_iterations,
              total_tool_calls, tool_iterations, latency_ms,
              model}) under a "trajectory" key. For assistant
              turns, this is the full thought + tool-use trace.
              For user turns, the trajectory is null. The "full"
              format is what you want for LLM retraining pipelines
              that need to teach tool-use + reasoning patterns.
              Lines will be ~5-50x larger than the thin format
              depending on the iteration count and tool-call size.

    Works for archived conversations too. The export is a stream
    (Flask Response with a generator) so large conversations don't
    blow the memory budget.

    Auth: same as the other /entities/* endpoints (login required).
    Use this for the per-conversation retraining dataset; use the
    /conversations list endpoint for the conversation inventory.
    """
    if not _agent_exists(name):
        return jsonify({"error": f"unknown agent: {name!r}"}), 400
    fmt = request.args.get("format", "thin").lower()
    if fmt not in ("thin", "full"):
        return jsonify({"error": f"format must be 'thin' or 'full', got {fmt!r}"}), 400
    ext = current_app.extensions["harness"]
    memory = ext["memory"]
    conv = memory.load_conversation(conv_id)
    if conv is None:
        return jsonify({"error": f"conversation not found: {conv_id}"}), 404
    import json as _json
    from flask import Response
    turns = conv.get("turns") or []
    # Stream JSONL
    def generate():
        for t in turns:
            record = {
                "role": t["role"],
                "content": t["content"],
                "model": t.get("model"),
                "created_at": t.get("created_at"),
                "turn_id": t["id"],
            }
            if fmt == "full":
                # Migration 011 (2026-10-19): trajectory is the
                # full per-iteration trace. None for user turns
                # and for legacy rows written before the migration.
                record["trajectory"] = t.get("trajectory")
            line = _json.dumps(record, ensure_ascii=False)
            yield line + "\n"
    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Content-Disposition": (
                f'attachment; filename="'
                f'{name}_{str(conv_id)[:8]}_{conv.get("updated_at", "")[:10]}_{fmt}.jsonl"'
            ),
        },
    )
