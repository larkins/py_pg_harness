"""Shared safety + audit-log helpers for the harness agent's tools
(memory + sql_query + exec_bash).

After the 2026-10-15 simplification (drop read_file, list_directory,
http_get because they were redundant with exec_bash), the path_safe
helper and its home-directory allowlist are no longer used. Kept
elsewhere in this file: the audit_log, build_dsn, exec_audit_log,
classify_command, and safe_env helpers — all still actively used.

DESIGN (post-simplification)
- audit_log(...) — append one JSON line to
  <install_dir>/logs/tool_audit.jsonl for the read-only-ish tools
  (memory_*, sql_query). Every call gets exactly one entry (after
  execution, with success/failure, duration, result summary).
- build_dsn(db_name) — returns a libpq DSN for the 'harness'
  database (read at first call from <install_dir>/.env via
  DATABASE_URL; the service user owns that file, so the read always
  succeeds) or for the optional 'aux' database, whose DSN is
  operator-configured (see the HARNESS_AUX_* env vars below).
  Cached for the process lifetime so we don't re-read on every call.
- exec_audit_log(...) — same shape but writes to
  <install_dir>/logs/exec_audit.jsonl (separate file because exec
  calls are higher-stakes).
- classify_command(command) + safe_env() + _HARD_BLOCK_PATTERNS +
  _SOFT_FLAG_PATTERNS — the exec_bash safety classifier. See
  exec_bash.py for the full design.

PATHS: nothing in this file is hardcoded. APP_ROOT is the install
directory (the repo root — this file lives at
<install_dir>/app/agent/tools/_safety.py, hence parents[3]) and
HOME_DIR is the service user's home directory. Both are derived at
import time, so the same code works on any host and under any Unix
user.
"""
from __future__ import annotations

import json
import os
import re
import time
import threading
from pathlib import Path
from typing import Any


# --- derived locations ----------------------------------------------------

# The install directory (repo root). This file is
# <install_dir>/app/agent/tools/_safety.py, so parents[3] is the root.
APP_ROOT = Path(__file__).resolve().parents[3]

# The service user's home directory (the Unix user the harness runs
# as). On the live install this is the same value the code used to
# hardcode.
HOME_DIR = Path.home()


# Audit log: append-only JSON lines. One process, the service user
# owns the file. The directory is reused from the harness's existing
# logging setup; if it's missing we create it.
_AUDIT_DIR = APP_ROOT / "logs"
_AUDIT_FILE = _AUDIT_DIR / "tool_audit.jsonl"
_AUDIT_LOCK = threading.Lock()


def audit_log(
    *,
    tool: str,
    args: dict[str, Any],
    user_id: int | None,
    conversation_id: str | None,
    outcome: str,            # "ok" | "blocked" | "error"
    result: Any = None,      # result summary (truncated by caller)
    duration_ms: int = 0,
    error: str | None = None,
) -> None:
    """Append one JSON line to the audit log. Safe to call from
    any thread; serialised by an internal lock. Never raises."""
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            # datetime.now(timezone.utc).isoformat() produces
            # "2026-10-14T04:47:40.123456+00:00". Drop the microseconds
            # + normalise the offset to literal "Z" for a cleaner
            # grep-friendly timestamp.
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "args": _redact_args(args),
            "outcome": outcome,
            "duration_ms": duration_ms,
            "error": error,
        }
        if result is not None:
            entry["result_summary"] = _summarise_result(result)
        line = json.dumps(entry, default=str, separators=(",", ":"))
        with _AUDIT_LOCK:
            with _AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        # Audit must never break the tool call. Log to stderr and
        # move on. The user can detect gaps by comparing the
        # timestamp in the audit file against the conversation
        # history.
        import sys
        print(f"audit_log failed for tool={tool}: {e}", file=sys.stderr)


# Heuristics: keep the audit file small + safe to grep.
def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Drop very large arg values, redact obvious secrets, and
    cap string lengths so one giant SQL query doesn't bloat the
    audit file."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        kl = k.lower()
        if any(s in kl for s in ("password", "secret", "token", "key", "credential")):
            out[k] = "***REDACTED***"
            continue
        if isinstance(v, str) and len(v) > 1000:
            out[k] = v[:1000] + f"...(truncated, total {len(v)} chars)"
            continue
        out[k] = v
    return out


def _summarise_result(result: Any) -> Any:
    """For dict results: keep keys + a small preview of each value.
    For string results: first 500 chars. For lists: first 5 items.
    The full result is returned to the model, so this is just for
    audit readability."""
    if isinstance(result, dict):
        return {k: _preview(v) for k, v in result.items()}
    return _preview(result)


