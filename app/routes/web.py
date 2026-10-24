"""Web routes — landing + chat UI placeholder + health."""
from __future__ import annotations

import html as html_lib
import re
import json
import logging

from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, session

from app.auth import current_user, load_user

log = logging.getLogger(__name__)

bp = Blueprint("web", __name__)


@bp.before_app_request
def _web_load_user():
    """Populate g.current_user from the session cookie for HTML routes.

    login_required_json already does this for /api/v1/* routes, but the
    /admin/last-prompt HTML view needs to know who's logged in to decide
    between (a) show, (b) 403, (c) redirect-to-login. Centralising the
    load here means both API + HTML routes see a consistent current_user.
    """
    from flask import g
    uid = session.get("_user_id")
    if uid is not None:
        user = load_user(uid)
        if user is not None:
            g.current_user = user
        else:
            session.clear()


@bp.route("/")
def index():
    """Landing page — Telegram-style entity list (refactor 2026-10-17).

    Shows the list of installed agents with their last message preview.
    Click an entity to open its persistent conversation at /c/<name>.
    The active_agent session var is no longer set here; the per-entity
    route sets it before any chat request.
    """
    return render_template("index.html")


@bp.route("/c/<entity>")
def entity_chat(entity: str):
    """Per-entity chat UI. Renders chat.html with the entity name."""
    return render_template("chat.html", entity=entity)


@bp.route("/g/<uuid:group_id>")
def group_chat_page(group_id):
    """Group chat UI (added 2026-10-23).

    Renders group.html for the given group. Access is by membership:
    a non-member gets a 403 rather than the page shell, and the data
    behind it is protected by the same check in the API.
    """
    from uuid import UUID as _UUID
    from app.models.group import GroupStore

    u = current_user()
    if u is None:
        # Not logged in: bounce to the landing page (login form).
        return redirect("/")
    ext = current_app.extensions["harness"]
    store = ext.get("group_store") or GroupStore(ext["pool"])
    group = store.get(str(group_id))
    if group is None:
        return ("<h1>404 — group not found</h1>"
                "<p><a href='/'>Back to harness</a></p>"), 404
    if not store.is_member(group.id, u.id):
        return ("<h1>403 — you are not a member of this group</h1>"
                "<p>Ask an admin to add you to it.</p>"
                "<p><a href='/'>Back to harness</a></p>"), 403
    return render_template(
        "group.html",
        group_id=group.id,
        group_name=group.name,
        group_description=group.description,
    )


@bp.route("/health")
def health():
    """Health probe.

    Returns three agent lists so a monitoring system can spot silent
    bootstrap degradation (configured agent not actually loaded):

      - `agents`         - the configured list from config.yaml:app.agents
      - `loaded_agents`  - the actual list from bootstrap_registry
                           (the agents whose bootstrap files passed all
                           the byte-cap + path checks and are ready
                           to serve chats)
      - `degraded`       - True if the two lists differ. Same-shape
                           status code: 200 either way, so health probes
                           don't 5xx just because an agent is mid-deploy.
                           Monitoring should alert on degraded=true.

    Before this change, /health only returned the configured list,
    so a bootstrap failure (e.g. a file over its byte cap, or a
    missing pie/<agent>/identity/ dir) was invisible - the harness
    would happily serve /health with the full list while the
    bootstrap_registry had only 2 of 3 agents. Catches the silent
    failure mode documented in the 1bef0e6 fix.
    """
    ext = current_app.extensions["harness"]
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    active = session.get("active_agent_name") or cfg.app.default_agent
    configured = list(cfg.app.agents)
    bootstrap_registry = ext.get("bootstrap_registry")
    if bootstrap_registry is not None:
        loaded = list(bootstrap_registry.list_agents())
    else:
        # Fallback for tests / very early boot: assume all configured
        # agents loaded. (bootstrap_registry is set in app/extensions.py
        # before any request can hit this route; the only case where
        # it's missing is when someone wires up a test app without
        # the full extensions hook.)
        loaded = configured
    loaded_set = set(loaded)
    missing_from_loaded = [a for a in configured if a not in loaded_set]
    extra_loaded = [a for a in loaded if a not in set(configured)]
    degraded = bool(missing_from_loaded) or bool(extra_loaded)
    return jsonify({
        "status": "ok" if not degraded else "degraded",
        "agent": active,
        "default_agent": cfg.app.default_agent,
        "agents": configured,
        "loaded_agents": loaded,
        "degraded": degraded,
        # Optional diagnostics - useful for the operator even if
        # monitoring just watches `degraded`:
        "agents_missing_from_loaded": missing_from_loaded,
        "extra_loaded_agents": extra_loaded,
        "model": ext["adapter"].name,
    }), 200


