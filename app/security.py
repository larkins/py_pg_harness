"""Security hardening for harness.

Phase 8 (2026-10-08):
- Security headers (X-Content-Type-Options, X-Frame-Options, HSTS,
  Referrer-Policy, CSP) applied to every response.
- Log redaction: any `LLM_API_KEY= ***` or `Authorization: Bearer ***`
  pattern in a log record is masked before emission.
- Per-request `X-Request-Id` for log correlation.
- Sanity check at startup: refuse to start if `.env` is world-readable,
  has empty required keys, or has a `***…` placeholder.
- Rate limiting via Flask-Limiter (in-memory, process-wide).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from flask import Flask, current_app, g, request

from .config import AppConfig

# Regex for redaction. Order matters: bearer first (more specific).
_REDACT_BEARER = re.compile(r"(Authorization:\s*Bearer\s+)([A-Za-z0-9_\-\.]+)", re.IGNORECASE)
_REDACT_KEY = re.compile(r"(LLM_API_KEY\s*=\s*)([^\s\"']+)", re.IGNORECASE)
_MASK = "***REDACTED***"


class KeyRedactionFilter(logging.Filter):
    """Mask any `LLM_API_KEY= ***` line or `Authorization: Bearer ***` that
    slips into a log record (msg + args). One filter instance is shared
    across all loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _REDACT_BEARER.sub(rf"\1{_MASK}", record.msg)
            record.msg = _REDACT_KEY.sub(rf"\1{_MASK}", record.msg)
        if record.args:
            new_args = []
            for a in record.args:
                if isinstance(a, str):
                    a = _REDACT_BEARER.sub(rf"\1{_MASK}", a)
                    a = _REDACT_KEY.sub(rf"\1{_MASK}", a)
                new_args.append(a)
            record.args = tuple(new_args)
        return True


_REDACTION_FILTER: KeyRedactionFilter | None = None


def install_log_redaction() -> None:
    """Install the redaction filter on the root logger (and key child loggers)."""
    global _REDACTION_FILTER
    if _REDACTION_FILTER is None:
        _REDACTION_FILTER = KeyRedactionFilter()
    root = logging.getLogger()
    if _REDACTION_FILTER not in root.filters:
        root.addFilter(_REDACTION_FILTER)
    for name in ("gunicorn.error", "werkzeug"):
        lg = logging.getLogger(name)
        if _REDACTION_FILTER not in lg.filters:
            lg.addFilter(_REDACTION_FILTER)


def apply_security_headers(app: Flask, config: AppConfig) -> None:
    h = config.security.headers

    @app.after_request
    def _headers(response):
        response.headers["X-Content-Type-Options"] = h.x_content_type_options
        response.headers["X-Frame-Options"] = h.x_frame_options
        response.headers["Strict-Transport-Security"] = h.strict_transport_security
        response.headers["Referrer-Policy"] = h.referrer_policy
        response.headers["Content-Security-Policy"] = h.csp
        return response


def install_request_id(app: Flask) -> None:
    """Add a per-request UUID for log correlation."""

    @app.before_request
    def _start():
        g.request_id = request.headers.get("X-Request-Id") or uuid4().hex[:12]

    @app.after_request
    def _end(response):
        rid = getattr(g, "request_id", None)
        if rid:
            response.headers["X-Request-Id"] = rid
            current_app.logger.info(
                "request_id=%s method=%s path=%s status=%s",
                rid, request.method, request.path, response.status_code,
            )
        return response


def sanity_check_env(env_path: str | os.PathLike[str] = ".env") -> None:
    """Refuse to start if .env is world-readable or contains an empty key
    or a `***…` placeholder. Idempotent: safe to call multiple times."""
    p = Path(env_path)
    if not p.exists():
        return
    mode = p.stat().st_mode & 0o777
    if mode & 0o044:
        raise RuntimeError(
            f"security: {p} is world-readable (mode={oct(mode)}). "
            f"Run: chmod 600 {p}"
        )
    content = p.read_text()
    for key in ("HARNESS_SECRET_KEY", "LLM_API_KEY", "DATABASE_URL"):
        # Match the line up to the newline. `\s*` around `=` is fine (it
        # only matches spaces), but the value capture must stop at the
        # first newline to avoid the next line leaking in (since
        # `\s*` greedily matches newlines).
        m = re.search(rf"^{key}=([^\n]*)", content, re.MULTILINE)
        if m and not m.group(1).strip():
            raise RuntimeError(f"security: {key} is empty in {p}")
    # Reject obvious placeholders. The value (no trailing whitespace) is
    # everything between `=` and the next whitespace-or-line-end.
    m = re.search(r"^LLM_API_KEY=([^\s\n]*)", content, re.MULTILINE)
    if m:
        val = m.group(1)
        if val.startswith("***") or "****" in val or val == "replace_me":
            raise RuntimeError(
                f"security: LLM_API_KEY looks like a placeholder ({val[:6]}…). "
                f"Edit {p} and set the real key."
            )


def install_rate_limiter(app: Flask, config: AppConfig) -> None:
    """Apply Flask-Limiter with the config's rate_limit values. In-memory
    storage; process-wide. For multi-worker, switch to Redis.
    """
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    lim = Limiter(
        get_remote_address,
        app=app,
        default_limits=[config.security.rate_limit.default],
        storage_uri="memory://",
        strategy="fixed-window",
    )
    app.extensions["limiter"] = lim
