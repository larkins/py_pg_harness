# openclaw_migration

Migrate an agent from a legacy OpenClaw workspace to a harness per-agent
schema. The target schema is `{agent}.memories` (long-form content) +
`{agent}.memory_chunks` (auto-chunked slices for semantic search, with
`source_table='memories'` to distinguish from turn-derived chunks).

## When to use

- A user asks to "migrate <agent_name>" (or any other agent name) from OpenClaw to harness
- A user wants to import `MEMORY.md` content + daily notes from
  `~/.openclaw/workspace/{agent}/` into a harness `{agent}.memories` table
- A user wants to copy `skills/` from the source workspace to a target
  location (typically `pie/<agent>/skills/` for per-agent skills, or
  `<install_dir>/skills/` for shared infra skills)
- **Not** for: creating a brand-new agent (use a different skill), or for
  reading an existing harness memory (use `memory_recall`)

## When NOT to use

- The user wants to migrate between two harness deployments (use direct DB dump/restore)
- The source is NOT a git-tracked OpenClaw workspace (verify the path looks
  like `~/.openclaw/workspace/{agent}/` with `AGENTS.md`, `MEMORY.md`,
  `SOUL.md`, `USER.md`, `IDENTITY.md` present)
- The target harness DB does not yet have an agent schema for the target
  agent (run `migrations/agents/agent_schema.sql` with
  `-v agent_name={agent}` first; see migration
  history: 006_install_runtime_grants.sql covers the grants,
  `agents/agent_schema.sql` covers the 4 base tables, and
  **migration 007_add_memories_table.sql** is what creates the
  `{agent}.memories` table — this migration must be applied first or
  the ingest will fail with "relation {agent}.memories does not exist")

## Target schema (post-Phase 13, 2026-10-15)

The target is the **per-agent `memories` table** (not the `memory_chunks`
table directly). The memories table holds the long-form text; the
`memory_chunks` table holds the auto-generated slices for semantic search.

```
{agent}.memories                    <-- LLM-callable, operator-curated
├── id                bigserial PK
├── content           text NOT NULL              <-- the full long-form text
├── embedding         halfvec(1024)              <-- embedding of FULL content
├── source_type       text ('chat'|'email'|'code'|'doc'|'system')
├── source_ref        text                       <-- e.g. "openclaw:memory/2026-06-13.md#Agieth API Keys"
├── importance        smallint 0-10
├── memory_hash       text UNIQUE                <-- SHA256(normalized_content)
├── status_commentary boolean
├── permanent         boolean DEFAULT false      <-- pin status (auto-injected)
├── pin_source        text ('model'|'operator'|'bootstrap')
├── pinned_at         timestamptz
├── metadata          jsonb DEFAULT '{}'
├── created_at        timestamptz DEFAULT now()  <-- OVERRIDDEN by import to file date
├── updated_at        timestamptz DEFAULT now()
└── created_by        text ('operator'|'agent'|'import')

{agent}.memory_chunks (auto-populated by write_memory)
├── id                bigserial PK
├── source_turn_id    bigint NULL    <-- for turn-derived chunks
├── source_memory_id  bigint NULL    <-- for memory-derived chunks
├── source_table      text GENERATED <-- 'turns' or 'memories'
├── conversation_id   uuid NULL      <-- nullable for memory-derived chunks
├── role, chunk_index, content, content_tsv, embedding, source_type,
│   source_ref, importance, memory_hash, status_commentary, permanent,
│   pin_source, pinned_at, metadata, created_at
```

**Why the memories table (not memory_chunks directly)?**

The OpenClaw memory archive has long-form content per logical entry
(typically 200-2000 tokens per `## ` heading). A single entry might
need to be split into 1-3 chunks for semantic search, but the LLM
should still see the whole entry when it's surfaced by recall. The
memories table is the canonical source; the memory_chunks table is
the search index. When a chunk is surfaced by semantic search, the
caller dereferences `source_table + source_row_id` to get the full
parent content (a `memories` row or a `turns` row).

## Inputs