@bp.route("/admin/last-prompt", methods=["GET"])
def admin_last_prompt_view():
    """Operator HTML view for the latest LLM prompt. Same data as
    /api/v1/admin/last-prompt, rendered as a <pre>-formatted page
    for human inspection. Redirects to /login (the chat UI) if not
    authenticated, or 403s if the user isn't an admin.
    """
    from app.agent import last_prompt as _lp
    u = current_user()
    if u is None:
        # Not logged in: bounce to the chat UI (which shows the login form).
        return redirect("/")
    if getattr(u, "role", "member") != "admin":
        return ("<h1>403 — admin role required</h1>"
                "<p>The /admin/last-prompt view is operator-only.</p>"
                "<p>Sign in as an admin user to inspect the most recent LLM prompt.</p>"), 403
    # ?agent=<name> scopes to a specific agent; missing = most recent.
    agent_name = request.args.get("agent") or None
    state = _lp.get(agent_name=agent_name)
    if state is None:
        if agent_name:
            return (f"<h1>No prompt captured yet for {agent_name!r}</h1>"
                    "<p>Send a chat to that agent first, then re-fetch.</p>"
                    "<p><a href='/'>Back to chat</a></p>"), 404
        return ("<h1>No prompt captured yet</h1>"
                "<p>Send a chat request first, then reload this page.</p>"
                "<p><a href='/'>Back to chat</a></p>"), 404
    pretty = json.dumps(state, indent=2, ensure_ascii=False)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>harness — last prompt</title>"
        "<style>body{font:13px/1.5 -apple-system,system-ui,sans-serif;"
        "margin:24px;color:#1a1a1a;max-width:1100px}"
        "h1{font-size:18px;margin:0 0 12px}"
        ".meta{color:#666;font-size:12px;margin-bottom:18px}"
        "pre{background:#f6f6f6;border:1px solid #e0e0e0;border-radius:6px;"
        "padding:16px;overflow:auto;white-space:pre-wrap;word-wrap:break-word}"
        ".msg-user{color:#1450a0}.msg-assistant{color:#1a6f1a}"
        ".msg-system{color:#666;font-style:italic}"
        "a{color:#2a6df4}</style></head><body>"
        f"<h1>Latest LLM prompt</h1>"
        f"<div class='meta'>captured {state.get('captured_at_iso')} &middot; "
        f"conv {state.get('conversation_id')} &middot; "
        f"user {state.get('user_id')} &middot; "
        f"agent {state.get('agent_name')} &middot; "
        f"model {state.get('model')} &middot; "
        f"iter {state.get('tool_iterations')} &middot; "
        f"{state.get('message_count')} msgs, {state.get('tool_count')} tools</div>"
        f"<pre>{pretty}</pre>"
        "<p><a href='/'>Back to chat</a> &middot; "
        "<a href='/api/v1/admin/last-prompt'>JSON</a></p>"
        "</body></html>"
    )


# Phase 16 (2026-10-20): the canned failure message the loop emits
# when max_tool_iterations is actually hit (loop.py ~line 701). Used to
# detect the "model fabricated this text to give up early" failure mode:
# when the model's final_text matches this template but the loop's
# tool_iterations is well below the configured cap, the message is the
# model's own output, not the harness's iteration-cap message. (See agent2
# turn 279 on 2026-10-20: model wrote "40 tool iterations" while the
# configured cap was 500 and the loop only ran 2 iterations.)
_CANNED_FAILURE_RE = re.compile(
    r"I wasn't able to finish reasoning within the allowed "
    r"(?P<stated>\d+) tool iterations",
    re.IGNORECASE,
)


def _detect_fabrication(final_text, max_iters, tool_iterations):
    """Return a (stated, max_iters, tool_iterations) tuple if the model's
    final_text matches the harness's iteration-cap template but the
    actual loop didn't hit the cap. Returns None otherwise.
    """
    if not final_text or max_iters is None:
        return None
    if tool_iterations >= max_iters:
        # The loop genuinely hit the cap; the text came from the harness.
        return None
    m = _CANNED_FAILURE_RE.search(final_text)
    if not m:
        return None
    try:
        stated = int(m.group("stated"))
    except (TypeError, ValueError):
        return None
    return stated, max_iters, tool_iterations