def _preview(v: Any) -> Any:
    if isinstance(v, str):
        return v[:200] + (f"...(truncated, total {len(v)} chars)" if len(v) > 200 else "")
    if isinstance(v, list):
        return [_preview(x) for x in v[:5]] + (
            [f"...(truncated, total {len(v)} items)"] if len(v) > 5 else []
        )
    return v


# --- DSN building (used by sql_query) -----------------------------------

_DSN_CACHE: dict[str, str] = {}
_DSN_LOCK = threading.Lock()

# The optional second ("aux") database is operator-configured. The
# resolution order is:
#   1. HARNESS_AUX_DSN       — the libpq DSN itself
#   2. HARNESS_AUX_DSN_ENV   — the NAME of another env var that
#                              holds the libpq DSN
#   3. HARNESS_AUX_ENV_FILE  — path to a .env file with individual
#                              PG* variables (PGHOST/PGPORT/PGDATABASE/
#                              PGUSER/PGPASSWORD) to build the DSN from
# If none of them is set, the aux database is simply unavailable and
# build_dsn() raises ValueError for that db_name.
AUX_DSN_ENV_VAR = "HARNESS_AUX_DSN"
AUX_DSN_ENV_NAME_VAR = "HARNESS_AUX_DSN_ENV"
AUX_ENV_FILE_ENV_VAR = "HARNESS_AUX_ENV_FILE"

# Comma-separated list of extra db_name aliases that resolve to the
# aux database (so an existing deployment can keep its historical
# name working). Example: HARNESS_AUX_DB_ALIASES=legacy_db
AUX_DB_ALIASES_ENV_VAR = "HARNESS_AUX_DB_ALIASES"


def _aux_db_aliases() -> list[str]:
    """Extra db_names that resolve to the aux database."""
    raw = os.environ.get(AUX_DB_ALIASES_ENV_VAR, "") or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


def known_db_names() -> list[str]:
    """The db_names build_dsn() accepts: 'harness', 'aux', plus any
    alias configured through HARNESS_AUX_DB_ALIASES."""
    return ["harness", "aux"] + _aux_db_aliases()


def build_dsn(db_name: str) -> str:
    """Return a libpq DSN for the named database.

    Recognised names:
      - 'harness' — the harness database itself. The DSN is read at
        first call from <install_dir>/.env via DATABASE_URL. The
        service user owns that file, so the read always succeeds.
      - 'aux' (plus any alias listed in HARNESS_AUX_DB_ALIASES) — an
        optional second database. Its DSN comes from the operator's
        HARNESS_AUX_* configuration; when none is set this database
        is unavailable and ValueError is raised.

    Cached for the process lifetime. Reads the .env file once on
    first call, then never touches disk again.
    """
    if db_name in _DSN_CACHE:
        return _DSN_CACHE[db_name]
    with _DSN_LOCK:
        if db_name in _DSN_CACHE:
            return _DSN_CACHE[db_name]
        if db_name == "harness":
            _DSN_CACHE[db_name] = _read_url_from_envfile(
                APP_ROOT / ".env", "DATABASE_URL"
            )
        elif db_name == "aux" or db_name in _aux_db_aliases():
            _DSN_CACHE[db_name] = _build_aux_dsn()
        else:
            known = known_db_names()
            raise ValueError(
                f"unknown db_name {db_name!r}; expected one of {known}"
            )
        return _DSN_CACHE[db_name]


def _read_url_from_envfile(path: Path, key: str) -> str:
    """Tiny dotenv reader — only handles KEY=VALUE, ignores comments
    and blank lines. The harness .env is well-formed (no export,
    no multiline, no quoted values with embedded newlines), so
    this is enough. Don't add dependencies; we already have
    python-dotenv in the harness venv if we ever need it."""
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(rf"^{re.escape(key)}\s*=\s*(.*)$", line)
        if m:
            val = m.group(1).strip()
            # Strip optional surrounding quotes
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            return val
    raise FileNotFoundError(f"{key!r} not found in {path}")


def _build_aux_dsn() -> str:
    """Build the DSN for the optional auxiliary database.

    Resolution order:
      1. HARNESS_AUX_DSN — a full libpq DSN.
      2. HARNESS_AUX_DSN_ENV — the name of another env var holding a
         full libpq DSN.
      3. HARNESS_AUX_ENV_FILE — path to a .env file with individual
         PG* variables, from which a DSN is assembled.

    Raises ValueError when the aux database is not configured.
    """
    dsn = (os.environ.get(AUX_DSN_ENV_VAR) or "").strip()
    if dsn:
        return dsn

    env_name = (os.environ.get(AUX_DSN_ENV_NAME_VAR) or "").strip()
    if env_name:
        dsn = (os.environ.get(env_name) or "").strip()
        if dsn:
            return dsn
        raise ValueError(
            f"{AUX_DSN_ENV_NAME_VAR}={env_name!r} but {env_name!r} is empty"
        )

    env_file = (os.environ.get(AUX_ENV_FILE_ENV_VAR) or "").strip()
    if env_file:
        return _build_dsn_from_pg_envfile(Path(env_file))

    raise ValueError(
        "the auxiliary database is not configured; set "
        f"{AUX_DSN_ENV_VAR} (or {AUX_DSN_ENV_NAME_VAR} / "
        f"{AUX_ENV_FILE_ENV_VAR}) to enable it"
    )


