#!/bin/bash
# install_reverie_timer.sh - (re)install the nightly reverie timer.
#
# The schedule comes from config.yaml (`reverie.at` + `reverie.timezone`),
# so the config really is the source of truth: change the config, re-run
# this script, and the timer follows.
#
# Installs (as the peristyle user):
#   ~/.config/systemd/user/harness-reverie.service
#   ~/.config/systemd/user/harness-reverie.timer
#
# Usage:
#   install_reverie_timer.sh            # install + enable + start the timer
#   install_reverie_timer.sh --no-start # install + enable, don't start now
#   install_reverie_timer.sh --print    # print the generated files, install nothing
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HARNESS_DIR"

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

UNIT_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="$UNIT_DIR/harness-reverie.service"
TIMER_FILE="$UNIT_DIR/harness-reverie.timer"

PRINT_ONLY=""
DO_START="yes"
for arg in "$@"; do
    case "$arg" in
        --no-start) DO_START="no" ;;
        --print)    PRINT_ONLY="yes" ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

# --- read the config ------------------------------------------------------
read -r REVERIE_AT REVERIE_TZ REVERIE_ENABLED <<EOF
$(python3 - <<'PY'
import os, yaml
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"), override=False)

def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else v

raw = yaml.safe_load(open("config.yaml").read())
rv = (raw.get("reverie") or {})

def resolve(v):
    if not isinstance(v, str):
        return v
    out = v
    while "${" in out:
        start = out.index("${")
        end = out.index("}", start)
        out = out[:start] + env(out[start + 2:end], "") + out[end + 1:]
    return out

at = resolve(str(rv.get("at", "03:00")))
tz = resolve(str(rv.get("timezone", "Australia/Brisbane")))
enabled = resolve(str(rv.get("enabled", "false"))).strip().lower() in ("1", "true", "yes", "on")
print(f"{at} {tz} {enabled}")
PY
)
EOF

if [ -z "$REVERIE_AT" ]; then
    echo "could not read reverie.at from config.yaml" >&2
    exit 1
fi
HH="${REVERIE_AT%%:*}"
MM="${REVERIE_AT##*:}"

# --- build the units ------------------------------------------------------
SERVICE_CONTENT="[Unit]
Description=harness nightly reverie (real agent turn: context cleanup + reflection)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${HARNESS_DIR}
ExecStart=${HARNESS_DIR}/scripts/trigger_reverie.sh
# HARNESS_INTERNAL_TOKEN (and friends) come from the harness .env.
EnvironmentFile=-${HARNESS_DIR}/.env
# A reverie turn is a full agent turn - it can take minutes.
TimeoutStartSec=3600

[Install]
WantedBy=default.target
"

TIMER_CONTENT="[Unit]
Description=harness nightly reverie schedule (from config.yaml: reverie.at)
Requires=harness-reverie.service

[Timer]
OnCalendar=*-*-* ${HH}:${MM}:00 ${REVERIE_TZ}
Persistent=true
Unit=harness-reverie.service

[Install]
WantedBy=timers.target
"

if [ -n "$PRINT_ONLY" ]; then
    echo "===== harness-reverie.service ====="
    echo "$SERVICE_CONTENT"
    echo "===== harness-reverie.timer ====="
    echo "$TIMER_CONTENT"
    exit 0
fi

mkdir -p "$UNIT_DIR"
printf '%s' "$SERVICE_CONTENT" > "$SERVICE_FILE"
printf '%s' "$TIMER_CONTENT" > "$TIMER_FILE"
chmod 644 "$SERVICE_FILE" "$TIMER_FILE"

systemctl --user daemon-reload
systemctl --user enable --now harness-reverie.timer 2>/dev/null || \
    systemctl --user enable harness-reverie.timer

echo "installed:"
echo "  $SERVICE_FILE"
echo "  $TIMER_FILE"
echo "  schedule: daily at ${REVERIE_AT} ${REVERIE_TZ}"
echo "  enabled in config: ${REVERIE_ENABLED}"
systemctl --user list-timers harness-reverie.timer --no-pager 2>/dev/null || true
