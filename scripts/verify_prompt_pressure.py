"""End-to-end check of the prompt-pressure warning against the REAL
harness state (real identity files, real config, real memory rows).

Run: set -a; . ./.env; set +a; .venv/bin/python verify_prompt_pressure.py
"""
import dataclasses
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from pathlib import Path

app = create_app()
ext = app.extensions["harness"]
loop = ext["loop"]
cfg = app.config["HARNESS_CONFIG_OBJ"]

print(f"config threshold: {cfg.prompt.pressure_threshold} "
      f"(enabled={cfg.prompt.pressure_enabled})")

for agent in cfg.app.agents:
    boot = ext["bootstrap_registry"].get(agent)
    usage = boot.file_usage() if boot else []
    worst = max(usage, key=lambda u: u["pct"]) if usage else None
    print(f"\n{agent}: worst identity file = "
          f"{worst['name']} {worst['bytes']}/{worst['cap']} "
          f"({worst['pct']*100:.1f}%)" if worst else f"{agent}: no bootstrap")

# Real permanent-memory usage, from the same call the loop makes.
mem = ext["memory_registry"]["agent2"]
chunks, overflowed = mem.get_permanent()
used = sum(len((c.content or "").split()) for c in chunks)
cap = cfg.memory.permanent.max_tokens
print(f"\npermanent memory: {used}/{cap} tokens "
      f"({100.0*used/cap:.1f}%) overflowed={overflowed}")

# Now compose a REAL prompt for agent2's real conversation and look for
# the warning in the system message.
conv = mem.get_or_create_persistent_conversation("agent2", user_id=72)
ut = mem.get_turn(299)
with app.app_context():
    msgs = loop._compose_messages(conv, "agent2", ut, mem,
                                  ext["bootstrap_registry"].get("agent2"))
system = msgs[0].content
print(f"\n--- live system prompt: {len(system)} chars, "
      f"{len(msgs)} messages ---")
print("warning present at threshold 0.85:",
      "context_pressure" in system)

# Same composition with the threshold lowered, to prove the live path
# fires (and to show what the agent would be told).
loop.config = dataclasses.replace(
    cfg, prompt=dataclasses.replace(cfg.prompt, pressure_threshold=0.80))
with app.app_context():
    msgs2 = loop._compose_messages(conv, "agent2", ut, mem,
                                   ext["bootstrap_registry"].get("agent2"))
sys2 = msgs2[0].content
print("warning present at threshold 0.80:", "context_pressure" in sys2)
if "context_pressure" in sys2:
    idx = sys2.find("<context_pressure")
    print("\n--- the warning the agent would receive ---")
    print(sys2[idx:idx + 1600])
