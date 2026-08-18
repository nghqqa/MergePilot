#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8-A2-c — producer timeout reconciliation contract tests.

Verifies reconcile_m4f_producer_timeout() and the late-recovery CAS
(_recover_producer_timeout_hold, wired into the drain success path)
against production SQL: predicates are asserted from the SQL actually
handed to the cursor at runtime, never from source-string greps alone.
No Docker, no network, no real database; mocks never pre-fabricate the
expected transition — they only deliver rows/rowcounts, and every state
change must be justified by the captured production SQL.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(ROOT / "tools" / "workflow-controller"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import controller as ctrl  # noqa: E402

CTRL_DIR = ROOT / "tools" / "workflow-controller"
CTRL_SOURCE = (CTRL_DIR / "controller.py").read_text(encoding="utf-8")

_RUN = "m5live-timeout-001"
_ROOM = "!room:matrix-local.hiclaw.io:18080"
_ROW = (_RUN, _ROOM, "test/repo", 42, 900)


def _sql(cursor, index):
    """SQL text of the index-th execute call (whitespace-normalized)."""
    return " ".join(str(cursor.execute.call_args_list[index][0][0]).split())


def _params(cursor, index):
    return cursor.execute.call_args_list[index][0][1]


class _ReconcileHarness(unittest.TestCase):
    """Runs production reconcile_m4f_producer_timeout with a mock conn."""

    def _reconcile(self, waiting=(_ROW,), rowcount=1, timeout=300,
                   only_mode=True, prefix="m5live-"):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.fetchall.return_value = list(waiting)
        cursor.rowcount = rowcount
        conn.cursor.return_value = cursor
        buf = io.StringIO()
        with patch.object(ctrl, 'ensure_pg', return_value=conn), \
             patch.object(ctrl, 'M4F_ONLY_MODE', only_mode), \
             patch.object(ctrl, 'M4F_RUN_PREFIX', prefix), \
             patch.object(ctrl, 'M4F_PRODUCER_TIMEOUT_SECONDS', timeout), \
             redirect_stdout(buf):
            held = ctrl.reconcile_m4f_producer_timeout()
        return held, cursor, buf.getvalue()


class TestProducerTimeoutConfig(unittest.TestCase):

    def test_disabled_by_default_is_full_noop(self):
        """timeout=0 must not touch the database at all."""
        with patch.object(ctrl, 'M4F_ONLY_MODE', True), \
             patch.object(ctrl, 'M4F_RUN_PREFIX', 'm5live-'), \
             patch.object(ctrl, 'M4F_PRODUCER_TIMEOUT_SECONDS', 0), \
             patch.object(ctrl, 'ensure_pg') as pg:
            self.assertEqual(ctrl.reconcile_m4f_producer_timeout(), 0)
            pg.assert_not_called()

    def _validate(self, timeout, only_mode=True):
        with patch.object(ctrl, 'M4F_PRODUCER_TIMEOUT_SECONDS', timeout), \
             patch.object(ctrl, 'M4F_ONLY_MODE', only_mode):
            ctrl._validate_l2_config()

    def test_illegal_ranges_reject_startup(self):
        for bad in (-5, 1, 299, 86401):
            with self.assertRaises(ValueError, msg="timeout=%s" % bad):
                self._validate(bad)
        # legal bounds and disabled pass in candidate mode
        self._validate(300)
        self._validate(86400)
        self._validate(0)

    def test_legacy_mode_enable_rejects_startup(self):
        with self.assertRaises(ValueError):
            self._validate(300, only_mode=False)

    def test_non_integer_rejected_at_import(self):
        env = dict(os.environ)
        env["M4F_PRODUCER_TIMEOUT_SECONDS"] = "abc"
        r = subprocess.run(
            [sys.executable, "-c", "import controller"],
            cwd=str(CTRL_DIR), env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ValueError", r.stderr)


class TestProducerTimeoutReconcile(_ReconcileHarness):

    def test_before_deadline_no_hold(self):
        """SELECT returns nothing eligible → zero UPDATEs emitted."""
        held, cursor, _ = self._reconcile(waiting=())
        self.assertEqual(held, 0)
        self.assertEqual(len(cursor.execute.call_args_list), 1)
        self.assertIn("SELECT", _sql(cursor, 0))

    def test_deadline_reached_atomic_cas_hold(self):
        held, cursor, _ = self._reconcile()
        self.assertEqual(held, 1)
        select_sql, update_sql = _sql(cursor, 0), _sql(cursor, 1)
        for sql in (select_sql, update_sql):
            # every waiting predicate is repeated in the atomic UPDATE
            self.assertIn("status='RUNNING'", sql)
            self.assertIn("NOT EXISTS (SELECT 1 FROM public.stage_events e", sql)
            self.assertIn("e.event_type='M4F_RUN'", sql)
            self.assertIn(
                "NOT EXISTS (SELECT 1 FROM public.revision_bindings b", sql)
        self.assertIn("created_at <= now() - make_interval(secs=>%s)",
                      select_sql)
        self.assertIn("created_at <= now() - make_interval(secs=>%s)",
                      update_sql)
        self.assertIn("UPDATE public.task_runs", update_sql)
        self.assertIn("LIMIT %s", select_sql)
        self.assertEqual(_params(cursor, 0)[1], 300)  # timeout in SELECT

    def test_hold_fields_and_log_exact(self):
        _, cursor, out = self._reconcile()
        update_sql = _sql(cursor, 1)
        self.assertIn("SET status='HOLD'", update_sql)
        self.assertIn("last_error=%s", update_sql)
        stage, reason, run_id, timeout = _params(cursor, 1)
        self.assertEqual(stage, "m4f_producer_timeout")
        self.assertTrue(
            reason.startswith("PRODUCER_TIMEOUT: no M4F_RUN after 900s"),
            reason)
        self.assertEqual(run_id, _RUN)
        self.assertEqual(timeout, 300)
        logged = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        self.assertEqual(len(logged), 1)
        entry = logged[0]
        self.assertEqual(entry["event"], "producer.timeout.held")
        self.assertEqual(entry["schema"], "mergepilot.observation.v1")
        # identifiers and ages only — no secret surface
        self.assertEqual(sorted(entry),
                         ["event", "pr_number", "repo", "room_id",
                          "run_id", "schema", "wait_seconds"])

    def test_repeat_reconcile_idempotent(self):
        """A held run no longer matches status='RUNNING' → zero changes."""
        first, _, _ = self._reconcile()
        second, cursor2, out2 = self._reconcile(waiting=())
        self.assertEqual((first, second), (1, 0))
        self.assertEqual(len(cursor2.execute.call_args_list), 1)
        self.assertNotIn("producer.timeout.held", out2)

    def test_rowcount_zero_not_reported(self):
        """CAS lost the race → no transition logged, return 0."""
        held, _, out = self._reconcile(rowcount=0)
        self.assertEqual(held, 0)
        self.assertNotIn("producer.timeout.held", out)

    def test_concurrent_cas_single_success(self):
        first, _, _ = self._reconcile(rowcount=1)
        second, _, _ = self._reconcile(rowcount=0)
        self.assertEqual(first + second, 1)

    def test_restart_new_connection_same_derivation(self):
        """Stateless derivation: fresh connection emits identical SQL."""
        _, c1, _ = self._reconcile()
        _, c2, _ = self._reconcile()
        self.assertEqual(_sql(c1, 0), _sql(c2, 0))
        self.assertEqual(_sql(c1, 1), _sql(c2, 1))

    def test_existing_events_and_binding_exclude_run(self):
        """Arrival = any M4F_RUN stage event (no status filter —
        PENDING/RUNNING/PROCESSED/terminal ERROR all count) keyed on
        e.run_id equality, plus existing revision bindings."""
        _, cursor, _ = self._reconcile()
        select_sql = _sql(cursor, 0)
        self.assertIn("e.run_id=task_runs.run_id", select_sql)
        # no per-status restriction inside the arrival subquery
        arrival = select_sql.split("NOT EXISTS (SELECT 1 FROM public.stage_events")[1]
        arrival = arrival.split(")")[0]
        self.assertNotIn("e.status", arrival)
        self.assertIn("b.run_id=task_runs.run_id", select_sql)

    def test_zero_governance_side_effects(self):
        """Reconcile may only issue one bounded SELECT and one CAS UPDATE
        on task_runs — no inserts, no binding/snapshot/envelope/skill
        machinery, nothing else."""
        _, cursor, _ = self._reconcile()
        calls = cursor.execute.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("SELECT", _sql(cursor, 0))
        self.assertIn("FROM public.task_runs", _sql(cursor, 0))
        self.assertIn("UPDATE public.task_runs", _sql(cursor, 1))
        for forbidden in ("INSERT", "bind_revision", "snapshot_job",
                          "envelope", "skill_job", "DELETE"):
            self.assertNotIn(forbidden, _sql(cursor, 0), forbidden)
            self.assertNotIn(forbidden, _sql(cursor, 1), forbidden)


class TestLateRecovery(unittest.TestCase):

    def _recover(self, rowcount=1):
        cursor = MagicMock()
        cursor.rowcount = rowcount
        recovered = ctrl._recover_producer_timeout_hold(cursor, _RUN)
        sql = " ".join(str(cursor.execute.call_args[0][0]).split())
        params = cursor.execute.call_args[0][1]
        return recovered, sql, params

    def test_recovery_cas_from_exact_timeout_hold(self):
        recovered, sql, params = self._recover()
        self.assertTrue(recovered)
        self.assertIn("SET status='RUNNING',current_stage='m4f',last_error=NULL",
                      sql)
        # exact-state CAS: only the producer-timeout HOLD matches
        self.assertIn("status='HOLD'", sql)
        self.assertIn("current_stage=%s", sql)
        # psycopg2 paramstyle doubles the literal % when params are present
        self.assertIn("last_error LIKE 'PRODUCER_TIMEOUT:%%'", sql)
        self.assertEqual(params, (_RUN, "m4f_producer_timeout"))
        # created_at must never be reset
        self.assertNotIn("created_at", sql)

    def test_non_timeout_hold_not_recovered(self):
        """Other HOLD causes (different stage / different last_error)
        cannot match the CAS; rowcount=0 → no recovery reported."""
        recovered, sql, _ = self._recover(rowcount=0)
        self.assertFalse(recovered)
        self.assertIn("current_stage=%s", sql)
        self.assertIn("last_error LIKE 'PRODUCER_TIMEOUT:%%'", sql)

    def test_recovery_wired_only_in_drain_success(self):
        """The recovery CAS has exactly one call site: the drain success
        transaction, ordered AFTER the stage_events PROCESSED update —
        so the existing M4F_RUN event is durable before recovery, and the
        reconcile can never re-hold the recovered run."""
        self.assertEqual(CTRL_SOURCE.count("_recover_producer_timeout_hold("),
                         2)  # def + single call site
        drain = CTRL_SOURCE.split("def drain_m4f_events", 1)[1]
        drain = drain.split("def ", 1)[0] if "def " in drain[10:] else drain
        processed_pos = drain.find("status='PROCESSED'")
        call_pos = drain.find("_recover_producer_timeout_hold(cur")
        self.assertGreater(processed_pos, 0)
        self.assertGreater(call_pos, processed_pos)
        # reconcile itself never flips a run back to RUNNING (it only HOLDs;
        # its status='RUNNING' occurrences are waiting predicates)
        reconcile_src = CTRL_SOURCE.split(
            "def reconcile_m4f_producer_timeout", 1)[1].split(
            "def _recover_producer_timeout_hold", 1)[0]
        self.assertNotIn("SET status='RUNNING'", reconcile_src)


if __name__ == "__main__":
    unittest.main()
