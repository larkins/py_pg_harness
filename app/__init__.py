"""Flask app factory for harness.

Loads config, builds extensions, registers blueprints, applies security
headers, installs log redaction + rate limiting + auth.

Phase 8 (2026-10-08): security hardening.
Phase 9 (2026-10-08): login against public.users (Flask-Login).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask
from flask_login import LoginManager

from .auth import bp as auth_bp
from .config import AppConfig, load_config
from .extensions import build_extensions
from .routes.api import bp as api_bp
from .routes.groups import bp as groups_bp
from .routes.web import bp as web_bp
from .security import (
    apply_security_headers,
    install_log_redaction,
    install_rate_limiter,
    install_request_id,
    sanity_check_env,
)


def _apply_per_route_rate_limits(app: Flask) -> None:
    """Apply per-route rate limits via Flask-Limiter's `lim.limit(...)`
    decorator pattern. We walk view_functions and rewrap matching
    endpoints. No-op if the limiter isn't installed.
    """
    cfg: AppConfig = app.config["HARNESS_CONFIG_OBJ"]
    lim = app.extensions.get("limiter")
    if lim is None:
        return

    chat_lim = cfg.security.rate_limit.chat
    mem_lim = cfg.security.rate_limit.memory
    ROUTE_LIMITS = {
        "api.chat": chat_lim,
        "api.memories_search": mem_lim,
        "api.memory_pin": mem_lim,
    }

    log_ = logging.getLogger(__name__)
    for endpoint, limit_str in ROUTE_LIMITS.items():
        view = app.view_functions.get(endpoint)
        if view is None:
            continue
        try:
            app.view_functions[endpoint] = lim.limit(limit_str)(view)
            log_.info("rate_limit applied: %s -> %s", endpoint, limit_str)
        except Exception as e:
            log_.warning("rate_limit wrap failed for %s: %s", endpoint, e)


def _init_login(app: Flask) -> None:
    """Wire up Flask-Login with our User model.

    - user_loader queries public.users by id
    - session cookie attrs: Secure, HttpOnly, SameSite=Lax
    - permanent session lifetime: 30 days
    - CSRF defense via SameSite=Lax + Origin-header check in app.auth
    """
    from .auth import load_user, init_csrf
    init_csrf(app)
    lm = LoginManager()
    lm.user_loader(load_user)
    lm.init_app(app)
    # Don't redirect to a login view — our API returns 401 JSON instead.
    lm.unauthorized_handler(lambda: ({"error": "login required", "code": "auth"}, 401))


def create_app(config: AppConfig | None = None) -> Flask:
    cfg = config or load_config(
        os.environ.get("HARNESS_CONFIG", "config.yaml"),
    )

    install_log_redaction()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    env_file = Path(os.environ.get("HARNESS_CONFIG", "config.yaml")).parent / ".env"
    sanity_check_env(env_file)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SECRET_KEY"] = cfg.app.secret_key
    app.config["HARNESS_DEFAULT_AGENT"] = cfg.app.default_agent
    app.config["HARNESS_CONFIG_OBJ"] = cfg
    # Phase 10: image uploads. MAX_CONTENT_LENGTH is Flask's hard
    # cap on request body size (multipart bodies included); requests
    # over the limit get a 413 before the view runs. Default 10 MiB.
    # HARNESS_UPLOAD_DIR is the on-disk location for stored files.
    upload_dir = os.environ.get(
        "HARNESS_UPLOAD_DIR",
        str(Path(__file__).resolve().parent.parent / "uploads"),
    )
    app.config["HARNESS_UPLOAD_DIR"] = upload_dir
    app.config["HARNESS_MAX_UPLOAD_BYTES"] = int(
        os.environ.get("HARNESS_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))
    )
    # MAX_CONTENT_LENGTH must be a little above the per-file cap so
    # a multipart body with the file + a small form envelope still
    # fits; the per-file cap is re-checked in the view.
    app.config["MAX_CONTENT_LENGTH"] = app.config["HARNESS_MAX_UPLOAD_BYTES"] + 64 * 1024
    # Session cookie params (set BEFORE LoginManager.init_app so it sees them).
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30

    app.extensions["harness"] = build_extensions(cfg)

    apply_security_headers(app, cfg)
    install_request_id(app)
    install_rate_limiter(app, cfg)
    _init_login(app)

    # Persist in-flight turns when the worker is asked to stop
    # (2026-10-22). gunicorn installs its own SIGTERM handler in
    # Worker.init_signals() -- which runs BEFORE load_wsgi(), so
    # create_app() is the right place to chain onto it. Without this, a
    # service restart in the middle of a turn leaves the user turn in
    # the table with no reply and no trace (Mal's "why didn't Evie
    # respond?" on 2026-10-22 07:51).
    try:
        from app.agent.turn_guard import install_sigterm_handler
        install_sigterm_handler()
    except Exception:
        logging.getLogger("app").exception("SIGTERM handler install failed")

    app.register_blueprint(web_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    # Group chats (2026-10-23): /api/v1/groups/* + /api/v1/agents +
    # /api/v1/admin/users.
    app.register_blueprint(groups_bp)
    # Apply per-route rate limits AFTER blueprint registration so the
    # view_functions dict has the endpoints.
    _apply_per_route_rate_limits(app)

    return app
