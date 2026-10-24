"""Prompt-pressure warning tests (2026-10-22).

Two conditional system-prompt warnings:

  1. an identity file at/over `prompt.pressure_threshold` of its byte cap
     -> warn: move the detail out to memories (via the memory tools) or
     to skills / scripts / docs, keep the identity file a table of
     contents. Over-cap identity files are a BOOT FAILURE, not bloat.

  2. the permanent-memory block at/over the threshold of
     `memory.permanent.max_tokens` -> warn: move the detail out to
     skills / scripts / docs, or write it as an UNPINNED memory and
     recall it on demand.

Run: set -a; . ./.env; set +a; .venv/bin/python tests/tests_prompt_pressure.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.agent.loop import AgentLoop
from app.agent.bootstrap import BootstrapLoader
from app.agent.memory import MemoryChunk

cfg = load_config()
THRESHOLD = cfg.prompt.pressure_threshold
print(f"threshold from config: {THRESHOLD} (enabled={cfg.prompt.pressure_enabled})")

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}{(' -- ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(name)


class FakeBootstrap:
    """Stands in for BootstrapLoader: file_usage() only."""

    def __init__(self, usage):
        self._usage = usage

    def file_usage(self):
        return self._usage

    def render(self):
        return "<bootstrap>"


class FakeChunk:
    def __init__(self, content):
        self.content = content


def apply(system_text, usage=None, chunks=None, overflowed=False,
          threshold=None, enabled=True):
    """Call the loop's pressure helper with fakes."""
    loop = AgentLoop.__new__(AgentLoop)          # no __init__ side effects
    loop.config = cfg
    # Override the threshold/enabled on a copy of the config object.
    import dataclasses
    loop.config = dataclasses.replace(
        cfg,
        prompt=dataclasses.replace(
            cfg.prompt,
            pressure_threshold=(cfg.prompt.pressure_threshold
                                if threshold is None else threshold),
            pressure_enabled=enabled))
    return AgentLoop._apply_prompt_pressure(
        loop, system_text,
        agent_name=cfg.app.default_agent,
        agent_bootstrap=FakeBootstrap(usage or []),
        permanent_chunks=chunks or [],
        permanent_overflowed=overflowed)


print("\n1. identity file over the threshold")
out = apply("BASE SYSTEM", usage=[
    {"name": "AGENTS.md", "bytes": 21000, "cap": 24000, "pct": 21000 / 24000},
])
check("warning emitted", "context_pressure" in out)
check("names the file", 'file="AGENTS.md"' in out)
check("says move detail out", "skills/" in out and "scripts/" in out and "docs/" in out)
check("mentions memories via tools", "memory_write" in out)
check("mentions table of contents", "table of contents" in out.lower())
check("warns about the boot failure", "booting" in out)

print("\n2. identity file under the threshold -> no warning")
out = apply("BASE SYSTEM", usage=[
    {"name": "AGENTS.md", "bytes": 12000, "cap": 24000, "pct": 0.5},
])
check("no warning", out == "BASE SYSTEM", out[:60])

print("\n3. permanent memory over the threshold")
chunks = [FakeChunk("word " * 9000)]          # 9000 tokens of 10000
out = apply("BASE SYSTEM", chunks=chunks)
check("warning emitted", "context_pressure" in out)
check("names permanent memory", 'target="permanent_memory"' in out)
check("suggests unpinned memory", "UNPINNED" in out)
check("mentions memory_recall", "memory_recall" in out)
check("mentions skills/scripts/docs",
      "skills/" in out and "scripts/" in out and "docs/" in out)

print("\n4. permanent memory under the threshold -> no warning")
out = apply("BASE SYSTEM", chunks=[FakeChunk("word " * 100)])
check("no warning", out == "BASE SYSTEM", out[:60])

print("\n5. overflow flag alone triggers the permanent-memory warning")
out = apply("BASE SYSTEM", chunks=[FakeChunk("word " * 10)], overflowed=True)
check("warning emitted", "context_pressure" in out)
check("says dropping is silent", "SILENTLY" in out)

print("\n6. the kill switch")
out = apply("BASE SYSTEM", usage=[
    {"name": "AGENTS.md", "bytes": 21000, "cap": 24000, "pct": 0.9}], enabled=False)
check("no warning when disabled", out == "BASE SYSTEM")

print("\n7. threshold is configurable (0.99 suppresses, 0.5 triggers)")
out = apply("BASE SYSTEM", usage=[
    {"name": "AGENTS.md", "bytes": 21000, "cap": 24000, "pct": 0.875}],
    threshold=0.99)
check("suppressed at 0.99", out == "BASE SYSTEM")
out = apply("BASE SYSTEM", usage=[
    {"name": "AGENTS.md", "bytes": 21000, "cap": 24000, "pct": 0.875}],
    threshold=0.5)
check("triggered at 0.5", "context_pressure" in out)

print("\n8. real bootstrap usage for every agent")
try:
    for agent in cfg.app.agents:
        boot = BootstrapLoader(cfg, str(Path(__file__).resolve().parents[1] / "pie" / agent / "identity"))
        usage = boot.file_usage()
        worst = max(usage, key=lambda u: u["pct"])
        print(f"  {agent}: " + ", ".join(
            f"{u['name']}={u['bytes']}/{u['cap']} ({u['pct']*100:.1f}%)"
            for u in usage))
        check(f"{agent} usage parses", all(
            isinstance(u["bytes"], int) and isinstance(u["cap"], int)
            for u in usage))
except Exception as e:
    print(f"  (skipped: {type(e).__name__}: {e})")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("ALL CHECKS PASSED")
