"""Unit tests for minio_cleanup.py (fail-closed + digest + preconditions)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import minio_cleanup as mc


class TestIsAllowedTarget(unittest.TestCase):
    def test_allows_codex_tmp(self):
        self.assertTrue(mc.is_allowed_target(
            "agents/reviewer/.codex/tmp/junk.tmp")[0])

    def test_allows_npm_npx(self):
        self.assertTrue(mc.is_allowed_target(
            "manager/.npm/_npx/pkg/x")[0])

    def test_rejects_soul(self):
        self.assertFalse(mc.is_allowed_target("agents/r/SOUL.md")[0])

    def test_rejects_skills(self):
        self.assertFalse(mc.is_allowed_target("agents/r/skills/s.py")[0])

    def test_rejects_config(self):
        self.assertFalse(mc.is_allowed_target("agents/r/config/mcporter.json")[0])

    def test_rejects_sessions(self):
        self.assertFalse(mc.is_allowed_target(
            "agents/r/.openclaw/agents/main/sessions/a.jsonl")[0])

    def test_rejects_shared_tasks(self):
        self.assertFalse(mc.is_allowed_target(
            "shared/tasks/m5live-run1-review/findings.md")[0])

    def test_rejects_minio_sys(self):
        self.assertFalse(mc.is_allowed_target(".minio.sys/multipart/abc")[0])

    def test_rejects_out_of_scope(self):
        self.assertFalse(mc.is_allowed_target("agents/r/other/file")[0])

    def test_rejects_empty(self):
        self.assertFalse(mc.is_allowed_target("")[0])

    def test_soul_under_allowed_prefix_denied(self):
        self.assertFalse(mc.is_allowed_target(
            "agents/r/.codex/tmp/SOUL.md")[0])


class TestEnumerateFailClosed(unittest.TestCase):
    def test_mc_failure_raises_not_empty(self):
        """Enumerate errors MUST raise, never return []."""
        def runner(argv, env=None):
            raise mc.McError("mc: connection refused")
        with self.assertRaises(mc.McError):
            mc.enumerate_prefix("local", "hiclaw-storage",
                                "agents/r/.codex/tmp/", runner)

    def test_nonzero_rc_raises(self):
        def runner(argv, env=None):
            raise mc.McError("rc=1")
        with self.assertRaises(mc.McError):
            mc.enumerate_prefix("local", "b", "p/", runner)

    def test_success_returns_keys(self):
        def runner(argv, env=None):
            return ("local/b/agents/r/.codex/tmp/a.tmp\n"
                    "local/b/agents/r/.codex/tmp/b.tmp\n")
        keys = mc.enumerate_prefix("local", "b", "agents/r/.codex/tmp/", runner)
        self.assertEqual(len(keys), 2)

    def test_build_plan_aborts_on_enumeration_failure(self):
        def runner(argv, env=None):
            raise mc.McError("no mc")
        with self.assertRaises(mc.McError):
            mc.build_plan("local", "b", ("r",), runner=runner)


class TestPlanDigest(unittest.TestCase):
    def test_digest_stable(self):
        def runner(argv, env=None):
            return "local/b/agents/r/.codex/tmp/a.tmp\n"
        plan = mc.build_plan("local", "b", ("r",), runner=runner)
        d1 = mc.compute_plan_digest(plan)
        d2 = mc.compute_plan_digest(plan)
        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 64)  # sha256 hex

    def test_digest_changes_with_plan(self):
        def runner1(argv, env=None):
            return "local/b/agents/r/.codex/tmp/a.tmp\n"
        def runner2(argv, env=None):
            return "local/b/agents/r/.codex/tmp/a.tmp\nlocal/b/agents/r/.codex/tmp/b.tmp\n"
        d1 = mc.compute_plan_digest(
            mc.build_plan("local", "b", ("r",), runner=runner1))
        d2 = mc.compute_plan_digest(
            mc.build_plan("local", "b", ("r",), runner=runner2))
        self.assertNotEqual(d1, d2)

    def test_digest_drift_aborts_apply(self):
        def runner(argv, env=None):
            return ""
        plan = mc.build_plan("local", "b", ("r",), runner=runner)
        with self.assertRaises(ValueError):
            mc.execute(plan, runner=runner, apply=True,
                       expected_digest="0" * 64,
                       is_idle_fn=lambda: True,
                       no_recent_uploads_fn=lambda h: True)


class TestPreconditions(unittest.TestCase):
    def test_no_callbacks_fail_closed(self):
        ok, reasons = mc.check_preconditions()
        self.assertFalse(ok)
        self.assertTrue(any("not provided" in r for r in reasons))

    def test_not_idle_aborts(self):
        ok, reasons = mc.check_preconditions(
            is_idle_fn=lambda: False,
            no_recent_uploads_fn=lambda h: True)
        self.assertFalse(ok)

    def test_recent_uploads_aborts(self):
        ok, reasons = mc.check_preconditions(
            is_idle_fn=lambda: True,
            no_recent_uploads_fn=lambda h: False)
        self.assertFalse(ok)

    def test_none_result_fail_closed(self):
        ok, _r = mc.check_preconditions(
            is_idle_fn=lambda: None,
            no_recent_uploads_fn=lambda h: True)
        self.assertFalse(ok)
        ok, _r = mc.check_preconditions(
            is_idle_fn=lambda: True,
            no_recent_uploads_fn=lambda h: None)
        self.assertFalse(ok)

    def test_all_pass(self):
        ok, reasons = mc.check_preconditions(
            is_idle_fn=lambda: True,
            no_recent_uploads_fn=lambda h: True)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])


class TestExecute(unittest.TestCase):
    def _plan(self):
        return [mc.plan_multipart("local", "b")]

    def test_default_dry_run_no_execution(self):
        calls = []

        def runner(argv, env=None):
            calls.append(argv)
            return ""
        results = mc.execute(self._plan(), runner=runner, apply=False)
        self.assertEqual(calls, [])
        self.assertTrue(all(r["ok"] for r in results))

    def test_apply_requires_preconditions(self):
        def runner(argv, env=None):
            return ""
        # No preconditions -> raises
        with self.assertRaises(ValueError):
            mc.execute(self._plan(), runner=runner, apply=True)

    def test_apply_with_passing_preconditions(self):
        calls = []

        def runner(argv, env=None):
            calls.append(argv)
            return ""
        mc.execute(self._plan(), runner=runner, apply=True,
                   is_idle_fn=lambda: True,
                   no_recent_uploads_fn=lambda h: True)
        self.assertEqual(len(calls), 1)

    def test_apply_aborts_if_not_idle(self):
        calls = []

        def runner(argv, env=None):
            calls.append(argv)
            return ""
        with self.assertRaises(ValueError):
            mc.execute(self._plan(), runner=runner, apply=True,
                       is_idle_fn=lambda: False,
                       no_recent_uploads_fn=lambda h: True)
        self.assertEqual(calls, [])  # nothing executed


class TestPlanMultipart(unittest.TestCase):
    def test_uses_mc_rm_incomplete_recursive(self):
        t = mc.plan_multipart("local", "b")
        self.assertEqual(t["argv"][0], "mc")
        self.assertIn("--incomplete", t["argv"])
        self.assertIn("--recursive", t["argv"])
        self.assertIn("--force", t["argv"])


class TestApplyFailClosed(unittest.TestCase):
    """--apply must fail-closed: stable status 3, clear message, no deletion,
    no traceback (no authoritative probe exists in this candidate)."""

    def _run_main_apply(self):
        import io
        old_err = sys.stderr
        old_out = sys.stdout
        sys.stderr = io.StringIO()
        sys.stdout = io.StringIO()
        try:
            rc = mc.main(["--apply"])
            err = sys.stderr.getvalue()
            _out = sys.stdout.getvalue()
        finally:
            sys.stderr = old_err
            sys.stdout = old_out
        return rc, err

    def test_returns_stable_nonzero(self):
        rc, _err = self._run_main_apply()
        self.assertEqual(rc, 3)

    def test_clear_message_no_traceback(self):
        # If main raised, the assertion above would surface an exception;
        # rc==3 + the message proves a clean fail-closed (no traceback).
        rc, err = self._run_main_apply()
        self.assertEqual(rc, 3)
        self.assertIn("not supported", err)
        self.assertIn("fail-closed", err)

    def test_apply_does_not_execute_any_rm(self):
        """--apply short-circuits before build_plan, so no mc rm is built."""
        import minio_cleanup as mod
        original_build = mod.build_plan
        called = {"n": 0}

        def tracking_build(*a, **kw):
            called["n"] += 1
            return original_build(*a, **kw)

        mod.build_plan = tracking_build
        try:
            rc, _err = self._run_main_apply()
        finally:
            mod.build_plan = original_build
        self.assertEqual(rc, 3)
        self.assertEqual(called["n"], 0)  # build_plan never called -> no plan


if __name__ == "__main__":
    unittest.main()
