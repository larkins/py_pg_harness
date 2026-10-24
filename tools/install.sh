#!/usr/bin/env bash
# =====================================================================
# install.sh — non-interactive, idempotent harness installer
# =====================================================================
#   tools/install.sh --config install.config.yaml [--dry-run]
#
# No prompts, no TTY required. Re-running it is a no-op (or a clean
# re-apply). --dry-run prints the plan and changes nothing.
#
# The installer is the ONLY thing that creates the shared public.users
# table. Other projects (email / webmail / accounts / projects) verify it
# with db/preflight_users.sql and refuse to install without it.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG=""
DRY_RUN=0
ASSUME_YES=0

usage() {
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config)   CONFIG="$2"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        --yes|-y)   ASSUME_YES=1; shift ;;
        --help|-h)  usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done

[ -n "$CONFIG" ] || { echo "ERROR: --config <file> is required" >&2; usage 2; }
[ -f "$CONFIG" ] || { echo "ERROR: no such config file: $CONFIG" >&2; exit 2; }

# ---------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '   \033[31mx\033[0m %s\n' "$*" >&2; exit 1; }
run()  {  # run a command, or print it in dry-run mode
    if [ "$DRY_RUN" = "1" ]; then
        printf '   \033[90m[dry-run]\033[0m %s\n' "$*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------
# config
# ---------------------------------------------------------------------
command -v python3 >/dev/null || fail "python3 is required"
python3 - <<'PY' || fail "PyYAML is required (pip install pyyaml)"
import yaml  # noqa: F401
PY

eval "$(python3 "$SCRIPT_DIR/install_config.py" --config "$CONFIG" --shell)"
CFG_REPO_ROOT="$REPO_ROOT"

INSTALL_DIR="${install_install_dir:-}"
OPERATOR_USER="${install_operator_user:-}"
DB_NAME="${database_name:-}"
DB_HOST="${database_host:-localhost}"
DB_PORT="${database_port:-5432}"
DB_MODE="${database_mode:-fresh}"
OPERATOR_ROLE="${database_operator_role:-}"
OPERATOR_PASSWORD="${database_operator_password:-}"
RUNTIME_ROLE="${database_runtime_role:-}"
AGENT_NAME="${agent_name:-agent}"
AGENT_DISPLAY="${agent_display_name:-Agent}"
AGENT_EMOJI="${agent_emoji:-🌳}"
ADMIN_EMAIL="${first_admin_email:-}"
ADMIN_DISPLAY="${first_admin_display_name:-}"
ADMIN_ROLE="${first_admin_role:-admin}"
ADMIN_PASSWORD="${first_admin_password:-}"
REPO_URL="${install_repo:-}"

[ -n "$INSTALL_DIR" ] || fail "install.install_dir is not set"
[ -n "$DB_NAME" ] || fail "database.name is not set"
[ -n "$OPERATOR_ROLE" ] || fail "database.operator_role is not set"
[ -n "$RUNTIME_ROLE" ] || fail "database.runtime_role is not set"

# =====================================================================
step "1. preflight"
# =====================================================================
MISSING=""
for b in python3 git psql curl tar; do
    command -v "$b" >/dev/null || MISSING="$MISSING $b"
done
[ -z "$MISSING" ] || fail "missing required tool(s):$MISSING"
info "tools ok (python3, git, psql, curl)"

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'PY' || fail "python >= 3.11 is required"
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)
PY
info "python $PYV"

if ! id "$OPERATOR_USER" >/dev/null 2>&1; then
    if [ "$DRY_RUN" = "1" ]; then
        warn "the operator user '$OPERATOR_USER' does not exist on THIS host —"
        warn "  that is expected for a dry run of a plan aimed at another host"
    else
        fail "the operator user '$OPERATOR_USER' does not exist on this host"
    fi
fi
info "operator user: $OPERATOR_USER"

# The harness needs a postgres superuser to create roles and (in fresh mode)
# the database. Try the available ways to reach one, and REMEMBER which one
# works — a failed probe is not "the database is missing".
PG_SUPER_CMD=""
if sudo -n -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
    PG_SUPER_CMD="sudo -n -u postgres psql"
elif psql -U postgres -tAc 'SELECT 1' >/dev/null 2>&1; then
    PG_SUPER_CMD="psql -U postgres"
else
    warn "no postgres superuser access — the role-creation step will fail"
    warn "(grant the operator NOPASSWD for psql, or create the roles by hand)"
fi
[ -n "$PG_SUPER_CMD" ] && info "postgres superuser access: ok ($PG_SUPER_CMD)"

