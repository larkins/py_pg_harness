"""Unit tests for the nightly reverie (2026-10-23).

Covers the pure logic:

  1. _writes_to  — the protected-file check (a WRITE to SOUL.md is
     refused; merely reading it or mentioning it in a heredoc is not)
  2. build_brief — the reverie prompt carries the current usage, the
     targets, the protected-file rule and the "end with proposals"
     instruction
  3. activity_count / plan_reverie — the "no turns ran in a day, don't
     fire" rule (reverie turns do not count as activity)
  4. optional_prompt_file — the optional per-agent REVERIE.md

No DB, no app stack.
"""
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

from app.agent.bootstrap import BootstrapLoader
from app.agent.reverie import (
    REVERIE_TRAJECTORY_KIND,
    build_brief,
    optional_prompt_file,
    reverie_trajectory,
)
from app.agent.tools.exec_bash import _writes_to


class _FakeBootstrap:
    def __init__(self, usage, root=str(Path(__file__).resolve().parents[1] / "pie" / "agent" / "identity"), files=None):
        self._usage = usage
        self.workspace_root = root
        self._files = files or {}

    def file_usage(self):
        return self._usage


class _FakeMemory:
    def __init__(self, usage):
        self._usage = usage

    def permanent_usage(self):
        return self._usage


class _FakeCfg:
    class reverie:  # noqa: N801 - mimics the frozen dataclass
        enabled = True
        agents = ()
        at = "03:00"
        timezone = "Australia/Brisbane"
        file_target_pct = 0.60
        memory_target_pct = 0.60
        activity_window_hours = 24
        user_email = "payroll@example.com"
        prompt_file = "REVERIE.md"
        protected_files = ("SOUL.md",)


class TestWritesTo(unittest.TestCase):
    """The protected-file guard: writes are blocked, mentions are not."""

    def test_redirect_to_protected_file(self):
        self.assertTrue(_writes_to("cat > /x/SOUL.md <<'EOF'", "SOUL.md"))
        self.assertTrue(_writes_to("echo hi >> SOUL.md", "SOUL.md"))
        self.assertTrue(_writes_to("echo hi >'SOUL.md'", "SOUL.md"))

    def test_write_verbs(self):
        self.assertTrue(_writes_to("tee SOUL.md", "SOUL.md"))
        self.assertTrue(_writes_to("sed -i 's/a/b/' SOUL.md", "SOUL.md"))
        self.assertTrue(_writes_to("mv notes.md SOUL.md", "SOUL.md"))
        self.assertTrue(_writes_to("rm -f SOUL.md", "SOUL.md"))
        self.assertTrue(_writes_to("cp a.md /dir/SOUL.md", "SOUL.md"))

    def test_plain_sed_is_not_a_write(self):
        self.assertFalse(_writes_to("sed 's/a/b/' SOUL.md", "SOUL.md"))

    def test_reading_is_allowed(self):
        self.assertFalse(_writes_to("cat SOUL.md", "SOUL.md"))
        self.assertFalse(_writes_to("grep -n rule SOUL.md", "SOUL.md"))
        self.assertFalse(_writes_to("wc -c SOUL.md", "SOUL.md"))

    def test_mention_in_another_files_heredoc_is_not_a_write(self):
        # The false positive that made the reverie agent route around a
        # blunt guard: a heredoc for an unrelated doc that mentions the
        # protected file in its body.
        cmd = (
            "cat > /tmp/memory_system.md <<'EOF'\n"
            "SOUL.md holds the rules and is never edited by the reverie.\n"
            "EOF"
        )
        self.assertFalse(_writes_to(cmd, "SOUL.md"))

    def test_python_open_write_mode(self):
        self.assertTrue(
            _writes_to("python3 -c \"open('SOUL.md','w').write('x')\"", "SOUL.md")
        )
        self.assertFalse(
            _writes_to("python3 -c \"print(open('SOUL.md').read())\"", "SOUL.md")
        )

    def test_other_file_is_not_protected(self):
        self.assertFalse(_writes_to("cat > AGENTS.md", "SOUL.md"))


