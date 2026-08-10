"""Unit tests for cleanup_run.py (RUN_ID-scoped; no Docker/mc)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import cleanup_run


class TestValidateRunId(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cleanup_run.validate_run_id("m5live-run1"), "m5live-run1")

    def test_valid_with_dots_dashes(self):
        self.assertEqual(cleanup_run.validate_run_id("m5.live-run_1"), "m5.live-run_1")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("")

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("../etc/hosts")
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("a/../b")

    def test_rejects_slash(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("a/b")

    def test_rejects_backslash(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("a\\b")

    def test_rejects_bad_charset(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("run;rm")

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id("A" * 65)

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            cleanup_run.validate_run_id(None)


class TestBuildPlan(unittest.TestCase):
    def test_scoped_to_run_id(self):
        plan = cleanup_run.build_plan("m5live-run1")
        descs = [t["desc"] for t in plan]
        self.assertEqual(len(plan), 6)
        for d in descs:
            self.assertIn("m5live-run1-", d)

    def test_all_three_stages(self):
        plan = cleanup_run.build_plan("m5live-run1")
        joined = " ".join(t["desc"] for t in plan)
        self.assertIn("review", joined)
        self.assertIn("fix", joined)
        self.assertIn("verify", joined)

    def test_no_broad_glob(self):
        plan = cleanup_run.build_plan("m5live-run1")
        for target in plan:
            argv_str = " ".join(target["argv"])
            self.assertNotIn("prune", argv_str)
            self.assertNotIn("$(docker", argv_str)
            self.assertNotIn("-aq", argv_str)
            if "rm" in target["argv"]:
                self.assertIn("m5live-run1", argv_str)

    def test_paths_precise(self):
        import re
        plan = cleanup_run.build_plan("m5live-run1")
        for target in plan:
            argv_str = " ".join(target["argv"])
            self.assertRegex(
                argv_str, r"m5live-run1-(review|fix|verify)",
                "target not precisely scoped: %s" % argv_str)

    def test_local_mirror_optional(self):
        plan = cleanup_run.build_plan("m5live-run1", include_local_mirror=False)
        kinds = [t["kind"] for t in plan]
        self.assertNotIn("exec", kinds)
        self.assertEqual(len(plan), 3)


class TestExecute(unittest.TestCase):
    def test_dry_run_does_not_call_runner(self):
        plan = cleanup_run.build_plan("m5live-run1")
        calls = []

        def runner(argv):
            calls.append(argv)
            return ""

        results = cleanup_run.execute(plan, runner=runner, apply=False)
        self.assertEqual(calls, [])
        self.assertTrue(all(r["ok"] for r in results))
        self.assertFalse(any(r["applied"] for r in results))

    def test_apply_calls_runner(self):
        plan = cleanup_run.build_plan("m5live-run1")
        calls = []

        def runner(argv):
            calls.append(argv)
            return ""

        cleanup_run.execute(plan, runner=runner, apply=True)
        self.assertEqual(len(calls), len(plan))
        self.assertTrue(all(r["applied"] for r in
                            cleanup_run.execute(plan, runner=runner, apply=True)))


class TestCheckPreconditions(unittest.TestCase):
    """The authoritative fail-closed gate that replaces the text hint."""

    def test_no_callbacks_fail_closed(self):
        ok, reasons = cleanup_run.check_preconditions("m5live-run1")
        self.assertFalse(ok)
        self.assertTrue(any("not wired" in r for r in reasons))

    def test_all_pass(self):
        ok, reasons = cleanup_run.check_preconditions(
            "m5live-run1",
            run_exists_fn=lambda rid: True,
            run_ended_fn=lambda rid: True,
            evidence_verified_fn=lambda rid: True)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_run_not_exists(self):
        ok, reasons = cleanup_run.check_preconditions(
            "m5live-run1",
            run_exists_fn=lambda rid: False,
            run_ended_fn=lambda rid: True,
            evidence_verified_fn=lambda rid: True)
        self.assertFalse(ok)
        self.assertTrue(any("not confirmed in authoritative" in r for r in reasons))

    def test_run_not_ended(self):
        ok, reasons = cleanup_run.check_preconditions(
            "m5live-run1",
            run_exists_fn=lambda rid: True,
            run_ended_fn=lambda rid: False,
            evidence_verified_fn=lambda rid: True)
        self.assertFalse(ok)
        self.assertTrue(any("not confirmed ended" in r for r in reasons))

    def test_evidence_not_verified(self):
        ok, reasons = cleanup_run.check_preconditions(
            "m5live-run1",
            run_exists_fn=lambda rid: True,
            run_ended_fn=lambda rid: True,
            evidence_verified_fn=lambda rid: False)
        self.assertFalse(ok)
        self.assertTrue(any("source_commit/binding" in r for r in reasons))

    def test_none_result_fail_closed(self):
        """Cannot determine (None) must be treated as fail-closed."""
        ok, _ = cleanup_run.check_preconditions(
            "m5live-run1",
            run_exists_fn=lambda rid: None,
            run_ended_fn=lambda rid: True,
            evidence_verified_fn=lambda rid: True)
        self.assertFalse(ok)


class TestApplyFailClosed(unittest.TestCase):
    """main(['--apply']) must be fail-closed: status 3, no plan, no deletion,
    no traceback."""

    def _run_main(self, argv):
        import io
        old_err = sys.stderr
        old_out = sys.stdout
        sys.stderr = io.StringIO()
        sys.stdout = io.StringIO()
        try:
            rc = cleanup_run.main(argv)
            err = sys.stderr.getvalue()
            out = sys.stdout.getvalue()
        finally:
            sys.stderr = old_err
            sys.stdout = old_out
        return rc, err, out

    def test_apply_returns_stable_nonzero(self):
        rc, _err, _out = self._run_main(["m5live-run1", "--apply"])
        self.assertEqual(rc, 3)

    def test_apply_clear_message(self):
        rc, err, _out = self._run_main(["m5live-run1", "--apply"])
        self.assertEqual(rc, 3)
        self.assertIn("not supported", err)
        self.assertIn("fail-closed", err)

    def test_apply_no_traceback(self):
        # rc==3 + a clean message proves main did not raise. If it had raised,
        # _run_main would propagate the exception (test failure), not return 3.
        rc, _err, _out = self._run_main(["m5live-run1", "--apply"])
        self.assertEqual(rc, 3)

    def test_apply_does_not_build_plan_or_delete(self):
        """--apply must short-circuit before build_plan; no mc/docker called."""
        original_build = cleanup_run.build_plan
        original_execute = cleanup_run.execute
        build_calls = {"n": 0}
        execute_calls = {"n": 0}

        def tracking_build(*a, **kw):
            build_calls["n"] += 1
            return original_build(*a, **kw)

        def tracking_execute(*a, **kw):
            execute_calls["n"] += 1
            return original_execute(*a, **kw)

        cleanup_run.build_plan = tracking_build
        cleanup_run.execute = tracking_execute
        try:
            rc, _err, _out = self._run_main(["m5live-run1", "--apply"])
        finally:
            cleanup_run.build_plan = original_build
            cleanup_run.execute = original_execute
        self.assertEqual(rc, 3)
        self.assertEqual(build_calls["n"], 0)  # no plan built
        self.assertEqual(execute_calls["n"], 0)  # no execution path entered

    def test_apply_missing_run_id_rejected(self):
        rc, err, _out = self._run_main(["--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_apply_invalid_run_id_rejected(self):
        rc, err, _out = self._run_main(["bad/run", "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("invalid RUN_ID", err)

    def test_apply_traversal_run_id_rejected(self):
        rc, _err, _out = self._run_main(["../etc/hosts", "--apply"])
        self.assertEqual(rc, 2)

    def test_dry_run_unchanged(self):
        """Dry-run (no --apply) still builds a plan and prints; no deletion."""
        original_build = cleanup_run.build_plan
        build_calls = {"n": 0}

        def tracking_build(*a, **kw):
            build_calls["n"] += 1
            return original_build(*a, **kw)

        cleanup_run.build_plan = tracking_build
        try:
            rc, _err, out = self._run_main(["m5live-run1"])
        finally:
            cleanup_run.build_plan = original_build
        self.assertEqual(rc, 0)
        self.assertEqual(build_calls["n"], 1)  # plan WAS built for dry-run
        self.assertIn("DRY-RUN", out)
        self.assertIn("[dry-run]", out)
        self.assertIn("m5live-run1-review", out)


if __name__ == "__main__":
    unittest.main()
