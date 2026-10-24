# agent_template/ — canonical per-agent bootstrap template

> **Source of truth for "what an agent's `identity/` should contain."**
> Captures only the critical `should.md` elements — the must-have
> sections that have proven, across the operator sweeps, to make a
> harness agent work reliably on complex tasks.

## What's in here

This folder is a **template**, not a live config. The harness's per-agent
content lives in `pie/{{AGENT_NAME}}/identity/` (per-agent repos at
`<your-org>/{{AGENT_NAME}}`, gitignored from the harness repo). When you add a
new agent or sweep an existing one, render these templates into the agent's
`identity/` directory and fill in the per-agent details (name, role, host,
working agreements).

| File | What it is | Critical sections |
|---|---|---|
| `IDENTITY.md.template` | Who am I, what do I do | Name, Display name, Creature, Vibe, Emoji, Avatar; the "What I do" + "How I work" + "Origin" sections |
| `SOUL.md.template` | Personality + standing rules | Core Truths, Boundaries, Vibe, Continuity, Operational Rules (Stay In This Session, Schedule Don't Bank, Memory Is Your Path To Persistence) |
| `USER.md.template` | Who you're helping + their context | Identity, operating principles, business context, workspace context, what they care about |
| `AGENTS.md.template` | The workspace + how to work in it | Session startup, memory sources, role description (three-duties pattern), red lines, ask-vs-do, execution closure rule, promise closure rule, group chat etiquette |
| `HEARTBEAT.md.template` | Polling + recurring tasks | Default behaviour (no heartbeat / minimal), recurring-task pattern, promise closure |
| `MEMORY.md.template` | Long-term curated memory | Identity, role, runtime, working agreements, standing references, ask-vs-do |

## Placeholders

Every `{{PLACEHOLDER}}` marker below is substituted by the installer
(`tools/install.sh`) when it renders the templates into
`pie/{{AGENT_NAME}}/identity/`. They are **installer's placeholders** —
there is nothing per-deployment baked into the templates themselves.

| Placeholder | What it becomes | Example |
|---|---|---|
| `{{AGENT_NAME}}` | The agent's short name (also its Postgres schema) | `atlas` |
| `{{AGENT_DISPLAY_NAME}}` | The human-readable agent name | `Atlas` |
| `{{AGENT_EMOJI}}` | A single emoji for the agent | `🗺️` |
| `{{USER_NAME}}` | The operator's name | `Alex` |
| `{{USER_EMAIL}}` | The operator's email | `alex@example.com` |
| `{{INSTALL_DIR}}` | The harness install directory | `/srv/harness` |
| `{{DB_NAME}}` | The Postgres database name | `harness` |
| `{{OPERATOR_USER}}` | The Unix user + Postgres operator role | `harness` |
| `{{RUNTIME_ROLE}}` | The Postgres role the app connects as | `harness_app` |
| `{{EMBEDDING_MODEL}}` | The embedding model name | `Qwen/Qwen3-Embedding-4B` |
| `{{EMBEDDING_BASE_URL}}` | The embedding server base URL | `http://127.0.0.1:8080/v1` |

Those are the **only** placeholders `tools/render_identity.py` knows —
any other `{{NAME}}` marker is reported as an unknown placeholder and
fails the render, so keep new ones out of the templates unless you
extend the renderer's value map at the same time.

Placeholders the installer does not know are left verbatim so the
operator can fill them in by hand — the `{Bracketed}` notes in the
templates are instructions, not substitutions.

## Why a template, not a generator

The harness has a `BootstrapLoader` (`app/agent/bootstrap.py`) that
**reads** the per-agent identity files — it doesn't generate them. The
operator's job is to keep the files under the size caps in
`config.bootstrap.files` and content-correct for the agent's role. A
checked-in template (this folder) is the way to make that operator's
job consistent across agents.

## How to use this template

### For a new agent

1. Create the per-agent repo: `gh repo create <your-org>/{{AGENT_NAME}} --private`
2. Add the per-repo deploy key for the harness operator Unix user
3. Clone to `pie/{{AGENT_NAME}}/` on the harness host
4. Render this template's files into `pie/{{AGENT_NAME}}/identity/`, removing the `.template` suffix
5. Fill in the per-agent details (name, creature, role, host, working agreements)
6. Add `{{AGENT_NAME}}` to `app.agents` in `{{INSTALL_DIR}}/config.yaml`
7. Apply the DB schema (`migrations/agents/{{AGENT_NAME}}_schema.sql`)
8. Restart the harness

### For an existing agent (sweep)

The operator sweeps followed this pattern: render the template into
the agent's `identity/`, customize for the role, and commit. The
"OpenClaw-era" wording in older agents should be replaced with
harness language.

## What "critical should.md elements" means

After the first round of operator sweeps, the elements that
consistently made the agent more reliable on complex tasks were:

- **A clear "What I do" section in IDENTITY.md** — the agent needs to
  know its role without having to infer it from AGENTS.md.
- **Standing operational rules in SOUL.md** (Stay In This Session,
  Schedule Don't Bank, Memory Is Your Path To Persistence) — these
  three rules consistently shaped the agent toward the "use tools in
  the main session, schedule follow-ups, write things down" behaviour
  that makes complex tasks survivable.
- **The execution closure rule in AGENTS.md** — `DONE:` / `FAILED:`
  with evidence. Without this, the agent silently swallows failures
  and the operator has to chase them.
- **The promise closure rule in AGENTS.md** — every promise to follow
  up gets a `create_timer` wakeup. Without this, promises evaporate
  across context compaction.
- **The "red lines" + ask-vs-do split in AGENTS.md** — explicit red
  lines (no exfiltration, no destructive ops without asking) and an
  explicit ask-vs-do list for the agent's role. The agent that
  internalizes these doesn't have to re-derive them every turn.
- **A thin MEMORY.md (<25 KB) with a "Standing references" section** —
  the agent needs to know where the canonical state lives (creds
  files, infra tables, runbooks) without rediscovering them every
  session.

What we **deliberately did not** keep in the templates:

- OpenClaw-specific runtime references (the harness replaced the
  OpenClaw runtime; anything that says "openclaw memory search" or
  "~/.openclaw/..." is a sweep target, not a template element)
- Per-agent role-specific scripts (those live in
  `pie/{{AGENT_NAME}}/scripts/` or `pie/{{AGENT_NAME}}/skills/`, not in
  the bootstrap)
- Historical context that used to live in a per-agent MEMORY.md
  (archived to `pie/{{AGENT_NAME}}/docs/MEMORY_ARCHIVE_<date>.md`
  if needed, not in the hot file)

## Byte caps

The rendered files must stay under the harness's bootstrap caps
(`config.bootstrap.files`, enforced at boot — over-cap files are a
**boot failure**, not a truncation):

| File | Cap (bytes) |
|---|---|
| `SOUL.md` | 8,000 |
| `USER.md` | 4,000 |
| `AGENTS.md` | 24,000 |
| `IDENTITY.md` | 8,000 |

Leave headroom: the templates render at roughly 30-50% of these caps,
so there is room for the operator's per-agent detail.