def _build_dsn_from_pg_envfile(path: Path) -> str:
    """Some .env files use individual PG* variables rather than a
    single DATABASE_URL. Build a libpq DSN from them."""
    text = path.read_text(encoding="utf-8", errors="replace")
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    host = env.get("PGHOST", "127.0.0.1")
    port = env.get("PGPORT", "5432")
    db = env.get("PGDATABASE")
    user = env.get("PGUSER")
    pw = env.get("PGPASSWORD")
    if not (db and user and pw):
        raise FileNotFoundError(
            f"PGDATABASE / PGUSER / PGPASSWORD must be set in {path}"
        )
    # libpq DSN form: postgresql://user:pass@host:port/db
    # URL-encode the password in case it has special chars.
    from urllib.parse import quote
    return f"postgresql://{user}:{quote(pw, safe='')}@{host}:{port}/{db}"


import re as _re


# Hard-blocked: deny outright, never run. Each pattern is matched
# as a substring (with word-boundary care where needed). The
# LLM is told in the system prompt that these are absolute.
_HARD_BLOCK_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    # Privilege escalation. The whole point of "without sudo" is
    # that the agent can't escalate. Any sudo/su is a hard block.
    (_re.compile(r"\bsudo\b"), "sudo not allowed (no privilege escalation)"),
    (_re.compile(r"^\s*su(\s|$)"), "su not allowed (no privilege escalation)"),
    (_re.compile(r"\bsu\s+-\s*"), "su - not allowed (no privilege escalation)"),
    # Catastrophic / destructive.
    (_re.compile(r"\brm\s+(-[a-zA-Z]*)?-rf?\s+/\s"), "rm -rf on root paths not allowed"),
    (_re.compile(r"\bmkfs(\.\w+)?\b"), "mkfs not allowed"),
    (_re.compile(r"\bdd\s+.*\bof=/dev/"), "dd of=/dev not allowed"),
    (_re.compile(r">\s*/dev/sd[a-z]"), "writing to /dev/sd* not allowed"),
    # Fork bomb.
    (_re.compile(r":\(\)\s*\{"), "fork-bomb pattern not allowed"),
    # Piped remote code execution — we have http_get for that,
    # safer because it has the URL/domain audit trail.
    (_re.compile(r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b"), "curl|sh not allowed"),
    (_re.compile(r"\bwget\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b"), "wget|sh not allowed"),
    # System paths that would be a privilege-escalation concern.
    (_re.compile(r"(^|\s)/etc/(passwd|shadow|sudoers|shadow\.)"), "/etc/{passwd,shadow,sudoers} not allowed"),
    (_re.compile(r"(^|\s)/etc/systemd/system/"), "modifying /etc/systemd/system/* not allowed"),
    # Privilege elevation via setuid bits.
    (_re.compile(r"\bchmod\s+[0-7]*[4-7][0-7][0-7]\b"), "chmod with setuid/setgid bits not allowed"),
    (_re.compile(r"\bchmod\s+777\b"), "chmod 777 not allowed"),
    # System-level systemd (mask/disable on system units, not user).
    (_re.compile(r"\bsystemctl\s+(mask|disable)\s+(?!(--user\s+))"), "systemctl mask/disable (system) not allowed; use --user"),
]


# Soft-flag: the agent must ask the user before running. Returns
# requires_confirmation=true. The LLM is told in the system prompt
# to re-call with confirm=true ONLY after the user has said
# "yes" in chat.
#
# NOTE: the systemctl pattern below only flags the project-defined
# "harness" / "harness-tunnel" service names. Other service names
# fall through unflagged — that is NOT an explicit allowlist, it is
# just what the current pattern matches. If you need a different
# project's services to require confirmation, add them to the regex
# below (or generalize via config — see follow-up work for OSS prep).
#
# The path-based patterns are derived from HOME_DIR (the service
# user's home) so the same rules apply on any install.
_SOFT_FLAG_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    (
        _re.compile(
            r"\bsystemctl\s+--user\s+(stop|restart|disable)\s+"
            r"(harness|harness-tunnel)\b"
        ),
        "stopping or restarting harness / harness-tunnel would "
        "kill the chat surface you're currently using. "
        "Confirm with the user before proceeding.",
    ),
    (
        _re.compile(r"\bchmod\s+(-[a-zA-Z]*)?-R\b"),
        "recursive chmod — confirm with the user before proceeding.",
    ),
    (
        # A .service file inside a systemd/ directory under the
        # service user's home (e.g. a project checkout that ships its
        # own unit files).
        _re.compile(
            r"(^|\s|>)\s*" + _re.escape(str(HOME_DIR))
            + r"/(?:[^/\s]+/)?systemd/.*\.service\b"
        ),
        "modifying a .service file in a systemd/ directory under the "
        "service user's home — confirm with the user before proceeding "
        "(the live unit is in ~/.config/systemd/user/).",
    ),
    (
        _re.compile(
            r"(^|\s|>)\s*" + _re.escape(str(HOME_DIR))
            + r"/\.config/systemd/user/.*\.service\b"
        ),
        "modifying a live systemd user unit — confirm with the "
        "user before proceeding (this would change the runtime "
        "unit, not just the in-repo file).",
    ),
    (
        _re.compile(
            r"\brm\s+(-[a-zA-Z]*)?-rf?\s+" + _re.escape(str(HOME_DIR))
            + r"/.*/uploads/"
        ),
        "rm -rf in an uploads/ directory — user-uploaded files "
        "could be lost. Confirm with the user before proceeding.",
    ),
    (
        _re.compile(r"(^|\s|;)\s*(dd\s+.*\bof=/dev/zero|truncate\s+-s\s+0\s+/|truncate\s+-s\s+0\s+/" + _re.escape(str(HOME_DIR)) + r"|find\s+/\s+-delete|find\s+/\s+-xdev)"),
        "operation that could fill the disk or wipe a tree — "
        "confirm with the user before proceeding.",
    ),
]


