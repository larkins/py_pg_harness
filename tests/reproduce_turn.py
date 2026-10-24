"""Reproduce an agent's failing turn in-process, with full tracebacks on stdout.

Mirrors create_app()'s wiring (pool -> embedding -> MemoryAPI -> AgentLoop)
so the real tool loop, real tools and real streaming adapter are exercised
without HTTP or auth. Anything the journal would have swallowed is printed.

Usage:
  set -a; . ./.env; set +a
  .venv/bin/python tests/reproduce_turn.py "<prompt>" [agent]
"""
import logging
import sys
import traceback
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="  LOG %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
for noisy in ("httpx", "httpcore", "urllib3", "psycopg"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from psycopg_pool import ConnectionPool
from app.config import load_config
from app.agent.llm import get_adapter
from app.agent.memory import MemoryAPI
from app.agent.embedding import EmbeddingClient
from app.agent.loop import AgentLoop
from app.agent.bootstrap import BootstrapLoader

config = load_config()
prompt = sys.argv[1]
agent = sys.argv[2] if len(sys.argv) > 2 else config.app.default_agent

pool = ConnectionPool(config.db.url, min_size=1, max_size=4,
                      kwargs={"autocommit": False}, open=True)
embedding = EmbeddingClient(config)

adapter = get_adapter(config)
adapter_registry = {}
if config.llm_per_agent and agent in config.llm_per_agent:
    adapter_registry[agent] = get_adapter(
        replace(config, llm=config.llm_per_agent[agent]))

bootstrap_registry = {}
for name in config.app.agents:
    try:
        bootstrap_registry[name] = BootstrapLoader(config, name)
    except Exception as e:
        print(f"  bootstrap {name} unavailable: {type(e).__name__}: {e}")

mem = MemoryAPI(pool=pool, embedding_client=embedding,
                agent_name=agent, config=config)

loop = AgentLoop(
    adapter=adapter,
    memory=mem,
    bootstrap=bootstrap_registry.get(agent) or bootstrap_registry.get(config.app.default_agent),
    config=config,
    bootstrap_registry=bootstrap_registry,
    memory_registry={agent: mem},
    adapter_registry=adapter_registry,
)

eff = adapter_registry.get(agent) or adapter
print(f"\n  agent={agent} model={eff.model} streaming={eff.streaming} "
      f"max_tokens={eff.default_max_tokens} timeout={eff.timeout_s}s\n")

history = []
n = 0
try:
    for i, ev in enumerate(loop.handle_chat_streaming(
            prompt, agent_name=agent, user_id=72)):
        t = ev.get("type")
        if t == "iter_text":
            print(f"  EVENT iter_text iter={ev.get('iter')} "
                  f"tools={ev.get('tool_calls')} "
                  f"text={(ev.get('text') or '')[:90]!r}")
        elif t == "tool_start":
            print(f"  EVENT tool_start {ev.get('name')} "
                  f"args={str(ev.get('args'))[:110]}")
        elif t == "tool_end":
            print(f"  EVENT tool_end {ev.get('name')} "
                  f"preview={(ev.get('preview') or '')[:80]!r}")
        elif t == "final":
            resp = ev.get("response")
            txt = getattr(resp, "assistant", "") or ""
            print(f"  EVENT final: {len(txt)} chars -> {txt[:70]!r}")
            print(f"               iterations={getattr(resp, chr(39)+chr(39).join([])+chr(39), None) or getattr(resp, chr(39)+chr(39).join([])+chr(39), None)}")
        elif t == "error":
            print(f"  EVENT error: {ev.get('message')}")
        else:
            print(f"  EVENT {t}")
except Exception:
    print("\n  *** EXCEPTION ESCAPED handle_chat_streaming ***")
    traceback.print_exc()
    sys.exit(1)

print("\n  done")