class TestBuildBrief(unittest.TestCase):
    def _brief(self, usage=None, pm=None):
        usage = usage or [
            {"name": "SOUL.md", "bytes": 3955, "cap": 8000, "pct": 0.494},
            {"name": "USER.md", "bytes": 938, "cap": 4000, "pct": 0.234},
            {"name": "AGENTS.md", "bytes": 20323, "cap": 24000, "pct": 0.847},
        ]
        pm = pm or {
            "count": 34, "total_tokens": 2592, "used_tokens": 2592,
            "dropped": 0, "cap": 10000, "overflowed": False,
        }
        return build_brief(
            "agent2", _FakeCfg(), _FakeBootstrap(usage), _FakeMemory(pm)
        )

    def test_brief_is_tagged(self):
        self.assertTrue(self._brief().startswith("<reverie"))

    def test_over_target_file_is_flagged_with_the_cut(self):
        brief = self._brief()
        self.assertIn("AGENTS.md", brief)
        self.assertIn("OVER TARGET", brief)
        # 20323 - 60% of 24000 = 5923 bytes to cut
        self.assertIn("5,923", brief)

    def test_under_target_file_is_ok(self):
        brief = self._brief()
        self.assertIn("USER.md", brief)
        self.assertIn("ok (target <= 60%)", brief)

    def test_protected_file_is_marked_protected(self):
        brief = self._brief()
        self.assertIn("PROTECTED - do not edit", brief)
        # and it must not be listed as over-target / cuttable
        self.assertNotIn("SOUL.md", "\n".join(
            line for line in brief.splitlines() if "OVER TARGET" in line
        ))

    def test_protected_file_rule_is_in_the_instructions(self):
        brief = self._brief()
        self.assertIn("SOUL.md IS PROTECTED", brief)
        self.assertIn("explicit permission", brief)

    def test_memory_target_is_stated(self):
        brief = self._brief()
        self.assertIn("permanent mem", brief)
        self.assertIn("6,000 tokens or less", brief)

    def test_proposals_instruction_present(self):
        brief = self._brief()
        self.assertIn("PROPOSALS", brief)
        self.assertIn("REFLECTION AND PLANNING", brief)

    def test_memory_over_target(self):
        pm = {
            "count": 40, "total_tokens": 9500, "used_tokens": 8000,
            "dropped": 6, "cap": 10000, "overflowed": True,
        }
        brief = self._brief(pm=pm)
        self.assertIn("OVER TARGET", brief)
        self.assertIn("DROPPED from your prompt", brief)


class TestTrajectory(unittest.TestCase):
    def test_marker_shape(self):
        from datetime import datetime, timezone
        t = reverie_trajectory("agent2", datetime.now(timezone.utc))
        self.assertEqual(t["kind"], REVERIE_TRAJECTORY_KIND)
        self.assertEqual(t["agent"], "agent2")
        self.assertIn("started_at", t)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = ""
        self.args = ()

    def execute(self, sql, args=()):
        self.sql = sql
        self.args = args

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    """Minimal pool stand-in: connection() -> conn, conn.cursor() -> cur."""

    def __init__(self, rows):
        self._rows = rows
        self.last_sql = ""

    def connection(self):
        pool = self

        class _Conn:
            def cursor(self_inner):
                cur = _FakeCursor(pool._rows)

                class _CurCtx:
                    def __enter__(self_inner2):
                        return cur

                    def __exit__(self_inner2, *a):
                        return False

                return _CurCtx()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _Conn()