def _render_last_emission_page(state: dict) -> str:
    """Pretty-print the last_emission state as a self-contained HTML page."""
    def esc(s: str) -> str:
        return html_lib.escape("" if s is None else str(s))
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>harness — last emission — " + esc(state.get("agent_name", "")) + "</title>",
        "<style>",
        "body{font:13px/1.5 -apple-system,system-ui,sans-serif;margin:24px;color:#1a1a1a;max-width:1100px;background:#fafafa}",
        "h1{font-size:20px;margin:0 0 8px}",
        "h2{font-size:15px;margin:24px 0 8px;color:#444}",
        "h3{font-size:14px;margin:18px 0 6px;color:#222}",
        ".meta{color:#666;font-size:12px;margin-bottom:18px}",
        ".user-input{background:#eef3ff;border:1px solid #c2d2f7;border-radius:6px;padding:10px 14px;margin:8px 0 18px;white-space:pre-wrap}",
        ".iter{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;margin:10px 0}",
        ".iter-num{display:inline-block;background:#1450a0;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;margin-right:8px}",
        ".finish{color:#666;font-size:11px;margin-left:6px}",
        ".assistant{background:#f1f8ec;border-left:3px solid #5fae3a;padding:8px 12px;margin:8px 0;white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px}",
        ".assistant.empty{color:#999;font-style:italic}",
        ".tool{background:#fff7e6;border-left:3px solid #d8a02c;padding:8px 12px;margin:6px 0;font-family:ui-monospace,Menlo,monospace;font-size:12px}",
        ".tool .name{color:#9c6a00;font-weight:600}",
        ".tool .args{color:#555;margin:4px 0}",
        ".tool .result{color:#333;margin-top:6px;white-space:pre-wrap;max-height:300px;overflow:auto;background:#fafafa;padding:6px 8px;border-radius:4px}",
        ".tool .result.error{background:#fdecea;color:#a02020}",
        ".final{background:#e8f0fe;border:1px solid #aac4f5;border-radius:8px;padding:14px 18px;margin:18px 0;white-space:pre-wrap}",
        ".final-label{color:#1450a0;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}",
        ".no-tools{color:#999;font-style:italic;padding:6px 0}",
        # Phase 16 (2026-10-20): fabrication banner + collapsible
        # sections for the JSONB columns.
        ".fabrication-banner{background:#fff4e5;border:1px solid #d8a02c;"
        "border-left:4px solid #c4730d;border-radius:6px;padding:12px 16px;"
        "margin:12px 0;color:#5a3000}",
        ".fabrication-banner strong{color:#7a3f00}",
        ".fabrication-banner code{background:#fff;border:1px solid #e0c89c;"
        "padding:1px 5px;border-radius:3px;font-size:12px}",
        ".details-block{background:#fff;border:1px solid #e0e0e0;"
        "border-radius:8px;padding:0;margin:14px 0;overflow:hidden}",
        ".details-block>summary{cursor:pointer;padding:12px 16px;"
        "background:#f5f5f5;font-weight:600;color:#333;"
        "border-bottom:1px solid #e0e0e0;list-style:none}",
        ".details-block>summary::-webkit-details-marker{display:none}",
        ".details-block>summary:before{content:'▸ ';color:#999;"
        "display:inline-block;transition:transform 0.15s}",
        ".details-block[open]>summary:before{transform:rotate(90deg)}",
        ".details-block[open]>summary{border-bottom:1px solid #e0e0e0}",
        ".details-block .json-body{padding:12px 16px;margin:0;"
        "background:#fafafa;font-family:ui-monospace,Menlo,monospace;"
        "font-size:12px;max-height:500px;overflow:auto;"
        "white-space:pre-wrap;word-wrap:break-word}",
        ".details-block .empty{color:#999;font-style:italic;padding:12px 16px}",
        # Phase 16.1 (2026-10-20): per-iter thinking blocks (Anthropic-style
        # extended thinking). Rendered as a collapsible <details> nested
        # inside each iteration card so the main view stays scannable.
        ".thinking{background:#f5f0ff;border-left:3px solid #7e57c2;border-radius:4px;margin:8px 0;overflow:hidden}",
        ".thinking>summary{cursor:pointer;padding:6px 12px;font-size:12px;color:#5a3a8a;font-weight:600;list-style:none}",
        ".thinking>summary::-webkit-details-marker{display:none}",
        ".thinking>summary:before{content:'▸ ';color:#7e57c2;display:inline-block;transition:transform 0.15s}",
        ".thinking[open]>summary:before{transform:rotate(90deg)}",
        ".thinking-body{padding:8px 14px;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#3a2855;white-space:pre-wrap;max-height:400px;overflow:auto;background:#faf6ff;border-top:1px solid #e0d4f5}",
        ".thinking .empty{color:#7e57c2;font-style:italic;padding:6px 12px}",
        "a{color:#2a6df4}",
        "</style></head><body>",
        f"<h1>Last emission — {esc(state.get('agent_name',''))}</h1>",
        f"<div class='meta'>captured {esc(state.get('captured_at_iso',''))} · "
        f"conv {esc(state.get('conversation_id','')[:8])} · "
        f"user {esc(state.get('user_id',''))} · "
        f"model {esc(state.get('model',''))} · "
        f"{state.get('total_iterations',0)} iterations, "
        f"{state.get('total_tool_calls',0)} tool calls, "
        f"{state.get('tool_iterations',0)}/{state.get('max_tool_iterations','?')} tool-iter "
        f"(cap {state.get('max_tool_iterations','?')}), "
        f"{state.get('latency_ms',0)}ms</div>",
        "<h2>User input</h2>",
        f"<div class='user-input'>{esc(state.get('user_text',''))}</div>",
        "<h2>Iterations</h2>",
    ]
    iterations = state.get("iterations") or []
    if not iterations:
        parts.append("<p class='no-tools'>(no iterations — likely a direct answer with no tool calls)</p>")
    for it in iterations:
        idx = it.get("iteration", 0)
        finish = it.get("finish_reason", "")
        text = it.get("assistant_text") or ""
        calls = it.get("tool_calls") or []
        results = it.get("tool_results") or []
        parts.append(f"<div class='iter'>")
        parts.append(f"<h3><span class='iter-num'>iter {idx + 1}</span>finish: <code>{esc(finish)}</code></h3>")
        if text.strip():
            parts.append(f"<div class='assistant'>{esc(text)}</div>")
        else:
            parts.append("<div class='assistant empty'>(no text — model returned tool calls only)</div>")
        # Phase 16.1 (2026-10-20): surface thinking blocks (Anthropic-style
        # extended thinking) inside each iter card. Collapsed by default;
        # the operator opens it when debugging a hallucination/fabrication
        # failure mode (see harness memory 2026-10-20 agent2 turn 279). The
        # blocks come through exactly as MiniMax emitted them — same
        # shape that round-trips back to the model.
        thinking_blocks = it.get("thinking_blocks") or []
        if thinking_blocks:
            parts.append("<details class='thinking'>")
            n = len(thinking_blocks)
            total_chars = sum(len(b.get("thinking", "")) for b in thinking_blocks)
            parts.append(
                f"<summary>Thinking — {n} block(s), {total_chars} chars</summary>"
            )
            for tb in thinking_blocks:
                tb_text = tb.get("thinking", "")
                if tb_text:
                    parts.append(
                        f"<div class='thinking-body'>{esc(tb_text)}</div>"
                    )
                else:
                    parts.append(
                        "<div class='empty'>(empty thinking block — model returned thinking content without value)</div>"
                    )
            parts.append("</details>")
        if calls:
            for j, call in enumerate(calls):
                name = call.get("name", "")
                args = call.get("arguments", {}) or {}
                result_obj = results[j].get("result") if j < len(results) else None
                is_error = isinstance(result_obj, dict) and "error" in result_obj and len(result_obj) <= 2
                parts.append("<div class='tool'>")
                parts.append(f"<div class='name'>→ {esc(name)}</div>")
                parts.append(f"<div class='args'>{esc(json.dumps(args, indent=2, ensure_ascii=False))}</div>")
                if result_obj is not None:
                    cls = "result error" if is_error else "result"
                    parts.append(f"<div class='{cls}'>{esc(json.dumps(result_obj, indent=2, ensure_ascii=False)[:5000])}</div>")
                parts.append("</div>")
        else:
            parts.append("<div class='no-tools'>(no tool calls on this iteration)</div>")
        parts.append("</div>")
    parts.append("<h2>Final text</h2>")
    # Phase 16 (2026-10-20): fabrication banner. If the model's text
    # matches the harness's iteration-cap template but the actual loop
    # didn't hit the cap, surface the discrepancy so the operator
    # doesn't mis-read the message as a real cap-hit.
    fabrication = _detect_fabrication(
        state.get("final_text"),
        state.get("max_tool_iterations"),
        state.get("tool_iterations"),
    )
    if fabrication is not None:
        stated, cap, actual = fabrication
        parts.append(
            "<div class='fabrication-banner'>"
            "<strong>⚠ Model fabricated this text.</strong> "
            "The final response says "
            f"<code>{stated} tool iterations</code> but the configured "
            f"cap is <code>{cap}</code>. The actual loop ran only "
            f"<code>{actual}</code> tool iterations — far below the cap. "
            "This text was generated by the model, not by the harness's "
            "iteration-cap code at loop.py ~line 701."
            "</div>"
        )
    parts.append(f"<div class='final-label'>What the user saw</div>")
    parts.append(f"<div class='final'>{esc(state.get('final_text',''))}</div>")

    # Phase 16 (2026-10-20): two collapsible sections rendering the
    # JSONB columns that get persisted to <agent>.turns (trajectory +
    # attachments). Both default to closed so the main view stays
    # scannable; the operator opens them when they need the raw shape.
    trajectory_obj = state.get("trajectory")
    attachments_obj = state.get("attachments") or []

    if trajectory_obj is None:
        traj_body = "<div class='empty'>(no trajectory captured)</div>"
    else:
        traj_body = (
            "<pre class='json-body'>"
            + esc(json.dumps(trajectory_obj, indent=2, ensure_ascii=False))
            + "</pre>"
        )
    parts.append("<details class='details-block'>")
    parts.append(
        f"<summary>Trajectory (JSON — mirrors &lt;agent&gt;.turns.trajectory JSONB, "
        f"{esc(str(len(iterations)))} iteration(s))</summary>"
    )
    parts.append(traj_body)
    parts.append("</details>")

    if not attachments_obj:
        att_body = (
            "<div class='empty'>(no attachments — attach_file tool was "
            "not called this turn)</div>"
        )
    else:
        att_body = (
            "<pre class='json-body'>"
            + esc(json.dumps(attachments_obj, indent=2, ensure_ascii=False))
            + "</pre>"
        )
    parts.append("<details class='details-block'>")
    parts.append(
        f"<summary>Attachments (JSON — mirrors &lt;agent&gt;.turns.attachments JSONB, "
        f"{esc(str(len(attachments_obj)))} item(s))</summary>"
    )
    parts.append(att_body)
    parts.append("</details>")

    parts.append("<p style='margin-top:24px'>"
                 "<a href='/'>Back to chat</a> &middot; "
                 "<a href='/admin/last-prompt'>Last prompt</a> &middot; "
                 "<a href='/api/v1/admin/last-emission'>JSON</a></p>")
    parts.append("</body></html>")
    return "".join(parts)


