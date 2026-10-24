"""Verify _complete_with_deltas actually emits deltas and returns a response."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.agent.llm import get_adapter
from app.agent.loop import AgentLoop
from app.agent.llm.base import Message

cfg = load_config()
loop = AgentLoop.__new__(AgentLoop)   # only needs the helper, no wiring

adapter = get_adapter(cfg)
msgs = [Message(role="user",
                content="Reply with exactly these 5 words and nothing else: "
                        "one two three four five")]

deltas = []
final = None
for kind, payload in loop._complete_with_deltas(adapter, msgs, None):
    if kind == "delta":
        deltas.append(payload)
    else:
        final = payload
        break

joined = "".join(deltas)
print(f"  delta events : {len(deltas)}")
print(f"  joined       : {joined!r}")
print(f"  final.text   : {(final.text or '')!r}")
print()
ok = len(deltas) > 1 and joined == (final.text or "")
print("  [PASS] deltas stream and reassemble exactly to the final text"
      if ok else "  *** FAIL: delta wiring is not producing/joining correctly ***")

# also confirm the no-on_delta adapter path still works (back-compat)
class NoDelta:
    adapter_id = "test"
    def complete(self, messages, tools=None, max_tokens=None, temperature=None):
        from app.agent.llm.base import LLMResponse
        return LLMResponse(text="no-delta adapter")
d2 = list(loop._complete_with_deltas(NoDelta(), msgs, None))
print(f"  [PASS] adapter without on_delta still returns: {d2[0][1].text!r}"
      if d2[0][0] == "done" and d2[0][1].text == "no-delta adapter"
      else "  *** FAIL: back-compat path broken ***")

raise SystemExit(0 if ok else 1)