def classify_command(command: str) -> tuple[str, str | None]:
    """Inspect a shell command. Returns one of:
        ("allow", None)        — the command passes both filters
        ("block", reason)      — hard-blocked, will not run
        ("confirm", reason)    — soft-flagged, requires confirm=true

    The LLM is told in the system prompt:
      - "allow"   -> run it
      - "block"   -> tool returns an error, do NOT retry
      - "confirm" -> tool returns requires_confirmation=true; ASK
                     THE USER in chat, then re-call with confirm=true
    """
    if not command or not command.strip():
        return "block", "empty command"

    for pattern, reason in _HARD_BLOCK_PATTERNS:
        if pattern.search(command):
            return "block", reason

    for pattern, reason in _SOFT_FLAG_PATTERNS:
        if pattern.search(command):
            return "confirm", reason

    return "allow", None


# Env-var blocklist for exec_bash. The runtime inherits
# a bunch of env vars (LLM_API_KEY, DATABASE_URL, JWT_SECRET, etc.)
# and the model could `printenv` them. We pass a stripped env to
# the subprocess so the only vars the model sees are the safe ones.
EXEC_BASH_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "TZ", "PWD", "OLDPWD", "TERM", "COLORTERM", "DISPLAY", "XDG_RUNTIME_DIR",
    "XDG_SESSION_ID", "XDG_SESSION_TYPE", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS",
    "HOSTNAME", "LOGNAME", "MAIL", "SHLVL", "_",
    "HARNESS_BIND", "HARNESS_CONFIG",
})
EXEC_BASH_SECRET_PATTERNS = (
    "KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL", "AUTH",
)


def safe_env() -> dict[str, str]:
    """Return a copy of os.environ with secret-looking vars removed.
    Used by exec_bash so the model can't `printenv LLM_API_KEY`."""
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in EXEC_BASH_SAFE_ENV_KEYS:
            out[k] = v
            continue
        ku = k.upper()
        if any(p in ku for p in EXEC_BASH_SECRET_PATTERNS):
            continue
        if len(v) >= 32 and any(c in v for c in "+/=") and v.replace("+", "").replace("/", "").replace("=", "").isalnum():
            continue
        out[k] = v
    return out


def exec_audit_log(
    *,
    command: str,
    cwd: str | None,
    user_id: int | None,
    conversation_id: str | None,
    outcome: str,         # "ok" | "blocked" | "confirm_required" | "error" | "timeout"
    exit_code: int | None = None,
    duration_ms: int = 0,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    blocked_reason: str | None = None,
    error: str | None = None,
) -> None:
    """Append one JSON line to the exec audit log. Separate file
    from tool_audit.jsonl because exec calls are higher-stakes
    and warrant their own grep target for incident response.

    File: <install_dir>/logs/exec_audit.jsonl
    """
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": command if len(command) <= 2000 else command[:2000] + "...(truncated)",
            "cwd": cwd,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "blocked_reason": blocked_reason,
            "error": error,
        }
        line = json.dumps(entry, default=str, separators=(",", ":"))
        with _AUDIT_LOCK:
            with (_AUDIT_DIR / "exec_audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        import sys
        print(f"exec_audit_log failed: {e}", file=sys.stderr)