pg_super() {  # pg_super <db> <psql args...>   (superuser path only)
    local db="$1"; shift
    if [ -z "$PG_SUPER_CMD" ]; then
        fail "no postgres superuser access — use apply_sql (which falls back to the operator)"
    fi
    if [ "$DRY_RUN" = "1" ]; then
        printf '   \033[90m[dry-run]\033[0m %s -d %s %s\n' "$PG_SUPER_CMD" "$db" "$*"
        return 0
    fi
    $PG_SUPER_CMD -d "$db" "$@"
}

# The operator connection: the role that owns the database. Used when there
# is no superuser (and for the things a superuser is not needed for).
apply_sql() {  # apply_sql <db> <psql args...>
    local db="$1"; shift
    if [ -n "$PG_SUPER_CMD" ]; then
        pg_super "$db" "$@"
        return $?
    fi
    local cmd=(psql -h "$DB_HOST" -p "$DB_PORT" -U "$OPERATOR_ROLE" -d "$db")
    if [ "$DRY_RUN" = "1" ]; then
        printf '   \033[90m[dry-run]\033[0m %s %s\n' "${cmd[*]}" "$*"
        return 0
    fi
    PGPASSWORD="$OPERATOR_PASSWORD" "${cmd[@]}" "$@"
}

# The app refuses to start without a model API key (app/security.py). Catch
# it here, where the message can say what to do.
if [ -z "${LLM_API_KEY:-}" ] && [ -z "${LLM_API_KEY_OVERRIDE:-}" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        warn "llm.api_key is empty in the config — the app will refuse to start."
        warn "  set llm.api_key in the config, or export LLM_API_KEY."
    else
        fail "llm.api_key is empty — the harness refuses to start without a model API key.
       Set it in the config (llm.api_key) or export LLM_API_KEY before installing."
    fi
fi

BIND_PORT="${HARNESS_BIND##*:}"
if command -v ss >/dev/null && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${BIND_PORT}\$"; then
    warn "port ${BIND_PORT} is already in use — the service may fail to start"
else
    info "port ${BIND_PORT}: free"
fi

if [ "$DB_MODE" = "existing" ]; then
    if [ -z "$PG_SUPER_CMD" ]; then
        # No superuser: prove the database exists by connecting to it.
        if psql -h "$DB_HOST" -p "$DB_PORT" -U "$OPERATOR_ROLE" -d "$DB_NAME" \
               -tAc 'SELECT 1' >/dev/null 2>&1; then
            info "database '$DB_NAME' exists (mode=existing, connected as $OPERATOR_ROLE)"
        else
            fail "cannot connect to the database '$DB_NAME' as '$OPERATOR_ROLE' —
       check the host/port and the credentials in the config"
        fi
    else
        DB_ROWS="$(pg_super postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null || true)"
        if [ -z "$DB_ROWS" ] && ! pg_super postgres -tAc 'SELECT 1' >/dev/null 2>&1; then
            fail "could not query the database list — check the postgres access"
        fi
        if printf '%s' "$DB_ROWS" | grep -q 1; then
            info "database '$DB_NAME' exists (mode=existing)"
        else
            fail "database '$DB_NAME' does not exist but database.mode=existing — create it first"
        fi
    fi
fi

if [ -d "$INSTALL_DIR/.git" ]; then
    info "install dir already present: $INSTALL_DIR (will update)"
elif [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    info "install dir already populated: $INSTALL_DIR (not a git checkout — kept as-is)"
else
    info "install dir: $INSTALL_DIR (will clone)"
fi

# =====================================================================
step "2. repository"
# =====================================================================
if [ -d "$INSTALL_DIR/.git" ]; then
    run git -C "$INSTALL_DIR" pull --ff-only
elif [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    # The directory is already populated but is not a git checkout — e.g. the
    # tree was staged by hand or shipped as a tarball (an air-gapped install).
    # Cloning into it would fail; updating it is not possible either.
    warn "$INSTALL_DIR is already populated and is not a git checkout —"
    warn "  keeping it as it is (stage the tree by hand or clone it yourself)"
    info "  the files there are used as-is"
else
    run git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------
# the python environment
# ---------------------------------------------------------------------
VENV_PY="$INSTALL_DIR/.venv/bin/python3"

if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
    run python3 -m venv "$INSTALL_DIR/.venv"
    run "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
    run "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
    info "python venv created with the requirements installed"
else
    info "python venv present"
fi

# =====================================================================
step "3. postgres roles"
# =====================================================================
if [ -n "$PG_SUPER_CMD" ]; then
    run pg_super postgres -v ON_ERROR_STOP=1 \
        -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${OPERATOR_ROLE}') THEN CREATE ROLE ${OPERATOR_ROLE} LOGIN; END IF; END \$\$;"
    run pg_super postgres -v ON_ERROR_STOP=1 \
        -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${RUNTIME_ROLE}') THEN CREATE ROLE ${RUNTIME_ROLE} LOGIN; END IF; END \$\$;"
    info "roles: ${OPERATOR_ROLE} (operator), ${RUNTIME_ROLE} (runtime)"
else
    # No superuser: the roles must already exist. Say exactly what is missing
    # rather than failing with a psql error.
    for _role in "$OPERATOR_ROLE" "$RUNTIME_ROLE"; do
        # The catalog is queried through the TARGET database: a .pgpass entry
        # is scoped to one database, so connecting to `postgres` would not
        # find the password.
        if psql -h "$DB_HOST" -p "$DB_PORT" -U "$OPERATOR_ROLE" -d "$DB_NAME" -tAc \
               "SELECT 1 FROM pg_roles WHERE rolname='$_role'" 2>/dev/null | grep -q 1; then
            info "role $_role: already exists"
        else
            fail "the role '$_role' does not exist and this installer has no superuser
       access to create it — create it as root:
         sudo -u postgres psql -c \"CREATE ROLE $_role LOGIN PASSWORD '...';\""
        fi
    done
fi

# =====================================================================
step "4. database"
# =====================================================================
if [ "$DB_MODE" = "fresh" ]; then
    run pg_super postgres -v ON_ERROR_STOP=1 \
        -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='${DB_NAME}') THEN CREATE DATABASE ${DB_NAME} OWNER ${OPERATOR_ROLE}; END IF; END \$\$;"
    info "database ${DB_NAME} created (mode=fresh)"
else
    info "database ${DB_NAME} (mode=existing, left alone)"
fi

# ---------------------------------------------------------------------
# the extensions the harness needs
# ---------------------------------------------------------------------
# vector (pgvector) provides the halfvec type the memory table uses,
# pg_trgm the trigram index, citext the case-insensitive email on the
# shared users table. Creating an extension needs a superuser, so it
# happens here -- the installer is the privileged step.
# vector is NOT a trusted extension (it has C functions), so it needs a
# superuser; citext and pg_trgm ARE trusted, so the database owner may
# create them. Try the superuser first, then the operator.
ensure_extension() {
    local ext="$1"
    if apply_sql "$DB_NAME" -tAc "SELECT 1 FROM pg_extension WHERE extname='$ext'" 2>/dev/null | grep -q 1; then
        info "extension $ext: already installed"
        return 0
    fi
    if [ -n "$PG_SUPER_CMD" ]; then
        run pg_super "$DB_NAME" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS ${ext};"
        return 0
    fi
    info "no superuser access — creating the trusted extension $ext as the operator"
    # PGPASSWORD may be empty: libpq then falls back to ~/.pgpass
    if PGPASSWORD="$OPERATOR_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" \
            -U "$OPERATOR_ROLE" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
            -c "CREATE EXTENSION IF NOT EXISTS ${ext};" ; then
        return 0
    fi
    fail "the $ext extension is missing from '$DB_NAME' and could not be created —
       it needs either a superuser or a trusted-extension database owner"
}

ensure_extension vector
ensure_extension pg_trgm
ensure_extension citext

# =====================================================================
step "5. the canonical public.users"
# =====================================================================
# The harness OWNS this table. In 'existing' mode we verify FIRST and
# refuse to continue if the table is missing or incompatible — a silent
# second users table is the bug this whole design removes.
if [ "$DB_MODE" = "existing" ]; then
    # The harness owns the shared table: absent -> create it, present ->
    # verify it. (The preflight is the gate for the OTHER projects, which
    # must never create it.)
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] check whether public.users exists in ${DB_NAME}"
        info "[dry-run]   absent -> create it with the canonical DDL"
        info "[dry-run]   present -> verify it with db/preflight_users.sql"
    else
        if apply_sql "$DB_NAME" -tAc \
               "SELECT to_regclass('public.users') IS NOT NULL" 2>/dev/null | grep -q t; then
            if ! apply_sql "$DB_NAME" -v ON_ERROR_STOP=1 \
                    -f "$INSTALL_DIR/db/preflight_users.sql" >/tmp/harness_preflight.log 2>&1; then
                cat /tmp/harness_preflight.log >&2
                fail "public.users exists in '${DB_NAME}' but is INCOMPATIBLE — fix it before installing"
            fi
            info "public.users verified in ${DB_NAME}"
        else
            info "public.users does not exist in ${DB_NAME} — the harness creates it (it owns the table)"
        fi
    fi
fi
run apply_sql "$DB_NAME" -v ON_ERROR_STOP=1 \
    -v "operator_role=${OPERATOR_ROLE}" -v "runtime_role=${RUNTIME_ROLE}" \
    -f "$INSTALL_DIR/migrations/015_canonical_users.sql"
run apply_sql "$DB_NAME" -v ON_ERROR_STOP=1 \
    -v "operator_role=${OPERATOR_ROLE}" -v "runtime_role=${RUNTIME_ROLE}" \
    -v "agent_name=${AGENT_NAME}" -v "admin_email=${ADMIN_EMAIL}" \
    -f "$INSTALL_DIR/migrations/003_seed_admin_user.sql"

# =====================================================================
step "6. migrations"
# =====================================================================
if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] tools/migrate.sh (public + agent schemas)"
else
    ( cd "$INSTALL_DIR" && \
      HARNESS_DB="$DB_NAME" APP_DB_OPERATOR_ROLE="$OPERATOR_ROLE" \
      APP_DB_RUNTIME_ROLE="$RUNTIME_ROLE" HARNESS_AGENT="$AGENT_NAME" \
      HARNESS_AGENTS="$AGENT_NAME" \
      ./tools/migrate.sh )
