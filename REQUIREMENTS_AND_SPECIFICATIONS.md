# harness — Requirements & Specifications

> **Status:** v0 — design phase. First instance: agent `<agent_name>`, served at `https://<harness_hostname>/`.
> **Audience:** anyone implementing or operating a harness deployment (the project team, future contributors, future-self).
> **Last updated:** 2026-10-08 (v0.3 — permanent memories via memory_pin, workspace bootstrap files with per-file caps, kimi/glm deferred, audit kept indefinitely).

This document is the canonical spec for `harness` and for any deployment of it. It is intentionally concrete — implementation should be mechanical from here.

---

## 1. Purpose

`harness` is a focused replacement for OpenClaw when only the agent-runtime half is needed. It serves a single chat surface (HTML + JSON), runs one or more named agents in a single process, and stores every memory as a row in Postgres with a hybrid (vector + trigram) index for retrieval.

It is **not**:

- A multi-channel gateway (no Telegram, Discord, Slack bindings)
- A distributed system (single Unix user, single process, single DB)
- A generic agent framework (no plugins, no tool-call marketplace, no scheduling cron)

It is:

- A Flask app that answers chat messages with model-generated replies
- A Postgres-backed memory engine that the agent reads from and writes to on every turn
- A multi-agent substrate: add another agent by installing another Postgres schema

## 2. Background & motivation

### 2.1 OpenClaw 2026.9.6 — out

OpenClaw 2026.9.6 has known issues that are open upstream and we have stopped waiting on fixes. Two of them block production work:

1. **Multi-bot routing** requires four pieces (entries / channels / defaultAccount / bindings[]); when one is missed, traffic silently falls back to the default agent. The bindings[] layer is fragile and undocumented.
2. **Memory tools in markdown files** (daily notes, `memory_get`, `memory_search`) require the model to compose search queries against unbounded free-text — a stretch for any model we have tried. The round-trip is wasteful and the retrieval quality is poor.

Per the project owner, 2026-10-08: *"2026.9.6 is very problematic and i've given up on openclaw."*

Harness is the replacement for the parts of OpenClaw we actually use. We keep the parts that work: Flask + Postgres + systemd + Cloudflare Tunnel + per-repo deploy keys + the standing tenant-isolation model.

### 2.2 Inspiration — an earlier memory-in-Postgres project

The model of "memories as rows in Postgres with vector + trigram retrieval" is borrowed from an earlier open-source OpenClaw memory bridge (MIT, Python, pgvector). That project is a general-purpose openclaw memory bridge and was declared dead in 2026-06 (per internal notes); it is **not** forked here. Harness takes the design principle — direct write/read into Postgres — and writes fresh code:

- Single Flask app, no separate bridge layer
- Per-agent schemas instead of a global memory schema
- Hybrid retrieval (vector + trigram) by default, not just vector
- Hybrid agent model support (MiniMax-M3 / GLM-5.3-flash / future) — the reference project assumes one model

The spec calls out a fork of the reference project as an **open option** for v0 implementation if it turns out to be more efficient than writing fresh; the recommendation is fresh, with the reference project as design input. See §11.

## 3. Design principles

1. **Postgres is the source of truth.** Every memory is a row. No daily notes, no markdown roundtrips.
2. **Single process, single Unix user.** No multi-tenant shenanigans. Each deployment is one `harness` systemd --user unit.
3. **Multi-agent is a Postgres concept, not a process concept.** Adding an agent = `CREATE SCHEMA <name>`. The Flask process serves all agents on the same chat surface via a path/header selector.
4. **Per-agent isolation via schemas.** Each agent's schema is owned by a per-agent role; `public` holds the shared user/auth model. Cross-schema reads happen only through the explicitly shared `public.users` table.
5. **Hybrid retrieval by default.** Both vector (pgvector `halfvec`) and trigram (`pg_trgm` GIN) are first-class. Both indexes are required.
6. **One chat surface.** HTML chat at `/` for the named agent; JSON at `/api/v1/chat` for programmatic use. No second channel.
7. **Shared infrastructure, not fork.** Embeddings come from a shared GPU embedding server (`127.0.0.1:8080/v1`, Qwen3-Embedding-4B with `?dimensions=1024`). SMTP / mail / Telegram are out of scope.
8. **Model abstraction is first-class.** Each supported model gets a dedicated adapter module (`app/agent/llm/<name>.py`) plus a dedicated system-prompt module (`app/agent/prompts/<name>.py`). The agent loop, memory API, and tool framework are model-agnostic. A model is added by writing a new module — no changes to the loop. Model selection is per-installation via `LLM_ADAPTER` in `.env`.
9. **Memory is a tool, not a context-dump.** The model is given a `memory_recall` tool and decides when to use it. The agent loop never auto-injects retrieved memories into the prompt — the model asks for what it needs. Models that fail to call the tool when they should are dropped from the supported list.
10. **Memory access is mediated.** The agent loop calls a `MemoryAPI` interface (Python class). It never writes SQL. Schema isolation, query validation, rate limits, and audit are enforced at the API layer. This is the safety boundary.

## 4. Architecture overview

```
┌────────────────────────────────────────────────────────────────┐
│ Browser → https://<harness_hostname>/                         │
│              ↓                                                  │
│   Cloudflare edge (TLS, rate limit)                             │
│              ↓                                                  │
│   Cloudflare Tunnel  (config_src: local, one new tunnel)        │
│              ↓ http://                                          │
│   Flask 0.0.0.0:<bind>   (gunicorn -w 1 -t 4, TLS on too)      │
│      ├── /                            → chat UI (static)       │
│      ├── /api/v1/chat                 → JSON chat              │
│      ├── /api/v1/memories/*           → memory debug / admin   │
│      └── /health                       → 200                    │
│              │                                                  │
│              ↓                                                  │
│   Agent loop (model-agnostic, in-process)                       │
│   ┌──────────────────────────────────────────────────┐         │
│   │  tools = [memory_recall, …]                      │         │
│   │  messages = [system, …history, user]              │         │
│   │  for i in 1..MAX_ITERATIONS:                      │         │
│   │     response = ADAPTER.complete(messages, tools)  │         │
│   │     if response.has_tool_calls:                   │         │
│   │        for call in response.tool_calls:           │         │
│   │           result = TOOL_REGISTRY[call.name].run(  │         │
│   │              call.arguments, ctx={agent,user,conv}│         │
│   │           )                                       │         │
│   │           messages.append(tool_result(call, result))│        │
│   │     else:                                          │         │
│   │        final_text = response.text                 │         │
│   │        break                                       │         │
│   └──────────────────────────────────────────────────┘         │
│              │              ↑                                   │
│              ↓              │                                   │
│   ┌─────────────────────┐  ┌────────────────────────────┐      │
│   │ LLM adapter module  │  │  Tool: memory_recall        │      │
│   │ (one of):           │  │  args: {query, top_k}       │      │
│   │  • minimax_m3       │  │  ──→ MemoryAPI.search(...)  │      │
│   │  • kimi_k3          │  │                              │      │
│   │  • glm_5_3_flash    │  │  MemoryAPI (safety boundary):│      │
│   │  • <open_weight>    │  │  • schema-isolated (agent)   │      │
│   │                     │  │  • validated params          │      │
│   │  self-contained:    │  │  • rate-limited              │      │
│   │  • model name       │  │  • audited                   │      │
│   │  • base URL         │  │  • hybrid retrieval          │      │
│   │  • tool format      │  │  (pgvector + trigram + tsv)  │      │
│   │  • quirk handling   │  │                              │      │
│   └─────────────────────┘  └────────────────────────────┘      │
│              │                                                  │
│              ↓                                                  │
│   shared GPU server (embeddings)  |  Postgres (state)            │
└────────────────────────────────────────────────────────────────┘
```

Key invariants:

- **Model-agnostic core**: the agent loop, MemoryAPI, tool registry, and DB layer are written once and never change when a new model is added.
- **Per-model adapter is a module**: `app/agent/llm/<adapter>.py` is a self-contained file that knows its model name, base URL, tool-call wire format, and quirks.
- **Memory is a tool, not a context-dump**: the model decides when to recall. Writes are automatic (side effect after each turn) — the model is responsible only for recall, not for deciding what to save.
- **Safety boundary at the MemoryAPI**: the agent loop never writes SQL. MemoryAPI enforces schema isolation per agent, validates parameters, rate-limits, and writes an audit row per call.

## 5. Operating constraints

- **Tenant isolation**: runs under the operator Unix user. NEVER `sudo`, NEVER touch other tenants.
- **Single process**: `gunicorn -w 1 -t 4`. One worker keeps the pg pool sane. Threads handle concurrency.
- **DB role split**:
  - **Operator role** `<operator_role>` — DDL (peer-auth, matches Unix user).
  - **Runtime role** `harness_app` — DML only, no `CREATE`/`DROP`/`ALTER`. Used by the app at runtime.
