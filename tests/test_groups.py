"""Unit tests for the group chat feature (added 2026-10-23).

Covers the pure logic that decides WHO answers in a group chat:

  1. parse_mentions  — which agents a message addresses
  2. render_entry    — how a transcript line is rendered into the
                       LLM prompt (speaker attribution)
  3. build_context   — the split of the transcript around the message
                       being answered, so the second agent sees the
                       first agent's reply

No DB, no app stack -- same style as tests/test_loop_repair.py.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.group_chat import (
    build_context,
    parse_mentions,
    render_entry,
)


class TestParseMentions(unittest.TestCase):
    AGENTS = ["agent1", "agent2", "agent3"]

    def test_single_mention(self):
        matched, unknown = parse_mentions("@agent1 please update the repos", self.AGENTS)
        self.assertEqual(matched, ["agent1"])
        self.assertEqual(unknown, [])

    def test_multiple_mentions_both_answer(self):
        matched, unknown = parse_mentions(
            "@agent1 @agent3, please update your git repos", self.AGENTS
        )
        self.assertEqual(matched, ["agent1", "agent3"])
        self.assertEqual(unknown, [])

    def test_mention_order_is_preserved(self):
        matched, _ = parse_mentions("@agent3 @agent1 @agent2 go", self.AGENTS)
        self.assertEqual(matched, ["agent3", "agent1", "agent2"])

    def test_duplicate_mention_only_one_turn(self):
        matched, _ = parse_mentions("@agent1 do it @agent1 and again", self.AGENTS)
        self.assertEqual(matched, ["agent1"])

    def test_case_insensitive(self):
        matched, _ = parse_mentions("@Agent1 hello", self.AGENTS)
        self.assertEqual(matched, ["agent1"])

    def test_no_mention_means_no_agent(self):
        matched, unknown = parse_mentions("just a normal message", self.AGENTS)
        self.assertEqual(matched, [])
        self.assertEqual(unknown, [])

    def test_unknown_mention_reported(self):
        matched, unknown = parse_mentions("@nosuchagent hello @agent1", self.AGENTS)
        self.assertEqual(matched, ["agent1"])
        self.assertEqual(unknown, ["nosuchagent"])

    def test_agent_not_in_roster_is_unknown(self):
        matched, unknown = parse_mentions("@agent3 hi", ["agent1", "agent2"])
        self.assertEqual(matched, [])
        self.assertEqual(unknown, ["agent3"])

    def test_email_address_is_not_a_mention(self):
        matched, unknown = parse_mentions("mail me at bob@example.com", self.AGENTS)
        self.assertEqual(matched, [])
        self.assertEqual(unknown, [])

    def test_at_all_expands_to_roster(self):
        matched, _ = parse_mentions("@all status please", self.AGENTS)
        self.assertEqual(matched, ["agent1", "agent2", "agent3"])

    def test_mention_in_the_middle(self):
        matched, _ = parse_mentions("hey @agent1 what about the repos?", self.AGENTS)
        self.assertEqual(matched, ["agent1"])

    def test_underscore_and_dash_in_name(self):
        matched, _ = parse_mentions("@some_agent-x hi", ["some_agent-x"])
        self.assertEqual(matched, ["some_agent-x"])


class TestRenderEntry(unittest.TestCase):
    def test_own_past_reply_is_an_assistant_message(self):
        out = render_entry(
            {"author_type": "agent", "author_name": "agent1", "content": "done"}, "agent1"
        )
        self.assertEqual(out, {"role": "assistant", "content": "done"})

    def test_other_agent_is_labelled(self):
        out = render_entry(
            {"author_type": "agent", "author_name": "agent3", "content": "done"}, "agent1"
        )
        self.assertEqual(out["role"], "user")
        self.assertIn("agent3", out["content"])

    def test_human_is_labelled(self):
        out = render_entry(
            {"author_type": "user", "author_name": "the operator", "content": "hello"}, "agent1"
        )
        self.assertEqual(out["role"], "user")
        self.assertIn("the operator", out["content"])

    def test_system_is_labelled(self):
        out = render_entry(
            {"author_type": "system", "author_name": "", "content": "notice"}, "agent1"
        )
        self.assertEqual(out["role"], "user")
        self.assertIn("system", out["content"])


class TestBuildContext(unittest.TestCase):
    def _msgs(self):
        return [
            {"id": 1, "author_type": "user", "author_name": "the operator", "content": "older"},
            {"id": 2, "author_type": "user", "author_name": "the operator", "content": "current"},
            {"id": 3, "author_type": "agent", "author_name": "agent1", "content": "first reply"},
        ]

    def test_split_around_the_current_message(self):
        before, after = build_context(self._msgs(), 2, "agent3")
        self.assertEqual(len(before), 1)
        self.assertIn("older", before[0]["content"])
        self.assertEqual(len(after), 1)
        self.assertIn("first reply", after[0]["content"])

    def test_second_agent_sees_the_first_agents_reply_as_context(self):
        """agent3 is the second agent: agent1's reply must not be rendered
        as agent3's own output."""
        before, after = build_context(self._msgs(), 2, "agent3")
        self.assertEqual(after[0]["role"], "user")
        self.assertIn("agent1", after[0]["content"])

    def test_first_agent_sees_nothing_after(self):
        # The transcript is re-read before each agent's turn, so when
        # the FIRST agent runs, nobody has replied to this message yet.
        before, after = build_context(self._msgs()[:2], 2, "agent1")
        self.assertEqual(after, [])
        self.assertEqual(before[0]["role"], "user")

    def test_own_reply_in_the_past_is_an_assistant_message(self):
        msgs = [
            {"id": 1, "author_type": "agent", "author_name": "agent1", "content": "mine"},
            {"id": 2, "author_type": "user", "author_name": "the operator", "content": "current"},
        ]
        before, _after = build_context(msgs, 2, "agent1")
        self.assertEqual(before[0]["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
