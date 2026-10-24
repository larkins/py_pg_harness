# Security

Harness security model, threat surface, mitigations, and operational notes.

> Phase 8 (2026-10-08): initial security sweep. Phase 9 (production sign-off + OSS readiness) gates public release.

## Threat model

### Trusted

- The host machine (`<hostname>`). The operator Unix user has no `sudo` by default.
- The Postgres server (peer-auth, no password exposure).
- The shared GPU embedding server (`127.0.0.1:8080`, LAN-only).
- The LLM API endpoints (auth via API key in `.env`).

### Not trusted

- **The chat surface itself** — anyone with the URL can send messages. v0 has no auth.
- **User-provided message content** — untrusted input. Treated as the prompt-injection surface.
- **Tool output** — we display tool results to the model. Compromise of `MemoryAPI` or the embedding service could let an attacker inject context.
- **The shared LLM API** — third party. We assume prompt-injection resilience; we do not assume the LLM is harmless.
- **The `.env` file** — contains `LLM_API_KEY` and `DATABASE_URL`. Mode 600 enforced at startup; checked by `sanity_check_env()`.

### What an attacker can do

- Send arbitrary messages to the agent (tool-use only, no exec / no file / no network).
- Read every public web response (chat UI, JSON, `/health`).
- Attempt prompt-injection: trick the agent into pinning or unpinning wrong chunks, or talking about things it shouldn't.

### What an attacker CANNOT do (by design)

- **Execute code on the server.** The agent has no `tools.exec`; the only tools are `memory_recall` / `memory_pin` / `memory_unpin`. The LLM cannot read files, browse the web, or run shell.
- **Access other tenants' data.** Harness is single-tenant under one operator Unix user; per-agent schema isolation prevents cross-agent reads.
- **Read API keys from logs.** `KeyRedactionFilter` masks `LLM_API_KEY= ***` and `Authorization: Bearer ***` patterns in every log record.
- **Exfiltrate the database via the agent.** The LLM has no SQL surface — it calls `MemoryAPI` methods, which enforce parametric queries and rate caps.
- **Bypass rate limits by sending 1000 requests/sec.** Flask-Limiter caps `/api/v1/chat` at 30/min/IP and `/api/v1/memories/*` at 60/min/IP, in-memory process-wide.
- **Make the LLM blow up the system with infinite tool loops.** Agent loop caps at `MAX_TOOL_ITERATIONS=5` and per-tool caps (`MAX_SEARCHES_PER_TURN=5`, `MAX_PINS_PER_TURN=3`, `MAX_UNPINS_PER_TURN=5`).
- **Saturate the model API with concurrent calls.** `asyncio.Semaphore(config.agent.max_concurrent_llm=8)` caps concurrency.

## OWASP top-10 mapping

| Risk | Mitigation in harness |
|---|---|
| A01 Broken Access Control | No auth in v0 (planned v0.2). Public chat only; no admin endpoints without per-request token. |
| A02 Cryptographic Failures | TLS at CF edge. `.env` mode 600 enforced. API keys never logged (KeyRedactionFilter). |
| A03 Injection (SQL) | All SQL uses `psycopg` `%s` placeholders. **No f-string SQL anywhere in `app/`** (enforced by `tests/test_security.py::test_no_fstring_sql`). |
| A03 Injection (prompt) | Model has no `tools.exec` / no file / no network. System prompt anchored (no-execution / no-browsing). LLM can only call `memory_recall` / `pin` / `unpin`. |
| A04 Insecure Design | Single-purpose design: chat + memory. No plugin system. No file upload. No exec. Threat-model documented here. |
| A05 Security Misconfiguration | `sanity_check_env()` refuses to start if `.env` is world-readable, has empty required keys, or has a `***…` placeholder. CSP/HSTS/X-Frame-Options set on every response. |
| A06 Vulnerable Components | Hand-curated `requirements.txt` with upper bounds. No transitive trust on model APIs. |
| A07 Identification & Auth Failures | n/a in v0 (no auth). When added: argon2/bcrypt, session cookies httpOnly+secure+SameSite=strict. |
| A08 Software & Data Integrity | Bootstrap files re-read on mtime change (operator edits pick up next prompt). Per-agent schema installer is idempotent. |
| A09 Security Logging | Per-request `X-Request-Id` for log correlation. `memory_audit` table for every MemoryAPI call. Append-only. No retention policy (per the project owner, 2026-10-08). |
| A10 Server-Side Request Forgery | No outbound HTTP from the agent loop. Embedding + LLM are configured singletons with hard-coded URLs in `.env`. |

## Defenses in depth

