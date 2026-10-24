#!/bin/bash
# wake_agent_main.sh — Invoked by a systemd --user .service generated
# by install_timer.sh. Reads the prompt from $1, POSTs to the harness
# /api/v1/internal/timer endpoint with the X-Harness-Token header.
#
# Usage: wake_agent_main.sh <prompt-file> [agent]
#
# Args:
#   $1  Path to a UTF-8 file containing the prompt body (required)
#   $2  Agent id (default: agent, or AGENT_ID env)
#
# Env (auto-sourced from $WORKING_DIRECTORY/.env via the .service's
# EnvironmentFile= directive):
#   HARNESS_INTERNAL_TOKEN  required — auth header value
#   HARNESS_BIND            default 0.0.0.0:5010
#
# Logs to journalctl when run under systemd.

set -euo pipefail

LOG_TAG="create-timer"

log() {
    # When run under systemd, logger writes to the journal.
    # When run interactively, fall back to stderr.
    if [[ -n "${INVOCATION_ID:-}" ]] || [[ -t 1 ]]; then
        echo "[$(date -Iseconds)] $*" >&2
    else
        logger -t "$LOG_TAG" "$*"
    fi
}

err() {
    log "ERROR: $*"
    exit 1
}

# Args
PROMPT_FILE="${1:-}"
AGENT="${2:-${AGENT_ID:-agent}}"

if [[ -z "$PROMPT_FILE" || ! -r "$PROMPT_FILE" ]]; then
    err "prompt file missing or unreadable: $PROMPT_FILE"
fi

# Env (sourced from .env via the .service's EnvironmentFile)
: "${HARNESS_INTERNAL_TOKEN:?HARNESS_INTERNAL_TOKEN not set — check EnvironmentFile= in the .service unit}"
: "${HARNESS_BIND:=0.0.0.0:5010}"

# Extract port from HARNESS_BIND (format: host:port)
HARNESS_PORT="${HARNESS_BIND##*:}"
HARNESS_HOST="127.0.0.1"  # always local — even if bind is 0.0.0.0, we reach it via loopback
HARNESS_URL="http://${HARNESS_HOST}:${HARNESS_PORT}"

log "waking ${AGENT} via ${HARNESS_URL} (prompt=$(wc -c < "$PROMPT_FILE") bytes)"

# Read the prompt into a variable
PROMPT="$(cat "$PROMPT_FILE")"
if [[ -z "$PROMPT" ]]; then
    err "prompt file is empty: $PROMPT_FILE"
fi

# POST to the harness internal/timer endpoint. Use --data-binary with
# @- to read JSON from stdin (avoids shell-escaping the prompt).
# Build the JSON payload safely with Python (the prompt may contain
# newlines, quotes, backslashes).
PAYLOAD=$(python3 -c '
import json, sys
agent = sys.argv[1]
with open(sys.argv[2]) as f:
    msg = f.read()
print(json.dumps({"agent": agent, "message": msg}))
' "$AGENT" "$PROMPT_FILE")

# POST with curl. --max-time 30s in case the harness is down. The
# harness returns 202 immediately (the chat runs in a background
# thread), so this should normally complete in <1s.
HTTP_RESPONSE=$(mktemp)
HTTP_CODE=$(curl -sS \
    --max-time 30 \
    -o "$HTTP_RESPONSE" \
    -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-Harness-Token: ${HARNESS_INTERNAL_TOKEN}" \
    --data-binary "$PAYLOAD" \
    "${HARNESS_URL}/api/v1/internal/timer" 2>&1) || err "curl failed: $HTTP_RESPONSE"

case "$HTTP_CODE" in
    202)
        log "triggered: $(cat "$HTTP_RESPONSE")"
        rm -f "$HTTP_RESPONSE"
        exit 0
        ;;
    401)
        err "auth failed (401) — HARNESS_INTERNAL_TOKEN mismatch"
        ;;
    400)
        err "bad request (400): $(cat "$HTTP_RESPONSE")"
        ;;
    *)
        err "unexpected status $HTTP_CODE: $(cat "$HTTP_RESPONSE")"
        ;;
esac
