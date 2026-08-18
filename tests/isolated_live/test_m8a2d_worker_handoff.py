#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8-A2-d — real Worker TASK_COMPLETED handoff composition tests.

Production fact discovered live (2026-08-18, run m8a2m-d2 on fixture PR
#625): the M8-A2 candidate TASK_SUBMITTED routing creates the run at the
legacy-shaped current_stage='review' with its review stage_run already
dispatched, while _m5_handoff_one historically required the M5-0B bridge
stage 'm4f_await_review'. Fix 1 (A2-d) accepts BOTH states for the review
handoff so the worker loop is not coupled to M4F_RUN arrival — verified
end-to-end with the real reviewer/fixer/verifier Matrix identities.

These tests drive the production _m5_handoff_one with a scripted cursor:
every state change must be justified by captured production SQL, never
by pre-fabricated results. No Docker, no network, no seeds.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(ROOT / "tools" / "workflow-controller"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import controller as ctrl  # noqa: E402

_RUN = "m5live-d2"
_ROOM = "!a2d:matrix-local.hiclaw.io:18080"
_REVIEWER = "@reviewer:matrix-local.hiclaw.io:18080"
_MANAGER = "@manager:matrix-local.hiclaw.io:18080"
_EVENT = "$evt-a2d-review"
_BODY = "TASK_COMPLETED: %s-review" % _RUN


def _sql(cursor):
    return [" ".join(str(c[0][0]).split()) for c in cursor.execute.call_args_list]


class _HandoffHarness(unittest.TestCase):
    """Drives production _m5_handoff_one with a scripted fetchone queue."""

    def _run_handoff(self, t_stage, t_status="RUNNING",
                     rec_sender=_REVIEWER, rec_status="RECEIVED"):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        # fetchone queue: meta lookup, task_runs FOR UPDATE, event FOR
        # UPDATE, review stage_run FOR UPDATE, checked stage_run re-read,
        # checked dispatch idempotency re-read
        _dispatch_row = (
            "m5-%s-fix-dispatch" % _RUN, _RUN, _ROOM, "fixer", "fix", 1,
            "[M5-0B] run %s-review 完成。请据 findings 提修复。完成时精确写一行"
            "(无代码块/无解释): TASK_COMPLETED: %s-fix" % (_RUN, _RUN))
        cur.fetchone.side_effect = [
            (_RUN,),
            (t_stage, t_status, _ROOM),
            (_RUN, "review", rec_sender, rec_status),
            (101,),
            (_RUN, "fix", "fixer", 1, "PENDING_DISPATCH"),
            _dispatch_row,
        ]
        conn.cursor.return_value = cur
        with patch.object(ctrl, 'M4F_ALLOWED_SENDERS',
                          ["reviewer", "fixer", "verifier", "manager", "admin"]), \
             patch.object(ctrl, 'M4F_ALLOWED_ROOMS', [_ROOM]):
            wrote = ctrl._m5_handoff_one(conn, _EVENT, _ROOM, rec_sender, _BODY)
        return wrote, cur, conn

    def _all_sql(self, cur):
        return "\n".join(_sql(cur))


class TestReviewStageComposition(_HandoffHarness):
    """Fix 1: the review handoff accepts both the M5-0B bridge state and
    the M8-A2 TASK_SUBMITTED-shaped 'review' state."""

    def test_review_stage_handoff_advances_from_m8a2_shape(self):
        wrote, cur, conn = self._run_handoff("review")
        self.assertTrue(wrote)
        sql = self._all_sql(cur)
        # full advance: review COMPLETED, fix stage + dispatch, await stage
        self.assertIn("UPDATE stage_runs SET status='COMPLETED'", sql)
        self.assertIn("INSERT INTO stage_runs", sql)
        self.assertIn("INSERT INTO dispatch_outbox", sql)
        self.assertIn("current_stage=%s", sql)
        self.assertIn("m4f_await_fix", str(cur.execute.call_args_list))
        conn.commit.assert_called()

    def test_review_stage_handoff_still_advances_from_bridge_shape(self):
        """Regression guard: the original M5-0B await state still works."""
        wrote, cur, _ = self._run_handoff("m4f_await_review")
        self.assertTrue(wrote)
        self.assertIn("INSERT INTO stage_runs", self._all_sql(cur))

    def test_fix_stage_handoff_does_not_accept_review_shape(self):
        """Only the review stage gets the composition; fix handoffs keep
        requiring their exact await stage (out-of-order fail-closed)."""
        body = "TASK_COMPLETED: %s-fix" % _RUN
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.side_effect = [
            (_RUN,),
            ("review", "RUNNING", _ROOM),
            (_RUN, "fix", "@fixer:matrix-local.hiclaw.io:18080", "RECEIVED"),
        ]
        conn.cursor.return_value = cur
        with patch.object(ctrl, 'M4F_ALLOWED_SENDERS',
                          ["reviewer", "fixer", "verifier", "manager", "admin"]), \
             patch.object(ctrl, 'M4F_ALLOWED_ROOMS', [_ROOM]):
            wrote = ctrl._m5_handoff_one(
                conn, _EVENT, _ROOM,
                "@fixer:matrix-local.hiclaw.io:18080", body)
        self.assertTrue(wrote)  # event finalized...
        sql = self._all_sql(cur)
        self.assertIn("no dispatch", str(cur.execute.call_args_list))
        self.assertNotIn("INSERT INTO stage_runs", sql)

    def test_wrong_sender_rejected_even_in_composition_shape(self):
        """sender must still match _M5_STAGE_SENDER['review']=='reviewer'."""
        wrote, cur, conn = self._run_handoff("review", rec_sender=_MANAGER)
        self.assertFalse(wrote)
        self.assertIn("sender mismatch", str(cur.execute.call_args_list))

    def test_missing_review_stage_run_blocks_advance(self):
        """M8-A2-d §6: accepting the composed 'review' state must NOT let a
        forged/legacy TASK_COMPLETED advance a run that has no active review
        stage_run — the production advance path locks the from_stage
        stage_run FOR UPDATE and errors without it."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.side_effect = [
            (_RUN,),
            ("review", "RUNNING", _ROOM),
            (_RUN, "review", _REVIEWER, "RECEIVED"),
            None,  # no active review stage_run
        ]
        conn.cursor.return_value = cur
        with patch.object(ctrl, 'M4F_ALLOWED_SENDERS',
                          ["reviewer", "fixer", "verifier", "manager", "admin"]), \
             patch.object(ctrl, 'M4F_ALLOWED_ROOMS', [_ROOM]):
            wrote = ctrl._m5_handoff_one(conn, _EVENT, _ROOM, _REVIEWER, _BODY)
        # event IS finalized (as ERROR — fail-closed) but nothing advances
        self.assertTrue(wrote)
        calls = str(cur.execute.call_args_list)
        self.assertIn("no active review stage_run", calls)
        sql = "\n".join(" ".join(str(c[0][0]).split())
                        for c in cur.execute.call_args_list)
        self.assertNotIn("INSERT INTO stage_runs", sql)
        self.assertNotIn("INSERT INTO dispatch_outbox", sql)

    def test_terminal_run_replay_is_zero_growth(self):
        """A late review handoff on a finished run is PROCESSED with no
        dispatch — no stage_runs, no outbox growth (live-verified with the
        real reviewer resending on 2026-08-18)."""
        wrote, cur, _ = self._run_handoff("m5_verify_passed", t_status="HOLD")
        self.assertTrue(wrote)
        self.assertIn("no dispatch", str(cur.execute.call_args_list))
        sql = self._all_sql(cur)
        self.assertNotIn("INSERT INTO stage_runs", sql)
        self.assertNotIn("INSERT INTO dispatch_outbox", sql)


if __name__ == "__main__":
    unittest.main()
