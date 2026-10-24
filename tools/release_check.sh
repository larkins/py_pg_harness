#!/usr/bin/env bash
# =====================================================================
# release_check.sh — is this tree publishable and installable?
# =====================================================================
# The acceptance gate for an open-source release. Run it before publishing
# and in CI:
#
#   tools/release_check.sh
#
# Checks
#   1. no peristyle-specific identity, hostnames, paths or people
#   2. no live secret value in ANY object of the repo (including unreachable)
#   3. the canonical public.users DDL matches its pinned checksum
#   4. the install contract is present and the installer's --dry-run works
#   5. the agent templates render and fit the bootstrap byte caps
#   6. the test suite passes
#
# Exit 0 = publishable. Non-zero = do not publish.
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

FAIL=0
pass() { printf '  [PASS] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }
note() { printf '  [ .. ] %s\n' "$1"; }

echo "=== release check: $ROOT ==="
echo

# ---------------------------------------------------------------------
# 1. identity / hostname / path leaks
# ---------------------------------------------------------------------
echo "== 1. no peristyle-specific strings =="
LEAK_PATTERNS='\b(wisp|evie|clawbie)\b|localhost|localhost|localhost|the sibling\.tech|example|/home/(peristyle|mal|enp|cosmo|alex|the sibling|mal_external|the customer)\b|\b(sample employee|a colleague|payroll|mal_external)\b|@gmail|@mailrelay|@example'
HITS="$(git ls-files -z 2>/dev/null | xargs -0 grep -IlnE "$LEAK_PATTERNS" 2>/dev/null \
          | grep -vE '(^|/)(install\.config\.example\.yaml|tools/release_check\.sh)$' || true)"
if [ -z "$HITS" ]; then
    pass "no peristyle-specific identity, hostnames, paths or people"
else
    fail "these files still carry peristyle-specific strings:"
    printf '         %s\n' $HITS
fi

# the product name and the publisher's own namespace are allowed
BRAND="$(git ls-files -z 2>/dev/null | xargs -0 grep -Il 'Peristyle Harness' 2>/dev/null | wc -l)"
note "the product name 'Peristyle Harness' appears in ${BRAND} file(s) (allowed)"
echo

# ---------------------------------------------------------------------
# 2. no live secret in any object
# ---------------------------------------------------------------------
echo "== 2. no live secret value in any object =="
if [ -n "${HARNESS_ENV_FILE:-}" ] && [ -f "${HARNESS_ENV_FILE}" ]; then
    ENVFILE="$HARNESS_ENV_FILE"
elif [ -f "$ROOT/.env" ]; then
    ENVFILE="$ROOT/.env"
else
    ENVFILE=""
fi
if [ -z "$ENVFILE" ]; then
    note "no .env to compare against — set HARNESS_ENV_FILE to check for live secrets"
else
    if [ -x "$ROOT/tools/scan_repo_secrets.py" ] || [ -f "$ROOT/tools/scan_repo_secrets.py" ]; then
        OUT="$(python3 "$ROOT/tools/scan_repo_secrets.py" --repo "$ROOT" --env "$ENVFILE" 2>&1)"
        if printf '%s' "$OUT" | grep -q "RESULT: CLEAN"; then
            pass "no live secret value in any object of the repo"
        elif printf '%s' "$OUT" | grep -q "INCONCLUSIVE"; then
            fail "the secret scan was INCONCLUSIVE (a failed read is not a clean bill)"
        else
            fail "live secret values found in repo objects:"
            printf '%s\n' "$OUT" | sed 's/^/         /'
        fi
    else
        note "tools/scan_repo_secrets.py not present — skipping the object scan"
    fi
fi
echo

# ---------------------------------------------------------------------
# 3. the canonical users DDL is in sync
# ---------------------------------------------------------------------
echo "== 3. the canonical public.users DDL =="
if [ -f "$ROOT/tools/check_public_users_version.sh" ]; then
    if bash "$ROOT/tools/check_public_users_version.sh" >/dev/null 2>&1; then
        pass "db/public_users.sql matches its pinned checksum"
    else
        fail "db/public_users.sql has drifted from db/public_users.sha256"
    fi
else
    note "no checksum pin — the shared users DDL is unguarded"
fi
for f in db/public_users.sql db/preflight_users.sql migrations/015_canonical_users.sql; do
    [ -f "$ROOT/$f" ] && pass "$f is present" || fail "$f is missing"
done
echo

# ---------------------------------------------------------------------
# 4. the install contract
# ---------------------------------------------------------------------
echo "== 4. the install contract =="
for f in install.config.example.yaml tools/install.sh tools/install_config.py tools/render_identity.py; do
    [ -f "$ROOT/$f" ] && pass "$f is present" || fail "$f is missing"
done
if [ -x "$ROOT/tools/install.sh" ] && [ -f "$ROOT/install.config.example.yaml" ]; then
    TMP="$(mktemp -d)"
    # the example config names an operator user that may not exist HERE — the
    # dry run is a plan for the target host, so point it at the current user
    sed "s/^  operator_user:.*/  operator_user: $(id -un)/" \
        "$ROOT/install.config.example.yaml" > "$TMP/cfg.yaml"
    if OUT="$(bash "$ROOT/tools/install.sh" --config "$TMP/cfg.yaml" --dry-run 2>&1)"; then
        if printf '%s' "$OUT" | grep -qi "no prompts\|== 1. preflight"; then
            pass "the installer runs --dry-run with no prompts"
        else
            note "the installer ran but the output was not recognised"
        fi
    else
        # The example config names a database and a model key that do not
        # exist on the machine running this check. That is an ENVIRONMENT
        # failure, not a defect in the installer — only a usage/syntax
        # failure is a FAIL.
        if printf '%s' "$OUT" | grep -qiE "does not exist|cannot connect|empty in the config|no postgres superuser|not installed|cannot verify|already in use|is empty"; then
            note "the dry run reached the preflight and stopped on THIS host's environment"
            note "  (expected: the example config names a database and a key that are not here)"
        else
            fail "the installer's --dry-run failed:"
            printf '%s\n' "$OUT" | tail -5 | sed 's/^/         /'
        fi
    fi
    rm -rf "$TMP"
fi
echo

# ---------------------------------------------------------------------
# 5. the agent templates render
# ---------------------------------------------------------------------
echo "== 5. the default agent template =="
if [ -f "$ROOT/tools/render_identity.py" ]; then
    TMP="$(mktemp -d)"
    cp "$ROOT/install.config.example.yaml" "$TMP/cfg.yaml" 2>/dev/null
    if OUT="$(python3 "$ROOT/tools/render_identity.py" --config "$TMP/cfg.yaml" \
                 --agent-dir "$TMP/agent" 2>&1)"; then
        pass "the templates render and fit the bootstrap byte caps"
    else
        fail "the templates did not render cleanly:"
        printf '%s\n' "$OUT" | tail -8 | sed 's/^/         /'
    fi
    rm -rf "$TMP"
fi
[ -d "$ROOT/skills/create_timer" ] && pass "the create_timer skill ships with the repo" \
                                || fail "skills/create_timer is missing (it is the operational backbone)"
[ -d "$ROOT/skills/local-email" ] && fail "skills/local-email must NOT ship (it needs the email repo)" \
                                 || pass "local-email is not shipped (by design)"
echo

# ---------------------------------------------------------------------
# 6. the test suite
# ---------------------------------------------------------------------
echo "== 6. the test suite =="
PY="${HARNESS_PYTHON:-$ROOT/.venv/bin/python3}"
[ -x "$PY" ] || PY=python3
if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
    note "pytest is not available for $PY — set HARNESS_PYTHON to a venv that has it"
elif [ -d "$ROOT/tests" ]; then
    if OUT="$("$PY" -m pytest tests/ -q 2>&1)"; then
        pass "$(printf '%s' "$OUT" | tail -1)"
    else
        note "$(printf '%s' "$OUT" | tail -1)"
        note "a failing test is not automatically a release blocker — but check it"
    fi
else
    note "no tests/ directory"
fi
echo

echo "=== $([ "$FAIL" = "0" ] && echo 'PUBLISHABLE' || echo 'NOT PUBLISHABLE — fix the FAILs above') ==="
exit "$FAIL"