@bp.route("/admin/last-emission", methods=["GET"])
def admin_last_emission_view():
    """Operator HTML view of the most recent thought stream.

    Companion to /admin/last-prompt: that view shows the prompt
    the model saw on its LAST LLM call (one snapshot); this view
    shows the full iteration-by-iteration stream of tool calls
    + text + results that the model produced in response to the
    user's most recent message.

    Accepts ?agent=<name> to scope to a specific agent. Without
    the param, returns the most recent emission across all agents.
    """
    import json as _json
    from app.agent import last_emission as _le
    u = current_user()
    if u is None:
        return redirect("/")
    if getattr(u, "role", "member") != "admin":
        return ("<h1>403 — admin role required</h1>", 403)
    agent_name = request.args.get("agent") or None
    state = _le.get(agent_name=agent_name)
    if state is None:
        if agent_name:
            return (f"<h1>No emission captured yet for {agent_name!r}</h1>"
                    "<p>Send a chat to that agent first, then re-fetch.</p>"
                    "<p><a href='/'>Back to chat</a></p>"), 404
        return ("<h1>No emission captured yet</h1>"
                "<p>Send a chat request first, then reload this page.</p>"
                "<p><a href='/'>Back to chat</a></p>"), 404
    return _render_last_emission_page(state)


