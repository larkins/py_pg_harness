"""Streaming parity tests: SSE path vs buffered path must be indistinguishable.

Run: set -a; . ./.env; set +a; .venv/bin/python tests/tests_stream_parity.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.agent.llm.minimax_m31_flash import LLM
from app.agent.llm.base import Message

cfg = load_config()


# temperature is pinned to 0.0. At the harness default of 0.7 the model is
# non-deterministic, so two identical calls return different prose and any
# text comparison between the two paths would fail for reasons that have
# nothing to do with streaming.
TEMP = 0.0


def run(streaming, m, tools=None, mt=1024):
    llm = LLM(cfg)
    llm.streaming = streaming
    d = []
    r = llm.complete([m], tools=tools, max_tokens=mt, temperature=TEMP,
                     on_delta=(d.append if streaming else None))
    return r, d


WEATHER = {"name": "get_weather", "description": "w",
           "parameters": {"type": "object",
                          "properties": {"city": {"type": "string"}},
                          "required": ["city"]}}

cases = [
    ("plain text, no tools",
     Message(role="user", content="In one sentence, what is a database index?"),
     None),
    ("two tool calls",
     Message(role="user",
             content="Use get_weather for Brisbane and for Sydney, two separate calls."),
     [WEATHER]),
    ("short reply",
     Message(role="user", content="Reply with exactly: OK"), None),
    ("tool with empty-string arg",
     Message(role="user", content="Call get_weather with city set to an empty string."),
     [WEATHER]),
]

allok = True
for label, msg, tools in cases:
    a, da = run(True, msg, tools)
    b, _ = run(False, msg, tools)

    # What is actually assertable. Cross-call TEXT equality is NOT: MiniMax
    # is non-deterministic even at temperature=0 (observed: two buffered
    # calls to the same prompt differ by a single word, "quickly" vs
    # "efficiently"), so comparing prose between two separate API calls
    # tests the sampler, not the transport.
    #
    # The streaming-correctness property is that the deltas reassemble
    # byte-exactly into the final text, and that the parsed structure
    # matches the buffered path's shape.
    reassembled = ("".join(da) == (a.text or "")) if a.text else (len(da) == 0)
    # Block SHAPE is also not comparable across calls: whether the model
    # emits a thinking block varies run to run. What must hold is that every
    # block the stream produced was fully assembled into a known type, with
    # tool_use input reconstructed from partial_json as valid JSON.
    KNOWN = {"text", "thinking", "tool_use"}
    blocks_a = [b_.get("type") for b_ in (a.raw.get("content") or [])]
    known_ok = all(t in KNOWN for t in blocks_a) and len(blocks_a) > 0
    tool_json_ok = all(
        isinstance(b_.get("input"), dict)
        for b_ in (a.raw.get("content") or []) if b_.get("type") == "tool_use")
    same_shape = known_ok and tool_json_ok
    same_tc = ([(t.name, t.arguments) for t in a.tool_calls]
               == [(t.name, t.arguments) for t in b.tool_calls])
    same_fr = a.finish_reason == b.finish_reason
    usage_ok = all(k in a.usage for k in ("input_tokens", "output_tokens"))
    no_leak = not any("_partial_json" in b_ for b_ in (a.raw.get("content") or []))

    ok = reassembled and same_shape and same_tc and same_fr and usage_ok and no_leak
    allok &= ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: "
          f"reassemble={reassembled} blocks_wellformed={same_shape} tools={same_tc} "
          f"finish={same_fr} usage={usage_ok} no_internal_leak={no_leak} "
          f"deltas={len(da)} blocks={blocks_a}")

# --- non-SSE fallback guard ---
llm = LLM(cfg)
llm.streaming = True


class FakeCtx:
    def __init__(self, r):
        self.r = r

    def __enter__(self):
        return self.r

    def __exit__(self, *a):
        return False


class FakeResp:
    status_code = 200
    headers = {"content-type": "application/json; charset=utf-8"}

    def read(self):
        return (b'{"id":"x","type":"message","role":"assistant",'
                b'"content":[{"type":"text","text":"plain fallback"}],'
                b'"stop_reason":"end_turn",'
                b'"usage":{"input_tokens":1,"output_tokens":2}}')

    def raise_for_status(self):
        return None


llm._client.stream = lambda *a, **k: FakeCtx(FakeResp())
data = llm._stream_to_message("u", {}, {}, None)
got = data["content"][0]["text"]
guard_ok = got == "plain fallback"
allok &= guard_ok
print(f"  [{'PASS' if guard_ok else 'FAIL'}] non-SSE fallback guard: "
      f"parsed buffered body instead of returning empty -> {got!r}")

print()
print("  ALL PARITY CHECKS PASSED" if allok else "  *** SOME CHECKS FAILED ***")
raise SystemExit(0 if allok else 1)