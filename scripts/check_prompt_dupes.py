"""Verify the prompt-duplication fix (2026-10-22).

_before_ the fix, every prompt carried the user's last message TWICE:
once as a plain-text history entry (the fresh user turn was already in
the table when the history scan ran) and once as the final user message
with the vision content block.

This script composes the prompt for a given conversation/turn and
counts how many times the user's message appears in it.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from pathlib import Path

AGENT = os.environ.get("CHECK_AGENT", "agent2")
CONV = os.environ.get("CHECK_CONV", "8b3ad9cd-1f31-4923-97aa-6c758352e63f")
TURN_ID = int(os.environ.get("CHECK_TURN", "299"))

app = create_app()
ext = app.extensions["harness"]
loop = ext["loop"]
mem = ext["memory_registry"][AGENT]
conv = mem.get_conversation(CONV)
ut = mem.get_turn(TURN_ID)
target = (ut.content or "").strip()

with app.app_context():
    msgs = loop._compose_messages(
        conv["id"], AGENT, ut, mem, ext["bootstrap_registry"].get(AGENT)
    )

hits = 0
for m in msgs:
    c = m.content
    if isinstance(c, str):
        hits += c.count(target)
    elif isinstance(c, list):
        for blk in c:
            if isinstance(blk, dict) and target in str(blk.get("text", "")):
                hits += 1

print("total messages in prompt:", len(msgs))
print("occurrences of the user's message:", hits)
print("roles (first 3):", [m.role for m in msgs[:3]])
print("last message role:", msgs[-1].role)