@bp.route("/admin/last-memories")
def admin_last_memories_view():
    """HTML view of the latest 20 rows in agent2.memory_chunks.

    Operator-only (role=admin). Shows the most recent 20 rows ordered
    by id DESC (which is also creation order, since id is a bigserial).
    Each row shows: id, role, content preview (first 200 chars),
    importance, permanent, pin_source, source_type/source_ref, created_at.

    Useful for seeing what agent2 just wrote / recalled / had pinned,
    in chronological order. Refreshes on every page load (no in-process
    state).
    """
    import os
    import psycopg
    from psycopg import sql

    u = current_user()
    if u is None:
        return redirect("/")
    if getattr(u, "role", "member") != "admin":
        return ("<h1>403 — admin role required</h1>"
                "<p>The /admin/last-memories view is operator-only.</p>"
                "<p>Sign in as an admin user to inspect the most recent memory rows.</p>"), 403

    # Query the 20 most recent rows. The runtime role has read+write on
    # the agent schema via the schema migration's GRANTs. The schema is
    # named after the agent, so the agent name IS the schema.
    db_url = os.environ.get("DATABASE_URL")
    # The per-agent schema is named after the agent; use the session's
    # active agent, falling back to the configured default.
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    agent_name = session.get("active_agent_name") or cfg.app.default_agent
    registry = current_app.extensions["harness"].get("bootstrap_registry")
    if registry is not None and agent_name not in registry.list_agents():
        agent_name = cfg.app.default_agent
    schema = agent_name
    rows = []
    try:
        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("""
                    SELECT id, role, content, importance, permanent, pin_source,
                           source_type, source_ref, created_at
                      FROM {}
                     ORDER BY id DESC
                     LIMIT 20
                    """).format(sql.Identifier(schema, "memory_chunks"))
                )
                rows = cur.fetchall()
    except Exception as e:
        return (f"<h1>DB error</h1><pre>{e}</pre>"
                "<p><a href='/'>Back to chat</a></p>"), 500

    body = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>harness — last 5 memories (agent2)</title>",
        "<style>body{font:13px/1.5 -apple-system,system-ui,sans-serif;"
        "margin:24px;color:#1a1a1a;max-width:1100px}"
        "h1{font-size:18px;margin:0 0 12px}"
        ".meta{color:#666;font-size:12px;margin-bottom:18px}"
        "table{border-collapse:collapse;width:100%;margin-top:12px}"
        "th,td{border:1px solid #e0e0e0;padding:8px;text-align:left;"
        "vertical-align:top;font-size:12px}"
        "th{background:#f6f6f6;font-weight:600}"
        "tr.pinned{background:#fffbe6}"
        "code{font-size:11px;color:#444;background:#f5f5f5;padding:1px 4px;"
        "border-radius:3px}"
        "a{color:#2a6df4}</style></head><body>",
        "<h1>Last 20 memory rows (agent2.memory_chunks)</h1>",
        f"<div class='meta'>{len(rows)} rows returned (newest first). "
        "Refreshes on every page load.</div>",
    ]

    if not rows:
        body.append("<p><em>No memory rows yet for agent2. Send a chat (or run memory_write) to create some.</em></p>")
    else:
        body.append("<table>")
        body.append("<tr>"
                    "<th>id</th><th>role</th><th>importance</th><th>permanent</th>"
                    "<th>source_type</th><th>source_ref</th><th>created_at</th>"
                    "<th>content (preview)</th></tr>")
        for row in rows:
            rid, role, content, importance, permanent, pin_source, stype, sref, created_at = row
            tr_class = " class='pinned'" if permanent else ""
            preview = (content or "")[:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            body.append(
                f"<tr{tr_class}>"
                f"<td>{rid}</td>"
                f"<td><code>{role}</code></td>"
                f"<td>{importance}</td>"
                f"<td>{'YES' if permanent else 'no'}"
                f" {('(' + pin_source + ')') if pin_source else ''}</td>"
                f"<td><code>{stype}</code></td>"
                f"<td><code>{(sref or '')[:40]}</code></td>"
                f"<td><code>{created_at.strftime('%Y-%m-%d %H:%M:%S') if created_at else ''}</code></td>"
                f"<td>{preview}</td>"
                f"</tr>"
            )
        body.append("</table>")

    body.append("<p style='margin-top:24px'>"
                "<a href='/'>Back to chat</a> &middot; "
                "<a href='/admin/last-prompt'>Last prompt</a> &middot; "
                "<a href='/api/v1/admin/last-prompt'>JSON</a></p>"
                "</body></html>")
    return "".join(body)


# --- /admin/prompts: all prompt files the harness injects ----------

def _list_identity_files(agent_name: str) -> list:
    """Return [{name, path, exists, size, content}] for one agent's identity/ dir.

    Reads from cfg.pie_root / agent / 'identity' (the BootstrapLoader's
    source). Missing files are returned with exists=False so the operator
    sees a complete inventory.
    """
    from pathlib import Path
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    pie_root = Path(cfg.app.pie_root)
    identity_dir = pie_root / agent_name / "identity"
    out = []
    # The canonical file list, in display order. Anything else in the
    # dir is also shown but at the bottom (sorted) for completeness.
    canonical = ["SOUL.md", "USER.md", "AGENTS.md", "IDENTITY.md", "HEARTBEAT.md", "MEMORY.md"]
    seen = set()
    for name in canonical:
        p = identity_dir / name
        out.append(_read_identity_file(name, p))
        seen.add(name)
    if identity_dir.exists():
        for p in sorted(identity_dir.iterdir()):
            if p.is_file() and p.name not in seen and not p.name.startswith("."):
                out.append(_read_identity_file(p.name, p))
    return out


def _read_identity_file(name, p):
    if not p.exists():
        return {"name": name, "path": str(p), "exists": False, "size": 0, "content": ""}
    try:
        content = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"name": name, "path": str(p), "exists": True, "size": p.stat().st_size,
                "content": f"<<error reading: {e}>>"}
    return {"name": name, "path": str(p), "exists": True, "size": len(content.encode("utf-8")),
            "content": content}


