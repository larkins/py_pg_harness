# Future plan: externalize turn trajectories to per-conversation files

**Status:** design spec. Not implemented. Triggers documented below.

## Why this file exists

Migration 011 (`migrations/011_turns_trajectory.sql`, 2026-10-19) added a `trajectory JSONB` column to `<schema>.turns` in all three agent schemas. The trajectory captures the full per-iteration trace (assistant text + tool calls + tool results + nudge flags) for each assistant turn, which is what we need for LLM retraining pipelines that teach tool-use + reasoning patterns.

**Today** the trajectory lives in PostgreSQL JSONB, in-line for small ones and TOAST-compressed + out-of-line for ones >2 KB (the default Postgres toast threshold). That works fine for the current volume (a few thousand turns across the installed agents; only a handful of these have a non-NULL trajectory as of 2026-10-19).

**If trajectory storage becomes a problem**, this document is the recipe for switching to per-conversation JSONL files in `pie/<agent>/trajectories/`, with the turns table storing only the filename.

## Triggers — when to execute this plan

Any of:

- **DB bloat**: `<db_name>.turns.toast` size exceeds, say, 5 GB (the trajectory column is the only thing that gets toasted; if it dominates, externalize).
- **Backup time**: `pg_dump` of `<db_name>` exceeds, say, 30 minutes (the trajectory column is by far the biggest contributor).
- **Per-conversation size**: any single `turns.trajectory` JSONB exceeds, say, 5 MB (the current model rarely produces trajectories > 50 KB; 5 MB means a runaway iteration or a model that learned to dump tool outputs back into prose).
- **Per-conversation total**: any conversation's `sum(length(trajectory::text))` exceeds, say, 100 MB (similar reasoning; the max_iters cap is 40 today, so 2.5 MB per iter × 40 = 100 MB).

## Design

### Storage layout

```
pie/
  <agent_name>/
    trajectories/
      <conversation_id>/
        <turn_id>.jsonl       # one line per iteration
        meta.json              # {turn_id: {iter_count, model, latency_ms, ...}}
  ...                    # one directory per installed agent
```

