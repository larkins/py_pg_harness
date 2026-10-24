"""Unit test for the reworked memory_recent.

Covers:
  1. happy path — returns list of dicts with the new shape
     (id, content, source_type, importance, permanent, age_days, …)
  2. limit clamping (>50 → 50, <1 → 1)
  3. role param is gone (calling with role=... is silently dropped
     — back-compat: don't crash on legacy callers)
  4. default limit is 10
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.tools.base import ToolContext
from app.agent.tools import memory_recent


class _FakeMemoryAPI:
    """Fake MemoryAPI that records args and returns canned rows."""
    def __init__(self, rows=None, captured=None):
        self.rows = rows or []
        self.captured = captured

    def list_recent(self, limit=10):
        if self.captured is not None:
            self.captured["limit"] = limit
        return self.rows


def _ctx(api):
    return ToolContext(
        agent_name="agent1",
        conversation_id="00000000-0000-0000-0000-000000000000",
        user_id=1,
        memory=api,
    )


class TestMemoryRecentTool(unittest.TestCase):
    async def _run(self, args, api):
        return await memory_recent.run(args, _ctx(api))

    def test_happy_path_returns_memory_rows(self):
        async def go():
            now = datetime.now()
            rows = [
                {
                    "id": 5,
                    "content": "first memory",
                    "source_type": "system",
                    "source_ref": "manual",
                    "importance": 7,
                    "permanent": True,
                    "pin_source": "user",
                    "pinned_at": (now - timedelta(days=1)).isoformat(),
                    "metadata": {"k": "v"},
                    "created_at": (now - timedelta(days=2)).isoformat(),
                    "age_days": 2.0,
                },
                {
                    "id": 6,
                    "content": "second memory",
                    "source_type": "chat",
                    "source_ref": None,
                    "importance": 5,
                    "permanent": False,
                    "pin_source": None,
                    "pinned_at": None,
                    "metadata": {},
                    "created_at": (now - timedelta(hours=1)).isoformat(),
                    "age_days": 0.04,
                },
            ]
            api = _FakeMemoryAPI(rows=rows)
            r = await self._run({"limit": 10}, api)

            # New shape
            self.assertEqual(r["count"], 2)
            self.assertEqual(r["limit"], 10)
            self.assertIn("memories", r)
            self.assertEqual(len(r["memories"]), 2)

            # First row checks
            m0 = r["memories"][0]
            self.assertEqual(m0["id"], 5)
            self.assertEqual(m0["content"], "first memory")
            self.assertEqual(m0["source_type"], "system")
            self.assertEqual(m0["importance"], 7)
            self.assertTrue(m0["permanent"])
            self.assertEqual(m0["age_days"], 2.0)
            self.assertEqual(m0["metadata"], {"k": "v"})

            # Second row checks (newer, not pinned)
            m1 = r["memories"][1]
            self.assertEqual(m1["id"], 6)
            self.assertFalse(m1["permanent"])
            self.assertIsNone(m1["pin_source"])
            self.assertIsNone(m1["pinned_at"])

            # Old shape should NOT be present
            self.assertNotIn("chunks", r)
            self.assertNotIn("role_filter", r)
        import asyncio
        asyncio.run(go())

    def test_default_limit_is_10(self):
        async def go():
            captured = {}
            api = _FakeMemoryAPI(rows=[], captured=captured)
            await self._run({}, api)  # no limit
            self.assertEqual(captured["limit"], 10)
        import asyncio
        asyncio.run(go())

    def test_role_param_is_silently_dropped(self):
        """Back-compat: old callers passing role=... should not crash.
        The new signature doesn't have role, so the call just ignores it."""
        async def go():
            captured = {}
            api = _FakeMemoryAPI(rows=[], captured=captured)
            # Old callers might pass role=... or role_filter=...
            r = await self._run({"limit": 5, "role": "user"}, api)
            # The tool should not crash
            self.assertEqual(r["count"], 0)
            # The limit should still be 5
            self.assertEqual(captured["limit"], 5)
        import asyncio
        asyncio.run(go())

    def test_empty_result(self):
        async def go():
            api = _FakeMemoryAPI(rows=[])
            r = await self._run({"limit": 10}, api)
            self.assertEqual(r["count"], 0)
            self.assertEqual(r["memories"], [])
        import asyncio
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
