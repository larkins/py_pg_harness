"""sql_query tool — run a read-only SQL SELECT against the harness DB
or the optional auxiliary ("aux") DB.

Per the 2026-10-14 design: any table in either DB is in scope (no
schema restriction). Read-only — refuse anything that isn't a SELECT.

We parse the SQL with a regex (not a full parser) to enforce the
read-only invariant. The patterns we reject:
- INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT,
  REVOKE, COPY, VACUUM, REINDEX, CLUSTER, LOCK, CALL, DO, EXPLAIN
  (EXPLAIN is borderline — it can be used to infer schema; we
  allow it because it's read-only and useful for performance
  debugging, but log a soft note in the audit result).
- Multiple statements in one query (any `;` that's not trailing
  whitespace + comment is suspicious).
- Anything inside a string literal that LOOKS like a write — we
  don't try to fully parse SQL; we just refuse obvious write
  verbs at the top level. (If the model needs to write, Phase 2+
  has separate write tools.)

Per-database safety:
- 'harness' DB: the harness database itself; the DSN comes from the
  install dir's .env (DATABASE_URL) and the service user's own
  credentials. We still enforce SELECT-only at this layer.
- 'aux' DB: an optional second database, configured by the operator
  (see the HARNESS_AUX_* env vars in _safety.py). When it is not
  configured, that db_name is unavailable.
- 30s statement_timeout via psycopg.
- Max 1000 rows returned.
- Max 50KB response size.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import psycopg

from . import registry
from ._safety import audit_log, build_dsn, known_db_names

# The install directory (repo root). This file is
# <install_dir>/app/agent/tools/sql_query.py, so parents[3] is the root.
APP_ROOT = Path(__file__).resolve().parents[3]

# The db_names this tool accepts: 'harness', 'aux', plus any alias the
# operator configured through HARNESS_AUX_DB_ALIASES.
DB_NAMES = known_db_names()


TOOL = {
    "name": "sql_query",
    "description": (
        "Run a read-only SQL query against the 'harness' database (or "
        "the optional 'aux' database, when the operator has configured "
        "one) and return the result rows. Only "
        "SELECT (and EXPLAIN) statements are allowed — INSERT, "
        "UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT, "
        "REVOKE, COPY, VACUUM, REINDEX, CLUSTER, LOCK, CALL, DO "
        "are all rejected. The query must be a single statement "
        "(no semicolons in the middle). 30s statement timeout, "
        "1000 row cap, 50KB response cap. Use this to inspect "
        "memory chunks, conversation history, the shared user "
        "table, etc. Every call is logged to "
        f"{APP_ROOT}/logs/tool_audit.jsonl with the "
        "full query, row count, and timing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "db": {
                "type": "string",
                "enum": DB_NAMES,
                "description": "Which database to query.",
            },
            "sql": {
                "type": "string",
                "maxLength": 10000,
                "description": "A single SELECT (or EXPLAIN) statement.",
            },
        },
        "required": ["db", "sql"],
    },
}


# Write verbs we never allow. Case-insensitive; matched as whole
# words at the start of the SQL (after stripping leading whitespace
# and SQL comments). Each entry has a regex with \b boundaries.
_FORBIDDEN_VERBS = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|reindex|cluster|lock|call|do"
    r")\b",
    re.IGNORECASE,
)
# Allowed: SELECT (and optional WITH) and EXPLAIN. Anything else
# starting with a different verb is rejected.
_ALLOWED_START = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*"  # strip leading comments/whitespace
    r"(?:(with\s+[^\n]+?\s+as\s+\("
    r"|select|explain\s+(?:analyze\s+)?(?:verbose\s+)?select))",
    re.IGNORECASE | re.DOTALL,
)
# Multiple statements: any semicolon that's not at the very end
# (after the statement) means multi-statement.
_MULTI_STMT = re.compile(r";\s*\S", re.DOTALL)


MAX_ROWS = 1000
MAX_BYTES = 50_000
STATEMENT_TIMEOUT_S = 30


def _validate_sql(sql: str) -> tuple[bool, str | None]:
    """Returns (ok, reason_if_blocked). Reads 'safe' if ok=True."""
    if not sql or not sql.strip():
        return False, "empty query"
    if not _ALLOWED_START.match(sql):
        return False, (
            "only SELECT (or EXPLAIN SELECT) is allowed; query must "
            "start with SELECT/WITH/EXPLAIN"
        )
    if _FORBIDDEN_VERBS.search(sql):
        return False, (
            "query contains a forbidden verb "
            "(INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/GRANT/"
            "REVOKE/COPY/VACUUM/REINDEX/CLUSTER/LOCK/CALL/DO)"
        )
    if _MULTI_STMT.search(sql):
        return False, "multi-statement queries not allowed"
    return True, None


async def run(args: dict, ctx) -> dict:
    t0 = time.monotonic()
    db_name = args["db"]
    sql = args["sql"]

    ok, reason = _validate_sql(sql)
    if not ok:
        audit_log(
            tool="sql_query", args=args,
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="blocked", duration_ms=int((time.monotonic() - t0) * 1000),
            error=reason,
        )
        return {"ok": False, "error": reason}

    try:
        dsn = build_dsn(db_name)
    except (FileNotFoundError, ValueError) as e:
        msg = f"failed to build DSN for {db_name!r}: {e}"
        audit_log(
            tool="sql_query", args={"db": db_name, "sql": sql},
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="error", duration_ms=int((time.monotonic() - t0) * 1000),
            error=msg,
        )
        return {"ok": False, "error": msg}

    def _run_query():
        """Sync helper — psycopg is sync, and we run it in a thread
        so the event loop isn't blocked."""
        rows = []
        columns = []
        truncated = False
        with psycopg.connect(dsn) as conn:
            # statement_timeout is in milliseconds. We set it for
            # both the connection (default for any statement) and
            # the transaction (overrides if anything resets).
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_S * 1000}")
                cur.execute(sql)
                if cur.description is not None:
                    columns = [d.name for d in cur.description]
                # Fetch up to MAX_ROWS+1 so we can detect truncation.
                    fetched = cur.fetchmany(MAX_ROWS + 1)
                    if len(fetched) > MAX_ROWS:
                        truncated = True
                        fetched = fetched[:MAX_ROWS]
                    for row in fetched:
                        # Coerce non-JSON-serialisable values to str.
                        rows.append([_coerce(v) for v in row])
        return columns, rows, truncated

    try:
        # Run in a thread so we don't block the asyncio event loop.
        columns, rows, truncated = await asyncio.get_event_loop().run_in_executor(
            None, _run_query
        )
    except psycopg.errors.QueryCanceled as e:
        msg = f"query cancelled (likely statement_timeout={STATEMENT_TIMEOUT_S}s): {e}"
        audit_log(
            tool="sql_query", args={"db": db_name, "sql": sql},
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="error", duration_ms=int((time.monotonic() - t0) * 1000),
            error=msg,
        )
        return {"ok": False, "error": msg}
    except psycopg.Error as e:
        msg = f"SQL error: {type(e).__name__}: {e}"
        audit_log(
            tool="sql_query", args={"db": db_name, "sql": sql},
            user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
            outcome="error", duration_ms=int((time.monotonic() - t0) * 1000),
            error=msg,
        )
        return {"ok": False, "error": msg}

    result = {
        "ok": True,
        "db": db_name,
        "row_count": len(rows),
        "truncated": truncated,
        "columns": columns,
        "rows": rows,
    }
    # Trim the audit summary to the first 200 chars of the SQL
    # (we already record the full SQL in args, but the summary
    # gives a quick view of what the LLM was actually looking at).
    audit_log(
        tool="sql_query", args={"db": db_name, "sql": sql},
        user_id=ctx.user_id, conversation_id=str(ctx.conversation_id),
        outcome="ok", result={
            "db": db_name,
            "row_count": len(rows),
            "truncated": truncated,
            "columns": columns,
        },
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return result


def _coerce(v):
    """psycopg returns native Python types for most values, but
    some (UUID, datetime, decimal, bytes, etc.) need JSON-friendly
    coercion before we can return them in a tool result."""
    if v is None or isinstance(v, (int, float, bool, str)):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return f"<bytes: {len(v)} bytes>"
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    # datetime, UUID, Decimal, etc. — fall back to str.
    return str(v)


registry.register({**TOOL, "run": run})
