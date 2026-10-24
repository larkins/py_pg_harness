"""Authentication for harness.

Phase 9 add. Login against `public.users` in the harness database.
Per the design pattern (MEMORY.md "Customer Postgres pattern"), `public.users`
is the canonical user table — owned by the operator role,
DML-allowed for the runtime role (`harness_app`).

Auth model (v0.2):
- Password hashing via Werkzeug's pbkdf2:sha256 (default 600k iterations, 16-byte salt).
- Session via Flask signed cookies. LoginManager handles the user_loader.
- Cookie attrs: Secure, HttpOnly, SameSite=Lax. Set on the response.
- POST /api/v1/auth/login accepts JSON `{email, password}` and sets the session.
- `@login_required` decorator on protected API routes.
- The /api/v1/auth/login route itself is public (returns 200 with the current
  auth status so the JS can decide whether to show the login form).
- POST /api/v1/auth/logout clears the session.

CSRF: SameSite=Lax blocks cross-origin form POSTs. Same-origin JS calls
(served from the same origin) carry the cookie automatically. We
also check the Origin header on POSTs to /api/v1/* — origin must match
the request's Host or be empty (same-origin).
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, request, session
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")

# CSRF Origin check — applied via before_request on the api blueprint
# (or globally). For now we do it per-route in this module.
_ALLOWED_ORIGIN_PATTERNS = ()  # set in init_csrf() based on the request's host


def init_csrf(app) -> None:
    """Configure the session cookie + Origin check.

    Called from create_app() after config is loaded. Sets:
    - session cookie: Secure, HttpOnly, SameSite=Lax
    - permanent session lifetime: 30 days
    - SESSION_COOKIE_SAMESITE = "Lax"
    - SESSION_COOKIE_HTTPONLY = True
    - SESSION_COOKIE_SECURE = True (only sent over HTTPS in production)

    We rely on SameSite=Lax + an Origin check (below) for CSRF defense.
    """
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SECURE", True)
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", 60 * 60 * 24 * 30)  # 30 days

    @app.before_request
    def _check_origin_on_state_changing():
        # Only POSTs / PUTs / DELETEs need Origin verification. GETs are safe.
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return None
        # Allow same-origin (Origin matches Host) and no Origin (curl / direct API)
        origin = request.headers.get("Origin")
        if origin is None:
            return None
        # Build a comparable host:port string
        host = request.host  # includes :port if non-default
        # Origin is "scheme://host[:port]"; compare host
        try:
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            origin_host = parsed.netloc
        except Exception:
            return jsonify({"error": "invalid Origin header"}), 403
        if origin_host != host:
            log.warning("auth: blocked cross-origin POST: origin=%s host=%s", origin, host)
            return jsonify({"error": "cross-origin request blocked"}), 403
        return None


# --- User model -----------------------------------------------------------

class User:
    """A `public.users` row, adapted for Flask-Login.

    Required by Flask-Login: is_authenticated, is_active, is_anonymous,
    get_id(). Plus a classmethod to load from the DB.
    """

    def __init__(self, row: dict) -> None:
        self.id = row["id"]
        self.email = row["email"]
        self.display_name = row.get("display_name") or row["email"]
        self.role = row.get("role") or "member"
        self.is_active_flag = bool(row.get("is_active", True))
        # We only pull password_hash when checking; never serialize to JSON.
        self._password_hash = row.get("password_hash")

    # Flask-Login required properties
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_active(self) -> bool:
        return self.is_active_flag

    @property
    def is_anonymous(self) -> bool:
        return False

    def get_id(self) -> str:
        return str(self.id)

    def check_password(self, password: str) -> bool:
        if not self._password_hash:
            return False
        try:
            return check_password_hash(self._password_hash, password)
        except (ValueError, TypeError):
            return False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "is_active": self.is_active_flag,
        }


def load_user(user_id: str) -> User | None:
    """Flask-Login user_loader. Called on every request to rehydrate the
    session user from the DB. Returns None if the user no longer exists
    or is inactive."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    pool = current_app.extensions["harness"]["pool"]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, email, display_name, role, is_active, password_hash "
                "FROM public.users WHERE id = %s",
                (uid,),
            )
            row = cur.fetchone()
    if not row or not row.get("is_active", True):
        return None
    return User(row)


# --- Auth blueprint routes -----------------------------------------------

@bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    pool = current_app.extensions["harness"]["pool"]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, email, display_name, role, is_active, password_hash "
                "FROM public.users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
    if not row or not row.get("is_active", True):
        return jsonify({"error": "invalid credentials"}), 401
    user = User(row)
    if not user.check_password(password):
        return jsonify({"error": "invalid credentials"}), 401

    # Set the session. session.clear() prevents session-fixation.
    session.clear()
    session["_user_id"] = user.get_id()
    session["_email"] = user.email
    session.permanent = True
    log.info("auth: login user_id=%s email=%s", user.id, user.email)
    return jsonify({"user": user.to_dict()}), 200


@bp.route("/logout", methods=["POST"])
def logout():
    user_id = session.get("_user_id")
    session.clear()
    log.info("auth: logout user_id=%s", user_id)
    return jsonify({"ok": True}), 200


@bp.route("/me", methods=["GET"])
def me():
    """Returns the current user or 401 if not authenticated.

    The JS calls this on page load to decide whether to show the login
    form or the chat.
    """
    user_id = session.get("_user_id")
    if not user_id:
        return jsonify({"authenticated": False}), 200
    user = load_user(user_id)
    if not user:
        session.clear()
        return jsonify({"authenticated": False}), 200
    return jsonify({"authenticated": True, "user": user.to_dict()}), 200


# --- Decorator for protected routes --------------------------------------

def login_required_json(f: Callable) -> Callable:
    """Like flask_login.login_required, but returns 401 JSON instead of
    redirecting to a login page (this is an API, not a server-rendered
    HTML app)."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        from flask import g as _g
        if not session.get("_user_id"):
            return jsonify({"error": "login required", "code": "auth"}), 401
        # Stash the user on g for the view to use.
        user = load_user(session["_user_id"])
        if user is None:
            session.clear()
            return jsonify({"error": "login required", "code": "auth"}), 401
        _g.current_user = user
        return f(*args, **kwargs)

    return wrapper


def current_user() -> User | None:
    """Convenience: returns the current user object or None.

    Looks at g.current_user (set by login_required_json or by the
    _web_load_user before_request hook in web.py for HTML routes).
    """
    from flask import g
    return getattr(g, "current_user", None)


# --- CLI: create_user ----------------------------------------------------

def create_user(email: str, password: str, display_name: str | None = None,
                role: str = "member") -> int:
    """Create or update a user. Returns the user's id.

    Idempotent: if the email already exists, updates the password hash
    (useful for password rotation) and display_name.
    """
    from psycopg.rows import dict_row
    pool = current_app.extensions["harness"]["pool"]
    ph = generate_password_hash(password, method="pbkdf2:sha256")
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO public.users (email, password_hash, display_name, role, is_active, updated_at)
                VALUES (%s, %s, %s, %s, TRUE, now())
                ON CONFLICT (email) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        display_name = COALESCE(EXCLUDED.display_name, public.users.display_name),
                        role = EXCLUDED.role,
                        updated_at = now()
                RETURNING id
                """,
                (email.lower(), ph, display_name or email.split("@")[0], role),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"])
