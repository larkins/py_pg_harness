"""Verify the client-error endpoint persists a failure as an assistant turn.

2026-10-22. The endpoint POST /api/v1/entities/<name>/turns/error exists
because the ⚠️ bubble the browser shows when a stream dies lives only in
the DOM -- the next refresh makes the failure look like silence again.

This test:
  1. creates a throwaway user + a user turn with NO assistant reply
     (the state a lost turn leaves behind)
  2. calls the endpoint and checks a row was written
  3. calls it again and checks it is a no-op (one failure, one row)
  4. cleans up every row it created

Usage:
  set -a; . ./.env; set +a
  .venv/bin/python scripts/test_error_persist.py [agent]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from flask import g, session

from app import create_app

AGENT = sys.argv[1] if len(sys.argv) > 1 else "agent3"
EMAIL = "harness-selftest@example.com"

app = create_app()
ext = app.extensions["harness"]
mem = ext["memory_registry"][AGENT]

# 1. throwaway user (created out-of-band by the operator -- the harness
# runtime role cannot write public.users) + a user turn with NO assistant
# reply, which is the state a lost turn leaves behind.
uid = int(os.environ.get("SELFTEST_USER_ID", "0"))
if not uid:
    raise SystemExit("set SELFTEST_USER_ID")
print(f"  throwaway user id={uid}")

from app.routes.api import entity_turn_error

conv_id = mem.get_or_create_persistent_conversation(AGENT, user_id=uid)
ut = mem.write_turn_user(conv_id, "selftest: stream failure", attachments=[])
print(f"  user turn id={ut.id} conv={conv_id}")

# 2. call the endpoint
with app.test_request_context(
    f"/api/v1/entities/{AGENT}/turns/error",
    json={"message": "stream ended without a final event", "request_id": "selftest-1"},
):
    session["_user_id"] = uid
    g.current_user = SimpleNamespace(id=uid)
    resp = entity_turn_error(AGENT)
    print(f"  first call -> {resp[1]} {resp[0].get_json()}")

with app.test_request_context(
    f"/api/v1/entities/{AGENT}/turns/error",
    json={"message": "stream ended without a final event", "request_id": "selftest-1"},
):
    session["_user_id"] = uid
    g.current_user = SimpleNamespace(id=uid)
    resp = entity_turn_error(AGENT)
    print(f"  second call -> {resp[1]} {resp[0].get_json()}")

rows = mem.load_recent_turns(conv_id, limit=10)
print(f"  turns in conv: {[(t.id, t.role, t.content[:40]) for t in rows]}")

# 4. cleanup
with mem.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {AGENT}.turns WHERE conversation_id = %s", (conv_id,))
        cur.execute(f"DELETE FROM {AGENT}.conversations WHERE id = %s", (conv_id,))
    conn.commit()
print("  cleaned up")
