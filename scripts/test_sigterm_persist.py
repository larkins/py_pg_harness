"""Verify the SIGTERM handler persists an in-flight turn.

2026-10-22. Starts a turn in a background thread, then sends SIGTERM to
this process. The handler in app/agent/turn_guard.py should write an
"interrupted" assistant turn for the in-flight turn BEFORE the process
exits -- which is what was missing when the project owner's the operator turn was destroyed by
a service restart on 2026-10-21 21:12.

Uses a throwaway user id so it does not touch anyone's conversation.

Usage:
  set -a; . ./.env; set +a
  .venv/bin/python scripts/test_sigterm_persist.py [agent] [user_id]
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app

AGENT = sys.argv[1] if len(sys.argv) > 1 else "agent3"
USER_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 990001

app = create_app()
ext = app.extensions["harness"]
loop = ext["loop"]

print(f"  SIGTERM handler installed: {callable(signal.getsignal(signal.SIGTERM))}")
print(f"  handler: {signal.getsignal(signal.SIGTERM)}")
print(f"  in-flight turns before: {__import__('app.agent.turn_guard', fromlist=['x']).in_flight()}")


def run():
    try:
        for ev in loop.handle_chat_streaming(
            "Count the files under the data directory recursively and "
            "report the total, then list the top-level directories.",
            agent_name=AGENT,
            user_id=USER_ID,
            request_id="sigterm-test",
        ):
            print(f"  EVENT {ev.get('type')}")
    except Exception as e:
        print(f"  turn ended: {type(e).__name__}: {e}")


t = threading.Thread(target=run, daemon=True)
t.start()
time.sleep(3.0)
print("  --- sending SIGTERM to self ---")
os.kill(os.getpid(), signal.SIGTERM)
time.sleep(5.0)
print("  NOT REACHED: the handler did not exit the process")
