#!/bin/bash
# install_timer.sh — Generate a systemd --user timer + service pair
# that POSTs to the harness's /api/v1/internal/timer endpoint.
#
# Usage:
#   install_timer.sh <name> --schedule <OnCalendar-spec> --prompt <text> [options]
#   install_timer.sh <name> --schedule <OnCalendar-spec> --prompt-file <path> [options]
#
# Options:
#   --agent         Agent id (default: $AGENT_ID, else 'agent'; AGENT_ID env also honoured)
#   --working-dir   WorkingDirectory for the service (default: the harness root)
#   --no-enable     Do not enable+start the timer after install
#   -h, --help      Show this help
#
# What it does:
#   1. Creates pie/<agent>/systemd/<name>.{service,timer,prompt} (committed to git)
#   2. Copies them to ~/.config/systemd/user/<name>.{service,timer}
#   3. systemctl --user daemon-reload
#   4. (unless --no-enable) systemctl --user enable --now <name>.timer
#
# Examples:
#   install_timer.sh the sibling-invoice --schedule "*-*-7 09:00:00 Australia/Brisbane" --prompt "..."
#   install_timer.sh enp-weekly-invoice  --schedule "Mon *-*-* 09:00:00 Australia/Brisbane" --prompt-file ./prompts/enp.txt
#   install_timer.sh heartbeat           --schedule "*-*-* *:00:00" --prompt "Run the hourly health check."

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 <name> --schedule <OnCalendar-spec> --prompt <text> [options]
       $0 <name> --schedule <OnCalendar-spec> --prompt-file <path> [options]

  <name>          Service+timer base name (e.g. the sibling-invoice)
  --schedule      systemd OnCalendar spec, e.g. "*-*-7 09:00:00 Australia/Brisbane"
  --prompt        Prompt body text (or use --prompt-file)
  --prompt-file   Read prompt body from a UTF-8 file
  --agent         Agent id (default: $AGENT_ID, else 'agent')
  --working-dir   WorkingDirectory for the service (default: the harness root)
  --no-enable     Do not enable+start the timer after install
  -h, --help      Show this help

Examples:
  $0 the sibling-invoice --schedule "*-*-7 09:00:00 Australia/Brisbane" --prompt "..."
  $0 enp-weekly-invoice  --schedule "Mon *-*-* 09:00:00 Australia/Brisbane" --prompt-file ./prompts/enp.txt
EOF
    exit 2
}

# Defaults
name=""
schedule=""
prompt=""
prompt_file=""
agent="${AGENT_ID:-agent}"
# The harness working directory. Derived from this script's own location
# (skills/create_timer/scripts/ -> repo root) unless overridden.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "$_SCRIPT_DIR/../../../.." && pwd)"
working_dir="${PATHS_WORKING_DIR:-$_REPO_ROOT}"
no_enable=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --schedule)   schedule="$2"; shift 2 ;;
        --prompt)     prompt="$2"; shift 2 ;;
        --prompt-file) prompt_file="$2"; shift 2 ;;
        --agent)      agent="$2"; shift 2 ;;
        --working-dir) working_dir="$2"; shift 2 ;;
        --no-enable)  no_enable=true; shift ;;
        -h|--help)    usage ;;
        -*)           echo "Unknown option: $1" >&2; usage ;;
        *)
            if [[ -z "$name" ]]; then
                name="$1"
            else
                echo "Unexpected positional arg: $1" >&2; usage
            fi
            shift
            ;;
    esac
done

# Validate
if [[ -z "$name" ]]; then
    echo "Error: <name> is required" >&2
    usage
fi
if [[ -z "$schedule" ]]; then
    echo "Error: --schedule is required" >&2
    usage
fi
if [[ -z "$prompt" && -z "$prompt_file" ]]; then
    echo "Error: --prompt or --prompt-file is required" >&2
    usage
fi
if [[ -n "$prompt" && -n "$prompt_file" ]]; then
    echo "Error: --prompt and --prompt-file are mutually exclusive" >&2
    usage
fi

# Read prompt from file if --prompt-file
if [[ -n "$prompt_file" ]]; then
    if [[ ! -r "$prompt_file" ]]; then
        echo "Error: prompt file unreadable: $prompt_file" >&2
        exit 2
    fi
    prompt="$(cat "$prompt_file")"
fi

# Paths
SKILL_DIR="${PATHS_SKILL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WAKE_SCRIPT="$SKILL_DIR/scripts/wake_agent_main.sh"
GIT_SYSTEMD_DIR="${PATHS_GIT_SYSTEMD_DIR:-$working_dir/pie/$agent/systemd}"
RUNTIME_SYSTEMD_DIR="${PATHS_RUNTIME_SYSTEMD_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"

# Sanity checks
if [[ ! -x "$WAKE_SCRIPT" ]]; then
    echo "Error: wake script not found or not executable: $WAKE_SCRIPT" >&2
    exit 2