- Files are untracked (each `pie/<agent>/` is a gitignored self-contained git repo; trajectory files are added to the agent's `.gitignore`).
- Per-turn files because most retraining pipelines want to pull a specific conversation; per-conversation indexing (meta.json) keeps the directory shallow and the inventory small.
- `.jsonl` because each iteration is independent and the LLM retraining loader can stream it line-by-line without re-parsing a big JSONB.

### Schema change

Drop the in-DB trajectory column. Replace with a filename pointer:

```sql
-- Migration 012 (FUTURE): externalize turn trajectories
ALTER TABLE <schema>.turns DROP COLUMN trajectory;
ALTER TABLE <schema>.turns ADD COLUMN trajectory_path TEXT NULL;
-- trajectory_path is the workspace-relative path within pie/<agent>/trajectories/,
-- e.g. '0236e00c-f3c0-4b82-a76e-6149e18cd5c2/822.jsonl'
```

- `trajectory_path NULL` for legacy rows written before the migration (the per-conversation file didn't exist for them; the trajectory is genuinely lost unless re-collected from `last_emission` snapshot logs).
- `trajectory_path NOT NULL` for all new turns.

### Application changes

`app/agent/memory.py`:

- `_insert_turn`: take `trajectory` (dict) OR `trajectory_path` (str). If both given, prefer the path (caller already externalized). If just trajectory, write the JSONL file at write time and store the path.
- `load_conversation`: when `turn.trajectory_path` is set, read the file and parse the JSONL into the same `iterations` shape. Cache in-memory (LRU) for the duration of a single request so a 100-iteration conversation doesn't re-read the file 100 times.

`app/routes/api.py`:

- Export endpoint: same shape — `format=thin` reads `turn.content` only; `format=full` reads the trajectory file when `trajectory_path` is set.
- New endpoint: `GET /api/v1/entities/<name>/trajectories/<turn_id>` (raw JSONL stream of the trajectory file). This is for retraining pipelines that want to pull a single turn's data without joining through `load_conversation`.

`app/agent/loop.py`:

- The `trajectory` dict in the loop is unchanged. The persistence layer decides whether to inline it in JSONB (today) or externalize to a file (future).
- The 50-turn prompt-context cap might need a tweak: today the loop reads the last 50 turns' content. If most of those turns have externalized trajectories, the loop should NOT load the trajectory file unless it's needed for the prompt (which it isn't — only `content` matters for the model context).

### File lifecycle

- **Write**: at the end of `handle_chat`, when the assistant turn is persisted. The trajectory file is written to disk first, then the turn row is INSERTed with the path. If the file write fails, the turn is still written with `trajectory_path = NULL` (lose the trajectory, keep the content) — the alternative is a failed chat.
- **Read**: on demand. Cache the parsed trajectory in a per-request LRU.
- **Delete**: when the conversation is purged (currently we don't purge; if we add a purge job, it should also `rm -rf pie/<agent>/trajectories/<conv_id>/`).
- **Archive**: when the conversation is archived (the 2026-10-19 `entity_reset_conversation` archive flow), the trajectory files stay in place. They're still reachable via the LLM retraining export.

### Backup implications

- `pg_dump` of the harness DB no longer carries the trajectory data — backups are smaller and faster. Good.
- `pie/<agent>/trajectories/` now contains the actual retraining data. It needs to be backed up separately, on a different cadence if appropriate.
- The per-agent pie repos are gitignored but tracked in their own git repos. The trajectory files would be `.gitignore`d (too large for git) and backed up by a separate mechanism — probably the existing external-Postgres-backup procedure extended to also tar the trajectory directories.
- **The split is the risk**: a DB-only backup without the trajectory tarball means the retraining data is incomplete. Mitigation: a single `tools/backup_with_trajectories.sh` that does `pg_dump` + `tar czf` of `pie/*/trajectories/` and writes a single SHA256SUMS. The LLM retraining export would then look for the matching pair.

### Migration steps (when triggered)

1. **Pre-flight**:
   - Verify the trigger condition (one of the four above).
   - Take a fresh harness DB backup.
   - Verify all 3 agent pies are at HEAD (no dirty working trees) so the gitignore + file-write paths are stable.
2. **Add the file-store scaffolding**:
   - For each agent, `mkdir -p pie/<agent>/trajectories/`.
   - Add `pie/<agent>/trajectories/` to each agent's `.gitignore`.
3. **Backfill** (if any existing rows have non-NULL trajectory):
   - For each turn with a non-NULL trajectory, write the file: `pie/<agent>/trajectories/<conv_id>/<turn_id>.jsonl` + update the row to set `trajectory_path` and `trajectory = NULL`.
   - One-shot script: `tools/externalize_trajectories.py` (no user interaction, idempotent).
4. **Schema migration** (Migration 012):
   - `ALTER TABLE <schema>.turns ADD COLUMN trajectory_path TEXT NULL;`
   - `ALTER TABLE <schema>.turns DROP COLUMN trajectory;` (after the backfill is verified)
5. **Code changes** (loop + memory + api):
   - Per the application-changes section above.
6. **Verify**:
   - `pg_dump` size should drop noticeably (the trajectory data is the bulk of the toast).
   - `chat` endpoint smoke test: trajectory still flows through to the response.
   - `export.jsonl?format=full` smoke test: trajectory still in output.
   - `chat` with a multi-iteration turn: trajectory file written, turn row references it.
7. **Backup script**:
   - Update `tools/backup.sh` to also `tar czf pie/*/trajectories/` alongside the pg_dump.
   - Add a restore counterpart that unpacks the trajectory tar into the right pie dirs.

### Estimated effort

- Code: 4–6 hours (memory + loop + api changes, plus the backfill script).
- Migration: 1–2 hours (depends on how many existing rows have non-NULL trajectory).
- Testing: 1–2 hours.
- Backup changes: 1 hour.

Total: ~8 hours. Trivial compared to the value of the retraining data.

### When NOT to do this

- If trajectory volume stays small (< 1 GB total across all agents) — keep it in JSONB, the operational simplicity is worth it.
- If the model architecture changes such that trajectories are no longer needed for retraining (e.g. switching to a non-tool-use model) — just drop the column entirely.
- If the per-conversation file directory becomes too large to enumerate (e.g. 10k+ conversations per agent) — switch to a per-month partitioning scheme (trajectories/2026-10/<conv_id>/) to keep directory listings bounded.

## Open questions

- **Compression**: gzipped vs raw JSONL for the trajectory files? Gzip cuts size ~3-5x but adds CPU on the read path. Probably worth it for the retraining use case (one-time read, not hot path).
- **Schema versioning**: should the JSONL have a `{"version": 1, "iterations": [...]}` envelope so we can change the format later? Yes, defensive.
- **Cross-agent sharing**: if a model is shared across agents (currently not, but possible), should trajectories be per-model or per-agent? Per-agent is fine; the model field is in the trajectory.

## Reference IDs

- `migrations/011_turns_trajectory.sql` (in-DB trajectory column, 2026-10-19)
- `peristyle-ai/py_pg_harness@b512889` (the conversation archive fix that made retraining feasible)
- `app/agent/loop.py` line ~720 (trajectory build)
- `app/agent/memory.py` `_insert_turn` (persistence)
- `app/routes/api.py` `entity_conversation_export_jsonl` (LLM retraining export, format=thin|format=full)
- This file: `docs/FUTURE_TRAJECTORY_EXTERNALIZATION.md`
