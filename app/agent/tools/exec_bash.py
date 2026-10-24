"""exec_bash tool — execute a shell command as the harness service user.

Per the 2026-10-14 design: this is the first write-capable tool.
Three hard rules:
  1. NO sudo, NO su, NO privilege escalation. The command runs
     in the harness process context with the service user's
     natural permissions. The service user has no sudoers entry
     for root commands; the tool runs subprocess.run() in the
     current process which is already the harness process.
  2. The command line is passed through `classify_command()` which
     has a hard-block list (sudo, rm -rf /, mkfs, /etc/passwd,
     curl|sh, etc.) and a soft-flag list (see the patterns in
     app/agent/tools/_safety.py for the current list — the
     LLM-facing summary lives in the system prompt).
  3. Every call lands in <install_dir>/logs/exec_audit.jsonl
     with command, cwd, exit_code, stdout_bytes, stderr_bytes,
     duration_ms, outcome (ok|blocked|confirm_required|error|timeout).

Soft-flag flow (the LLM is taught this in the system prompt):
  1. User asks the agent to do something destructive (e.g. restart harness)
  2. Agent calls exec_bash(command="systemctl --user restart harness")
  3. Tool returns {"ok": false, "requires_confirmation": true, "reason": "..."}
  4. Agent surfaces the reason to the user in chat: "this would kill
     the chat surface — confirm?"
  5. User says "yes go ahead" (or "no, do X instead")
  6. Agent re-calls exec_bash(command="...", confirm=true)
  7. Tool runs the command, returns stdout/stderr/exit_code

Working dir defaults to the service user's home directory. Output cap
is 50KB per stream (stdout/stderr truncated independently). Default
timeout 30s, max 120s. Hard timeout via subprocess.run(timeout=...)
plus a thread-based watchdog for the case where the child process
ignores SIGTERM.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import registry
from ._safety import (
    classify_command,
    exec_audit_log,
    safe_env,
)

# --- derived locations ----------------------------------------------------

# The install directory (repo root). This file is
# <install_dir>/app/agent/tools/exec_bash.py, so parents[3] is the root.
APP_ROOT = Path(__file__).resolve().parents[3]

# The service user's home directory — the default working directory
# and the sandbox root for cwd validation.
HOME_DIR = Path.home()


# --- protected-file detection (2026-10-23) --------------------------------
# `_writes_to(command, name)` answers: does this command WRITE to the
# named file? It is used to enforce "SOUL.md may only be edited with
# the user's permission" during a reverie turn, without blocking
# commands that merely read the file or mention it in passing.
#
# Detected write forms:
#   * a shell redirect whose target is the file:  > SOUL.md, >> path/SOUL.md
#   * an argument of a write-ish command:        tee/sed -i/mv/cp/rm/...
#   * a python open() in write mode:             open('SOUL.md', 'w')
# Reading (cat/grep/less) and mentions inside a heredoc body are NOT
# writes.
_WRITE_VERBS = (
    "tee", "sed", "mv", "cp", "rm", "truncate", "dd", "install",
    "chmod", "chown", "unlink", "rename", "patch",
)


def _writes_to(command: str, name: str) -> bool:
    """True if `command` writes to the file called `name`."""
    if not command or not name:
        return False
    # 1. redirect target:  > name  /  >> name  (possibly quoted)
    redirect = re.compile(
        r"(?:^|[^0-9A-Za-z_])>>?\s*[\"']?(?P<path>[^\s\"'|&;<>]+)"
    )
    for m in redirect.finditer(command):
        target = m.group("path").strip().strip("\"'")
        if target == name or target.endswith("/" + name):
            return True

    # 2. write-ish command with the file as an argument
    for verb in _WRITE_VERBS:
        # `sed -i` only writes with -i; plain sed prints.
        pat = re.compile(
            r"(?:^|[;&|]\s*|\b)(?P<cmd>" + re.escape(verb) + r")\b(?P<rest>[^;&|]*)"
        )
        for m in pat.finditer(command):
            rest = m.group("rest")
            if verb == "sed" and "-i" not in rest:
                continue
            if verb == "tee" and "|" not in command and "<" not in command:
                # `tee file` writes the file; keep it.
                pass
            for token in re.split(r"\s+", rest):
                token = token.strip("\"'")
                if token == name or token.endswith("/" + name):
                    return True

    # 3. python open() in write mode
    for m in re.finditer(r"open\s*\(\s*[\"'][^\"']*", command):
        seg = command[m.start():m.start() + 200]
        if name not in seg:
            continue
        if re.search(r"[\"']\s*,\s*[\"'][waxr\+b]*[\"']", seg) or re.search(
            r"mode\s*=\s*[\"'][wax]", seg
        ):
            return True
    return False



TOOL = {
    "name": "exec_bash",
    "description": (
        "Execute a shell command as the harness service user (no sudo, no "
        "privilege escalation). The command is parsed by a "
        "command-safety classifier: certain operations (sudo, rm -rf /, "
        "mkfs, /etc/passwd, curl|sh, fork bombs) are hard-blocked and "
        "return an error. Other operations (recursive chmod, "
        "stop/restart of the harness service, modifications to "
        "*.service files, rm -rf in uploads/) are soft-flagged — "
        "the tool returns requires_confirmation=true and the LLM "
        "MUST ask the user in chat before re-calling with "
        "confirm=true. "
        f"Working dir defaults to {HOME_DIR}/. Output is "
        "truncated to 50KB per stream (stdout + stderr independently). "
        "Timeout 30s default, 120s max. Returns exit_code, "
        "stdout, stderr, truncated flags, and the original command. "
        f"Every call is logged to {APP_ROOT}/logs/exec_audit.jsonl "
        "with the full command line, cwd, exit_code, byte counts, "
        "and outcome."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "maxLength": 4000,
                "description": "Shell command to run. Goes through shell=True.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    f"Working directory. Defaults to {HOME_DIR}/. "
                    f"Must be under {HOME_DIR}/."
                ),
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": 120,
                "default": 30,
                "description": "Timeout in seconds. Default 30, max 120.",
            },
            "confirm": {
                "type": "boolean",
                "default": False,
                "description": "Set true to confirm a soft-flagged command. The LLM must have asked the user first.",
            },
        },
        "required": ["command"],
    },
}


MAX_OUTPUT_BYTES = 50_000  # per stream (stdout / stderr)
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
DEFAULT_CWD = str(HOME_DIR)


# Watchdog: if subprocess.run(timeout=...) is interrupted by a
# signal (e.g. the harness gunicorn worker), the child may not
# get the SIGTERM. We run a timer thread that kills the child
# if the main call hangs beyond timeout_s + 5s grace. Belt-and-braces
# for the case where the main thread is stuck.
def _watchdog_kill(process: subprocess.Popen, grace_s: float) -> None:
    """Kill the process tree after grace_s seconds if it's still alive."""
    time.sleep(grace_s)
    if process.poll() is None:
        try:
            # SIGTERM first, escalate to SIGKILL after 2s
            process.terminate()
            time.sleep(2)
            if process.poll() is None:
                process.kill()
        except Exception:
            pass