def _get_nudge_messages() -> list:
    """Return [{index, message, condition}] for each loop-level nudge."""
    try:
        from app.agent import loop as _loop
    except Exception as e:
        return [{"index": 0, "message": f"<<error importing app.agent.loop: {e}>>",
                 "condition": "import failure"}]
    n3 = _loop._NUDGE_MESSAGES.get(3) if hasattr(_loop, "_NUDGE_MESSAGES") else "<<not defined>>"
    n4 = _loop._NUDGE_MESSAGES.get(4) if hasattr(_loop, "_NUDGE_MESSAGES") else "<<not defined>>"
    return [
        {"index": 1, "message": getattr(_loop, "_NUDGE_MESSAGE", "<<not defined>>"),
         "condition": "1st narrate: model described a tool call in prose instead of a tool_use block, or hallucinated an output (matched by _NARRATE_RE or _CLAIM_RE)."},
        {"index": 2, "message": getattr(_loop, "_NUDGE_MESSAGE_2", "<<not defined>>"),
         "condition": "2nd narrate: same failure mode, stricter message — points out that the user can see which tool calls actually ran, so prose claims are visible lies."},
        {"index": 3, "message": n3,
         "condition": "3rd narrate (Phase 15.10): 'one more chance' escalation for the intent-without-execution failure mode."},
        {"index": 4, "message": n4,
         "condition": "4th (final) narrate: 'this is your last chance' escalation. Beyond _MAX_NUDGES=4 the loop suppresses further nudges and the turn ends."},
        {"index": "intent", "message": getattr(_loop, "_NUDGE_INTENT_MESSAGE", "<<not defined>>"),
         "condition": "Phase 15.9: separate intent-without-execution nudge — 'describe AND execute'. Different trigger than the narrate nudges; fires when the model said 'I will do X' but didn't actually call a tool."},
    ]


def _get_adapter_system_prompt() -> dict:
    """Return {adapter, system_prompt_template, docstring} for the active LLM adapter."""
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    adapter_name = cfg.llm.adapter
    mod_name = f"app.agent.prompts.{adapter_name}"
    try:
        import importlib
        mod = importlib.import_module(mod_name)
    except Exception as e:
        return {"adapter": adapter_name, "system_prompt_template": "",
                "docstring": f"<<error importing {mod_name}: {e}>>"}
    return {
        "adapter": adapter_name,
        "system_prompt_template": getattr(mod, "SYSTEM_PROMPT", "<<no SYSTEM_PROMPT constant>>"),
        "docstring": (mod.__doc__ or "").strip(),
    }


def _get_bootstrap_config() -> list:
    """Return [{name, path, max_bytes}] from cfg.bootstrap.files."""
    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    out = []
    for name, file_cfg in cfg.bootstrap.files.items():
        out.append({"name": name, "path": file_cfg.path, "max_bytes": file_cfg.max_bytes})
    return out


