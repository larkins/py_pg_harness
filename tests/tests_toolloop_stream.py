"""Tool-loop round-trip test for the streaming adapter.

Exercises the full agent cycle the loop performs:
  complete() -> tool_calls -> format_tool_result() -> append assistant
  content (incl. thinking blocks) -> complete() again.

This is the part most likely to break under streaming, because
extract_assistant_content must round-trip the streamed thinking blocks back
onto the wire exactly as the buffered path produced them.

Run: set -a; . ./.env; set +a; .venv/bin/python tests/tests_toolloop_stream.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.agent.llm.minimax_m31_flash import LLM
from app.agent.llm.base import Message

cfg = load_config()
WEATHER = {"name": "get_weather", "description": "Get the weather for a city",
           "parameters": {"type": "object",
                          "properties": {"city": {"type": "string"}},
                          "required": ["city"]}}

msgs = [Message(role="user",
                content="What is the weather in Brisbane? Use the get_weather tool.")]

llm = LLM(cfg)
print(f"  streaming={llm.streaming}")

r1 = llm.complete(msgs, tools=[WEATHER], max_tokens=512, temperature=0.0)
print(f"  iter1: tool_calls={[(t.name, t.arguments) for t in r1.tool_calls]} "
      f"thinking_blocks={len(r1.thinking_blocks)} finish={r1.finish_reason}")

assert r1.tool_calls, "iter1 produced no tool call"
call = r1.tool_calls[0]

# Exactly what the loop does after a tool call.
assistant_content = llm.extract_assistant_content(r1)
tool_msg = llm.format_tool_result(
    call, "Sunny, 27C, light breeze (simulated tool output)")
msgs.append(Message(role="assistant", content=assistant_content))
msgs.append(tool_msg)

r2 = llm.complete(msgs, tools=[WEATHER], max_tokens=512, temperature=0.0)
print(f"  iter2: text={(r2.text or '')[:70]!r}")
print(f"        finish={r2.finish_reason} thinking_blocks={len(r2.thinking_blocks)}")

ok = bool(r2.text) and r2.finish_reason == "stop"
print()
print("  [PASS] full tool-loop round-trip completed on the streaming path"
      if ok else "  *** FAIL: iter2 did not complete ***")
raise SystemExit(0 if ok else 1)