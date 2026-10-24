"""Unit test for the memory_delete tool + MemoryAPI.delete_memory.

Covers:
  1. happy path — existing memory, valid reason → returns {deleted: True, content_preview, ...}
  2. memory_id is missing → returns error dict, no DB write
  3. memory doesn't exist → returns LookupError-shaped error
  4. empty reason is treated as None
  5. DB exception is caught and surfaced

Stubs the LLM-side context: a fake MemoryAPI with the methods the
tool uses (delete_memory). No real DB. Mirrors the style of
tests/test_loop_repair.py so it runs without the full app stack.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.tools.base import ToolContext
from app.agent.tools import memory_delete


class _FakeMemoryAPI:
    """Mimic MemoryAPI for the test. The tool only calls
    delete_memory, so we only need to stub that one method."""
    def __init__(self, *, fail_with=None, side_effect=None):
        self._fail_with = fail_with
        self._side_effect = side_effect
        self.calls = []

    def delete_memory(self, memory_id, reason=None, source="model"):
        self.calls.append({
            "memory_id": memory_id,
            "reason": reason,
            "source": source,
        })
        if self._side_effect is not None:
            raise self._side_effect
        if self._fail_with is not None:
            raise self._fail_with
        return {
            "memory_id": memory_id,
            "deleted": True,
            "was_permanent": False,
            "content_preview": "memory content preview…",
            "reason": reason,
        }


def _ctx(api: _FakeMemoryAPI) -> ToolContext:
    return ToolContext(
        agent_name="agent1",
        conversation_id="00000000-0000-0000-0000-000000000000",
        user_id=1,
        memory=api,
    )


class TestMemoryDeleteTool(unittest.TestCase):
    async def _run(self, args, api):
        ctx = _ctx(api)
        return await memory_delete.run(args, ctx)

    def test_happy_path(self):
        async def go():
            api = _FakeMemoryAPI()
            r = await self._run(
                {"memory_id": 42, "reason": "duplicate of memory_id=37"},
                api,
            )
            self.assertEqual(r["memory_id"], 42)
            self.assertTrue(r["deleted"])
            self.assertEqual(r["reason"], "duplicate of memory_id=37")
            self.assertEqual(api.calls[0]["source"], "model")
            self.assertEqual(api.calls[0]["memory_id"], 42)
        import asyncio
        asyncio.run(go())

    def test_missing_memory_id(self):
        async def go():
            api = _FakeMemoryAPI()
            r = await self._run({"reason": "x"}, api)
            self.assertIn("error", r)
            self.assertIn("memory_id is required", r["error"])
            # no DB call
            self.assertEqual(len(api.calls), 0)
        import asyncio
        asyncio.run(go())

    def test_memory_not_found(self):
        async def go():
            api = _FakeMemoryAPI(fail_with=LookupError("memory 999 not found in agent1"))
            r = await self._run({"memory_id": 999}, api)
            self.assertIn("error", r)
            self.assertIn("memory 999 not found", r["error"])
            self.assertEqual(r["memory_id"], 999)
            self.assertFalse(r["deleted"])
        import asyncio
        asyncio.run(go())

    def test_empty_reason_treated_as_none(self):
        async def go():
            api = _FakeMemoryAPI()
            r = await self._run(
                {"memory_id": 5, "reason": "   "},
                api,
            )
            self.assertEqual(r["memory_id"], 5)
            # reason was stripped and passed as None
            self.assertIsNone(api.calls[0]["reason"])
        import asyncio
        asyncio.run(go())

    def test_no_reason_at_all(self):
        async def go():
            api = _FakeMemoryAPI()
            r = await self._run({"memory_id": 5}, api)
            self.assertTrue(r["deleted"])
            self.assertIsNone(api.calls[0]["reason"])
        import asyncio
        asyncio.run(go())

    def test_db_exception_caught(self):
        async def go():
            api = _FakeMemoryAPI(side_effect=RuntimeError("connection refused"))
            r = await self._run({"memory_id": 1}, api)
            self.assertIn("error", r)
            self.assertIn("delete failed", r["error"])
            self.assertIn("connection refused", r["error"])
            self.assertEqual(r["memory_id"], 1)
            self.assertFalse(r["deleted"])
        import asyncio
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