- **No plaintext secrets**: `.env` is `chmod 600` and gitignored. TLS cert/key never committed.
- **No public ingress** other than via Cloudflare Tunnel. `127.0.0.1:<bind>` is the only listener; the LAN hop from cloudflared is `http://`.
- **Single agent per deployment by default.** v0 ships one agent (`<agent_name>`); multi-agent support is via per-schema install (§9).
- **First-class observability**: structured JSON logs to `journalctl --user -u harness.service`; `/health` for CF health checks.

## 6. Domain model

### 6.1 `public` schema (shared)

```sql
-- Users (auth, agents-on-behalf-of, sharing future apps)
CREATE TABLE public.users (
    id            BIGSERIAL PRIMARY KEY,
    email         CITEXT UNIQUE NOT NULL,
    password_hash TEXT,                    -- bcrypt; nullable for SSO-only users
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member','viewer')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

For v0 there is **no auth** — the chat surface is open to anyone with the URL. The `public.users` table is still created because (a) future auth will need it, (b) the chat history may want to attach to a user (anonymous is fine via cookie session id), (c) it serves as the model for any sibling app that wants to share auth later.

### 6.2 Per-agent schema (e.g., `<agent_name>`)

Each agent lives in its own schema. The DDL below is the per-agent template; the schema name is substituted at install time (`-v agent_name=<name>`). Tables:

```sql
-- Conversation threads (one per "session" — typically browser session)
CREATE TABLE <agent_name>.conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       BIGINT REFERENCES public.users(id) ON DELETE SET NULL,   -- nullable for anonymous
    agent_name    TEXT NOT NULL DEFAULT '<agent_name>',
    title         TEXT,                                                   -- auto-generated first-turn summary
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Turns (one row per user or assistant message)
CREATE TABLE <agent_name>.turns (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES <agent_name>.conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    token_count     INTEGER,
    model           TEXT,                                                  -- which model produced this turn
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX turns_conv_idx ON <agent_name>.turns (conversation_id, created_at);

-- Memory chunks (the "memories")
CREATE TABLE <agent_name>.memory_chunks (
    id              BIGSERIAL PRIMARY KEY,
    source_turn_id  BIGINT REFERENCES <agent_name>.turns(id) ON DELETE SET NULL,
    conversation_id UUID REFERENCES <agent_name>.conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    content         TEXT NOT NULL,
    content_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding       halfvec(1024),                                         -- Matryoshka-truncated Qwen3-Embedding-4B
    source_type     TEXT NOT NULL DEFAULT 'chat' CHECK (source_type IN ('chat','email','code','doc','system')),
    source_ref      TEXT,                                                  -- e.g. file path, email msg-id, doc URL
    importance      SMALLINT NOT NULL DEFAULT 5 CHECK (importance BETWEEN 0 AND 10),
    memory_hash     TEXT NOT NULL,                                         -- SHA-256 of whitespace-normalized content; UNIQUE below
    status_commentary BOOLEAN NOT NULL DEFAULT FALSE,                      -- set by classifier; status-y turns won't be retrieved
    permanent       BOOLEAN NOT NULL DEFAULT FALSE,                        -- if TRUE, injected into every prompt (see §7.6)
    pin_source       TEXT,                                                   -- 'model' (memory_pin tool) | 'operator' (API) | NULL
    pinned_at       TIMESTAMPTZ,                                           -- NULL unless permanent = TRUE
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (memory_hash, conversation_id)
);

-- Partial index — small/fast for the every-prompt injection query.
-- No HNSW here: permanent memory is filtered by the index, retrieved by id/order, never by similarity.
CREATE INDEX memory_chunks_permanent_idx
    ON <agent_name>.memory_chunks (permanent, pinned_at DESC)
    WHERE permanent = TRUE;

-- Vector index (HNSW, cosine)
CREATE INDEX memory_chunks_emb_hnsw_idx
    ON <agent_name>.memory_chunks USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Trigram index for fuzzy text search
CREATE INDEX memory_chunks_content_trgm_idx
    ON <agent_name>.memory_chunks USING gin (content gin_trgm_ops);

-- Full-text search index (English)
CREATE INDEX memory_chunks_tsv_idx
    ON <agent_name>.memory_chunks USING gin (content_tsv);

-- Per-agent config (JSONB key-value)
CREATE TABLE <agent_name>.agent_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Key column notes:**

- `embedding halfvec(1024)` — Matryoshka-truncated Qwen3-Embedding-4B. **CRITICAL**: native 2560-dim exceeds Postgres page limit; `?dimensions=1024` is mandatory. See MEMORY.md.
- `content_tsv tsvector GENERATED` — maintained by Postgres; supports `@@ to_tsquery` queries.
- `metadata JSONB` — extensible per-chunk attributes (URLs, file refs, tags) without schema changes.

### 6.3 Extensions

- `pgcrypto` (for `gen_random_uuid()`)
- `pg_trgm` (trigram)
- `vector` (pgvector — provides `halfvec`)
- `citext` (case-insensitive email)

## 7. Memory system

This section describes the **storage** and **retrieval** mechanics. The **API** the model calls is in §7.4. The **tool wiring** is in §8.

### 7.1 Write path (synchronous, per turn)

After the assistant turn is produced, the system (not the model) does this:

1. The (user, assistant) pair is stored in `<agent>.turns`.
2. The user content is chunked (see §7.3). For each chunk:
   - **Dedupe check**: compute `SHA-256(normalize_whitespace(chunk.text))` → look up `<agent>.memory_chunks.memory_hash` for the same `conversation_id`. If present, skip insert.
   - **Status-commentary filter**: if the classifier (lifted from the reference project's `status_commentary.py`) flags this chunk as "checking…/running…" assistant noise, insert with `status_commentary=TRUE` so retrieval will downweight it.
   - Get an embedding from the shared GPU server (`POST /v1/embeddings`, model `Qwen/Qwen3-Embedding-4B`, `dimensions=1024`).
   - Insert into `<agent>.memory_chunks` with `role='user'`, `source_turn_id=<user turn>`, `memory_hash=<hash>`.
3. The assistant content is chunked and embedded the same way, `role='assistant'`.

Both writes happen in the same DB transaction as the turn inserts. Failure to embed = fail the request.

> **Note for future work**: a `memory_remember` tool that lets the model control writes can be added later (§11.5). v0 keeps writes automatic — the model is responsible for *recall*, not for *what gets saved*.

### 7.2 Retrieval path

Hybrid retrieval, called via the MemoryAPI (`memory.search(query, top_k, recency_decay_days)`):

1. Embed the query (same GPU server, same model, same dimension).
2. **Hybrid retrieval** (Reciprocal Rank Fusion over two ranked lists):
   - **Vector list**: `ORDER BY embedding <=> $1::halfvec(1024) LIMIT 30`. Cosine distance.
   - **Trigram list**: `content % $1 OR content ILIKE '%' || $1 || '%'` ranked by `similarity(content, $1) DESC LIMIT 30`.
   - Filter: `status_commentary = FALSE` (status-y chunks never surface unless explicitly searched).
   - RRF score per chunk: `Σ 1 / (60 + rank_i)` across both lists. Top-K = 10 chunks.
3. Apply recency decay: `final_score = rrf_score * exp(-Δdays / 30)`. So a 30-day-old memory is weighted ~37% of a fresh one. Status-commentary chunks also get an extra `0.1×` weight (they survive but rank poorly).
4. Fetch the parent `<agent>.turns` rows for each retrieved chunk (1 round) so the response can include the surrounding context (a window of ±1 turn).
5. Return top-10 (chunk + neighbor context) to the caller.

### 7.3 Chunking strategy

- **Length budget**: 800 tokens max per chunk (rough; use `len(content.split())` as a proxy for v0).
- **Split strategy**: greedy paragraph split. If a paragraph is over the budget, split on sentence boundary (regex `(?<=[.!?])\s+`).
- **Overlap**: 50 tokens between adjacent chunks.
- **Minimum chunk size**: 20 tokens. Tiny chunks are dropped.

### 7.4 MemoryAPI — the safety boundary

The agent loop never writes SQL. It calls `MemoryAPI`, a Python class in `app/agent/memory.py`. The class is the single ingress point for memory access; the agent loop and the public `/api/v1/memories/*` debug routes both use it.

```python
class MemoryAPI:
    def __init__(self, db_pool, embedding_client, agent_name: str):
        ...

    def search(self, query: str, top_k: int = 10,
               recency_decay_days: int = 30) -> list[MemoryChunk]:
        """Hybrid retrieval. Returns top-k chunks with content + metadata."""
        ...

    def write_turns(self, conv_id: UUID, user_text: str, assistant_text: str,
                    model: str) -> tuple[Turn, Turn]:
        """Persist a (user, assistant) pair + their memory chunks. One tx.
        Dedupe + status-commentary filter applied per chunk."""
        ...

    def get_permanent(self, max_tokens: int = 10000) -> tuple[list[MemoryChunk], bool]:
        """Return all permanent memory chunks, ordered by importance desc + pinned_at asc.
        Stops at max_tokens. Returns (chunks, overflowed) where overflowed=True means
        more were available but truncated."""
        ...

    def pin(self, chunk_id: int, source: Literal['model', 'operator']) -> MemoryChunk:
        """Mark chunk permanent. source='model' for memory_pin tool,
        source='operator' for admin API. Audit row in memory_audit."""
        ...

    def unpin(self, chunk_id: int, source: Literal['model', 'operator']) -> MemoryChunk:
        """Demote chunk from permanent. Chunk remains in memory_chunks;
        just stops being auto-injected."""
        ...
```

**What the API enforces (safety invariants):**

- **Schema isolation**: every query is namespaced by `agent_name`; you cannot read another agent's memories even if you know the conversation UUID. The DB role used at the connection level is also restricted to `<agent>` (GRANT done in §13.2).
- **Parametric queries only**: psycopg `%s` placeholders. No string interpolation. (Linted by `bandit` rule B608.)
- **Rate limit**: `MAX_SEARCHES_PER_TURN = 5`. The agent loop enforces this; the API also has a per-conversation counter.
- **Audit row per call**: every `search()` and `write_turns()` writes a row to `<agent>.memory_audit (ts, op, query_hash, top_k, latency_ms, result_count)`. Query hash = SHA-256 of normalized query — for deduplication in audit review.
- **No raw SQL escape hatch**: there is no `execute_raw()` method. If a feature genuinely needs raw SQL, it has to live in the API class (and get a PR review).
- **Tool-visible surface = `search` only (v0)**: the `memory_recall` tool calls `search()`. `write_turns()` is called by the agent loop automatically, not by the model. In v0, models cannot save or delete memories via the tool surface.

### 7.5 Embedding model

- **Server**: `http://127.0.0.1:8080/v1` (the shared GPU embedding server).
- **Model**: `Qwen/Qwen3-Embedding-4B`.
- **Dimensions**: `1024` (Matryoshka; pgvector-safe).
- **Auth**: `Authorization: Bearer $EMBEDDING_API_KEY` if set, else unauthenticated (LAN).
- **Timeout**: 10 s per call; circuit-break after 3 consecutive failures.

### 7.6 Permanent memories (agent-curated, injected every prompt)

Some facts must always be in front of the model — user identity, standing instructions, project state, anything the agent decides it can never afford to forget. These are stored as rows in `<agent>.memory_chunks` with `permanent = true`, fetched at prompt-composition time, and injected into every prompt as a distinct block.

**Write path** — three ways to pin a memory:

1. **Model-driven** (most common): the model calls the `memory_pin(chunk_id)` tool after a turn. The agent loop promotes the chunk and writes `pin_source='model'`, `pinned_at=now()`.
2. **Operator-driven** (manual curation): operator hits `PUT /api/v1/memories/<chunk_id>/pin` with `{"permanent": true}`. Writes `pin_source='operator'`.
3. **Bootstrap** (initial install): operator inserts rows directly into `<agent>.memory_chunks` via SQL with `permanent = true, pin_source = 'bootstrap'`.

**Read path** — at every prompt composition, the agent loop fetches:

```sql
SELECT id, content, role, pinned_at, pin_source
  FROM <agent>.memory_chunks
 WHERE permanent = TRUE
 ORDER BY importance DESC, pinned_at ASC;
```

Truncation happens in the loop, not the query — the loop reads up to `MEMORY_PERMANENT_MAX_TOKENS` (default 10,000) tokens worth of permanent memory and stops. If the cap is hit, a `<truncation_warning>` banner is added to the prompt naming how many chunks were dropped. The audit table logs `op_over_run = true` for every prompt that hit the cap.

**Tools** — `memory_pin(chunk_id)` and `memory_unpin(chunk_id)` are first-class tools in §8.5. Model can call them, with the same audit/limit framework as `memory_recall`.

**Cap and configuration** (config.yaml):

```yaml
memory:
  permanent:
    max_tokens: 10000           # total budget for all permanent memory; token-approx via len(content.split())
    warn_overflow: true         # if true, inject <truncation_warning> block when cap is hit
    pin_audit: true             # every pin/unpin writes to memory_audit
```

**Unpinning**: `memory_unpin(chunk_id)` sets `permanent = false, pin_source = NULL, pinned_at = NULL`. The chunk remains in `memory_chunks` (still retrievable via `memory_recall`); it just stops being auto-injected.

**Why collective cap OK here (but not for .MD files)**: permanent memory is in the database. The truncation warning names what was dropped. The operator can query `SELECT COUNT(*) FROM <agent>.memory_chunks WHERE permanent = TRUE` and see the state. With .MD files (next section), truncation would be silent and you couldn't tell which file ate the budget.

### 7.7 Workspace bootstrap files (.MD injection + per-file caps)

Several workspace-level markdown files are loaded at agent startup and injected into every prompt. These give the agent standing context about itself, its human, and the environment. Like OpenClaw's bootstrap files:

| File | Source | Purpose |
|---|---|---|
| `SOUL.md` | workspace root | Persona / tone |
| `USER.md` | workspace root | About the human |
| `AGENTS.md` | workspace root | Standing rules |
| `IDENTITY.md` | workspace root | Self-identity (name, emoji, theme) |
| `TOOLS.md` | workspace root | Local infra notes (not loaded into prompt — too verbose; available via tool only) |
| `MEMORY.md` | workspace root | Curated long-term memory (separate from database permanent memories) |

> `TOOLS.md` is intentionally NOT injected — it can be 20k+ bytes and is meant for the agent's reference, not its prompt. It becomes a tool target for `file_read` later (v0.1+).

**Where they live**: under the agent's workspace root (`$WORKSPACE_ROOT`, mirroring the OpenClaw layout). For per-agent overrides, `<agent_name>/<file>` takes precedence over the workspace `<file>` — so `<agent_name>/SOUL.md` overrides `SOUL.md` for that agent.

**Per-file caps** (config.yaml). NO collective cap:

```yaml
bootstrap:
  files:
    SOUL.md:        { path: SOUL.md,             max_bytes: 8000 }
    USER.md:        { path: USER.md,             max_bytes: 4000 }
    AGENTS.md:      { path: AGENTS.md,           max_bytes: 12000 }
    IDENTITY.md:    { path: IDENTITY.md,         max_bytes: 4000 }
    MEMORY.md:      { path: MEMORY.md,           max_bytes: 12000 }
  behavior_on_overflow: fail   # 'fail' = refuse to start; 'truncate' = log + continue (NOT recommended)
```

**Behavior on overflow**: `behavior_on_overflow = fail` is the default. The agent refuses to start and logs the offending file with actual size and configured cap:

```
FATAL: bootstrap file SOUL.md is 12453 bytes; cap is 8000 bytes.
       Operator must trim the file OR raise the cap in config.yaml.
       Refusing to start harness.
```

No silent truncation, no truncation banner in the prompt. If the file is over its cap, the agent doesn't run until the operator resolves it.

**Per-agent overrides (v0.1)**:

```yaml
agents:
  <agent_name>:
    bootstrap_overrides:
      SOUL.md: { path: <agent_name>/SOUL.md }   # agent-specific persona
      # Files not in bootstrap_overrides fall back to the workspace file.
```

**Why per-file caps and not a collective cap** (per the project owner, 2026-10-08 09:18):

> *"a collective cap leads to dangerous and hard to debug behaviour."*

A collective cap means files silently drop each other from the prompt. When the agent behaves inconsistently, you don't know whether USER.md got truncated, or SOUL.md, or both. Per-file caps make overflow loud and per-file debuggable: the offending file is named in the error.

**Read path** — at agent startup, after caps are validated:

```
<bootstrap>
  <SOUL.md>      [...content, ≤ SOUL.md cap...]
  <USER.md>      [...content, ≤ USER.md cap...]
  <AGENTS.md>    [...content, ≤ AGENTS.md cap...]
  <IDENTITY.md>  [...content, ≤ IDENTITY.md cap...]
  <MEMORY.md>    [...content, ≤ MEMORY.md cap...]
</bootstrap>
```

This block is injected into the system prompt (or as the first user message, depending on adapter preference).

**Memory safety interaction**: workspace `MEMORY.md` is a *separate mechanism* from database permanent memories (§7.6). Both are injected every prompt. `MEMORY.md` is for operator-curated high-level standing facts (e.g. "current project: harness v0.2"). Database permanent memories are for facts the model decides to pin during a conversation. They're complementary, not redundant.

## 8. Model abstraction layer

A model is a **pluggable adapter module**, not a config string. The agent loop, MemoryAPI, tool registry, and DB layer are written once and never change when a new model is added. Adding a model = writing one new module under `app/agent/llm/` and one under `app/agent/prompts/`.

### 8.1 Adapter interface

Every model implements `BaseLLM` (in `app/agent/llm/base.py`):

```python
class BaseLLM(Protocol):
    name: str                  # human-readable, e.g. "minimax/MiniMax-M3"
    adapter_id: str            # module name, e.g. "minimax_m3"

    def __init__(self, config: AppConfig): ...
    async def complete(self, messages: list[Message],
                       tools: list[Tool] | None = None,
                       max_tokens: int = 2048,
                       temperature: float = 0.7) -> LLMResponse: ...
    def format_tool_result(self, call: ToolCall, result: Any) -> Message:
        """Render a tool result in the format this model expects
        (Anthropic tool_result block, OpenAI tool message, etc.)."""
    ...
```

`LLMResponse` is a normalized object — the loop never sees wire-format details:

```python
@dataclass
class LLMResponse:
    text: str | None                       # final text, if model produced one
    tool_calls: list[ToolCall]             # tool calls, if model made any
    finish_reason: str                     # "stop" | "tool_use" | "length" | "error"
    raw: Any                               # underlying provider response (for logging)
    usage: dict                            # tokens_in, tokens_out, latency_ms
```

The loop only branches on `response.text` (final answer) vs `response.tool_calls` (continue the loop).

### 8.2 Adapter registry and loading

`app/agent/llm/__init__.py` exports a registry and factory:

```python
ADAPTERS = {
    "minimax_m3":      "app.agent.llm.minimax_m3",
    # Future: "kimi_k3": "app.agent.llm.kimi_k3",  — added when explicitly built
    # Future: "glm_5_3_flash": "app.agent.llm.glm_5_3_flash",
}

def get_adapter(config: AppConfig) -> BaseLLM:
    adapter_id = config.LLM_ADAPTER        # e.g. "minimax_m3"
    if adapter_id not in ADAPTERS:
        raise ValueError(f"Unknown LLM_ADAPTER: {adapter_id}. "
                         f"Available: {list(ADAPTERS)}")
    module = importlib.import_module(ADAPTERS[adapter_id])
    return module.LLM(config)
```

`LLM_ADAPTER` lives in `.env`. Only one adapter is loaded per process — no parallel fan-out. The import is at startup, so missing dependencies fail fast.

### 8.3 Supported adapters (v0)

| Adapter ID | Model | Provider | Tool-call format | Quirks handled in adapter |
|---|---|---|---|---|
| `minimax_m3` | `minimax/MiniMax-M3` | Anthropic-compat endpoint `https://api.minimax.io/anthropic` | Anthropic `tool_use` blocks | Strip `<extras>` envelope (MEMORY.md lesson); `/v1` endpoint garbles history — use Anthropic only |

> **v0 ships one adapter (`minimax_m3`) per the project owner, 2026-10-08 09:18.** Other models (`kimi_k3`, `glm_5_3_flash`, etc.) get added later as their own dedicated adapter modules — **no template pattern, no worked example**. Model support is explicit per model.

> **Quality bar (project owner, 2026-10-08)**: a model is added to `ADAPTERS` only after we verify it can (a) call `memory_recall` correctly when given a system prompt that names the tool and (b) reason over the returned chunks to produce a coherent answer, AND (c) handle `memory_pin`/`memory_unpin` correctly (call when appropriate, not call for non-memories). Models that fail any of these are dropped from the supported list. We would rather ship one working model than nine broken ones.

### 8.4 Per-model system prompts

Each adapter pairs with a system-prompt module in `app/agent/prompts/<adapter_id>.py`:

```python
# app/agent/prompts/minimax_m3.py
SYSTEM_PROMPT = """You are {agent_name}, an AI assistant in harness.

You have these tools:
- memory_recall(query, top_k) → search your long-term memory for relevant past content.

When the user asks a question:
1. Decide if the question requires recalling prior context. If yes, call memory_recall.
2. If memory returns nothing useful, answer from your general knowledge.
3. If memory returns relevant chunks, weave them into your answer with a brief reference to source/date.
4. Never claim to remember something you have not retrieved.
5. You cannot browse the web, read files, or execute code. If asked, say so and answer from memory or refuse.

Tone: {agent_tone} (configured in agent_config).
"""
```

The prompt is rendered at agent-startup with `agent_name` and `agent_tone` substituted from the per-agent config. Per-model system prompts are the only model-specific user-facing copy; everything else in the loop is shared.

### 8.5 Tool framework

Tools are pluggable too. `app/agent/tools/base.py` defines `Tool`:

```python
class Tool(Protocol):
    name: str
    description: str
    parameters: dict           # JSON schema
    async def run(self, args: dict, ctx: ToolContext) -> dict: ...
```

`ToolContext` carries: `agent_name`, `conversation_id`, `user_id` (if auth later), and a `MemoryAPI` instance.

`app/agent/tools/memory_recall.py` is the one tool that ships in v0:

```python
# registers itself on import
TOOL = Tool(
    name="memory_recall",
    description="Search your long-term memory for content relevant to the query. "
                "Returns a list of memory chunks with content, source role (user/assistant), "
                "and approximate age in days. Use this when the user asks a question that "
                "might relate to prior conversation history or stored facts.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "The semantic search query. Be specific."},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 30,
                      "default": 10, "description": "How many chunks to return."},
        },
        "required": ["query"],
    },
    run=memory_recall_run,     # async (args, ctx) -> {"chunks": [...]}
)
```

`TOOL_REGISTRY` in `app/agent/tools/registry.py` collects all `TOOL` exports. The agent loop gets the registered tools and passes them to the adapter on each `complete()` call.

**Two additional tools in v0** (per the project owner, 2026-10-08 09:18):

```python
# app/agent/tools/memory_pin.py
TOOL_PIN = Tool(
    name="memory_pin",
    description="Promote a memory chunk to permanent status. The chunk will then be "
                "injected into every prompt as standing context. Use this when the user "
                "or the conversation establishes a fact that must be remembered across "
                "all future conversations — e.g. the user's name, a long-running project, "
                "a standing instruction. Do NOT pin ephemeral facts, recent conversation "
                "details, or anything that should naturally age out of memory.",
    parameters={
        "type": "object",
        "properties": {
            "chunk_id": {"type": "integer",
                           "description": "The id of the chunk to pin (returned by memory_recall)."},
            "reason":   {"type": "string",
                           "description": "Short justification for why this should be permanent. ""
                                          "Recorded in the audit row."},
        },
        "required": ["chunk_id", "reason"],
    },
    run=memory_pin_run,    # async (args, ctx) -> {"chunk_id": X, "permanent": true}
)

# app/agent/tools/memory_unpin.py
TOOL_UNPIN = Tool(
    name="memory_unpin",
    description="Demote a previously-pinned memory chunk. The chunk remains in memory " 
                "(still retrievable via memory_recall) but stops being auto-injected. " 
                "Use this when a previously-permanent fact is no longer relevant.",
    parameters={
        "type": "object",
        "properties": {
            "chunk_id": {"type": "integer", "description": "The chunk id to unpin."},
        },
        "required": ["chunk_id"],
    },
    run=memory_unpin_run,
)
```

Both tools go through `MemoryAPI.pin()` / `MemoryAPI.unpin()` with `source='model'`. Each call writes a `memory_audit` row. Pin rate limit: `MAX_PINS_PER_TURN = 3` (loop-enforced AND API-enforced). Unpin rate limit: `MAX_UNPINS_PER_TURN = 5`.

Future tools (in priority order, none in v0):
- `web_search` — out of scope until we have a vetted search provider
- `file_read` — out of scope; prompts model to refuse instead
- `memory_forget` — explicit user-driven memory deletion (UI affordance, not just unpin)

### 8.6 Turn management

The "turn" abstraction (one user message → one assistant response, possibly with intermediate tool calls) lives in `app/agent/turn.py`. It is **model-agnostic**: state is `(conversation_id, agent_name, messages_so_far, tool_iterations)`. Per-model quirks (Anthropic envelope format, OpenAI message ordering, etc.) live in the adapter's `format_tool_result` and the message-rendering code.

The loop in §9 never branches on model identity. It calls `adapter.complete(messages, tools)` and `adapter.format_tool_result(call, result)` — both are part of the `BaseLLM` contract.

## 9. Agent loop

```python
async def handle_chat(user_text: str, conversation_id: UUID | None,
                      agent_name: str, ctx: RequestContext) -> ChatResponse:
    conv = load_or_create_conversation(conversation_id, agent_name, ctx)
    user_turn = memory.write_turn_user(conv.id, user_text)
                # NB: write_turn_user inserts the turn row but does NOT
                # embed+chunk yet. Embedding happens after the model responds,
                # once we know the full conversation content for that turn.

    # Build the message stream from the conversation + new user turn.
    # The order of injected blocks (top -> bottom of prompt):
    #   1. system prompt (from app.agent.prompts.<adapter_id>)
    #   2. <bootstrap> workspace .MD files (SOUL.md, USER.md, AGENTS.md, IDENTITY.md, MEMORY.md)
    #   3. <permanent_memories> rows from <agent>.memory_chunks WHERE permanent = TRUE
    #   4. ...history (last 20 turns)
    #   5. user (the new message)
    messages = compose_messages(conv, user_turn,
                                system_prompt=PROMPTS[adapter_id],
                                bootstrap=BOOTSTRAP_LOADER.load(agent_name),
                                permanent_chunks=memory.get_permanent(
                                    max_tokens=cfg.MEMORY_PERMANENT_MAX_TOKENS),
                                agent_name=agent_name)

    # Tool-use loop
    tool_iterations = 0
    final_text = None
    while tool_iterations < MAX_TOOL_ITERATIONS:    # default 5
        response = await adapter.complete(messages, tools=TOOL_REGISTRY.values())
        log_response(response, ctx)

        if not response.tool_calls:
            final_text = response.text
            break

        # Execute each tool call sequentially
        for call in response.tool_calls:
            if call.name not in TOOL_REGISTRY:
                # Unknown tool — model hallucinated. Inject an error result so
                # the model can recover, rather than crashing the loop.
                result = {"error": f"Unknown tool: {call.name}"}
            else:
                try:
                    result = await TOOL_REGISTRY[call.name].run(
                        call.arguments,
                        ctx=ToolContext(agent_name=agent_name,
                                        conversation_id=conv.id,
                                        memory=memory))
                except Exception as e:
                    log_tool_error(call, e, ctx)
                    result = {"error": f"{type(e).__name__}: {e}"}

            # Adapter formats the result in its own wire format
            messages.append(adapter.format_tool_result(call, result))

        tool_iterations += 1

    if final_text is None:
        # Model exhausted tool iterations without producing text. Fall back.
        final_text = ("I wasn't able to finish reasoning within the allowed tool "
                      "iterations. Could you rephrase or simplify the question?")

    # Persist the assistant turn + embed both new turns into memory
    assistant_turn = memory.write_turn_assistant(
        conv.id, final_text, model=adapter.name,
        latency_ms=int((time.monotonic() - ctx.t0) * 1000),
    )
    memory.write_turns(conv.id, user_turn, assistant_turn)

    return ChatResponse(
        conversation_id=conv.id,
        assistant=final_text,
        model=adapter.name,
        latency_ms=int((time.monotonic() - ctx.t0) * 1000),
        tool_iterations=tool_iterations,
        retrieved_chunk_ids=[],   # populate if memory_recall was called
    )
```

### 9.1 Prompt composition (message stream)

The agent loop builds `messages` from:

```
[
    system,                 # from app.agent.prompts.<adapter_id>.SYSTEM_PROMPT
                            # rendered with agent_name + agent_tone from agent_config

    system_or_user: <bootstrap>
            # Workspace .MD files (SOUL.md, USER.md, AGENTS.md, IDENTITY.md, MEMORY.md)
            # Loaded at startup; each file already cap-checked then.
            # Injected as one synthetic message (system role for Anthropic-compat, 
            # user role for OpenAI-style; adapter decides).

    system_or_user: <permanent_memories>
            # memory_chunks WHERE permanent = TRUE, ordered by importance DESC, pinned_at ASC
            # Truncated at MEMORY_PERMANENT_MAX_TOKENS. If truncated and 
            # MEMORY_PERMANENT_WARN_OVERFLOW=true, a <truncation_warning> sub-block is appended.
            # Logged to memory_audit as op='permanent_fetch', overflowed=true if capped.

    ...history,             # last 20 turns from <agent>.turns in order
    user,                   # the new user message (this turn)
]
```

The history window of 20 is a soft cap; in v0 it's never summarized. If history grows past this, older turns are dropped from the prompt (but still in the DB and still in memory_chunks via recall). Future work: a `summarize_old_turns` step.

**Why bootstrap + permanent memories are *separate* blocks** (not merged into one):

- Bootstrap files are loaded once at startup; their caps are validated then.
- Permanent memories are loaded every prompt; their cap is enforced then.
- Two different failure modes, two different fixes — easier to debug when separated.
- The model sees both blocks but knows that bootstrap is "the workspace's standing context" and permanent memories are "things pinned during prior conversations". Subtle but distinct.

### 9.2 Invariants

- **No auto-recall**: the loop never calls `memory.search()` on the model's behalf. If the model wants context, it must call the tool.
- **Tool calls are bounded**: `MAX_TOOL_ITERATIONS = 5` outer; `MAX_SEARCHES_PER_TURN = 5` inner; `MAX_PINS_PER_TURN = 3`; `MAX_UNPINS_PER_TURN = 5`. Loop terminates with a fallback message on exhaustion.
- **Tool errors are recoverable**: a tool exception becomes a tool result with `{"error": "..."}`. The model sees the error and can retry or skip. The loop does not crash.
- **No exception bubbles to the user**: the only user-visible failure is `final_text` ending up as the fallback message. Everything else is logged and recovered.
- **Bootstrap + permanent memories are read-only at prompt time**: the agent loop never writes bootstrap files or flips `permanent` from this code path — only via the explicit pin/unpin tools or operator API.

### 9.3 Streaming (optional, v0.1)

Server-Sent Events at `/api/v1/chat/stream`. Out of scope for v0 — JSON-only is acceptable.

## 10. Multi-agent extension

The whole point of per-agent schemas is that adding an agent is mechanical.

### 10.1 Install another agent

```bash
# 1. Create per-agent role (operator-grade; DDL within their own schema)
sudo -u postgres psql -d <db_name> -c "
    CREATE ROLE <agent_runtime_role> LOGIN PASSWORD '...';
    GRANT CONNECT ON DATABASE <db_name> TO <agent_runtime_role>;
    GRANT USAGE, CREATE ON SCHEMA <agent_name> TO <agent_runtime_role>;
    ALTER SCHEMA <agent_name> OWNER TO <agent_runtime_role>;
"

# 2. Run the per-agent installer
psql -d <db_name> -v agent_name=<agent_name> -f migrations/agents/<agent_name>_schema.sql
# (This is a templated file — the schema name is substituted via -v)
```

### 10.2 Routing

The chat URL includes the agent name:

- `GET /` → serves chat UI for the **default** agent (config: `HARNESS_DEFAULT_AGENT=<agent_name>`)
- `GET /?agent=foo` → serves chat UI for `foo`
- `POST /api/v1/chat {"agent": "foo", ...}` → routes to agent `foo`'s schema

If the agent schema doesn't exist, 404. No implicit creation — agents are an ops decision.

### 10.3 Cross-agent isolation

- One agent cannot read another agent's `memory_chunks` (different schemas).
- Both share `public.users`.
- One agent's runtime role has `USAGE` on `public` only (no `CREATE`); DDL is done by the operator role (`<operator_role>`).

## 11. APIs

### Web (HTML)

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | none | Chat UI for default agent |
| `/health` | GET | none | `200 {"status":"ok"}` |
| `/static/...` | GET | none | Static assets |

### JSON

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/api/v1/chat` | POST | none | One-shot chat (see below) |
| `/api/v1/chat/stream` | POST | none | SSE-streamed chat (v0.1) |
| `/api/v1/conversations` | GET | none | List recent conversations (last 50) |
| `/api/v1/conversations/<id>` | GET | none | Get conversation + turns |
| `/api/v1/memories` | GET | none | List recent memory chunks (debug) |
| `/api/v1/memories/search?q=...` | GET | none | Hybrid retrieval (debug — returns chunks + scores) |

### `POST /api/v1/chat`

```json
// Request
{
    "agent": "<agent_name>",                    // optional, default = HARNESS_DEFAULT_AGENT
    "conversation_id": "uuid-or-null",         // null = start new
    "message": "user text here"
}

// 200 Response
{
    "conversation_id": "uuid",
    "assistant": "model response",
    "model": "minimax/MiniMax-M3",
    "latency_ms": 1234,
    "retrieved_memory_ids": [101, 102, 103]    // for debug
}
```

## 12. Implementation plan (fork the reference project vs fresh)

**Recommendation:** write fresh. After a full recon of the reference repo on 2026-10-08, the reasons are clearer:

- The reference project's `memory_engine/` (a 282-line schema file, dual-vector design, repair-job pipeline) is well-tested but **single-tenant** — everything in `public`, no schemas, no per-agent role separation. We'd have to rip out the schema to get per-agent isolation.
- The reference project's `src/` memory package is a half-finished Phase-1 rewrite that never got the bridge wired up. Forks would carry dead code.
- It has **no chunking** (one memory = one row of arbitrary length; long inputs embedded whole). The `?dimensions=1024` Matryoshka truncation caps useful context per memory at ~1024 tokens of cosine-relevant content. Harness's chunked retrieval is a real improvement here.
- It has **no trigram / full-text indexing** — only vector. Fuzzy keyword queries (`%` / `ILIKE`) are slow. Harness gains this for free from `pg_trgm`.
- It assumes one embedding model + one LLM. Harness takes both via env vars.
- It ships 6 systemd services + 5 timers (chat-bridge, repair, reverie, sub-agent-activity, compaction-monitor, TTS proxy). Most of this surface area is **out of scope** for harness v0.

**Biggest new piece (v0.2 — per the project owner, 2026-10-08)**: the model abstraction layer (§8) is now first-class. Each supported model has a self-contained adapter module — this is bigger than the memory engine in v0. Adding the second adapter is the most informative exercise; that's why `kimi_k3` and `glm_5_3_flash` slots are reserved even if not implemented in v0.

**Adoptable bits** (lifted from the reference project's design):

- `memory_hash TEXT UNIQUE` column (SHA-256 of whitespace-normalized content) — dedupe before insert. Cheap and saves a class of duplicates.
- `status_commentary BOOLEAN NOT NULL DEFAULT FALSE` column — heuristic flag for "checking…/running…" assistant turns that should not become memories. Lift the classifier from `memory_engine/status_commentary.py`.
- `memory_text` per-chunk + `importance SMALLINT` scoring — useful for future weighted retrieval (the reference project uses a 0-10 scale).
- `source_type` (`chat`/`email`/`code`/`doc`/`system`) + `source_ref` columns — provenance for cross-app ingestion later.
- `reverie_summarized BOOLEAN` column (if/when we add reveries) — enables incremental summarization without losing unprocessed memories.

**Index choice** (HNSW vs IVFFlat): the reference project uses IVFFlat with `lists = 100`. Harness uses HNSW — better for incremental insert (no `lists` rebalance after every batch), better recall at <1M rows. HNSW build cost is acceptable on <100k rows. Switch to IVFFlat only if corpus >500k rows and write-amplification on HNSW becomes a problem.

**Final call:** write fresh. Lift `memory_hash`, `status_commentary`, `source_type`/`source_ref`, and the status-commentary classifier as imports (not as a fork).

This is the only decision worth deferring past the spec.

## 13. Configuration

### `.env` (chmod 600, gitignored)

```bash
# Flask
HARNESS_SECRET_KEY=        # python3 -c "import secrets; print(secrets.token_urlsafe(48))"
HARNESS_BIND=0.0.0.0:5010
HARNESS_TLS_CERT=<tls_dir>/cert.pem
HARNESS_TLS_KEY=<tls_dir>/key.pem
FLASK_ENV=production
PYTHONUNBUFFERED=1

# Postgres
DATABASE_URL=postgresql+psycopg2://harness_app:***@localhost:5432/harness
APP_DB_OPERATOR_ROLE=<operator_role>   # for migrations
APP_DB_RUNTIME_ROLE=harness_app     # for the app
HARNESS_DB=harness

# Agent
HARNESS_DEFAULT_AGENT=<agent_name>

# LLM (per-installation, NOT per-agent)
# LLM_ADAPTER picks which adapter module is loaded (see §8.3).
# The model name, base URL, and tool-call format live INSIDE the adapter.
LLM_ADAPTER=minimax_m3              # one of: minimax_m3, kimi_k3, glm_5_3_flash, open_weight
LLM_API_KEY=                        # Adapter-specific; env var read by the adapter, not the loop
LLM_MAX_TOKENS=2048                 # Response cap; adapter-enforced
LLM_TIMEOUT_S=60                    # Wallclock cap on a single complete() call
LLM_TEMPERATURE=0.7                 # Default; some adapters may clamp

# Embeddings (shared GPU server)
EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
EMBEDDING_DIMENSIONS=1024
EMBEDDING_API_KEY=                  # optional; LAN may be unauthenticated
EMBEDDING_TIMEOUT_S=10

# Memory tuning
MEMORY_TOP_K=10
MEMORY_VECTOR_LIMIT=30
MEMORY_TRIGRAM_LIMIT=30
MEMORY_RECENCY_DECAY_DAYS=30
MEMORY_CHUNK_MAX_TOKENS=800
MEMORY_CHUNK_MIN_TOKENS=20
MEMORY_CHUNK_OVERLAP_TOKENS=50

# Permanent memory (injected every prompt)
MEMORY_PERMANENT_MAX_TOKENS=10000        # total budget for permanent chunks; warn if exceeded
MEMORY_PERMANENT_WARN_OVERFLOW=true      # inject <truncation_warning> when cap is hit
MEMORY_PERMANENT_PIN_AUDIT=true          # every pin/unpin -> memory_audit

# Workspace bootstrap files (loaded at startup; per-file caps)
BOOTSTRAP_SOUL_MAX_BYTES=8000
BOOTSTRAP_USER_MAX_BYTES=4000
BOOTSTRAP_AGENTS_MAX_BYTES=12000
BOOTSTRAP_IDENTITY_MAX_BYTES=4000
BOOTSTRAP_MEMORY_MAX_BYTES=12000
BOOTSTRAP_OVERFLOW=fail           # 'fail' = refuse to start; 'truncate' = log + continue (NOT recommended)

# Rate limiting (Flask-Limiter)
RATE_LIMIT_DEFAULT=100/minute
RATE_LIMIT_CHAT=30/minute
```

### `config.yaml` (committed)

```yaml
app:
  name: harness
  bind: ${HARNESS_BIND}
  tls:
    cert: ${HARNESS_TLS_CERT}
    key:  ${HARNESS_TLS_KEY}
  default_agent: ${HARNESS_DEFAULT_AGENT}

db:
  url: ${DATABASE_URL}
  operator_role: ${APP_DB_OPERATOR_ROLE}
  runtime_role:  ${APP_DB_RUNTIME_ROLE}
  database: ${HARNESS_DB}

llm:
  adapter: ${LLM_ADAPTER}            # selects app/agent/llm/<adapter>.py
  api_key: ${LLM_API_KEY}            # read by the adapter; loop never sees it
  max_tokens: ${LLM_MAX_TOKENS}
  timeout_s: ${LLM_TIMEOUT_S}
  temperature: ${LLM_TEMPERATURE}

embedding:
  base_url: ${EMBEDDING_BASE_URL}
  model: ${EMBEDDING_MODEL}
  dimensions: ${EMBEDDING_DIMENSIONS}
  api_key: ${EMBEDDING_API_KEY}
  timeout_s: ${EMBEDDING_TIMEOUT_S}

memory:
  top_k: ${MEMORY_TOP_K}
  vector_limit: ${MEMORY_VECTOR_LIMIT}
  trigram_limit: ${MEMORY_TRIGRAM_LIMIT}
  recency_decay_days: ${MEMORY_RECENCY_DECAY_DAYS}
  chunk:
    max_tokens: ${MEMORY_CHUNK_MAX_TOKENS}
    min_tokens: ${MEMORY_CHUNK_MIN_TOKENS}
    overlap_tokens: ${MEMORY_CHUNK_OVERLAP_TOKENS}

security:
  headers:
    x_content_type_options: nosniff
    x_frame_options: DENY
    strict_transport_security: max-age=15552000; includeSubDomains
    referrer_policy: strict-origin-when-cross-origin
  csp: "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
  rate_limit:
    default: ${RATE_LIMIT_DEFAULT}
    chat: ${RATE_LIMIT_CHAT}
```

## 14. Deployment

### 13.1 Unix user

```bash
# Create the operator Unix user (mirrors the customer-onboarding pattern)
sudo useradd --system --create-home --shell /bin/bash \
    --groups systemd-journal <operator_user>

# Set up systemd --user linger
sudo loginctl enable-linger <operator_user>
mkdir -p ~<operator_user>/.config/systemd/user
chown -R <operator_user>:<operator_user> ~<operator_user>/.config

# SSH for git push (per-repo deploy key)
sudo -u <operator_user> bash -c '
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    # Deploy key installed via setup_github_deploy_key.sh
'

# PG peer-auth operator role
sudo -u postgres psql -c "
    CREATE ROLE <operator_role> LOGIN;
    ALTER ROLE <operator_role> INHERIT;
"
```

### 13.2 Postgres database

```bash
sudo -u postgres createdb <db_name>
sudo -u postgres psql -d <db_name> <<'SQL'
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS citext;

    -- The new DB has its own public; create the user table there
    -- (NOT shared with py_pg_accounts/public.users — harness is its own universe for v0)
    CREATE TABLE public.users (
        id            BIGSERIAL PRIMARY KEY,
        email         CITEXT UNIQUE NOT NULL,
        password_hash TEXT,
        display_name  TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member','viewer')),
        is_active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- Operator owns public
    ALTER SCHEMA public OWNER TO <operator_role>;

    -- Runtime role
    CREATE ROLE harness_app LOGIN PASSWORD '...';
    GRANT CONNECT ON DATABASE harness TO harness_app;
    GRANT USAGE ON SCHEMA public TO harness_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO harness_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO harness_app;
SQL

# Run per-agent schema installer
sudo -u <operator_user> bash -c '
    cd ~/<repo_name>
    PGPASSWORD=*** cat ~/.db_password) psql -h localhost -U <operator_role> -d <db_name> \
        -v agent_name=<agent_name> -f migrations/agents/<agent_name>_schema.sql
'
```

### 13.3 App

```bash
sudo -u <operator_user> bash -c '
    cd ~
    if [ ! -d <repo_name> ]; then
        git clone <repo_clone_url>
        cd <repo_name>
        git config user.name "<repo_name>"
        git config user.email "<operator_email>"
    fi
    cd <repo_name>
    cp .env.example .env
    # ...fill in secrets...
    chmod 600 .env
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip wheel
    pip install -r requirements.txt
    ./tools/migrate.sh
'

# TLS cert (SAN includes every routed hostname)
sudo -u <operator_user> bash -c '
    mkdir -p ~/tls
    openssl req -x509 -nodes -newkey rsa:2048 -days 730 \
        -subj "/CN=$(hostname)" \
        -addext "subjectAltName=IP:<host_ip>,DNS:<harness_hostname>" \
        -keyout ~/tls/key.pem -out ~/tls/cert.pem
    chmod 600 ~/tls/*.pem
'

# systemd --user
sudo -u <operator_user> bash -c '
    cp systemd/harness.service ~/.config/systemd/user/
    XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user daemon-reload
    XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user enable harness.service
    XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user start harness.service
'
```

### 13.4 Cloudflare Tunnel

**Decision:** dedicated `harness-tunnel`, `config_src: local`.

```bash
# Tunnel token already provisioned at setup
GH_TOKEN=... gh api .../cfd_tunnel/harness-tunnel # if not, create
```

Local config at `~/.cloudflared/config.yml`:

```yaml
tunnel: <HARNESS_TUNNEL_UUID>
credentials-file: ~/.cloudflared/<HARNESS_TUNNEL_UUID>.json

ingress:
  - hostname: <harness_hostname>
    service: http://127.0.0.1:5010
    originRequest:
      noTLSVerify: false
      connectTimeout: 10s
      keepAliveTimeout: 90s
  - service: http_status:404
```

DNS: `<harness_hostname>` CNAME → `<HARNESS_TUNNEL_UUID>.cfargotunnel.com` (proxied).

### 13.5 Smoke tests

```bash
# Health
CF_IP=$(dig +short <harness_hostname> @1.1.1.1 | head -1)
curl -skS -m 15 -i --resolve "<harness_hostname>:443:$CF_IP" \
  https://<harness_hostname>/health
# Expected: HTTP/2 200 {"status":"ok"}

# Chat
curl -skS -m 30 https://<harness_hostname>/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello. What is 2+2?"}'
# Expected: 200 {"assistant": "...", ...}

# Memory write + retrieve round-trip
curl -skS -m 30 https://<harness_hostname>/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "My favourite number is 42."}'
sleep 2
curl -skS -m 30 https://<harness_hostname>/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is my favourite number?"}'
# Expected: assistant references "42"
```

## 15. Security & guardrails

### 15.1 Threat model

Public chat surface → untrusted user input. The model may be jailbroken, may produce harmful output, may be tricked into calling tools in unintended ways. Threats:

- **Prompt injection** via user messages (and via retrieved memory content, since memory is also untrusted at the source).
- **Tool misuse**: model calls `memory_recall` with extreme parameters, or in a tight loop, to amplify cost or DoS the embedding service.
- **Cross-agent data leak**: a bug in routing or schema isolation lets one agent read another's memories.
- **SQL injection** through any unparameterized path into the database.
- **Egress exfiltration**: model output is sent somewhere we don't control (only the LLM provider, the embedding server, and Postgres on localhost should ever receive harness data).

### 15.2 MemoryAPI safety boundary

The MemoryAPI (§7.4) is the single ingress point for memory access. Defenses:

- **Schema isolation**: every query is namespaced by `agent_name`. The runtime DB role is also restricted to the agent schema. (Both enforced — defense in depth.)
- **No raw SQL escape hatch**: no `execute_raw()` or similar. New SQL goes through a PR.
- **Parametric queries only**: psycopg `%s` placeholders. Linted by `bandit` rule B608.
- **Rate limit per turn**: `MAX_SEARCHES_PER_TURN = 5`. Loop-enforced AND API-enforced.
- **Audit row per call**: every `search()` and `write_turns()` writes to `<agent>.memory_audit`. Query hash + result count + latency.
- **Tool surface = `search` only (v0)**: model can't write or delete memories. Writes are automatic and not tool-callable.

### 15.3 Prompt-injection defence

1. **No exec / file / network tools** beyond `memory_recall`. Even if a model is jailbroken, the blast radius is "send a different system prompt to the same LLM endpoint." Strong containment.
2. **System prompt anchored.** Per-adapter (e.g. for `minimax_m3`): *"You are {agent_name}, an AI assistant in harness. You cannot browse the web, read files, or execute code. If asked, say so and answer from memory or refuse."*
3. **Memory isolation per agent.** Cross-agent memory reads are physically impossible (different schemas, different roles).
4. **No telemetry / upload.** Outbound calls only to (a) the LLM provider, (b) the local GPU server, (c) Postgres on localhost. No other egress.
5. **Log redaction.** Strip API keys from logs; never log full conversation content unless `--debug-verbosity=2` (and then only to a tmpfs scratch). Memory audit rows store `query_hash` (SHA-256), not the query text.

### 15.4 Model-call hardening

- **`LLM_TIMEOUT_S=60` hard cap** to avoid a slow model holding a worker.
- **`LLM_MAX_TOKENS=2048` cap** to limit response size.
- **`MAX_TOOL_ITERATIONS=5`** outer cap on the tool-use loop (§9.2).
- **`max_concurrent=8` semaphore** around LLM calls so 100 simultaneous chats don't blow up the model quota.
- **Single shared `httpx.AsyncClient`** with HTTP/2 + connection pool of 16.

### 15.5 Adapter isolation

The adapter module is the *only* place that knows a model's wire format. If a model behaves unexpectedly, the fix lives in the adapter — not in the loop. This means a misbehaving model can't corrupt the agent loop, the database, or the prompt composition.

- Adapters are loaded by name (`LLM_ADAPTER`); an unknown adapter name fails fast at startup.
- The loop imports `BaseLLM` from `app.agent.llm.base`. It never imports any concrete adapter directly.
- Adapters may not import from `app.agent.loop`, `app.agent.memory`, or `app.routes.*`. (Enforced by code review; the dependency graph should be one-way: loop → adapter, never the reverse.)

### 15.6 Database hardening

- Runtime role has no `CREATE`/`DROP`/`ALTER`.
- Every user-supplied string goes through parameterized queries (`psycopg` `%s`).
- No `format()` / f-string SQL anywhere — enforced by lint rule (`sqlfluff` or `bandit`).
- `pg_dump` daily to Seagate Expansion (per standing pattern; see `postgres-external-backup` skill).

### 15.7 Rate limiting

`Flask-Limiter`, in-memory (single-process):

- `/api/v1/chat`: `30/minute` per IP
- `/api/v1/memories/*`: `60/minute` per IP
- All other: `100/minute` per IP

Per-turn tool-call cap: `MAX_TOOL_ITERATIONS = 5` (loop), `MAX_SEARCHES_PER_TURN = 5` (API). Both enforced.

### 15.8 Network

- Binds `0.0.0.0:5010` (all interfaces).
- LAN firewall: only accept ingress from the Cloudflare Tunnel source (`192.168.4.55`); allow LAN (`192.168.4.0/24`) for operator.
- Public internet: only via CF tunnel. Port 5010 not exposed on WAN.

### 15.9 Pinning risks (permanent memory)

The `memory_pin` tool gives the model a way to write to the every-prompt prompt block. This is a privilege. Mitigations:

- **Rate-limited**: `MAX_PINS_PER_TURN = 3` (loop) + per-conversation counter (API). A model cannot flood the permanent slot in one turn.
- **Total cap**: all permanent memory is bounded by `MEMORY_PERMANENT_MAX_TOKENS` (default 10,000). Even if the model pins 100 chunks, only the highest-importance ones fit; the rest are dropped.
- **Operator override**: operator can call `PUT /api/v1/memories/<id>/pin` with `{"permanent": false}` to demote any chunk regardless of `pin_source`.
- **Audit**: every pin/unpin writes to `memory_audit` with `op='pin' | 'unpin'`, `chunk_id`, and for model-driven pins, the model's stated `reason`. Operator can review the audit log for spam or abuse.
- **No deletion via tool**: pin/unpin only changes the `permanent` flag. Chunks remain in the database. Actual deletion is operator-only (and v0 doesn't expose a delete endpoint — only unpin).
- **Cap-overflow visibility**: when the cap is hit, a `<truncation_warning>` block is injected and `memory_audit` records `op_over_run=true`. Operator notices via audit query.

### 15.10 Bootstrap file integrity

Workspace .MD files are loaded at startup and re-read on every prompt (via the loader cache, but invalidated on file mtime change). Risks:

- **Operator-only write path**: the agent loop never writes bootstrap files. Only the operator (via filesystem) can change them.
- **Per-file caps make overflow loud**: see §7.7. No silent truncation.
- **Mtime-aware cache**: bootstrap files are re-read when their mtime changes. A `SOUL.md` edit by the operator takes effect on the next prompt without restarting the agent.

## 16. Open-source considerations

Repo is private MIT now; will go public after security sweep + prod sign-off. To make that cheap:

1. **No literal deployment-specific hostnames or domains in code or migrations.** Use `${HARNESS_DB}`, `${HARNESS_BIND}`, etc. — already done in `.env.example` and `config.yaml`.
2. **No customer-specific names** in committed code. Use `<your-org>`, `<agent>` etc. in `migrations/agents/<agent_name>_schema.sql` — the agent name should be a `-v` substitution variable, not a literal.
3. **No real auth secrets** — `.env.example` shows structure only.
4. **No operator paths** (operator-local script directories, infrastructure-validation row IDs) in the spec or code.
5. **Generic README tone** — already generic.
6. **Repo name neutral from commit 1** — `harness` ✓.

## 17. Out-of-scope / non-goals (v0)

- Telegram / multi-channel gateways
- Authentication / login flows
- Multi-tenant SaaS
- Cron / scheduling / heartbeat
- Code execution / file access
- Web search
- Voice / TTS / STT
- Image generation / understanding (v0.1)
- Mobile-native apps

If a customer asks for any of the above, point them at OpenClaw (if it gets fixed) or the reference stack. Harness is opinionated.

## 18. Phases & milestones

| Phase | Deliverable | Exit criterion |
|---|---|---|
| 0 (now) | Spec, repo, README, LICENSE | This doc merged |
| 1 | Unix user + DB + tunnel + DNS | `curl https://<harness_hostname>/health → 200` |
| 2 | Flask skeleton + /health + /api/v1/chat stub | `POST /api/v1/chat {"message":"hi"} → 200 {"assistant":"hi"}` |
| 3 | **BaseLLM + minimax_m3 adapter + tool framework** (recall + pin + unpin tools) | `LLM_ADAPTER=minimax_m3` loads; loop calls `complete()`; tool calls dispatch to registry; `memory_pin` round-trip works |
| 4 | **MemoryAPI + memory_recall + write path + permanent memories + workspace bootstrap loader** | Round-trip test: tell the agent X, ask about X, model calls `memory_recall`, answer references X. Pin test: pin Y, start new conv, ask about Y, model answers from permanent slot. Bootstrap test: start with over-cap SOUL.md, agent refuses to start with clear error |
| 5 | Agent prompt composition + multi-turn + per-agent schema install | Multi-turn chat with memory; `?agent=foo` routes to `foo` schema |
| 6 | Second adapter (`kimi_k3` or `glm_5_3_flash`) | Both `LLM_ADAPTER=kimi_k3` and `LLM_ADAPTER=glm_5_3_flash` work; adapter quality test passes for both |
| 7 | UI (chat page + sidebar) | Browser-renderable chat with conversation history |
| 8 | Security sweep | Rate limit, headers, log redaction, OWASP top-10 review, MemoryAPI audit table populated |
| 9 | Production sign-off + open-source readiness | Repo public-ready (LICENSE, README, no secrets) |

## 19. Open questions

### Resolved 2026-10-08

- ✅ **LLM adapter default** — `minimax_m3` for v0. (Per the project owner: "minimax m3 is my favourite model.")
- ✅ **Other adapters deferred** — kimi_k3, glm_5_3_flash added later as their own dedicated adapter modules. No template slot. (Per the project owner: "model support will be explicit by model.")
- ✅ **`memory_audit` retention** — keep indefinitely by design. No retention policy needed. (Per the project owner: "keep indefinitely by design.")
- ✅ **Auto-recall fallback** — no `LLM_AUTO_RECALL` env. Tool-only by design. If a model can't handle the tool, drop it. (Per the project owner: permanent memories are now a separate mechanism via `memory_pin` tool.)
- ✅ **Permanent memories** — added in v0.2 (this revision). `permanent BOOLEAN`, `memory_pin` + `memory_unpin` tools, `MEMORY_PERMANENT_MAX_TOKENS` cap (default 10,000), per-file caps on workspace bootstrap .MD files, hard fail on overflow.

### Still open before Phase 2

2. **Adapter quality bar — actual test**. Spec'd: must call `memory_recall` correctly, reason over returned chunks, AND handle `memory_pin`/`memory_unpin` correctly. Need to write this as a pytest + a small eval harness before Phase 4.
3. **Conversation history vs memory** — should the prompt include the most-recent 20 turns verbatim, or summarize? v0: verbatim (simple). v0.2: summarize older turns via LLM.
4. **Authentication** — defer to v0.2; the chat is open for now. Add a single-user gate (`HARNESS_GATE_PASSWORD`) before then.
5. **Memory summary cron** — the reference project has "reveries". v0: skip; consider later.
6. **`memory_hash` scope** — spec'd `UNIQUE (memory_hash, conversation_id)`. Per-conversation UNIQUE is the default.
7. **`source_type` for v0** — `chat` is the only source in v0. Column exists for future email/code/doc ingestion.

### New open questions from v0.2

8. **Bootstrap file location** — workspace files (SOUL.md etc.) live at `$WORKSPACE_ROOT`. For per-agent overrides, `<agent_name>/<file>` takes precedence. OK as-is? (No alternative proposed.)
9. **Bootstrap re-read behavior** — currently re-reads on mtime change (next prompt picks up operator edits). Alternative: only at startup. mtime-aware is more flexible; restart is more predictable. **Default: mtime-aware** with a 1-second stat cache.
10. **Pin reasons in audit** — every model-driven pin records the `reason` string. Should it be exposed to the operator UI? v0.2 dashboard concern. **Default: log only, no UI yet.**
11. **Pin spam protection** — `MAX_PINS_PER_TURN=3` is a soft cap. If the model pins 3 random chunks every turn, the permanent slot fills up. Audit query should make this visible. **No action beyond audit visibility for v0.**
12. **Per-agent bootstrap overrides** — spec'd `<agent_name>/<file>` precedence. When does the v0.1 implementation ship? Folds into Phase 5 (multi-agent).

### Deferred (not blocking)

- Cross-agent memory search: v0 has no `cross_agent_read`. Future: per-pair grant.
- Self-hosted open-weight adapters: out of v0 scope entirely.

## Related

- An earlier MIT memory-in-postgres project — design inspiration. Not forked.
- A sibling accounts service — design pattern (Flask + peer-auth PG + rate-limit). OSS.
- A sibling mail service — design pattern (systemd --user + CF tunnel + customer boilerplate).
- MEMORY.md "CF tunnels — `config_src: cloudflare` means local file edits DO NOT propagate" — applies for the new harness tunnel: use `config_src: local`.
- MEMORY.md "pgvector halfvec dim ceiling" — use `halfvec(1024)`, never native 2560-dim.
- MEMORY.md "Model quirks — extended to minimax/MiniMax-M3" — if MiniMax-M3 is used as agent, the `notifyOnExit` flags are NOT relevant (harness is a pure chat app, no exec).
- `customer-webapp-boilerplate` skill — same deployment pattern.