The skill expects the following inputs (ask the user if any are missing
or ambiguous):

1. **Source workspace path** — defaults to `~/.openclaw/workspace/{agent}/`
   on the host where the OpenClaw agent lived. It may be a different Unix
   user's home on that host (e.g. `<source_user_home>/.openclaw/workspace/{agent}/`)
   or the harness operator's own home.
2. **Agent name** — the target agent name in the harness (e.g. `<agent_name>`).
   This determines the Postgres schema name
   (`{agent}.memories`, `{agent}.memory_chunks`) and the per-agent pie
   directory.
3. **Target harness DATABASE_URL** — the runtime role's connection string
   (typically `postgresql://<runtime_role>:***@127.0.0.1:5432/<db_name>`).
   The skill will NOT read this from the environment; it must be supplied
   (or extracted from the harness `.env`).
4. **Harness home directory** — where the harness install lives. Default
   `<install_dir>/`. The skill computes `pie_root = {harness_root}/pie`.
5. **Embedding endpoint** — for chunk embedding (default:
   `http://127.0.0.1:8080/v1` with `Qwen/Qwen3-Embedding-4B` and
   `?dimensions=1024`).
6. **Mode** — `memory_archive` (default, ingest the memory/ folder) or
   `pie_filesystem` (copy identity + skills to pie/<agent>/) or `both`.

## Outputs