class TestActivityAndPlan(unittest.TestCase):
    """The "if no turns ran in a day, don't fire" rule."""

    def test_activity_count_reads_the_turns_table(self):
        from app.agent.reverie import activity_count
        pool = _FakePool([(7,)])
        self.assertEqual(activity_count(pool, "agent2", 24), 7)

    def test_activity_count_rejects_a_bad_agent_name(self):
        from app.agent.reverie import activity_count
        pool = _FakePool([(1,)])
        with self.assertRaises(ValueError):
            activity_count(pool, "agent2; DROP TABLE turns", 24)

    def test_plan_skips_when_there_is_no_activity(self):
        from app.agent.reverie import plan_reverie
        pool = _FakePool([(0,)])
        plan = plan_reverie(pool=pool, cfg=_FakeCfg(), agent_name="agent2")
        self.assertFalse(plan["should_run"])
        self.assertIn("no activity", plan["reason"])

    def test_plan_runs_when_there_is_activity(self):
        from app.agent.reverie import plan_reverie
        pool = _FakePool([(3,)])
        plan = plan_reverie(pool=pool, cfg=_FakeCfg(), agent_name="agent2")
        self.assertTrue(plan["should_run"])
        self.assertEqual(plan["activity"], 3)

    def test_force_overrides_the_activity_rule(self):
        from app.agent.reverie import plan_reverie
        pool = _FakePool([(0,)])
        plan = plan_reverie(pool=pool, cfg=_FakeCfg(), agent_name="agent2", force=True)
        self.assertTrue(plan["should_run"])

    def test_disabled_config_skips(self):
        from app.agent.reverie import plan_reverie

        class _Off:
            class reverie:  # noqa: N801
                enabled = False
                activity_window_hours = 24

        pool = _FakePool([(9,)])
        plan = plan_reverie(pool=pool, cfg=_Off(), agent_name="agent2")
        self.assertFalse(plan["should_run"])
        self.assertIn("disabled", plan["reason"])


class TestBootstrapRefresh(unittest.TestCase):
    """The identity files are re-read per turn, not cached at boot."""

    def _loader(self, tmpdir, file_map, caps):
        # Built OUTSIDE the class body: a comprehension inside a class
        # body would resolve `files` to the class attribute being
        # assigned, not to the argument.
        file_cfgs = {
            name: type("F", (), {"path": p, "max_bytes": caps[name]})()
            for name, p in file_map.items()
        }

        class _Cfg:
            class bootstrap:  # noqa: N801
                behavior_on_overflow = "fail"
                files = file_cfgs

        return BootstrapLoader(_Cfg(), workspace_root=tmpdir)

    def test_refresh_picks_up_an_edit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "AGENTS.md")
            with open(p, "w") as fh:
                fh.write("first version")
            loader = self._loader(d, {"AGENTS.md": "AGENTS.md"}, {"AGENTS.md": 1000})
            self.assertIn("first version", loader.render())
            with open(p, "w") as fh:
                fh.write("second version, edited by the agent")
            # No restart, no reload() call - just the next get().
            loader.refresh()
            self.assertIn("second version", loader.render())
            self.assertNotIn("first version", loader.render())

    def test_over_cap_file_keeps_the_last_good_content(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "AGENTS.md")
            with open(p, "w") as fh:
                fh.write("good content")
            loader = self._loader(d, {"AGENTS.md": "AGENTS.md"}, {"AGENTS.md": 20})
            with open(p, "w") as fh:
                fh.write("x" * 500)   # over the cap
            loader.refresh()
            # The bad edit does NOT break the turn: the last good
            # content is still served (and the error is logged).
            self.assertIn("good content", loader.render())

    def test_deleted_file_leaves_the_prompt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "AGENTS.md")
            with open(p, "w") as fh:
                fh.write("content")
            loader = self._loader(d, {"AGENTS.md": "AGENTS.md"}, {"AGENTS.md": 1000})
            os.remove(p)
            loader.refresh()
            self.assertNotIn("content", loader.render())


class TestOptionalPromptFile(unittest.TestCase):
    def test_missing_file_is_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            b = MagicMock()
            b.workspace_root = d
            self.assertEqual(optional_prompt_file(b, "REVERIE.md"), "")

    def test_present_file_is_returned(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "REVERIE.md")
            with open(p, "w") as fh:
                fh.write("always check the payroll docs first\n")
            b = MagicMock()
            b.workspace_root = d
            self.assertIn("payroll docs", optional_prompt_file(b, "REVERIE.md"))


if __name__ == "__main__":
    unittest.main()
