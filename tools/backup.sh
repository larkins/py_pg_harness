#!/bin/bash
# Daily pg_dump of the harness database.
# Run as the operator via a systemd --user timer.
#
# Usage: tools/backup.sh [/path/to/backup/dir]
#   default dir: $HARNESS_BACKUP_DIR, else $HOME/harness-backups
#
# Environment:
#   DATABASE_URL           the database to dump (required)
#   APP_DB_OPERATOR_ROLE   role to connect as (default: harness_operator)
#   HARNESS_DB             name used in the dump filename (default: harness)
#   HARNESS_BACKUP_DIR     default output directory
#                          (default: $HOME/harness-backups)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

: "${DATABASE_URL:?DATABASE_URL not set}"
: "${APP_DB_OPERATOR_ROLE:=harness_operator}"

# Parse DB host/port/name from URL.
HOST=$(echo "$DATABASE_URL" | sed -E 's|^postgresql?://[^@]+@||; s|/.*$||')
PORT=$(echo "$HOST" | sed -E 's|^.*:||')
HOST=$(echo "$HOST" | sed -E 's|:.*$||')
DB=$(echo "$DATABASE_URL" | sed -E 's|^.*@.*/||; s|\?.*$||')

# The database to dump: HARNESS_DB, else the database named in the URL.
: "${HARNESS_DB:=$DB}"
: "${HARNESS_BACKUP_DIR:=${HOME}/harness-backups}"

# The password in the URL (empty when the URL carries no password).
PGPASSWORD=""
if echo "$DATABASE_URL" | grep -qE '^postgresql?://[^:/@]+:[^@]*@'; then
    PGPASSWORD=$(echo "$DATABASE_URL" | sed -E 's|^postgresql?://[^:]+:||; s|@.*$||')
fi

BACKUP_DIR="${1:-$HARNESS_BACKUP_DIR}"
mkdir -p "$BACKUP_DIR"

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/${HARNESS_DB}-${TS}.sql.gz"

echo "[$(date -Iseconds)] backup start: $HARNESS_DB -> $OUT"
PGPASSWORD="$PGPASSWORD" \
    pg_dump -h "$HOST" -p "${PORT:-5432}" -U "$APP_DB_OPERATOR_ROLE" -d "$HARNESS_DB" \
        --no-owner | gzip > "$OUT"

sha256sum "$OUT" > "$OUT.sha256"
echo "[$(date -Iseconds)] backup done: $OUT ($(stat -c %s "$OUT") bytes)"

# Retention: keep 14 days.
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +14 -delete 2>/dev/null || true
echo "[$(date -Iseconds)] retention sweep done"
