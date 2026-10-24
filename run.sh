#!/bin/bash
# gunicorn entrypoint for the harness app.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

[ -d .venv ] && source .venv/bin/activate

# --- log to BOTH the journal and a readable file (2026-10-21) ---
# gunicorn's --access/--error-logfile are "-" (stdout/stderr), which systemd
# sends to the journal. That is unreadable by the operator's own automation,
# so a chat failure could only be diagnosed by hand. `exec > >(...)` keeps
# gunicorn as the main process with its own PID and exit status -- a plain
# `| tee` would make the pipeline exit status tee's and silently defeat
# Restart=on-failure.
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/harness.log") 2>&1

# Worker timeout raised 600 -> default 1800 on 2026-10-21 (per Mal).
# A real Evie payroll turn reached iteration 12 with a 17k-char thinking
# block and had not finished at 7 minutes; 600s was too tight for
# legitimate long agentic runs. NOTE: gunicorn kills the WORKER and the
# master respawns it, so systemd NRestarts does NOT move. Do not use
# NRestarts as a health signal for this service -- check the worker PID.
# (Comments must stay ABOVE this line: inside a backslash continuation a
#  # line terminates the command and gunicorn loses its app argument.)
exec gunicorn \
    --workers 1 \
    --threads 4 \
    --bind "${HARNESS_BIND:-0.0.0.0:5010}" \
    --access-logfile - \
    --error-logfile - \
    --timeout "${GUNICORN_TIMEOUT:-1800}" \
    "app:create_app()"
