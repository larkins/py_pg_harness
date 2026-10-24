#!/bin/bash
# trigger_reverie.sh - fire the nightly reverie turn(s).
#
# Called by harness-reverie.service (the systemd --user timer generated
# by scripts/install_reverie_timer.sh from the `reverie.at` setting in
# config.yaml).
#
# The reverie is a REAL agent turn (not a heartbeat): the harness runs
# it with the agent's last 50 turns of history, asks it to clean up its
# own context budget down to the configured target percentages, and to
# finish with reflection + proposals for the user. The turn is captured
# in the turns table like any other turn.
#
# The harness does the work (activity check, brief, turn); this script
# only pokes it. It talks to 127.0.0.1 directly (no Cloudflare, no
# session cookie) and authenticates with HARNESS_INTERNAL_TOKEN.
#
# Usage:
#   trigger_reverie.sh [--agent <name>] [--force] [--preview]
#
#   --agent <name>  only this agent (default: every agent in the config)
#   --force         ignore the "no activity in the window" rule
#   --preview       print the brief + the fire/skip decision, run nothing
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HARNESS_DIR"

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

: "${HARNESS_INTERNAL_TOKEN:?HARNESS_INTERNAL_TOKEN not set (check .env)}"

BASE_URL="${HARNESS_BASE_URL:-http://127.0.0.1:${HARNESS_PORT:-5010}}"
LOG_TAG="harness-reverie"

log() {
    if [[ -n "${INVOCATION_ID:-}" ]] || [[ -t 1 ]]; then
        echo "[$(date -Iseconds)] $*" >&2
    else
        logger -t "$LOG_TAG" "$*"
    fi
}

AGENT=""
FORCE=""
PREVIEW=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)   AGENT="$2"; shift 2 ;;
        --force)   FORCE="true"; shift ;;
        --preview) PREVIEW="true"; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -n "$PREVIEW" ]; then
    URL="$BASE_URL/api/v1/internal/reverie/preview"
    [ -n "$AGENT" ] && URL="$URL?agent=$(printf '%s' "$AGENT" | sed 's/ /%20/g')"
    curl -sS --max-time 120 -H "X-Harness-Token: $HARNESS_INTERNAL_TOKEN" "$URL" \
        | python3 -m json.tool
    exit 0
fi

BODY="{}"
if [ -n "$AGENT" ]; then
    BODY=$(printf '{"agent": "%s"}' "$AGENT")
fi
if [ -n "$FORCE" ]; then
    BODY=$(printf '{"agent": "%s", "force": true}' "$AGENT")
fi

log "firing reverie: body=$BODY"
RESP=$(curl -sS --max-time 300 -w '\n%{http_code}' \
    -X POST "$BASE_URL/api/v1/internal/reverie" \
    -H "X-Harness-Token: $HARNESS_INTERNAL_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY")
CODE=$(printf '%s' "$RESP" | tail -n1)
PAYLOAD=$(printf '%s' "$RESP" | sed '$d')
log "response ($CODE): $PAYLOAD"

if [ "$CODE" != "202" ] && [ "$CODE" != "409" ]; then
    log "ERROR: reverie trigger returned $CODE"
    exit 1
fi
exit 0
