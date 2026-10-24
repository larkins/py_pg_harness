# Plan: Multiple agents in one unix session (harness)

**Date:** 2026-10-14
**Author:** the harness maintainer (per the project owner's 2026-10-14 20:10 AEST direction)
**Status:** PROPOSAL — for review before any code changes

## Goal

Run **two agents in the same unix session** (the operator Unix user on one host):

- **`<agent_a>`** (existing) — the default persona
- **`<agent_b>`** (new) — migrated from a legacy OpenClaw install on another host

Both served at separate hostnames (no shared port conflict):

- `<agent_a>.<domain>` → existing HMI/API on `127.0.0.1:5010` (or whatever the agent's port is)
- `<agent_b>.<domain>` → new HMI/API on `127.0.0.1:5020` (placeholder — pick a free port)

The two agents share the same harness PostgreSQL database but live in **separate schemas** (`<agent_a>.*` vs `<agent_b>.*`). They share `public.users` (the auth table) and the runtime role. They do NOT share memory_chunks, conversations, or agent_config.

## Current state (verified 2026-10-14)

| Component | State |
|---|---|
| Harness repo at `<install_dir>/` | ✅ checked out (not a git repo yet — pending init) |
| Harness service | ✅ running as `<operator_user>@harness.service` (gunicorn) on `127.0.0.1:5010` |
| Runtime DB role | ✅ exists, granted USAGE + DML on the `<agent_a>` schema |
| `<agent_a>` schema | ✅ created with 5 tables: `conversations`, `turns`, `memory_chunks`, `memory_audit`, `agent_config` |
| `public.users` | ✅ shared auth table (1 user seeded: `admin@example.com`) |
| `<agent_b>` schema | ❌ not yet created |
| CF tunnel for harness | ✅ `<harness_tunnel>` (id `<tunnel_id>`) serves `<harness_hostname>` |
| `<agent_b>`'s source workspace | ✅ on the source host at `<source_workspace>/<agent_b>/` (git-tracked, 79 daily notes, 8 skills) |

## Architecture choice: schema-per-agent, NOT process-per-agent

The harness spec (§1, §3) is explicit: **"Multi-agent is a Postgres concept, not a process concept. Adding an agent = `CREATE SCHEMA <name>`."** The Flask process serves all agents on the same chat surface via a path/header selector.

The project owner's design also aligns: 2 different schemas (good — that part is spec-correct), but 2 different systemd --user services (a process-per-agent variant).

**Trade-off matrix:**

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Single harness service, 2 schemas** (spec default) | No new service; matches spec; less code; shared process; shared LLM client pool; shared embedding client; shared rate limit machinery | Both agents share the same Python process — one agent's bug could crash the other (mitigated by Flask blueprints + try/except); agent selector has to be added to the route layer | ✅ **recommended** for v0 |
| **B. Two systemd --user services, 2 schemas** (the project owner's proposal) | Per-agent process isolation (one agent's crash doesn't take down the other); per-agent resource limits; cleaner mental model | Code duplication (the gunicorn + Flask factory would be shared via a Python module, but each service is its own process); 2x memory; 2x CPU overhead; needs a mechanism to coordinate (which agent owns which schema, which agent handles a given request — but the agent is selected by URL not by process) | ⚠️ viable but over-engineered for v0 |
| **C. Single process, agent selected by URL prefix** (e.g. `/<agent_b>/chat`, `/<agent_a>/chat`) | One process, one service, but URL routes to different schema | URL structure is awkward; breaks if a user wants a per-agent hostname (would need nginx-style path stripping) | ❌ worse than A |

**Recommendation: A (single harness service, 2 schemas) for v0. Re-evaluate B if isolation becomes a real need.**

That said, **if the project owner wants B**, here's the minimal-impact implementation. Both A and B are described below.

## Option A (recommended): single service, 2 schemas

### What changes

1. **DB migration: create the `<agent_b>` schema** by copying `migrations/agents/<agent_a>_schema.sql` to `migrations/agents/<agent_b>_schema.sql`, changing all `<agent_a>` → `<agent_b>`. Add `migrations/agents/README.md` to document the convention.

2. **Harness app: agent selector middleware** (one new file, ~50 lines) — `app/agent/selector.py`:
   - Reads the `Host` header from the incoming request
   - Maps hostname → agent name: `<agent_a>.<domain>` → `<agent_a>`, `<agent_b>.<domain>` → `<agent_b>`
   - Stashes the agent name on `flask.g` so blueprints can read it
   - Returns 404 for unknown hostnames (instead of falling through to the default agent — security)
   - The chat UI is a static SPA; it just needs to know which agent it's serving (set in a meta tag at build time or via a `/api/v1/whoami` call)

3. **Harness app: schema-name lookup** in the MemoryAPI + every SQL-touching function (replace `f'<agent_a>.{table}'` with `f'{g.agent_name}.{table}'` — but ONLY where the g is available, i.e. inside a request context. For background jobs that don't have a request, pass `agent_name` explicitly).
   - **Alternative**: keep `<agent_a>.*` as the default for the existing routes (no changes needed), and add NEW routes `/<agent_b>/...` that hardcode `<agent_b>.*`. Two separate route trees, each with its own blueprint. Less surgical but lower-risk.

4. **Cloudflare Tunnel ingress** for the existing harness tunnel:
   - `<harness_hostname>` → `http://127.0.0.1:5010` (existing)
   - `<agent_a>.<domain>` → `http://127.0.0.1:5010` (new — just the same upstream with a different hostname)
   - `<agent_b>.<domain>` → `http://127.0.0.1:5010` (new — same upstream; the harness app routes to the right schema based on Host header)

5. **DNS records** (CNAMEs) for `<agent_a>.<domain>` and `<agent_b>.<domain>` pointing to the tunnel.

6. **Bootstrap files** — the `bootstrap.files` config in `config.yaml` already supports per-file paths. We add a `bootstrap.agents` section to specify which bootstrap files to load for which agent (or, simpler: the bootstrap files live at `{workspace}/<agent_b>/` vs `{workspace}/<agent_a>/` and the config picks the right one based on the agent name).

7. **Memory migration** — use the `openclaw_migration` skill (just created) to ingest `<agent_b>`'s `MEMORY.md` + 79 daily notes + 8 skills from the source workspace.

8. **`<agent_b>` user record** — create a user in `public.users` for the agent (so it has an auth identity). Run `python3 -m app.tools.create_user --email <agent_email> --password ... --role member`. Or, if the agent's existing OpenClaw auth is being migrated, restore the password hash directly.

### What does NOT change

- The `harness` Flask app at `<install_dir>/` — same code, same service
- The harness-tunnel cloudflared daemon — same process, just new ingress rules
- The runtime DB role — same role, just granted DML on the new `<agent_b>` schema
- The `public.users` table — shared, no changes
- The `MEMORY.md`/`AGENTS.md`/etc. bootstrap files for `<agent_a>` — unchanged

### Config additions to `config.yaml`

```yaml
# Multi-agent mapping
agents:
  <agent_a>:
    schema: <agent_a>
    display_name: "<Agent A Display Name>"
    hostnames: ["<agent_a>.<domain>", "<harness_hostname>"]  # <harness_hostname> stays for backward compat
    default: true
  <agent_b>:
    schema: <agent_b>
    display_name: "<Agent B Display Name>"
    hostnames: ["<agent_b>.<domain>"]
    default: false
```

The `default: true` agent is used for unknown hostnames (logs a warning; we want the security posture to be "unknown host → 404", but having a default is useful for dev).

## Option B (the project owner's proposal): two services, two ports, two processes

### What changes (additional to Option A)

1. **Two systemd --user services**:
   - `harness-<agent_a>.service` (existing, just renamed from `harness.service`) on port 5010
   - `harness-<agent_b>.service` (new) on port 5020 (or whatever's free)

2. **Service files** committed in `systemd/`:
   - `systemd/harness-<agent_a>.service` (production)
   - `systemd/harness-<agent_b>.service` (new)

3. **Cloudflare Tunnel ingress** (already done if we use one tunnel for both):
   - `<agent_a>.<domain>` → `http://127.0.0.1:5010`
   - `<agent_b>.<domain>` → `http://127.0.0.1:5020`

4. **Per-agent config** in `config.yaml` (each service loads the same file but the `agent_name` differs):
   - `<agent_a>` service: `HARNESS_AGENT_NAME=<agent_a>` in the systemd `Environment=`
   - `<agent_b>` service: `HARNESS_AGENT_NAME=<agent_b>` in the systemd `Environment=`
   - The app reads `HARNESS_AGENT_NAME` from env and uses it as the schema name (no host-based dispatch needed)

5. **Two working copies of the harness repo** (or one + symlinks for shared state) — the systemd `WorkingDirectory=` points to the same `<install_dir>/` so they share the same code; the env var differentiates them.

### What does NOT change (vs Option A)

- Same DB, same schema-per-agent pattern
- Same bootstrap files
- Same memory migration

### Trade-off note for Option B

The two services use the **same Flask app** (same `app/` directory, same `config.yaml`, same `requirements.txt`). The only thing that differs is the env var `HARNESS_AGENT_NAME`. So Option B is essentially Option A with an extra layer of process isolation. The extra process is only worth it if:
- You expect one agent to be much more resource-intensive (then a `MemoryHigh=` / `CPUQuota=` per service is useful)
- You want one agent to fail independently (then a `Restart=on-failure` on each is useful)
- You want to start/stop/restart one agent without affecting the other

For v0, **none of these apply**. Both agents have similar resource profiles, both need to be reliable, and both should always be running. Option A is the right call for v0.

If/when we get to **3+ agents** (e.g. `<agent_a>`, `<agent_b>`, `<agent_c>`, `<agent_d>`), the calculus changes. With 4 agents all in one process, a single OOM crash takes down all 4. With 4 services, isolation is real. But that's a v1 problem.

## Implementation plan (Option A)

### Phase 1: DB schema for the new agent (30 min)

1. Copy `migrations/agents/<agent_a>_schema.sql` → `migrations/agents/<agent_b>_schema.sql`, replace all `<agent_a>` → `<agent_b>`
2. Add a `migrations/agents/README.md` documenting the convention (one file per agent, copy `<agent_a>_schema.sql` as a template)
3. Apply the new migration: `psql -U <operator_role> -d <db_name> -f migrations/agents/<agent_b>_schema.sql`
4. Grant DML to the runtime role: `GRANT USAGE, SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA <agent_b> TO <runtime_role>;` (already in the migration template)
5. Verify with `\dt <agent_b>.*` and a quick `SELECT 1 FROM <agent_b>.memory_chunks LIMIT 1;`

### Phase 2: Agent selector middleware (1-2 hours)

1. Add `agents` block to `config.yaml` (above)
2. Create `app/agent/selector.py` (~50 lines)
3. Wire it into the Flask app factory in `app/__init__.py` — install as `@app.before_request` (or `@app.url_value_preprocessor`)
4. Update the chat routes + memory routes to read `flask.g.agent_name` instead of hardcoding `<agent_a>`
5. Test: `curl -H "Host: <agent_a>.<domain>" http://127.0.0.1:5010/api/v1/whoami` → `{"agent": "<agent_a>"}`. Same with `<agent_b>.<domain>`.

### Phase 3: DNS + tunnel (15 min)

1. CF API: `POST /zones/{zone_id}/dns_records` for `<agent_a>` (CNAME → `<tunnel_id>.cfargotunnel.com`) and `<agent_b>` (same)
2. CF API: `PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` to add the two new ingress rules
3. Verify: `curl https://<agent_a>.<domain>/whoami` returns 200; `curl https://<agent_b>.<domain>/whoami` returns 200

### Phase 4: Memory migration (1-2 hours, mostly waiting on embeddings)

1. SSH to the source host, pull the agent's workspace (it's git-tracked, so just `git pull` to make sure it's current)
2. Run the `openclaw_migration` skill (the one we just created):
   - Pre-flight checks (DB reachable, embedding endpoint reachable, schema exists)
   - Ingest `MEMORY.md` → `<agent_b>.memory_chunks` with `permanent=TRUE, importance=10, pin_source='operator'`
   - Ingest 79 `memory/*.md` files → `<agent_b>.memory_chunks` with `permanent=FALSE, importance=5`
   - Copy 8 `skills/` → `<install_dir>/skills/`
   - Audit rows
3. Verify: `SELECT COUNT(*) FROM <agent_b>.memory_chunks WHERE permanent = TRUE;` ≈ 10-30 chunks (depending on chunking). `WHERE permanent = FALSE` ≈ 100-500 chunks. `memory_audit` has 80+ new rows.

### Phase 5: Agent user + auth (15 min)

1. `python3 -m app.tools.create_user --email <agent_email> --password <from-memory-or-fresh> --display-name '<Agent B Display Name>' --role member`
2. (Optional) If the agent's existing OpenClaw password is being preserved: skip step 1, instead `UPDATE public.users SET password_hash = <copied from source> WHERE email = '<agent_email>'`
3. Test login: `curl -X POST -H "Host: <agent_b>.<domain>" -H "Content-Type: application/json" -d '{"email":"<agent_email>","password":"..."}' https://<agent_b>.<domain>/api/v1/auth/login`

### Phase 6: Bootstrap files (1 hour)

1. Copy the agent's `SOUL.md`, `USER.md`, `AGENTS.md`, `IDENTITY.md` from `<source_workspace>/<agent_b>/` to `<install_dir>/agents/<agent_b>/` (or wherever the config points)
2. Verify the bootstrap loader in `app/__init__.py` (or wherever it lives) reads from the per-agent directory
3. Restart `harness.service`
4. Verify: a fresh chat session with the new agent's HMI loads its `SOUL.md` (not the default agent's)

### Phase 7: Smoke test (30 min)

1. Open `<agent_a>.<domain>/chat` in a browser — verify the first agent's persona is loaded
2. Open `<agent_b>.<domain>/chat` in a browser — verify the second agent's persona is loaded
3. Send a message to each — verify it goes to the right schema (check `<agent_a>.conversations` and `<agent_b>.conversations` after)
4. Check that the `permanent=TRUE` memories from MEMORY.md are visible in both agents' first prompt (no — only the AGENT's own permanent memories, not cross-agent)
5. Check that `memory_recall` searches return only the agent's own chunks (not cross-agent leaks)

### Phase 8: Topology validation + commit (15 min)

1. Record the validation rows in your deployment's validation store (e.g. `<agent_b>_schema_created`, `<agent_b>_memory_ingested`, `<agent_b>_cf_tunnel_routing`)
2. Commit all the new files: `migrations/agents/<agent_b>_schema.sql`, `migrations/agents/README.md`, `app/agent/selector.py`, `config.yaml` changes, `docs/PLAN_MULTI_AGENT_SAME_SESSION.md` (this file)
3. `git push` to `peristyle-ai/py_pg_harness` on the default branch (or a new branch if the project owner prefers)

## What I'm NOT proposing (out of scope for v0)

- **Per-agent LLM adapter choice** — both agents would use the same `LLM_ADAPTER` from `.env`. If you want one agent on minimax and the other on kimi, that needs an `agent.llm_adapter` field. Easy to add later.
- **Per-agent rate limits** — both share the same `RATE_LIMIT_*` config. Per-agent limits need an `agent.rate_limit` field.
- **Per-agent model temperature** — both use the same `LLM_TEMPERATURE`. Per-agent needs `agent.temperature`.
- **Per-agent bootstrap file caps** — both use the same `BOOTSTRAP_*_MAX_BYTES`. Per-agent needs `agents.{name}.bootstrap.*`.
- **Cross-agent memory access** — explicit non-feature. Schema isolation is the safety boundary.
- **Per-agent audit retention** — both use the same `memory_audit` retention. Per-agent needs `agent.audit_retention_days`.
- **The agent's existing OpenClaw session history** — `~/.openclaw/agents/<agent_name>/sessions/` is ~1.3M. Migrating session logs into the harness is a separate effort (and possibly not desired — the harness's `turns` table will accumulate from the moment the agent starts chatting, not retroactively).

## Open questions for the project owner

1. **Option A vs Option B** — confirm Option A (single service, 2 schemas) is the right call. Or do you want Option B (2 services, 2 ports) for the isolation reasons you mentioned?
2. **The agent's password** — does the new harness auth need to match the existing OpenClaw password (so the user can log in with the same credentials), or are we rotating? If rotating, I'll need a new password.
3. **Bootstrap files** — should the new agent's `SOUL.md` / `USER.md` / `AGENTS.md` be copied verbatim, or do you want a fresh persona?
4. **Memory retention** — the harness spec doesn't say what happens to `permanent=TRUE` chunks. Should they have a TTL? Should there be a way to "unpin" old memories? (MemoryAPI has `unpin` already, so this is a workflow question, not an API question.)
5. **The agent's HMI URL** — `<agent_b>.<domain>` is the natural choice. Confirm?
6. **Migration timing** — do you want this done tonight, or in a follow-up session? Tonight would be ~6 hours of work. Follow-up gives time to think about Option A vs B and answer the open questions.

## Reference IDs (for the git commit)

- peristyle-ai/py_pg_harness branch: `main` (or `feat/multi-agent` if the project owner prefers)
- New files: `migrations/agents/<agent_b>_schema.sql`, `migrations/agents/README.md`, `app/agent/selector.py`, `docs/PLAN_MULTI_AGENT_SAME_SESSION.md`
- Modified: `config.yaml` (add `agents:` block), `app/__init__.py` (wire selector), `migrations/agents/<agent_a>_schema.sql` (no changes — just a reference for the new agent)
- Companion skill: `skills/openclaw_migration/SKILL.md` (already created, separate commit)
