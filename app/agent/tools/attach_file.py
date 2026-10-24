"""attach_file tool — copy a server-side file into the harness uploads
dir and return a URL the chat UI can render as a file-attachment icon.

Why: the agent routinely generates files the user wants to
download — logs, CSV exports, generated code, debug bundles.
The LLM has a `exec_bash` tool, but that just dumps file contents
into the chat text. We want a *downloadable* file the user can click
on. This tool gives the LLM that capability.

How it works (server-side flow):
  1. Validate `path` is under the service user's home dir and
     <= 10 MiB.
  2. Validate the file extension is in an allowlist (text + common
     binary formats the user might actually want to download).
  3. Read the file, base64 it, write a fresh <uuid>.<ext> copy to
     `<install_dir>/uploads/` — same dir the user-side image
     uploads land in, served by the existing auth-gated
     /api/v1/uploads/<id>.<ext> route.
  4. Return the URL + metadata. The loop picks up the attachment
     and:
       - emits an `attachment` streaming event so the live UI
         bubble can show the icon as the file is attached
       - persists the attachment on the assistant turn (so a
         page reload still shows the icon)
       - includes it in the final event so the buffered /
         admin /last-emission path can show it too

Security:
  - `path` is forced under the service user's home directory via the
    same pattern exec_bash uses (Path.resolve() + relative_to()).
    No symlink escape.
  - The on-disk copy gets a fresh uuid so the served URL is
    unguessable (matches the existing /uploads serve endpoint
    which re-validates `<32-hex>.<ext>`).
  - Audit log: every call lands in
    <install_dir>/logs/attach_file_audit.jsonl with source path,
    size, dest id, mime, and outcome. Use this to review what's
    been shared with the user — files in /uploads/ are auth-gated
    but not encrypted-at-rest.

Not in scope: redaction, virus scanning, DLP. The service user
already owns the source file; this just bridges file -> URL.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import time
import uuid
from pathlib import Path

from . import registry


# --- derived locations ----------------------------------------------------

# The install directory (repo root). This file is
# <install_dir>/app/agent/tools/attach_file.py, so parents[3] is the root.
APP_ROOT = Path(__file__).resolve().parents[3]

# The service user's home directory — the sandbox root for source
# paths and the default working area of the harness.
HOME_DIR = Path.home()


# Tighter than the image-upload allowlist (we accept more text-ish
# extensions here because the chat UI renders them as file
# download icons, not inline <img> tags). Browsers don't render
# most of these in-place; the user clicks the icon and downloads.
ALLOWED_EXTS = frozenset({
    # text-ish
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
    # images (let the user download a generated chart)
    "png", "jpg", "jpeg", "gif", "webp", "svg",
    # documents
    "pdf",
    # archives (small ones — capped at MAX_BYTES below)
    "zip", "tar", "gz", "tgz", "bz2", "xz",
    # data
    "parquet", "sqlite", "db",
    # misc
    "diff", "patch", "ipynb",
})

MAX_BYTES = 10 * 1024 * 1024  # 10 MiB — matches HARNESS_MAX_UPLOAD_BYTES
DEFAULT_ROOT = str(HOME_DIR)
AUDIT_PATH = str(APP_ROOT / "logs" / "attach_file_audit.jsonl")


TOOL = {
    "name": "attach_file",
    "description": (
        "Copy a file the user can already see on this server (under "
        f"{DEFAULT_ROOT}/) into the harness uploads dir and return a "
        "URL the chat UI renders as a file-attachment icon (the "
        "paperclip). Use this whenever the user would benefit from "
        "downloading a file the agent just produced or found — e.g. "
        "a log, a CSV export, a generated report, a config snippet, "
        "a chart image, a debug bundle, a PDF. The file path must "
        f"be absolute and under {DEFAULT_ROOT}/. Maximum 10 MiB. "
        "Allowlisted extensions: text (txt, md, log, csv, json, "
        "yaml, toml, ini, env, xml, html, css, sql, etc.), code "
        "(py, sh, js, ts, go, rs, java, ...), images (png, jpg, "
        "gif, webp, svg), documents (pdf), archives (zip, tar, "
        "gz, tgz, bz2, xz), data (parquet, sqlite, db), and a few "
        "misc (diff, patch, ipynb). Rejected extensions return an "
        "error — pick a different artifact or surface the path in "
        "chat instead. Every call is audited in "
        f"{AUDIT_PATH}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute path to the file on this server. "
                    f"Must be under {DEFAULT_ROOT}/. The file is "
                    "copied verbatim to the uploads dir and served "
                    "via a fresh unguessable URL — the original "
                    "path is NOT exposed to the client."
                ),
            },
            "display_name": {
                "type": "string",
                "description": (
                    "Optional override for the icon's filename "
                    "label. Defaults to the source basename. "
                    "Useful when the source path is long or "
                    "ugly and you want a tidier label."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional one-line caption shown in the "
                    "tool result (and stored in the audit log). "
                    "The chat UI currently only shows the icon; "
                    "this is mostly for the LLM's bookkeeping."
                ),
            },
        },
        "required": ["path"],
    },
}


def _audit(payload: dict) -> None:
    """Append a single JSON line to the attach_file audit log.
    Best-effort: a logging failure must not break the tool."""
    try:
        Path(AUDIT_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _resolve_safe(path_str: str) -> Path:
    """Resolve `path_str` and verify it sits under the service user's
    home directory.

    Mirrors the exec_bash cwd check: equality with the root OR a
    startswith check with a trailing slash (Path(home) strips the
    trailing slash, so naive startswith(home + "/") is also broken —
    we do both, plus a resolved relative_to)."""
    p = Path(path_str)
    if not p.is_absolute():
        # Reject relative paths outright; the model can always
        # rewrite to absolute via exec_bash (`realpath $file`).
        raise ValueError(f"path must be absolute (got {path_str!r})")
    resolved = p.resolve()
    root = Path(DEFAULT_ROOT).resolve()
    # Same trailing-slash gotcha as exec_bash: explicit equality
    # for the root itself, otherwise resolved must be a child.
    if str(resolved) != str(root) and not str(resolved).startswith(str(root) + "/"):
        raise ValueError(
            f"path must be under {DEFAULT_ROOT}/ (got {resolved})"
        )
    return resolved


async def run(args: dict, ctx) -> dict:
    t0 = time.monotonic()
    raw_path = args.get("path", "")
    display_name = args.get("display_name") or ""
    description = args.get("description") or ""

    audit_base: dict = {
        "ts": time.time(),
        "user_id": ctx.user_id,
        "conversation_id": str(ctx.conversation_id),
        "source_path": raw_path,
        "display_name": display_name,
        "description": description,
    }

    if not raw_path:
        return {
            "ok": False,
            "error": "path is required",
            "code": "missing_path",
        }

    # 1. Validate path is under the service user's home directory
    try:
        src = _resolve_safe(raw_path)
    except ValueError as e:
        audit_base.update({"outcome": "blocked", "error": str(e), "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {"ok": False, "error": str(e), "code": "path_out_of_sandbox"}

    if not src.is_file():
        audit_base.update({"outcome": "not_found", "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {"ok": False, "error": f"not a regular file: {src}", "code": "not_found"}

    # 2. Extension allowlist
    ext = src.suffix.lower().lstrip(".")
    if not ext:
        audit_base.update({"outcome": "blocked", "error": "no extension", "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {
            "ok": False,
            "error": (
                "file has no extension; refusing to attach (add a "
                "known extension like .txt, .csv, .log, or write to "
                "a new path with one)"
            ),
            "code": "no_extension",
        }
    if ext not in ALLOWED_EXTS:
        audit_base.update({"outcome": "blocked", "error": f"extension {ext!r} not allowed", "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {
            "ok": False,
            "error": (
                f"extension {ext!r} is not allowlisted. Allowed: "
                f"{sorted(ALLOWED_EXTS)}"
            ),
            "code": "ext_not_allowed",
            "allowed_extensions": sorted(ALLOWED_EXTS),
        }

    # 3. Size cap
    try:
        size = src.stat().st_size
    except OSError as e:
        audit_base.update({"outcome": "error", "error": str(e), "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {"ok": False, "error": f"stat failed: {e}", "code": "stat_failed"}
    if size > MAX_BYTES:
        audit_base.update({"outcome": "too_large", "size": size, "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {
            "ok": False,
            "error": (
                f"file is {size} bytes, exceeds {MAX_BYTES} byte cap. "
                f"Trim it first (e.g. exec_bash 'tail -n 1000 ...' "
                f"into a new file) and attach that."
            ),
            "code": "too_large",
            "size": size,
            "max_bytes": MAX_BYTES,
        }
    if size == 0:
        audit_base.update({"outcome": "empty", "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {
            "ok": False,
            "error": "file is empty (0 bytes); nothing to attach",
            "code": "empty",
        }

    # 4. Read + write
    try:
        blob = src.read_bytes()
    except OSError as e:
        audit_base.update({"outcome": "error", "error": str(e), "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {"ok": False, "error": f"read failed: {e}", "code": "read_failed"}

    content_type, _ = mimetypes.guess_type(src.name)
    if not content_type:
        content_type = "application/octet-stream"

    # The on-disk filename is `<uuid_hex>.<ext>` — same shape the
    # /api/v1/uploads/<file_id> serve endpoint expects. Use
    # uuid4().hex (32 chars, lowercase) instead of secrets.token_hex
    # so the regex in serve_upload (`[0-9a-f]{32}`) matches.
    file_id = uuid.uuid4().hex
    upload_dir = APP_ROOT / "uploads"
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        on_disk = upload_dir / f"{file_id}.{ext}"
        on_disk.write_bytes(blob)
    except OSError as e:
        audit_base.update({"outcome": "error", "error": str(e), "duration_ms": int((time.monotonic() - t0) * 1000)})
        _audit(audit_base)
        return {"ok": False, "error": f"write to uploads failed: {e}", "code": "write_failed"}

    sha = hashlib.sha256(blob).hexdigest()
    duration_ms = int((time.monotonic() - t0) * 1000)

    final_display = (display_name.strip() or src.name)[:256]
    final_url = f"/api/v1/uploads/{file_id}.{ext}"

    audit_base.update({
        "outcome": "ok",
        "size": size,
        "sha256": sha,
        "dest_id": file_id,
        "dest_path": str(on_disk),
        "dest_url": final_url,
        "mime": content_type,
        "duration_ms": duration_ms,
    })
    _audit(audit_base)

    return {
        "ok": True,
        "outcome": "attached",
        "attachment": {
            "url": final_url,
            "filename": final_display,
            "content_type": content_type,
            "size": size,
        },
        "message": f"Attached {final_display} ({size} bytes)",
    }


registry.register({**TOOL, "run": run})
