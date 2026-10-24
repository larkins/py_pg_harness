"""Re-run a turn that is ALREADY in the turns table, without writing a
second user turn.

Why this exists
---------------
On 2026-10-21 20:57 the project owner sent the operator "ok ive made the actual payment to
a customer, please register that in the accounts system...". The turn was
accepted and ran for ~14 minutes (39 iterations, ~55 tool calls), then
the harness service was restarted underneath it and the turn was lost:
the user row survived in `agent2.turns`, the assistant row never existed.

Re-sending the message from the browser would write a SECOND user row.
That row would show up twice in the conversation and twice in the
prompt (the prompt already carries the message twice -- once from the
history scan and once as the fresh user message -- so a duplicate row
makes it three times), and it would pollute the training corpus with a
message the user never typed twice.

This script re-runs the turn in-process against the EXISTING user turn:

  * no new user row is written
  * the prompt is byte-identical to the one the original attempt saw
    (the history scan already includes the persisted user turn, exactly
    as it did when it was first written)
  * one new assistant row is written, with the real answer

Usage:
  set -a; . ./.env; set +a
  .venv/bin/python scripts/retry_turn.py --agent agent2 \
      --conversation 8b3ad9cd-1f31-4923-97aa-6c758352e63f \
      [--user-turn-id 299] [--user-id 72]

`--user-turn-id` defaults to the LAST user turn of the conversation.
`--user-id` defaults to the conversation's own user_id.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="  LOG %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
for noisy in ("httpx", "httpcore", "urllib3", "psycopg"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from app import create_app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--conversation", required=True)
    ap.add_argument("--user-turn-id", type=int, default=None)
    ap.add_argument("--user-id", type=int, default=None)
    args = ap.parse_args()

    agent = args.agent

    # Build the app exactly as the worker does, so the turn runs with
    # the same bootstrap, memory, tools, adapters and app context (the
    # vision path reads uploaded images through current_app).
    app = create_app()
    ext = app.extensions["harness"]
    loop = ext["loop"]
    mem = ext["memory_registry"][agent]

    # Find the conversation and the user turn to re-run.
    conv = mem.get_conversation(args.conversation)
    if conv is None:
        print(f"  conversation {args.conversation} not found in {agent}.conversations")
        return 2
    user_id = args.user_id if args.user_id is not None else conv["user_id"]

    if args.user_turn_id is not None:
        user_turn = mem.get_turn(args.user_turn_id)
        if user_turn is None:
            print(f"  user turn {args.user_turn_id} not found")
            return 2
        if str(user_turn.conversation_id) != str(conv["id"]):
            print(f"  turn {user_turn.id} belongs to conversation "
                  f"{user_turn.conversation_id}, not {conv['id']}")
            return 2
    else:
        turns = mem.load_recent_turns(conv["id"], limit=50)
        user_turns = [t for t in turns if t.role == "user"]
        if not user_turns:
            print("  no user turn found in the conversation")
            return 2
        user_turn = user_turns[-1]

    print(f"\n  agent={agent} conv={conv['id']} user_id={user_id}")
    print(f"  re-running user turn id={user_turn.id}: "
          f"{(user_turn.content or '')[:120]!r}\n")

    # Guard: the loop resolves the conversation from (agent, user_id).
    # If that is not the conversation we were asked to retry, stop.
    resolved = mem.get_or_create_persistent_conversation(agent, user_id=user_id)
    if str(resolved) != str(conv["id"]):
        print(f"  refusing: (agent={agent}, user_id={user_id}) resolves to "
              f"conversation {resolved}, not {conv['id']}")
        return 2

    n = 0
    with app.app_context():
        try:
            for ev in loop.handle_chat_streaming(
                "",
                agent_name=agent,
                user_id=user_id,
                reuse_user_turn=user_turn,
                request_id=f"retry-{user_turn.id}",
            ):
                t = ev.get("type")
                n += 1
                if t == "iter_text":
                    print(f"  EVENT iter_text iter={ev.get('iter')} "
                          f"tools={ev.get('tool_calls')} "
                          f"text={(ev.get('text') or '')[:100]!r}")
                elif t == "tool_start":
                    print(f"  EVENT tool_start {ev.get('name')} "
                          f"args={str(ev.get('args'))[:120]}")
                elif t == "tool_end":
                    print(f"  EVENT tool_end {ev.get('name')} ok={ev.get('ok')} "
                          f"preview={(ev.get('result_preview') or ev.get('preview') or '')[:90]!r}")
                elif t == "final":
                    resp = ev.get("response")
                    txt = getattr(resp, "assistant", "") or ""
                    print(f"\n  EVENT final: {len(txt)} chars")
                    print("  " + txt[:3000].replace("\n", "\n  "))
                    print(f"  iterations={getattr(resp, 'tool_iterations', '?')} "
                          f"latency_ms={getattr(resp, 'latency_ms', '?')}")
                elif t == "error":
                    print(f"  EVENT error: {ev.get('message')}")
                else:
                    print(f"  EVENT {t}")
        except Exception:
            print("\n  *** EXCEPTION ESCAPED handle_chat_streaming ***")
            traceback.print_exc()
            return 1

    print(f"\n  done ({n} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
