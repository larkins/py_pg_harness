#!/usr/bin/env bash
# =====================================================================
# check_public_users_version.sh
# =====================================================================
# Guards the ONE rule of the shared users table: there must be exactly
# one canonical DDL, and every project that vendors it must vendor the
# SAME bytes.
#
# The canonical file lives in db/public_users.sql. Its sha256 is pinned
# in db/public_users.sha256. CI (or a pre-install hook) runs this script:
# if the vendored copy has drifted from the pin, the check FAILS.
#
# The shared source of truth is the `peristyle-ai/db_users` repo; this
# script is the vendoring contract for it. Update the pin deliberately:
#   sha256sum db/public_users.sql
#
# Usage: tools/check_public_users_version.sh [--quiet]
# Exit 0 = in sync. Exit 1 = drifted (a project is about to create a
# divergent users table — exactly the failure this prevents).
# =====================================================================
set -euo pipefail

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANONICAL="$ROOT/db/public_users.sql"
PIN="$ROOT/db/public_users.sha256"

if [ ! -f "$CANONICAL" ]; then
    echo "FAIL: $CANONICAL is missing" >&2
    exit 1
fi
if [ ! -f "$PIN" ]; then
    echo "FAIL: $PIN is missing (the canonical DDL has no pinned checksum)" >&2
    exit 1
fi

ACTUAL="$(sha256sum "$CANONICAL" | awk '{print $1}')"
EXPECTED="$(awk '{print $1}' "$PIN")"

if [ "$ACTUAL" != "$EXPECTED" ]; then
    echo "FAIL: the canonical public.users DDL has drifted from its pin." >&2
    echo "      expected $EXPECTED" >&2
    echo "      actual   $ACTUAL" >&2
    echo "      If this was deliberate, re-pin it:" >&2
    echo "        sha256sum db/public_users.sql > db/public_users.sha256" >&2
    echo "      If it was not, another project is vendoring a DIFFERENT" >&2
    echo "      users table — that is the bug this check exists to catch." >&2
    exit 1
fi

[ "$QUIET" = "1" ] || echo "OK: canonical public.users DDL matches its pinned checksum ($ACTUAL)"
exit 0
