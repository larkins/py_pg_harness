#!/usr/bin/env python3
"""CLI to create or update a user in public.users.

Usage:
    python3 tools/create_user.py \
        --email admin@example.com \
        --password '...' \
        --display-name 'Admin' \
        --role admin

If the email already exists, the password hash is rotated and the
display_name / role are updated.

The app connects with DATABASE_URL, so the role in that URL has to be
allowed to INSERT into public.users -- the operator role, not the
runtime role (the runtime role is SELECT-only there). Override the URL
for the call:

    DATABASE_URL=postgresql://<operator_role>@localhost:5432/<db> \
        python3 tools/create_user.py --email ... --password ...

Run from the harness repo root, or from any directory that has the
`app/` package on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import os
import sys

# Make the app package importable when run from the repo root or from
# the install directory.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # parent of tools/

# Load .env if present (so DATABASE_URL is set).
env_path = os.path.join(HERE, "..", ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k, v)

from app import create_app          # noqa: E402
from app.auth import create_user as _create_user  # noqa: E402


def main():
    p = argparse.ArgumentParser(description="Create or update a public.users row")
    p.add_argument("--email", required=True, help="User email (will be lowercased)")
    p.add_argument("--password", required=True, help="Plaintext password; hashed with pbkdf2:sha256 at write time")
    p.add_argument("--display-name", default=None, help="Display name (defaults to email local part)")
    p.add_argument("--role", default="member", choices=("admin", "member", "viewer"))
    args = p.parse_args()

    app = create_app()
    with app.app_context():
        uid = _create_user(
            email=args.email,
            password=args.password,
            display_name=args.display_name,
            role=args.role,
        )
    print(f"OK: user_id={uid} email={args.email.lower()} role={args.role}")


if __name__ == "__main__":
    main()
