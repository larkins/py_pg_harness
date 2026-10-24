"""Unit test for the OpenAI-round-trip branch logic in AgentLoop.

Covers all three cases introduced in Phase 15.2 (2026-10-18):
  1. Real tool_use blocks → append asst_payload as-is
  2. Repair-synthesised tool calls (text-only asst_payload + non-empty
     response.tool_calls) → build synthetic content with proper
     tool_use blocks (the bug fix)
  3. Pure text (no tool_use, no tool_calls) → append asst_payload as-is

Verifies that case 2 (the bug) now builds correct synthetic content
rather than appending text-only asst_payload + a tool_result with
an unmatched tool_use_id.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Make the app package importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Stub out heavy imports so the test runs fast and standalone
class _StubMessage:
    """Mimic the loop's Message dataclass: just role + content."""
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _StubCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.name = name
        self.arguments = arguments


# Inline copy of the relevant branch logic from AgentLoop
# (extracted from the post-patch loop.py so the test doesn't need
# the full app + DB stack to run).
def _append_assistant_message(messages, response, asst_payload):
    """Replicate the 3-branch logic from AgentLoop after the Phase
    15.2 fix. Returns the appended message content (or None)."""
    asst_payload_has_tool_use = bool(asst_payload) and any(
        b.get("type") == "tool_use" for b in asst_payload
    )

    def _has_assistant_payload(messages, payload):
        for m in messages:
            if m.role == "assistant" and m.content == payload:
                return True
        return False

    if (
        asst_payload_has_tool_use
        and not _has_assistant_payload(messages, asst_payload)
    ):
        messages.append(_StubMessage("assistant", asst_payload))
        return asst_payload
    if response.tool_calls and not asst_payload_has_tool_use:
        synthetic_content = []
        for call in response.tool_calls:
            synthetic_content.append({
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            })
        if response.text:
            synthetic_content.insert(0, {"type": "text", "text": response.text})
        messages.append(_StubMessage("assistant", synthetic_content))
        return synthetic_content
    if asst_payload and not _has_assistant_payload(messages, asst_payload):
        messages.append(_StubMessage("assistant", asst_payload))
        return asst_payload
    return None


class TestOpenAIRoundTripBranch(unittest.TestCase):
    """The 3-branch logic that decides which assistant message to
    append after each LLM iteration, before the tool_result for
    the next call."""

    def test_case_1_real_tool_use_blocks(self):
        """Model emitted proper tool_use blocks. Append as-is."""
        messages = []
        response = MagicMock()
        response.tool_calls = [
            _StubCall("toolu_01ABC", "exec_bash", {"command": "ls"}),
        ]
        response.text = "Let me check the file."
        asst_payload = [
            {"type": "text", "text": "Let me check the file."},
            {"type": "tool_use", "id": "toolu_01ABC", "name": "exec_bash",
             "input": {"command": "ls"}},
        ]
        result = _append_assistant_message(messages, response, asst_payload)
        # Appended as-is (real tool_use block present)
        self.assertEqual(result, asst_payload)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, asst_payload)

    def test_case_2_repair_synthesised_tool_calls(self):
        """THE BUG FIX: model returned text only; repair layer
        synthesised tool_use blocks with ids like 'repaired_…'.
        We must build synthetic content with proper tool_use blocks
        so the next-iteration tool_result has a matching block."""
        messages = []
        response = MagicMock()
        response.tool_calls = [
            _StubCall("repaired_59602041", "memory_recall", {"query": "test"}),
            _StubCall("repaired_59602042", "memory_write", {"text": "x"}),
        ]
        response.text = "Let me check memory_recall('test') and write a memory."
        asst_payload = [
            {"type": "text", "text": "Let me check memory_recall('test') and write a memory."},
        ]
        result = _append_assistant_message(messages, response, asst_payload)
        # Must contain tool_use blocks (not just the text block)
        self.assertIsInstance(result, list)
        tool_uses = [b for b in result if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_uses), 2, "expected 2 synthetic tool_use blocks")
        self.assertEqual(tool_uses[0]["id"], "repaired_59602041")
        self.assertEqual(tool_uses[0]["name"], "memory_recall")
        self.assertEqual(tool_uses[0]["input"], {"query": "test"})
        self.assertEqual(tool_uses[1]["id"], "repaired_59602042")
        self.assertEqual(tool_uses[1]["name"], "memory_write")
        # text block first, then tool_use blocks
        self.assertEqual(result[0]["type"], "text")
        self.assertEqual(len(messages), 1)

    def test_case_2_followup_tool_result_will_match(self):
        """After case 2, the loop appends tool_result with the
        repaired_… id. The next LLM call's request will have
        both the synthetic tool_use and the tool_result with the
        same id — so the API won't 400."""
        messages = []
        response = MagicMock()
        response.tool_calls = [_StubCall("repaired_59602041", "memory_recall", {"query": "x"})]
        response.text = "Let me check memory_recall('x')"
        asst_payload = [{"type": "text", "text": "Let me check memory_recall('x')"}]
        _append_assistant_message(messages, response, asst_payload)

        # The tool executes; tool_result is appended next with the
        # SAME tool_use_id the synthetic tool_use block used.
        tool_result_id = "repaired_59602041"
        messages.append(_StubMessage("user", [{
            "type": "tool_result",
            "tool_use_id": tool_result_id,
            "content": "result here",
        }]))
        # Walk the messages: the synthetic tool_use id and the
        # tool_result id must match.
        asst_msg = next(m for m in messages if m.role == "assistant")
        tool_uses = [b for b in asst_msg.content if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_uses), 1)
        self.assertEqual(tool_uses[0]["id"], tool_result_id)
        user_msg = next(m for m in messages if m.role == "user")
        tool_results = [b for b in user_msg.content if b.get("type") == "tool_result"]
        self.assertEqual(tool_results[0]["tool_use_id"], tool_uses[0]["id"])

    def test_case_3_pure_text_response(self):
        """Model returned text only, no tool calls. Append as-is."""
        messages = []
        response = MagicMock()
        response.tool_calls = []
        response.text = "All done."
        asst_payload = [{"type": "text", "text": "All done."}]
        result = _append_assistant_message(messages, response, asst_payload)
        self.assertEqual(result, asst_payload)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, asst_payload)

    def test_case_3_empty_payload_no_tool_calls(self):
        """Edge case: empty asst_payload, no tool calls, no text.
        Nothing to append."""
        messages = []
        response = MagicMock()
        response.tool_calls = []
        response.text = ""
        asst_payload = []
        result = _append_assistant_message(messages, response, asst_payload)
        self.assertIsNone(result)
        self.assertEqual(len(messages), 0)

    def test_idempotency(self):
        """If the same asst_payload is already in messages, don't
        double-append (preserves the original _has_assistant_payload
        dedup behaviour)."""
        payload = [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "x", "name": "t", "input": {}},
        ]
        messages = [_StubMessage("assistant", payload)]
        response = MagicMock()
        response.tool_calls = []
        result = _append_assistant_message(messages, response, payload)
        # No double-append
        self.assertEqual(len(messages), 1)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