async def run(args: dict, ctx) -> dict:
    t0 = time.monotonic()
    command = args["command"]
    cwd_arg = args.get("cwd") or DEFAULT_CWD
    timeout_s = int(args.get("timeout_s", DEFAULT_TIMEOUT_S))
    if timeout_s > MAX_TIMEOUT_S:
        timeout_s = MAX_TIMEOUT_S
    confirm = bool(args.get("confirm", False))

    # 0. Protected files (2026-10-23). The reverie turn sets
    #    ctx.protected_paths to the identity files that may only be
    #    edited with the user's explicit permission (SOUL.md).
    #
    #    The check is deliberately PRECISE: it blocks commands that
    #    WRITE to the protected file, not commands that merely mention
    #    it. A blunt "mentions it -> block" rule was tried first and
    #    it backfired: the reverie agent's heredoc for an unrelated doc
    #    mentioned SOUL.md in passing, the command was blocked, and the
    #    agent routed around the guard with a placeholder. A guard the
    #    agent can be expected to work around is not a guard.
    protected = tuple(getattr(ctx, "protected_paths", ()) or ())
    if protected:
        for p in protected:
            if p and _writes_to(command, p):
                msg = (
                    f"{p} is protected: it may only be changed with the user's "
                    f"explicit permission. Propose the change in your report "
                    f"instead of making it."
                )
                exec_audit_log(
                    command=command, cwd=cwd_arg,
                    user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
                    outcome="blocked", duration_ms=int((time.monotonic() - t0) * 1000),
                    blocked_reason=msg,
                )
                return {"ok": False, "outcome": "blocked", "error": msg,
                        "command": command}

    # 1. Classify the command
    classification, reason = classify_command(command)
    if classification == "block":
        exec_audit_log(
            command=command, cwd=cwd_arg,
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="blocked", duration_ms=int((time.monotonic() - t0) * 1000),
            blocked_reason=reason,
        )
        return {
            "ok": False,
            "outcome": "blocked",
            "error": reason,
            "command": command,
        }
    if classification == "confirm" and not confirm:
        exec_audit_log(
            command=command, cwd=cwd_arg,
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="confirm_required", duration_ms=int((time.monotonic() - t0) * 1000),
            blocked_reason=reason,
        )
        return {
            "ok": False,
            "outcome": "confirm_required",
            "requires_confirmation": True,
            "reason": reason,
            "command": command,
        }
    # If classification is "confirm" and confirm=True, fall through
    # and run. We log the explicit confirmation in the audit.

    # 2. Validate cwd
    cwd_path = Path(cwd_arg)
    if not cwd_path.is_absolute():
        cwd_path = (Path(DEFAULT_CWD) / cwd_path).resolve()
    # cwd must be at or under the service user's home (we run as the
    # service user, no reason to escape the home tree). Accept
    # cwd==the home dir itself, not just paths under it. The previous
    # `relative_to()` check rejected the home dir itself (it's not a
    # child of itself). The simple `startswith(home + "/")` is *also*
    # broken: `Path(home)` strips the trailing slash, so the
    # comparison is `home.startswith(home + "/")` which is False.
    # Fix: explicit equality check OR the with-trailing-slash form.
    home_str = str(HOME_DIR)
    cwd_str = str(cwd_path)
    if cwd_str != home_str and not cwd_str.startswith(home_str + "/"):
        msg = f"cwd must be under {home_str}/ (got {cwd_path})"
        exec_audit_log(
            command=command, cwd=str(cwd_path),
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="error", duration_ms=int((time.monotonic() - t0) * 1000),
            error=msg,
        )
        return {"ok": False, "error": msg}

    if not cwd_path.is_dir():
        msg = f"cwd is not a directory: {cwd_path}"
        exec_audit_log(
            command=command, cwd=str(cwd_path),
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="error", duration_ms=int((time.monotonic() - t0) * 1000),
            error=msg,
        )
        return {"ok": False, "error": msg}

    # 3. Run the command
    env = safe_env()
    # Mark the env so the model can see it's a sandboxed run
    env["HARNESS_TOOL_EXEC_BASH"] = "1"

    try:
        # subprocess.run is sync, so run it in a thread to keep
        # the asyncio event loop responsive.
        def _run():
            return subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                cwd=str(cwd_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )

        proc = await asyncio.get_event_loop().run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        # subprocess.run raises TimeoutExpired if the child doesn't
        # exit within timeout_s. Kill it (children are killed
        # automatically by subprocess.run on timeout; stdout/stderr
        # are empty).
        duration = int((time.monotonic() - t0) * 1000)
        exec_audit_log(
            command=command, cwd=str(cwd_path),
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="timeout", exit_code=None,
            duration_ms=duration, stdout_bytes=0, stderr_bytes=0,
            error=f"command exceeded {timeout_s}s timeout",
        )
        return {
            "ok": False,
            "outcome": "timeout",
            "error": f"command exceeded {timeout_s}s and was killed",
            "command": command,
            "timeout_s": timeout_s,
        }
    except FileNotFoundError as e:
        msg = f"cwd doesn't exist: {e}"
        exec_audit_log(
            command=command, cwd=str(cwd_path),
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="error", duration_ms=int((time.monotonic() - t0) * 1000),
            error=msg,
        )
        return {"ok": False, "error": msg}

    # 4. Truncate output
    out = proc.stdout or ""
    err = proc.stderr or ""
    out_truncated = False
    err_truncated = False
    if len(out) > MAX_OUTPUT_BYTES:
        out = out[:MAX_OUTPUT_BYTES] + f"\n... (truncated, total {len(proc.stdout)} bytes)"
        out_truncated = True
    if len(err) > MAX_OUTPUT_BYTES:
        err = err[:MAX_OUTPUT_BYTES] + f"\n... (truncated, total {len(proc.stderr)} bytes)"
        err_truncated = True

    duration = int((time.monotonic() - t0) * 1000)
    exec_audit_log(
        command=command, cwd=str(cwd_path),
        user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
        outcome="ok", exit_code=proc.returncode,
        duration_ms=duration,
        stdout_bytes=len(proc.stdout or ""), stderr_bytes=len(proc.stderr or ""),
        blocked_reason=reason if classification == "confirm" else None,
    )

    return {
        "ok": proc.returncode == 0,
        "outcome": "ok",
        "command": command,
        "cwd": str(cwd_path),
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": err,
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
        "timeout_s": timeout_s,
        "duration_ms": duration,
        "confirmed_via_explicit_flag": confirm and classification == "confirm",
    }


# asyncio import lives at the bottom so the module-level imports
# don't pull in heavy deps for tools that don't need async.
import asyncio  # noqa: E402


registry.register({**TOOL, "run": run})
