---
name: create_timer
description: Schedule a recurring wake-up of a harness agent main session via systemd --user timer + service. Use when you need recurring automation that should run with full main-session tools (memory_recall, memory_pin, exec_bash, local-email, etc.) — monthly invoices, weekly reports, hourly checks, nightly reveries, etc. The systemd .service POSTs to the harness's /api/v1/internal/timer endpoint with the prompt; the harness dispatches a fresh chat turn to the active agent (the default agent). The conversation appears in the HMI under user_id=3 (the operator, the admin).
version: 2.0.0  # harness-native rewrite of the OpenClaw-era skill
metadata:
  harness:
    requires:
      env:
        - HARNESS_INTERNAL_TOKEN  # set in <harness_root>/.env
        - HARNESS_BIND             # default 0.0.0.0:5010
      bins:
        - systemctl                # systemd --user
        - curl                     # for the timer-fired POST
    primaryEnv: HARNESS_INTERNAL_TOKEN
  openclaw:
    emoji: "⏰"
    tags:
      - scheduling
      - systemd
      - timer
      - cron
      - main-session
      - harness
---

# create_timer — Systemd scheduling for harness agent main sessions

## Why this skill exists

The harness has no built-in cron / scheduler. The OpenClaw-era
`automations` / `cron` mechanism doesn't exist in the harness. If
you want a recurring task (monthly invoice, weekly report, nightly
reverie, etc.), the cleanest path is a `systemd --user` timer + service
pair that POSTs to the harness's `/api/v1/internal/timer` endpoint
when it fires.

The endpoint requires a shared secret in the `X-Harness-Token`
header (set via `HARNESS_INTERNAL_TOKEN` in the harness `.env`).
Since the systemd --user service runs as the harness service user, it can
read the .env directly.

## Requirements

- The harness is running (`harness.service` is active)
- `HARNESS_INTERNAL_TOKEN` is set in `<harness_root>/.env`
- The service user has linger enabled (so systemd --user survives
  logout):
  ```bash
  sudo loginctl enable-linger <service_user>
  ```

## Quick start

```bash
# Install a timer that fires daily at 9 AM Brisbane time
pie/<agent_name>/skills/create_timer/scripts/install_timer.sh \
  the sibling-invoice \
  --schedule "*-*-* 09:00:00 Australia/Brisbane" \
  --prompt "Generate this week's the sibling invoice using the
            book-keeper skill, then email it to customer@example.com
            with the subject 'the sibling Weekly Invoice — <week>'.
            Don't ask for confirmation — just do it."

# Verify the timer is scheduled
systemctl --user list-timers

# Test the service manually (without waiting for the timer)
systemctl --user start the sibling-invoice.service
journalctl --user -u the sibling-invoice.service -n 20
```

## install_timer.sh options

```
Usage: install_timer.sh <name> --schedule <OnCalendar-spec> --prompt <text> [options]
       install_timer.sh <name> --schedule <OnCalendar-spec> --prompt-file <path> [options]

  <name>          Service+timer base name (e.g. the sibling-invoice)
  --schedule      systemd OnCalendar spec, e.g. "*-*-7 09:00:00 Australia/Brisbane"
  --prompt        Prompt body text (or use --prompt-file)
  --prompt-file   Read prompt body from a UTF-8 file
  --agent         Agent id (default: agent; AGENT_ID env also honoured)
  --working-dir   WorkingDirectory for the service (default: <harness_root>)
  --no-enable     Do not enable+start the timer after install
  -h, --help      Show this help

Examples:
  install_timer.sh the sibling-invoice --schedule "*-*-7 09:00:00 Australia/Brisbane" --prompt "..."
  install_timer.sh enp-weekly-invoice  --schedule "Mon *-*-* 09:00:00 Australia/Brisbane" --prompt-file ./prompts/enp.txt
  install_timer.sh heartbeat           --schedule "*-*-* *:00:00" --prompt "Run the hourly health check across all services."

OnCalendar spec reference:
  "*-*-* 09:00:00"                    daily at 9 AM
  "Mon *-*-* 09:00:00"                 Mondays at 9 AM
  "*-*-1 09:00:00"                     first of every month at 9 AM
  "*-*-7 09:00:00"                     7th of every month at 9 AM
  "*-*-* *:00:00"                      every hour on the hour
  See man systemd.time(7) for full spec
```

## What install_timer.sh does

1. Creates `<harness_root>/pie/<agent>/systemd/<name>.service`
   and `<name>.timer` (committed to git; portable across hosts)
2. Copies them to `~/.config/systemd/user/<name>.{service,timer}`
3. `systemctl --user daemon-reload`
4. (unless --no-enable) `systemctl --user enable --now <name>.timer`

The prompt is stored in `<name>.prompt` next to the .service. The
.service's ExecStart calls wake_agent_main.sh with the prompt file
as arg $1.

## Files

- `scripts/install_timer.sh` — installer (see above)
- `scripts/wake_agent_main.sh` — the script that POSTs to the harness
- `references/systemd-units.md` — example output of install_timer.sh

## Caveats

- **Prompt size**: keep prompts under 8 KB. The harness's `Message.content`
  field has no hard cap, but very large prompts waste tokens. For
  long-running scripts, use `--prompt-file` with a file generated by
  another tool.
- **No rate limiting on the timer endpoint**: the harness's `/api/v1/internal/timer`
  is not rate-limited (it's a service-to-service endpoint). Don't set
  up a timer that fires more than every few minutes; you'll spam the
  LLM.
- **Auth is shared-secret**: the `HARNESS_INTERNAL_TOKEN` is read by
  any process that can read `<harness_root>/.env`. Keep the
  token out of the prompt, logs, and any other agent-visible context.
  Rotate by editing the .env and restarting `harness.service` + the
  affected timers.
- **Lingering**: the service user MUST have linger enabled
  (`sudo loginctl enable-linger <service_user>`) or systemd --user timers
  will stop firing when the user logs out.

## Comparison to the OpenClaw-era skill

| Aspect | OpenClaw (v1) | Harness (v2, this) |
|---|---|---|
| Scheduler | OpenClaw `automations` (subagent-only) | systemd --user timer (real cron) |
| Auth | OpenClaw session cookie | X-Harness-Token shared secret |
| Wake mechanism | `openclaw agent --session-key ...` | `curl POST /api/v1/internal/timer` |
| Main-session | Subagent (restricted exec) | True main session (full tool access) |
| Cross-host | Local only (cwd-dependent) | Runs as the harness service user |

The harness version is simpler: a curl + a systemd unit. No OpenClaw
runtime needed. The trade-off: this skill depends on the harness
being up (whereas OpenClaw's automations ran independently).