@bp.route("/admin/prompts")
def admin_prompts_view():
    """Operator HTML view: all prompt files the harness injects.

    Shows, in order:
      1. Per-agent identity files (SOUL.md, USER.md, AGENTS.md,
         IDENTITY.md, HEARTBEAT.md, MEMORY.md) for each registered
         agent. These are the always-injected context files.
      2. The LLM adapter's SYSTEM_PROMPT (model-specific copy).
      3. The loop-level nudges (conditionally-inserted mid-conversation
         when the model narrates tool calls in prose or expresses
         intent without execution).
      4. The bootstrap config (which files are loaded + the byte cap
         per file).

    Auth: admin role required, same as the other /admin/* views.
    """
    from datetime import datetime
    u = current_user()
    if u is None:
        return redirect("/")
    if getattr(u, "role", "member") != "admin":
        return ("<h1>403 — admin role required</h1>"
                "<p>The /admin/prompts view is operator-only.</p>"), 403

    cfg = current_app.config["HARNESS_CONFIG_OBJ"]
    agents = list(cfg.app.agents)
    identity_by_agent = {a: _list_identity_files(a) for a in agents}
    adapter = _get_adapter_system_prompt()
    nudges = _get_nudge_messages()
    bootstrap = _get_bootstrap_config()

    def esc(s):
        return html_lib.escape("" if s is None else str(s))

    def file_size(n):
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / 1024 / 1024:.2f} MB"

    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append("<title>harness — prompt files</title>")
    parts.append("<style>")
    parts.append("body{font:13px/1.5 -apple-system,system-ui,sans-serif;margin:0;color:#1a1a1a;background:#fafafa}")
    parts.append(".wrap{max-width:1200px;margin:0 auto;padding:24px 28px 80px}")
    parts.append("h1{font-size:22px;margin:0 0 4px}")
    parts.append("h2{font-size:18px;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid #e0e0e0}")
    parts.append("h3{font-size:15px;margin:18px 0 8px;color:#333}")
    parts.append("h4{font-size:13px;margin:14px 0 6px;color:#444;font-weight:600}")
    parts.append(".meta{color:#666;font-size:12px;margin-bottom:6px}")
    parts.append(".nav{background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:10px 14px;margin:14px 0 24px;font-size:12px}")
    parts.append(".nav a{color:#2a6df4;margin-right:14px;text-decoration:none}")
    parts.append(".nav a:hover{text-decoration:underline}")
    parts.append(".agent{border:1px solid #e0e0e0;border-radius:8px;margin:14px 0;background:#fff;overflow:hidden}")
    parts.append(".agent-header{background:#f3f5f9;padding:10px 16px;font-weight:600;font-size:13px;border-bottom:1px solid #e0e0e0;display:flex;justify-content:space-between;align-items:center}")
    parts.append(".agent-header .tag{font-weight:400;font-size:11px;color:#666;background:#e6ecf5;padding:2px 8px;border-radius:4px}")
    parts.append(".file{border-top:1px solid #f0f0f0;padding:0}")
    parts.append(".file:first-child{border-top:none}")
    parts.append(".file summary{cursor:pointer;padding:10px 16px;font-size:12px;background:#fafafa;list-style:none;display:flex;justify-content:space-between;align-items:center}")
    parts.append(".file summary::-webkit-details-marker{display:none}")
    parts.append(".file summary::before{content:'\\25B6';display:inline-block;margin-right:8px;font-size:9px;color:#888;transition:transform 0.1s}")
    parts.append(".file[open] summary::before{transform:rotate(90deg)}")
    parts.append(".file summary:hover{background:#f0f3f7}")
    parts.append(".file .meta{font-size:11px;color:#666;display:inline-block;margin-left:12px}")
    parts.append(".file .name{font-weight:600;font-size:12px}")
    parts.append(".file .path{color:#888;font-size:11px;margin-left:8px;font-family:ui-monospace,Menlo,monospace}")
    parts.append(".file .badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:8px;font-weight:600;vertical-align:middle}")
    parts.append(".badge-always{background:#dff5e0;color:#1a6f1a}")
    parts.append(".badge-missing{background:#fde4e4;color:#a02020}")
    parts.append(".badge-extra{background:#fff7e6;color:#9c6a00}")
    parts.append("pre{background:#fcfcfc;border:1px solid #e8e8e8;border-radius:4px;padding:14px 18px;"
                 "overflow:auto;white-space:pre-wrap;word-wrap:break-word;font-family:ui-monospace,Menlo,monospace;"
                 "font-size:12px;line-height:1.55;margin:0 16px 14px}")
    parts.append(".adapter-prompt{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:14px 18px;margin:8px 0}")
    parts.append(".nudge{background:#fff;border:1px solid #e0e0e0;border-left:3px solid #d8a02c;border-radius:4px;padding:12px 16px;margin:10px 0}")
    parts.append(".nudge .cond{color:#9c6a00;font-size:11px;margin-bottom:8px;font-weight:600}")
    parts.append(".nudge pre{margin:0;background:#fafafa}")
    parts.append("table{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}")
    parts.append("th,td{border:1px solid #e0e0e0;padding:6px 10px;text-align:left;vertical-align:top}")
    parts.append("th{background:#f6f6f6;font-weight:600}")
    parts.append("code{font-family:ui-monospace,Menlo,monospace;font-size:11px;background:#f5f5f5;padding:1px 4px;border-radius:3px;color:#444}")
    parts.append(".footer{margin-top:40px;padding-top:14px;border-top:1px solid #e0e0e0;font-size:11px;color:#888}")
    parts.append("</style></head><body>")
    parts.append("<div class='wrap'>")
    parts.append("<h1>Harness prompt files</h1>")
    parts.append("<div class='meta'>Operator view &mdash; all files the harness injects into the LLM prompt, in injection order. "
                 "Updated live from disk on every page load. Identity files are per-agent (one set per agent in <code>pie/&lt;agent&gt;/identity/</code>). "
                 "System prompt + nudges are global (one each, shared across agents).</div>")
    parts.append("<div class='nav'>")
    parts.append("<a href='/'>Back to chat</a>")
    parts.append("<a href='/admin/last-prompt'>Last raw prompt (runtime)</a>")
    parts.append("<a href='/admin/last-emission'>Last emission</a>")
    parts.append("<a href='/admin/last-memories'>Last memory rows</a>")
    parts.append("</div>")

    # Section 1: per-agent identity files
    parts.append("<h2>1. Per-agent identity files (always-injected context)</h2>")
    parts.append("<div class='meta'>Read by <code>app/agent/bootstrap.py::BootstrapLoader</code> at app startup (and on <code>reload_all()</code>). "
                 "Wrapped in a <code>&lt;bootstrap&gt;...&lt;/bootstrap&gt;</code> block and prepended to the LLM prompt. "
                 "Which files are loaded is configured in <code>config.yaml</code> under <code>bootstrap.files</code> (see section 4). "
                 "Byte caps are enforced at boot; if any file exceeds its cap and <code>behavior_on_overflow</code> is <code>fail</code>, the harness refuses to start.</div>")
    canonical_names = {"SOUL.md", "USER.md", "AGENTS.md", "IDENTITY.md", "HEARTBEAT.md", "MEMORY.md"}
    for agent_name in agents:
        is_default = (agent_name == cfg.app.default_agent)
        files = identity_by_agent[agent_name]
        total_size = sum(f["size"] for f in files if f["exists"])
        parts.append(f"<div class='agent'>")
        parts.append(f"<div class='agent-header'>{esc(agent_name)}"
                     f"<span class='tag'>{'default' if is_default else 'agent'} &middot; "
                     f"{sum(1 for f in files if f['exists'])}/{len(files)} files present &middot; "
                     f"{file_size(total_size)} total</span></div>")
        for f in files:
            badge = ""
            cls = "file"
            if not f["exists"]:
                badge = "<span class='badge badge-missing'>missing</span>"
            elif f["name"] in canonical_names:
                badge = "<span class='badge badge-always'>always injected</span>"
            else:
                badge = "<span class='badge badge-extra'>on disk &middot; not in bootstrap.files</span>"
            parts.append(f"<details class='{cls}'>")
            parts.append(f"<summary>"
                         f"<span><span class='name'>{esc(f['name'])}</span>"
                         f"<span class='path'>{esc(f['path'])}</span>{badge}</span>"
                         f"<span class='meta'>{file_size(f['size']) if f['exists'] else '&mdash;'}</span>"
                         f"</summary>")
            if f["exists"]:
                parts.append(f"<pre>{esc(f['content'])}</pre>")
            else:
                parts.append(f"<pre style='color:#a02020'>&lt;file not present on disk&gt;</pre>")
            parts.append("</details>")
        parts.append("</div>")

    # Section 2: LLM adapter system prompt
    parts.append("<h2>2. LLM adapter system prompt (model-specific)</h2>")
    parts.append(f"<div class='meta'>From <code>app/agent/prompts/{esc(adapter['adapter'])}.py</code> (the <code>SYSTEM_PROMPT</code> constant). "
                 f"This is the only model-specific copy in the harness; everything else in the loop is shared. "
                 f"Injected as the <code>system</code> message of the LLM request, with <code>{{agent_name}}</code> and <code>{{agent_tone}}</code> "
                 f"rendered at agent startup.</div>")
    if adapter["docstring"]:
        parts.append(f"<div class='adapter-prompt'><strong>Module docstring:</strong><br><pre style='margin:8px 0 0'>{esc(adapter['docstring'])}</pre></div>")
    parts.append(f"<div class='adapter-prompt'><strong>SYSTEM_PROMPT template:</strong><br>"
                 f"<pre style='margin:8px 0 0'>{esc(adapter['system_prompt_template'])}</pre></div>")

    # Section 3: loop nudges
    parts.append("<h2>3. Conditionally-inserted nudges (from the agent loop)</h2>")
    parts.append("<div class='meta'>From <code>app/agent/loop.py</code>. These are appended to the conversation as a <code>user</code> message "
                 "when the model fails to emit a <code>tool_use</code> block (or claims a side effect in prose). The loop caps at <code>_MAX_NUDGES=4</code> "
                 "per turn &mdash; past that, further nudges are suppressed and the turn ends with no useful work done.</div>")
    for n in nudges:
        parts.append("<div class='nudge'>")
        parts.append(f"<div class='cond'>Nudge #{n['index']} &mdash; fires when: {esc(n['condition'])}</div>")
        parts.append(f"<pre>{esc(n['message'])}</pre>")
        parts.append("</div>")

    # Section 4: bootstrap config
    parts.append("<h2>4. Bootstrap config (which files are loaded + size caps)</h2>")
    parts.append("<div class='meta'>From <code>config.yaml</code> under <code>bootstrap</code>. Caps are in bytes; the env-var values are loaded at boot.</div>")
    parts.append("<table>")
    parts.append("<tr><th>File</th><th>Path (relative to <code>pie/&lt;agent&gt;/identity/</code>)</th><th>Max bytes</th><th>Max KB</th></tr>")
    for b in bootstrap:
        parts.append(f"<tr><td><code>{esc(b['name'])}</code></td>"
                     f"<td><code>{esc(b['path'])}</code></td>"
                     f"<td>{b['max_bytes']:,}</td>"
                     f"<td>{b['max_bytes'] / 1024:.1f} KB</td></tr>")
    parts.append("</table>")
    parts.append(f"<div class='meta'>Behavior on overflow: <code>{esc(cfg.bootstrap.behavior_on_overflow)}</code> "
                 f"(<code>fail</code> = refuse to start with FATAL log; <code>truncate</code> = warn and cut at cap).</div>")

    parts.append("<div class='footer'>"
                 "Generated by <code>app/routes/web.py::admin_prompts_view</code> on "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} AEST. "
                 "Files re-read from disk on every page load &mdash; no caching. "
                 "If you edit an identity file in <code>pie/&lt;agent&gt;/identity/</code>, the next page load shows the new content; "
                 "the harness's loaded bootstrap updates only after a <code>POST /api/v1/admin/reload-bootstrap</code> (planned) or a harness restart."
                 "</div>")
    parts.append("</div></body></html>")
    return "".join(parts)