fi
if [[ ! -d "$working_dir" ]]; then
    echo "Error: working-dir does not exist: $working_dir" >&2
    exit 2
fi

# Create dirs
mkdir -p "$GIT_SYSTEMD_DIR" "$RUNTIME_SYSTEMD_DIR"

# Prompt file (git-tracked, lives next to the .service in pie/<agent>/systemd/)
PROMPT_FILE_GIT="$GIT_SYSTEMD_DIR/${name}.prompt"
PROMPT_FILE_RUNTIME="$RUNTIME_SYSTEMD_DIR/${name}.prompt"
printf "%s" "$prompt" > "$PROMPT_FILE_GIT"
cp "$PROMPT_FILE_GIT" "$PROMPT_FILE_RUNTIME"
chmod 644 "$PROMPT_FILE_GIT" "$PROMPT_FILE_RUNTIME"

# .service file
SERVICE_FILE_GIT="$GIT_SYSTEMD_DIR/${name}.service"
SERVICE_FILE_RUNTIME="$RUNTIME_SYSTEMD_DIR/${name}.service"
cat > "$SERVICE_FILE_GIT" <<EOF
# Generated by create_timer/install_timer.sh on $(date -Iseconds)
# Target: the harness /api/v1/internal/timer endpoint
# Agent: ${agent}
# Schedule: ${schedule}
# Edit + commit to update; run install_timer.sh to reinstall.
# Optional CA bundle for outbound HTTPS (e.g. a self-signed mail server).
# Only emitted when the operator points at a cert; unset => no CA override.
SSL_CERT_LINES=""
if [ -n "${PATHS_SSL_CERT:-}" ] && [ -f "${PATHS_SSL_CERT}" ]; then
    SSL_CERT_LINES="Environment=SSL_CERT_FILE=${PATHS_SSL_CERT}
Environment=REQUESTS_CA_BUNDLE=${PATHS_SSL_CERT}"
fi

[Unit]
Description=create_timer wake for ${name} (agent=${agent})
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${working_dir}
ExecStart=${WAKE_SCRIPT} ${PROMPT_FILE_RUNTIME} ${agent}
# Pull HARNESS_INTERNAL_TOKEN from the harness .env so the wake script
# can authenticate to the local harness. systemd --user services start
# with a sanitized environment; we need to source .env explicitly.
EnvironmentFile=-${working_dir}/.env
# Mail server is HTTPS with a self-signed cert. Expose the cert path so any
# script the timer spawns (process_accounts_email, reconcile_payments,
# generate_enp_invoice, etc.) can pick it up via SSL_CERT_FILE without
# having to set it per-call. Pinned on 2026-10-18 by the operator because the
# /etc/ssl/certs/<mail-server>.pem symlink that the operator used
# to inherit disappeared; we patch via Environment= here instead of
# touching /etc (no privilege).
${SSL_CERT_LINES}
EOF
cp "$SERVICE_FILE_GIT" "$SERVICE_FILE_RUNTIME"
chmod 644 "$SERVICE_FILE_GIT" "$SERVICE_FILE_RUNTIME"

# .timer file
TIMER_FILE_GIT="$GIT_SYSTEMD_DIR/${name}.timer"
TIMER_FILE_RUNTIME="$RUNTIME_SYSTEMD_DIR/${name}.timer"
cat > "$TIMER_FILE_GIT" <<EOF
# Generated by create_timer/install_timer.sh on $(date -Iseconds)
# Schedule: ${schedule}
[Unit]
Description=create_timer schedule for ${name}
Requires=${name}.service

[Timer]
OnCalendar=${schedule}
Persistent=true
Unit=${name}.service

[Install]
WantedBy=timers.target
EOF
cp "$TIMER_FILE_GIT" "$TIMER_FILE_RUNTIME"
chmod 644 "$TIMER_FILE_GIT" "$TIMER_FILE_RUNTIME"

echo "Created:"
echo "  ${SERVICE_FILE_GIT}"
echo "  ${TIMER_FILE_GIT}"
echo "  ${PROMPT_FILE_GIT}"
echo "  (also copied to ${RUNTIME_SYSTEMD_DIR}/)"

# Reload systemd
systemctl --user daemon-reload

# Enable + start (unless --no-enable)
if [[ "$no_enable" == "false" ]]; then
    systemctl --user enable --now "${name}.timer"
    echo ""
    echo "Timer enabled + started. Verify with:"
    echo "  systemctl --user list-timers | grep ${name}"
    echo "  systemctl --user status ${name}.timer"
    echo ""
    echo "Test the service manually (without waiting for the timer):"
    echo "  systemctl --user start ${name}.service"
    echo "  journalctl --user -u ${name}.service -n 20"
else
    echo ""
    echo "Skipped enable+start (--no-enable). To enable later:"
    echo "  systemctl --user enable --now ${name}.timer"
fi