1. **`{agent}.memories` rows** — one per logical entry (## heading) from
   `MEMORY.md` and `memory/YYYY-MM-DD.md`. Set:
   - `created_at` = the file's date (UTC midnight) for daily notes; the
     file's mtime for `MEMORY.md`
   - `importance` = 5 for daily notes, 7 for `MEMORY.md` (curated long-form)
   - `source_type` = 'system'
   - `source_ref` = `openclaw:{absolute_path}#{heading_slug}`
   - `created_by` = 'import'
   - `permanent` = false (operator can pin individual memories later)
   - The text is auto-chunked into `{agent}.memory_chunks` with
     `source_memory_id` set and `source_table='memories'` (via the
     generated column)
2. **Skills copied** (pie_filesystem mode): source `skills/` → target
   `pie/<agent>/skills/`. Skills are NOT re-copied if they already exist
   at the target (idempotent; operator can merge manually).
3. **Identity files copied** (pie_filesystem mode): `SOUL.md`, `USER.md`,
   `AGENTS.md`, `IDENTITY.md` → `pie/<agent>/identity/`, scrubbed of
   OpenClaw-specific and machine-specific references.
4. **Audit row** in `{agent}.memory_audit` for the bulk import (one row
   per source file, plus a `write_memory` audit per section).
5. **Migration report** — a summary of: files ingested, memories created
   (per file, per date), chunks generated, skills copied/skipped,
   identity files scrubbed/copied, total time. Printed to stdout AND
   saved to `<log_dir>/openclaw_migration_{agent}_{YYYYMMDD_HHMMSS}.log`.

## Source workspace layout (assumed)

The source is a git-tracked OpenClaw persona workspace. Expected files:

```
{workspace}/
├── AGENTS.md           (operator rules — only used in pie_filesystem mode)
├── IDENTITY.md         (name, theme, creature — only used in pie_filesystem mode)
├── MEMORY.md           (long-term memory — INGESTED in memory_archive mode)
├── SOUL.md             (persona — only used in pie_filesystem mode)
├── USER.md             (operator info — only used in pie_filesystem mode)
├── HEARTBEAT.md        (heartbeat rules — NOT migrated)
├── DREAMS.md           (open tasks — NOT migrated)
├── memory/
│   ├── 2026-04-15.md   (daily notes — INGESTED in memory_archive mode)
│   ├── 2026-04-16.md
│   └── ...             (arbitrary number; 76 in the reference run as of 2026-10-15)
├── skills/
│   ├── local-email/SKILL.md   (copied in pie_filesystem mode)
│   └── ...             (arbitrary number)
└── dreaming/           (NOT migrated)
```

**Rule:** In `memory_archive` mode, only `MEMORY.md` and `memory/*.md` are
ingested. In `pie_filesystem` mode, only `AGENTS.md`/`SOUL.md`/`USER.md`/
`IDENTITY.md` and `skills/` are copied. Everything else is left in place.

## Pre-flight checks (always do these first)

Before any write, verify:

1. **Source path exists and is readable** — `ls {workspace}/MEMORY.md memory/`
   should succeed
2. **Target DB is reachable** — `psql {target_url} -c "SELECT 1"` returns 0
3. **Target agent schema exists** — `psql {target_url} -c "SELECT 1 FROM {agent}.memory_chunks LIMIT 1"` returns 0
4. **The `{agent}.memories` table exists** — `psql {target_url} -c "SELECT 1 FROM {agent}.memories LIMIT 1"` returns 0. If it fails with "relation does not exist", STOP — migration 007 has not been applied. Run `psql {target_url} -f migrations/007_add_memories_table.sql` first.
5. **The harness_app role has DML on `{agent}.memories`** — `psql {target_url} -c "SELECT has_table_privilege('harness_app', '{agent}.memories', 'SELECT,INSERT,UPDATE,DELETE')"` should return `t`. (The default migration 006 + 007 grants this; verify if you suspect the role is missing privileges.)
6. **Embedding endpoint is reachable** — `curl -X POST {embedding_url}/embeddings -d '{"model":"Qwen/Qwen3-Embedding-4B","input":"test","dimensions":1024}'` returns 200
7. **Source git status is clean** (optional but recommended) — `cd {workspace} && git status` should show no uncommitted changes (avoids accidentally migrating WIP content)

If any check fails, stop and report which one (do not write half a migration).

## Step-by-step procedure (memory_archive mode)

### Step 1: Ingest MEMORY.md

```python
import os
import sys
import json
import hashlib
import re
from datetime import datetime
from pathlib import Path

# Use the harness's own MemoryAPI — it handles chunking, embedding,
# audit rows, and idempotency. Don't bypass it with raw SQL.
sys.path.insert(0, os.environ.get('HARNESS_ROOT', os.getcwd()))
os.chdir(os.environ.get('HARNESS_ROOT', os.getcwd()))

from app.agent.memory import MemoryAPI
from app.agent.embedding import EmbeddingClient
from app.config import load_config
from psycopg_pool import ConnectionPool

cfg = load_config()
pool = ConnectionPool(os.environ['HARNESS_DATABASE_URL'], min_size=1, max_size=2)
emb = EmbeddingClient(cfg)
api = MemoryAPI(pool=pool, embedding_client=emb, agent_name=AGENT, config=cfg)

source_workspace = '<source_workspace>/<agent_name>/'  # or whatever
memory_md = os.path.join(source_workspace, 'MEMORY.md')

if os.path.exists(memory_md):
    text = Path(memory_md).read_text()
    mtime = datetime.fromtimestamp(os.path.getmtime(memory_md))

    # Split on `## ` headings. Skip the preamble (before the first heading).
    # The preamble is usually just a title + date.
    parts = re.split(r'(?m)^## (.+?)$', text)
    sections = []
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = (parts[i+1] if i+1 < len(parts) else '').strip()
        if not heading or not body or len(body) < 30:
            continue
        full = f"## {heading}\n\n{body}".strip()
        if len(full) < 50:
            continue
        sections.append((heading, full))

    print(f'MEMORY.md: {len(sections)} sections, mtime={mtime.isoformat()}')
    written = 0
    skipped = 0
    errors = 0
    for heading, content in sections:
        source_ref = f"openclaw:{memory_md}#{heading[:50]}"
        try:
            api.write_memory(
                text=content,
                importance=7,                     # MEMORY.md is curated
                source_type='system',
                source_ref=source_ref,
                pin=False,
                created_at=mtime,                 # use file's mtime
                created_by='import',
            )
            written += 1
        except LookupError:
            skipped += 1                         # duplicate (idempotent)
        except Exception as e:
            errors += 1
            print(f'  ERR: {heading[:30]}: {e}')
    print(f'MEMORY.md: {written} written, {skipped} skipped, {errors} errors')
```

### Step 2: Ingest memory/YYYY-MM-DD.md (daily notes)

```python
from datetime import date, datetime

memory_dir = os.path.join(source_workspace, 'memory')
files = sorted(os.listdir(memory_dir))

written = 0
skipped = 0
errors = 0
for fname in files:
    if not re.match(r'^\d{4}-\d{2}-\d{2}', fname):
        continue                                  # skip non-date files
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', fname)
    file_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # UTC midnight for created_at — keeps the row's timestamp
    # on the event date, not the import time.
    effective_dt = datetime(file_date.year, file_date.month, file_date.day)

    fpath = os.path.join(memory_dir, fname)
    text = Path(fpath).read_text(errors='replace')
    parts = re.split(r'(?m)^## (.+?)$', text)
    sections = []
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = (parts[i+1] if i+1 < len(parts) else '').strip()
        if not heading or not body or len(body) < 30:
            continue
        full = f"## {heading}\n\n{body}".strip()
        if len(full) < 50:
            continue
        sections.append((heading, full))

    file_written = 0
    for heading, content in sections:
        source_ref = f"openclaw:{fpath}#{heading[:50]}"
        try:
            api.write_memory(
                text=content,
                importance=5,                     # daily notes default
                source_type='system',
                source_ref=source_ref,
                pin=False,
                created_at=effective_dt,
                created_by='import',
            )
            written += 1
            file_written += 1
        except LookupError:
            skipped += 1
        except Exception as e:
            errors += 1
            print(f'  ERR: {fname} / {heading[:30]}: {e}')
    print(f'  {fname}: {file_written} sections (date={file_date})')
print(f'memory/: {written} total written, {skipped} skipped, {errors} errors')
```

### Step 3: Verify

```python
with pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM {agent}.memories')
        m = cur.fetchone()[0]
        cur.execute(
            f'SELECT count(*) FROM {agent}.memory_chunks WHERE source_table = %s',
            ('memories',),
        )
        c = cur.fetchone()[0]
        cur.execute(
            f'SELECT min(created_at)::date, max(created_at)::date FROM {agent}.memories'
        )
        dmin, dmax = cur.fetchone()
        print(f'  {agent}.memories: {m}')
        print(f'  {agent}.memory_chunks from memories: {c}')
        print(f'  date range: {dmin} -> {dmax}')
```

### Step 4: Send status to operator

Report:
- Per-file section count + per-file date
- Total memories created
- Total chunks generated (memories.embedding was also embedded, so each
  memory has 1-3 chunks depending on length)
- Date range (oldest → newest)
- Any errors or skipped files
- Migration log path
- Time-based query example for the operator to verify (e.g.
  `SELECT count(*) FROM <agent_name>.memories WHERE created_at >= '2026-06-01' AND created_at < '2026-07-01';`)

## Important notes

### Idempotency

- **`write_memory()` is content-hash idempotent.** Re-running the migration
  on the same source+target is safe — the second call hits the
  `SELECT 1 FROM {agent}.memories WHERE memory_hash = ...` check and
  raises `LookupError`, which the script catches and counts as "skipped".
  No re-embedding, no re-chunking.
- **No re-embedding on duplicates.** If a memory with the same content
  already exists, the new call is rejected before embedding.
- **Skills are NOT idempotent.** If a skill with the same name exists at
  the target, it is skipped (not overwritten). If the operator wants to
  overwrite, they must `rm -rf {target_skill}` first and re-run.

### Source data fidelity

- **`created_at` is set to the file's date** (UTC midnight), not the
  import time. This is what enables the time-based query use case
  ("show me June 2026 memories"). The file mtime is used for `MEMORY.md`
  since it doesn't have a date in its filename.
- **`source_ref` is preserved** as
  `openclaw:{absolute_path}#{heading_slug}` for traceability. A future
  cleanup or re-migration can filter on
  `source_ref LIKE 'openclaw:%'`.
- **`created_by` is set to `'import'`** so the operator can distinguish
  imported memories from LLM-written ones (`'agent'`) and operator-written
  ones (`'operator'`).

### What's NOT in scope

- The skill does NOT touch `{agent}.turns` or `{agent}.conversations` —
  those are session-derived and not part of the OpenClaw memory archive.
- The skill does NOT touch the OpenClaw sqlite stores at
  `~/.openclaw/memory/{agent}.sqlite` (or `.migrated`). Those were the
  runtime vector+BM25 stores used by OpenClaw; the operator-visible
  durable record is the `memory/YYYY-MM-DD.md` + `MEMORY.md` files,
  which is what this skill ingests. (If the user wants the chunks
  surfaced by the old sqlite's vector index, that's a separate task —
  see "Recovering from OpenClaw sqlite stores" below.)
- The skill does NOT call the harness `memory_write` or `memory_update`
  LLM tools — it goes through `MemoryAPI.write_memory()` directly via the
  Python class. The LLM tools are rate-limited and not designed for bulk
  ingest.
- The skill does NOT touch `public.users` — if the target agent needs a
  user record (for the harness auth flow), create that separately via
  `app/tools/create_user`.

### Recovering from OpenClaw sqlite stores (advanced)

The OpenClaw `~/.openclaw/memory/{agent}.sqlite.migrated` is a BGE-M3
vector index of every chunk ever produced by OpenClaw. Some of those
chunks are operator-curated long-form memories that pre-date the
`memory/YYYY-MM-DD.md` files. If the operator wants to import those too:

1. Read the `chunks` table (id, path, start_line, end_line, text,
   updated_at). Each row has a `path` (e.g. `MEMORY.md`,
   `memory/2026-04-15.md`, `sessions/.../agent_main.jsonl`).
2. Skip `path` values starting with `sessions/` (those are turn-derived
   session logs — already in the turn chunks; importing them would
   duplicate content).
3. For non-session chunks, look up the file by `path` and find the
   original line range (`start_line` to `end_line`). Extract that
   slice from the source file and treat it as one memory entry.
4. Set `source_ref = f"openclaw-sqlite:{chunk.id}"` and `created_at`
   from the chunk's `updated_at`.
5. Call `api.write_memory(...)` as in Step 1/2 above.

This is best-effort; expect significant overlap with the file-based
ingest and the operator should expect a lot of "skipped" duplicates.

### What about the original 119 orphan chunks that were dropped?

The old `memory_write` tool (pre-Phase 13) wrote chunks directly to
`{agent}.memory_chunks` without a `source_turn_id`. The Phase 13
migration **drops these 120 orphan chunks** (1 from the default agent +
119 from the migrated agent) per the project owner's decision of 2026-10-15
19:53: *"no drop all the existing chunks and turns rows, no serious work has
been done yet with either agent on the new harness"*.

If the operator wants to preserve those memories, they should re-run
this skill before Phase 13's migration. After Phase 13, those
memories are gone from the DB but may still be recoverable from the
OpenClaw sqlite stores (see "Recovering from OpenClaw sqlite stores"
above).

## pie/ filesystem target (pie_filesystem mode)

For harness installations using the multi-agent `pie/<agent>/` design
(several agents served from one process with per-agent bootstrap +
memory registries), the migration target is the filesystem, not the
database. This mode copies identity files + skills into the per-agent
directory and writes the new content as the first commit on
`<your-org>/<agent>`.

**Note on memory/ files in pie_filesystem mode:** Do NOT copy `MEMORY.md`
or `memory/YYYY-MM-DD.md` to `pie/<agent>/` — those are now in the
`{agent}.memories` table, loaded by the harness on demand. The
`pie/<agent>/identity/` directory holds only the 4 bootstrap files
(SOUL, USER, AGENTS, IDENTITY) and the LLM loads them at prompt time
from the file system.

### Inputs (pie_filesystem mode)

1. **Source workspace path** — same as memory_archive mode
2. **Agent name** — `<agent_name>`, etc.
3. **Harness root** — the harness install dir. Default `<install_dir>/`.
   The skill computes `pie_root = {harness_root}/pie`.
4. **Harness git identity** — `git config user.email` / `user.name` for
   the initial commit (e.g. `<operator_email>` / `<operator_name>`).

### Target layout (pie_filesystem mode)

```
{harness_root}/pie/<agent>/
├── identity/
│   ├── SOUL.md         (migrated from source, scrubbed)
│   ├── USER.md         (migrated from source, scrubbed)
│   ├── AGENTS.md       (migrated from source, scrubbed)
│   └── IDENTITY.md     (migrated from source, scrubbed)
├── skills/             (copied from source/skills/, scrubbed)
├── scripts/            (.gitkeep only — agent writes these on its own)
├── docs/               (.gitkeep only — agent writes these on its own)
├── systemd/            (.gitkeep only — agent writes these on its own)
└── README.md           (generated; describes the pie/ layout)
```

### Step-by-step (pie_filesystem mode)

#### Step 1: Pre-flight (same as memory_archive mode, plus these)

1. Verify `pie/<agent>/` is not already a git repo (`ls pie/<agent>/.git` should fail)
2. Verify the `<your-org>/<agent>` GitHub repo exists (create it via the
   GH API or your tunnel-setup skill's `setup_github_deploy_key.sh` if not)
3. Verify the operator Unix user has a per-repo deploy key + SSH Host
   block in `~/.ssh/config`

#### Step 2: Copy identity files to `pie/<agent>/identity/`

```bash
SRC="$SOURCE_WORKSPACE"
DST="$HARNESS_ROOT/pie/$AGENT/identity"
sudo -u "$OPERATOR_USER" mkdir -p "$DST"
for f in SOUL.md USER.md AGENTS.md IDENTITY.md; do
    if [[ -f "$SRC/$f" ]]; then
        sudo -u "$OPERATOR_USER" cp "$SRC/$f" "$DST/$f.unswept"
    fi
done
```

#### Step 3: Review sweep (THE CRITICAL STEP)

OpenClaw workspaces are full of references that will mislead a harness
context. Walk each `.unswept` file and replace / remove:

**OpenClaw-specific references to scrub:**

- `~/.openclaw/workspace/` paths — replace with `pie/<agent>/` equivalents
- `~/.openclaw/agents/{agent}/` paths — note that harness has no per-agent
  Unix user; the agent identity lives in the pie/ directory
- `~/.openclaw/memory/{agent}.sqlite` (deprecated DB) — the harness uses
  Postgres `{agent}.memory_chunks` and `{agent}.memories` instead
- `~/.openclaw/agents/{agent}/agent/openclaw-agent.sqlite` (current DB) —
  same replacement
- OpenClaw-specific skill paths (`SKILL.md` files that reference OpenClaw
  config or commands) — review each skill individually
- OpenClaw `cron` / `wake` references — harness uses `systemd --user` +
  timers in `pie/<agent>/systemd/` instead
- The phrase "OpenClaw" / "openclaw" — keep only in historical context;
  rewrite present-tense references to "harness" or "the harness"

**Machine-specific references to scrub:**

- Hostnames other than the target host (e.g. references to the source
  host when migrating to the harness host, or vice versa) — replace with
  the target host or remove entirely
- IP addresses (e.g. `192.168.4.55`, `144.6.112.4`, etc.) — replace with
  the target host's IPs or remove
- Unix user paths (`<source_user_home>/.ssh/...`, etc.) —
  replace with the target harness's Unix user (`~/.ssh/...`)
- Credentials module paths (operator-local credential modules) — these
  are operator-specific; replace with the canonical credential store path
  for the target installation

**Recommended sweep approach:**

```bash
for f in $DST/*.unswept; do
    echo "=== $(basename $f) ==="
    # Read the file, apply scrub patterns, write to {f%.unswept}
done
```

For each file, present the diff to the operator and get sign-off
before committing. Don't auto-apply patterns — machine-specific references
especially need human review.

#### Step 4: Copy skills/ (also goes through the review sweep)

```bash
SRC_SKILLS="$SOURCE_WORKSPACE/skills"
DST_SKILLS="$HARNESS_ROOT/pie/$AGENT/skills"
sudo -u "$OPERATOR_USER" mkdir -p "$DST_SKILLS"
for skill_dir in "$SRC_SKILLS"/*/; do
    skill_name=$(basename "$skill_dir")
    if [[ -d "$DST_SKILLS/$skill_name" ]]; then
        echo "  skill $skill_name already exists at target; skipping"
        continue
    fi
    sudo -u "$OPERATOR_USER" cp -r "$skill_dir" "$DST_SKILLS/$skill_name"
    echo "  copied skill: $skill_name (REVIEW REQUIRED)"
done
```

#### Step 5: Init git + first commit + push

```bash
cd "$HARNESS_ROOT/pie/$AGENT"
sudo -u "$OPERATOR_USER" git init -b main
sudo -u "$OPERATOR_USER" git config user.email "$OPERATOR_EMAIL"
sudo -u "$OPERATOR_USER" git config user.name "$OPERATOR_NAME"
for f in identity/*.unswept; do
    sudo -u "$OPERATOR_USER" mv "$f" "${f%.unswept}"
done
sudo -u "$OPERATOR_USER" tee README.md >/dev/null <<README
# $AGENT
[...generated per-agent README; see the agent_template/README.md for the template...]
README
sudo -u "$OPERATOR_USER" git add .
sudo -u "$OPERATOR_USER" git commit -m "chore: initial $AGENT pie/ contents (identity files + skills + README)"
sudo -u "$OPERATOR_USER" git remote add origin "$AGENT_REPO_URL"
sudo -u "$OPERATOR_USER" git push -u origin main
```

#### Step 6: Restart the harness (so it picks up the new agent)

```bash
sudo -u "$OPERATOR_USER" XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart harness.service
# Verify
curl -s http://127.0.0.1:5010/health | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['agents'])"
# Should now include the new agent
```

### Failure modes (pie_filesystem mode)

- **Pie dir already has content** — stop, report. The operator must decide
  whether to merge or replace.
- **GH repo doesn't exist** — create it first (see your tunnel-setup skill).
- **Per-repo deploy key missing** — set it up first (same script).
- **Review sweep skipped** — DO NOT push. The harness will load
  OpenClaw-specific references into the agent's bootstrap, leading to
  confused agent behavior. The review sweep is the most error-prone
  step; always do it.

### What pie_filesystem mode does NOT do

- Does NOT ingest `MEMORY.md` or `memory/*.md` — run memory_archive mode for that.
- Does NOT touch the harness DB at all — the agent starts with empty
  memory in the harness until memory_archive mode is run.
- Does NOT start, stop, or modify any systemd services (other than
  restarting the harness at the end).
- Does NOT modify the harness `config.yaml` to add the new agent — the
  operator must do that (add to `app.agents`) and restart.

## Failure modes & recovery

- **Embedding endpoint down** — `MemoryAPI.write_memory()` will raise on
  the first embed call. Retry with exponential backoff (3 tries, 1s/2s/4s).
  If still failing, abort and report.
- **DB connection lost mid-migration** — restart the script. The
  `write_memory()` is content-hash idempotent, so re-running will skip
  the already-imported memories (LookupError caught in the loop).
- **`{agent}.memories` table doesn't exist** — STOP. The operator must
  run `migrations/007_add_memories_table.sql` first. The migration is
  idempotent so re-running it is safe.
- **the runtime role lacks privileges on `{agent}.memories`** — STOP. The
  default `006_install_runtime_grants.sql` + `007_add_memories_table.sql`
  grant DML, but if the role was customized, re-apply the grant.
- **Source file too large** — split it into sub-files before running.
  The harness spec caps the memory content at ~32K chars (anything
  larger is rejected by the LLM tool's validation; `MemoryAPI.write_memory()`
  itself doesn't cap, but downstream tools might).
- **Permission denied on source** — stop, report. The operator must fix
  the permissions.

## What this skill does NOT do

- It does NOT migrate `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`,
  `HEARTBEAT.md`, `DREAMS.md`, `inbox/`, `media/`, `accounting/`, or
  any non-memory/non-skill content. These need separate handling (e.g.
  pie_filesystem mode for the persona bootstrap, manual copy for
  non-skill workspace files).
- It does NOT start, stop, or modify any systemd services.
- It does NOT call the harness MemoryAPI via HTTP — it imports the
  Python class directly and writes synchronously. The LLM-callable
  `memory_write` / `memory_update` tools are rate-limited and not
  designed for bulk ingest.
- It does NOT touch `public.users` — if the target agent needs a user
  record, that must be created separately (e.g. via `app/tools/create_user`).
- It does NOT touch the old OpenClaw sqlite stores. If the operator
  wants chunks from the OpenClaw BGE-M3 vector index, see "Recovering
  from OpenClaw sqlite stores" above.

## Reference IDs (commit-shaped, for topology)

When the skill completes, record the outcome in your deployment's validation
store (optional — the harness itself does not require it). If you keep one,
log a row like this (operator role, your own validation DB):

```sql
INSERT INTO topology.validations
   (service, target, validation_type, status, evidence, verified_by, verified_at)
VALUES ('harness', 'openclaw_migration_{agent}@{host}', 'memory_ingest', 'pass',
        jsonb_build_object(
            'source_workspace', '{source_path}',
            'mode', '{memory_archive|pie_filesystem|both}',
            'memories_created', {n_memories},
            'chunks_from_memories', {n_chunks},
            'date_range', jsonb_build_array('{dmin}', '{dmax}'),
            'skills_copied', {n_skills},
            'skills_skipped', {n_skip},
            'identity_files_copied', {n_identity},
            'migration_log', '{log_path}',
            'completed_at', '{iso}'
        ),
        '{operator}', now())
ON CONFLICT (service, target, validation_type) DO UPDATE
SET status = EXCLUDED.status, evidence = EXCLUDED.evidence,
    verified_by = '{operator}', verified_at = EXCLUDED.verified_at;
```

## Reference example (real run, 2026-10-15)

The Phase 13 + historical import run for the original installation:

| Agent | Daily .md files | MEMORY.md sections | Total memories | Date range |
|---|--:|--:|--:|---|
| `<agent_b>` | 76 | 23 | 373 | 2026-04-15 → 2026-10-15 |
| `<agent_c>` | 105 | 13 | 306 | 2026-03-19 → 2026-10-15 |

After ingest, a time-based query like:
```sql
SELECT count(*) FROM <agent_name>.memories
WHERE created_at >= '2026-06-01' AND created_at < '2026-07-01';
```
returns 87 rows for June 2026 — the operator can browse them by date.

The auto-chunking in `MemoryAPI.write_memory()` produced 946 chunks for one
agent + 953 for the other (avg 2.5 chunks per memory — most memories are
short enough to fit in 1 chunk; long debugging sessions got 5-8 chunks each).

## Provenance

- v1 (2026-10-14): Initial version. Wrote chunks directly to
  `{agent}.memory_chunks` with `permanent=TRUE` for `MEMORY.md` content.
  Used raw SQL INSERTs, no `MemoryAPI`.
- v2 (2026-10-15): **Phase 13 rewrite** for the new `memories` table
  design. The skill now uses `MemoryAPI.write_memory()` to create
  long-form memory rows + auto-generated chunk slices, with
  `source_table='memories'` to distinguish from turn-derived chunks. The
  split is on `## ` headings (one memory per logical entry); `created_at`
  is set to the file's date (UTC midnight) for daily notes, file mtime
  for `MEMORY.md`. The Phase 13 migration (007_add_memories_table.sql)
  added the `memories` table; the previous 120 orphan chunks (1 from the
  default agent + 119 from the migrated agent) that the old `memory_write`
  tool created without a `source_turn_id` were dropped per the project
  owner's decision of 2026-10-15 19:53.
- First use of v2: historical import of the two agents' memory/ archives
  on 2026-10-15. Result: 679 memories, 1899 chunks, with date range
  covering 2026-03-19 → 2026-10-15.