fi

# =====================================================================
step "7. configuration"
# =====================================================================
run "$VENV_PY" "$INSTALL_DIR/tools/install_config.py" --config "$CONFIG" --write "$INSTALL_DIR"
if [ "$DRY_RUN" != "1" ]; then
    # the runtime password must be in the .env, not just in the URL
    python3 - "$INSTALL_DIR" "$INSTALL_RUNTIME_PASSWORD" <<'PY'
import os, sys
d, pw = sys.argv[1], sys.argv[2]
p = os.path.join(d, ".env")
with open(p, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
out = []
for l in lines:
    if l.startswith("DATABASE_URL="):
        l = "DATABASE_URL=" + l.split("=", 1)[1]
    out.append(l)
with open(p, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(p, 0o600)
PY
    info ".env + config.yaml written (mode 600)"
fi

# =====================================================================
step "8. the default agent"
# =====================================================================
PIE_DIR="$INSTALL_DIR/pie/$AGENT_NAME"
run mkdir -p "$PIE_DIR/identity" "$PIE_DIR/skills" "$PIE_DIR/systemd"
run "$VENV_PY" "$INSTALL_DIR/tools/render_identity.py" --config "$CONFIG" --agent-dir "$PIE_DIR"
if [ "$DRY_RUN" != "1" ]; then
    # create_timer ships with the harness; local-email deliberately does not
    if [ -d "$INSTALL_DIR/skills/create_timer" ]; then
        cp -r "$INSTALL_DIR/skills/create_timer" "$PIE_DIR/skills/"
        info "create_timer skill installed"
    fi
    info "local-email NOT installed (by design — copy it in after the email repo)"
fi

# =====================================================================
step "9. the first admin"
# =====================================================================
if [ -n "$ADMIN_PASSWORD" ]; then
    PW="$ADMIN_PASSWORD"
else
    PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
fi
if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] create_user.py --email ${ADMIN_EMAIL} --role ${ADMIN_ROLE}"
else
    ( cd "$INSTALL_DIR" && \
      set -a && . ./.env && set +a && \
      "$VENV_PY" tools/create_user.py --email "$ADMIN_EMAIL" --password "$PW" \
        --display-name "$ADMIN_DISPLAY" --role "$ADMIN_ROLE" )
    info "admin ${ADMIN_EMAIL} ready"
fi

# =====================================================================
step "10. service"
# =====================================================================
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] systemd --user unit harness.service in ${UNIT_DIR}"
else
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/harness.service" <<EOF
[Unit]
Description=Peristyle Harness
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/run.sh
Restart=always
RestartSec=5
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now harness.service
    info "harness.service enabled"
    printf '   waiting for /health'
    for _ in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${BIND_PORT}/health" >/dev/null 2>&1; then
            printf ' ok\n'
            break
        fi
        printf '.'
        sleep 1
    done
    printf '\n'
fi

# =====================================================================
step "summary"
# =====================================================================
cat <<EOF
   install dir : ${INSTALL_DIR}
   operator    : ${OPERATOR_USER}
   database    : ${DB_NAME} (mode=${DB_MODE})  roles: ${OPERATOR_ROLE} / ${RUNTIME_ROLE}
   agent       : ${AGENT_NAME} (${AGENT_DISPLAY} ${AGENT_EMOJI})
   admin       : ${ADMIN_EMAIL}
   bind        : ${HARNESS_BIND}
EOF
if [ "$DRY_RUN" != "1" ]; then
    if [ -n "$ADMIN_PASSWORD" ]; then
        echo "   admin password: (as supplied)"
    else
        echo "   admin password: ${PW}   <- save it now, it is not stored"
    fi
    echo "   next: open the web UI, log in, and add more agents."
    echo "   optional: copy the local-email skill in after installing the email repo."
fi
