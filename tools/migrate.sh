#!/bin/bash
# Apply the per-agent schemas and the public-schema migrations.
#
# Run as the operator role (peer auth, or a DATABASE_URL in .env).
#
# Environment:
#   DATABASE_URL           the database to migrate, e.g.
#                          postgresql://<role>:<pw>@<host>:5432/<db>  (required)
#   APP_DB_OPERATOR_ROLE   role that owns the schemas and the tables
#                          (default: harness_operator)
#   APP_DB_RUNTIME_ROLE    role the app connects as (default: harness_app)
#   HARNESS_AGENTS         comma-separated agent roster. When unset the
#                          `app.agents` list from config.yaml is used, and
#                          when that is empty too, HARNESS_DEFAULT_AGENT.
#   HARNESS_DEFAULT_AGENT  single-agent fallback (default: agent)
set -euo pipefail
cd "$(dirname "$0")/.."

# Load DATABASE_URL from .env if present
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

: "${DATABASE_URL:?DATABASE_URL not set (check .env)}"
: "${APP_DB_OPERATOR_ROLE:=harness_operator}"
: "${APP_DB_RUNTIME_ROLE:=harness_app}"
: "${HARNESS_DEFAULT_AGENT:=agent}"

# Parse host/port/db from URL.
# DATABASE_URL=postgresql://runtime_role:pw@localhost:5432/database
HOST=$(echo "$DATABASE_URL" | sed -E 's|^postgresql?://[^@]+@||; s|/.*$||')
PORT=$(echo "$HOST" | sed -E 's|^.*:||')
HOST=$(echo "$HOST" | sed -E 's|:.*$||')
DB=$(echo "$DATABASE_URL" | sed -E 's|^.*@.*/||; s|\?.*$||')

# The password in the URL (empty when the URL carries no password).
PGPASSWORD=""
if echo "$DATABASE_URL" | grep -qE '^postgresql?://[^:/@]+:[^@]*@'; then
    PGPASSWORD=$(echo "$DATABASE_URL" | sed -E 's|^postgresql?://[^:]+:||; s|@.*$||')
fi

echo "Migrating $DB on $HOST:$PORT as $APP_DB_OPERATOR_ROLE"

run_sql() {
    local file="$1"; shift
    echo "  -> $file"
    PGPASSWORD="$PGPASSWORD" \
        psql -h "$HOST" -p "${PORT:-5432}" -U "$APP_DB_OPERATOR_ROLE" -d "$DB" \
             -v ON_ERROR_STOP=1 "$@" -f "$file"
}

# The agent roster: HARNESS_AGENTS, else the app.agents list in
# config.yaml, else HARNESS_DEFAULT_AGENT.
agent_roster() {
    local out=""
    if [ -n "${HARNESS_AGENTS:-}" ]; then
        out=$(printf '%s\n' "$HARNESS_AGENTS" | tr ',' '\n' \
              | sed -E 's/[[:space:]]+//g' | grep -v '^$' || true)
    elif [ -f config.yaml ]; then
        out=$(awk '
            /^[[:space:]]*#/ { next }
            /^[[:space:]]*agents:[[:space:]]*$/ { inlist = 1; next }
            inlist && /^[[:space:]]*-[[:space:]]*/ {
                sub(/^[[:space:]]*-[[:space:]]*/, "")
                sub(/[[:space:]]*$/, "")
                if ($0 != "") print $0
                next
            }
            inlist { inlist = 0 }
        ' config.yaml || true)
    fi
    [ -n "$out" ] || out="$HARNESS_DEFAULT_AGENT"
    printf '%s\n' "$out"
}

AGENTS=$(agent_roster)
AGENT_SCHEMAS=$(printf '%s\n' "$AGENTS" | paste -sd, -)
echo "Agents: $AGENT_SCHEMAS"

# 1. The shared base tables first: the agent schemas FK to public.users
#    and the admin seed inserts into it, so it has to exist before them.
for f in migrations/*_canonical_users.sql; do
    [ -f "$f" ] || continue
    run_sql "$f" \
        -v "agent_schemas=$AGENT_SCHEMAS" \
        -v "operator_role=$APP_DB_OPERATOR_ROLE" \
        -v "runtime_role=$APP_DB_RUNTIME_ROLE"
done

# 2. The per-agent schemas: the public migrations below add columns to
#    the agent tables, so those tables have to exist.
for agent in $AGENTS; do
    run_sql migrations/agents/agent_schema.sql \
        -v "agent_name=$agent" \
        -v "operator_role=$APP_DB_OPERATOR_ROLE" \
        -v "runtime_role=$APP_DB_RUNTIME_ROLE"
done

# 3. The remaining public-schema migrations.
for f in migrations/*.sql; do
    [ -f "$f" ] || continue
    case "$f" in
        migrations/*_canonical_users.sql) continue ;;
    esac
    run_sql "$f" \
        -v "agent_schemas=$AGENT_SCHEMAS" \
        -v "operator_role=$APP_DB_OPERATOR_ROLE" \
        -v "runtime_role=$APP_DB_RUNTIME_ROLE"
done

echo "Migrations complete."