1. **No tools.exec** (per MEMORY.md `notifyOnExit` lesson). The agent cannot run shell.
2. **No file access** (no `read`/`write` tool). Bootstrap files are loaded at app startup, not via tool call.
3. **Parametric SQL only**. `MemoryAPI` is the safety boundary; the agent loop never sees SQL.
4. **Schema isolation**. `public.users` is the only shared table. Per-agent schemas (`<agent_name>`) have their own `memory_chunks`, `turns`, `conversations`, `memory_audit`.
5. **Runtime role = DML only**. `<runtime_role>` cannot `CREATE`/`DROP`/`ALTER`. DDL is the operator role (`<operator_role>`).
6. **LLM-API key never logged**. `KeyRedactionFilter` masks `LLM_API_KEY= ***` and `Authorization: Bearer ***` patterns in any log record (msg + args).
7. **Rate limiting at the Flask boundary**. `/api/v1/chat` = 30/min/IP, `/api/v1/memories/*` = 60/min/IP. In-memory process-wide (single gunicorn worker).
8. **Per-tool rate limits inside the loop**. `MAX_SEARCHES_PER_TURN=5`, `MAX_PINS_PER_TURN=3`, `MAX_UNPINS_PER_TURN=5`. Even if Flask-Limiter is bypassed, the loop enforces.
9. **Concurrent LLM cap**. `asyncio.Semaphore(max_concurrent_llm=8)` around `adapter.complete()`. Prevents one slow request from monopolizing the worker.
10. **Per-request `X-Request-Id`**. Every response carries the id; the access log line includes it. Trace a request from CF edge to tool result with one identifier.
11. **CSP** with `default-src 'self'`. Inline styles allowed (chat UI uses them) but no inline scripts, no external sources.

## Operational notes

### Backups

`tools/backup.sh` does daily `pg_dump` to a configurable backup directory (set `BACKUP_DIR` in the environment; there is no default path baked into the script), sha256-manifested, 14-day retention. Wire as a systemd --user timer in Phase 9.

### Key rotation

- `LLM_API_KEY` lives in the harness `.env` at the install root (mode 600).
- Rotate by editing the file, then `sudo -u <operator_user> systemctl --user restart harness.service`.
- Old keys remain valid until revoked at the provider. Plan a quarterly rotation.

### .env placement

The harness-wide `harness/.env` is for shared infrastructure secrets only
(LLM keys, DB URLs, TLS material, rate-limit config, internal tokens). It is
gitignored and never committed; mode 0600 enforced at startup.

**Per-agent secrets** (anything scoped to a single agent — e.g. one agent's
email credentials, a per-agent API key) live in that agent's own .env at:

    pie/<agent_name>/.env

That file is also gitignored, must be mode 0600, and is loaded by the agent's
own `env_loader.py` (e.g. `pie/<agent_name>/scripts/env_loader.py`) with
`override=False`. That means: values already in the real process environment
(from `harness/.env` at startup) still win, but agent-specific secrets flow in
from the agent's own .env. This isolates secrets between agents that all
operate in the same unix user session — agent A can't read agent B's secrets
even if both share the harness process.

### Logging

Logs go to journald (`journalctl --user -u harness.service`). The `KeyRedactionFilter` runs on every record before it's emitted. To verify:

```bash
sudo -u <operator_user> journalctl --user -u harness.service \
    | grep -E 'sk-[A-Za-z0-9_-]{8,}|Authorization.*Bearer'
# (no output = no leaks)
```

### Rate limit bypass

If a single client needs to make more than 30 chat requests per minute (e.g. a CI job), it should rotate source IPs or use a different identifier. Flask-Limiter uses `get_remote_address` (peer IP) as the key by default; X-Forwarded-For is honored if `app.config["RATELIMIT_HEADERS_ENABLED"] = True` (we don't enable this for v0 — CF edge already rate-limits).

## Audit tests

```bash
# Off the dev box (no live model):
pytest tests/test_security.py

# Live (requires running harness on the prod endpoint):
HARNESS_RUN_LIVE_TESTS=1 pytest tests/test_security.py
```

Tests cover:
- `KeyRedactionFilter` (msg + args, LLM key + Bearer)
- `sanity_check_env` (world-readable, placeholder, empty, happy)
- All `app/` files free of f-string SQL
- `cfg.agent` exposes the four MAX_* attributes
- Live: security headers on `/health`, `/`, `/api/v1/conversations`
- Live: `/health` body has no key-shape leaks
- Live: `X-Request-Id` round-trips

## Production sign-off checklist (Phase 9)

- [ ] Run `pytest tests/test_security.py -v`; all green
- [ ] Run live integration test: `HARNESS_RUN_LIVE_TESTS=1 pytest tests/test_security.py`
- [ ] Run `python -m bandit -r app/` (or equivalent) — no high-severity issues
- [ ] Verify no LLM_API_KEY leak: tail `journalctl --user -u harness.service` for 24h, grep for `sk-…`
- [ ] Wire `tools/backup.sh` to a systemd --user timer (daily, 02:00 AEST)
- [ ] Verify `pg_dump` works end-to-end (one-shot manual run, then check the file)
- [ ] Document `.env` rotation procedure in operator runbook
- [ ] Confirm public repo has no secrets in git history (`git log -p | grep -E 'sk-[A-Za-z0-9]{20,}'` → empty)

## Open for Phase 9

- Rate limiting storage: in-memory is fine for single gunicorn worker. If we ever go multi-worker, switch to Redis (`storage_uri="redis://..."`).
- Per-route rate limit is currently applied via a `before_request` check. Cleaner would be a decorator on each route function. The current approach is correct but more verbose.
- API key rotation runbook not yet in OPERATIONS.md (will be added with the backup runbook in Phase 9).
- Log redaction is regex-based. A real exfil vector would be base64-encoded keys, which our regex won't catch. Mitigation: never log response bodies (we currently log `e.response.text[:500]` on HTTP errors — could leak a key in the error body). The MiniMax error responses don't contain keys, but other providers might. TODO: redact any string of length >= 30 that matches `^[A-Za-z0-9_-]{30,}$` from log records.
