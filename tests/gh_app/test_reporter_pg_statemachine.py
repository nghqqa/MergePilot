#!/usr/bin/env python3
"""M8-GH-4B2 REAL ephemeral-PG reporter state-machine tests.

Phase-B group: requires ``EPHEMERAL_PG_VERIFY=1`` AND the MergePilot-Test
daemon Running (the harness refuses to start a Stopped distro). Without
authorization every test skips — the fake-path coverage lives in
``test_checks_mapping_and_reporter.py``.

What this exercises against REAL PostgreSQL (fake HTTP transport only):
  * claim/reap SQL actually execute (CAS, SKIP LOCKED, attempt gating);
  * max_attempts terminal transitions persist (``last_error`` classes);
  * an expired-LEASED exhausted row is reaped with ZERO HTTP calls;
  * ``github_drain._UPSERT_CHECK_SQL`` grants a fresh attempt budget ONLY
    on a real triple change (same-version upsert never revives TERMINAL).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "gh-app"),
          str(ROOT / "tools" / "workflow-controller"),
          str(ROOT / "tests" / "isolated_live")):
    if p not in sys.path:
        sys.path.insert(0, p)

import checks_reporter as cr                              # noqa: E402
import github_drain as gd                                 # noqa: E402

_REPO = "example/fixture-repo"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_EXTERNAL = "mergepilot/gh-" + "9" * 24


class _StaticTransport:
    """Scripted responses per call (default: the same status for every
    call); counts the HTTP calls."""

    def __init__(self, status, body=None, sequence=None):
        self.status = status
        self.body = body if body is not None else {}
        self.sequence = list(sequence or [])
        self.calls = 0

    def __call__(self, method, url, *, headers, body):
        self.calls += 1
        if self.sequence:
            status, resp_body = self.sequence.pop(0)
            return status, {}, resp_body
        return self.status, {}, self.body


def _authorized():
    if os.environ.get("EPHEMERAL_PG_VERIFY") != "1":
        return None
    sys.path.insert(0, str(ROOT / "tests" / "isolated_live"))
    from ephemeral_harness import check_execution_auth
    auth = check_execution_auth()
    return auth if auth.get("authorized") else None


@unittest.skipUnless(_authorized(), "EPHEMERAL_PG_VERIFY=1 + running "
                     "MergePilot-Test daemon required (never auto-started)")
class TestReporterPgStateMachine(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from ephemeral_harness import check_execution_auth
        from ephemeral_executor import EphemeralExecutor
        cls.executor = EphemeralExecutor(str(ROOT),
                                         authorization_context=(
                                             check_execution_auth()))
        cls.addClassCleanup(cls.executor.cleanup_and_verify)
        cls.executor.start_and_prepare()
        # The harness chain stops at m4f1; apply the M8-GH-1 ingress
        # migration so github_check_outbox/github_deliveries exist.
        m8gh1 = (ROOT / "tools" / "audit-db" /
                 "m8gh1_github_ingress.sql").read_text(encoding="utf-8")
        with cls._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(m8gh1)
            conn.commit()

    @classmethod
    def _admin_conn(cls):
        return cls.executor._connect(
            password=cls.executor._admin_password, user="mergepilot")

    def _conn_factory(self):
        return self._admin_conn()

    def _seed_outbox(self, run_id, *, attempt=0, state="PENDING",
                     sha=_SHA_A, external=_EXTERNAL):
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_runs (run_id, repo, status) "
                    "VALUES (%s, %s, 'RUNNING') "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (run_id, _REPO))
                cur.execute(
                    "INSERT INTO github_check_outbox "
                    "(outbox_id, run_id, repo, pr_number, observed_head_sha,"
                    " external_id, check_run_id, desired_status,"
                    " desired_conclusion, publish_state, attempt_count,"
                    " next_retry_at) "
                    "VALUES (%s, %s, %s, 7, %s, %s, NULL, 'in_progress',"
                    " NULL, %s, %s, now() - interval '1 second') "
                    "ON CONFLICT (external_id) DO NOTHING",
                    ("chk-" + run_id, run_id, _REPO, sha, external,
                     state, attempt))
            conn.commit()

    def _fetch_row(self, external):
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT publish_state, last_error, attempt_count, "
                    "check_run_id, desired_version FROM github_check_outbox "
                    "WHERE external_id = %s", (external,))
                return cur.fetchone()

    def test_publish_success_persists_published_state(self):
        run = "gh-" + "1" * 24
        ext = "mergepilot/" + run
        self._seed_outbox(run, external=ext)
        transport = _StaticTransport(
            200, sequence=[(200, {"check_runs": []}),   # lookup miss
                           (201, {"id": 424242})])      # create
        outcome = cr.publish_once(self._conn_factory,
                                   api_base="http://fake",
                                   transport=transport, token="t")
        self.assertEqual(outcome, "published")
        state, last_error, attempts, check_run_id, version = \
            self._fetch_row(ext)
        self.assertEqual(state, "PUBLISHED")
        self.assertIsNone(last_error)
        self.assertEqual(check_run_id, 424242)

    def test_exhaustion_reaches_terminal_with_class(self):
        run = "gh-" + "2" * 24
        ext = "mergepilot/" + run
        max_attempts = 3
        self._seed_outbox(run, external=ext)
        outcomes = []
        for _ in range(max_attempts):
            transport = _StaticTransport(503, sequence=[
                (503, {}), (503, {})])   # lookup + publish both 5xx
            outcomes.append(cr.publish_once(
                self._conn_factory, api_base="http://fake",
                transport=transport, token="t",
                max_attempts=max_attempts))
            if outcomes[-1] == "retry":
                # collapse the backoff window so the next round is
                # immediately eligible (the window itself is covered by
                # the fake-path tests)
                with self._admin_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE github_check_outbox SET next_retry_at ="
                            " now() - interval '1 second' "
                            "WHERE external_id = %s", (ext,))
                    conn.commit()
        self.assertEqual(outcomes[:-1], ["retry"] * (max_attempts - 1))
        self.assertEqual(outcomes[-1], "terminal")
        state, last_error, attempts, _id, _v = self._fetch_row(ext)
        self.assertEqual(state, "TERMINAL")
        self.assertEqual(last_error, "MAX_ATTEMPTS:HTTP_5XX")
        self.assertEqual(attempts, max_attempts)
        # one more round: nothing left to claim below max
        idle = cr.publish_once(self._conn_factory, api_base="http://fake",
                               transport=_StaticTransport(503),
                               token="t", max_attempts=max_attempts)
        self.assertEqual(idle, "idle")

    def test_crashed_expired_lease_reaped_without_http(self):
        run = "gh-" + "3" * 24
        ext = "mergepilot/" + run
        # simulate the crash aftermath: expired LEASED at max attempts
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_runs (run_id, repo, status) "
                    "VALUES (%s, %s, 'RUNNING')", (run, _REPO))
                cur.execute(
                    "INSERT INTO github_check_outbox "
                    "(outbox_id, run_id, repo, pr_number, observed_head_sha,"
                    " external_id, desired_status, publish_state,"
                    " attempt_count, next_retry_at, lease_expires_at) "
                    "VALUES (%s, %s, %s, 7, %s, %s, 'in_progress', 'LEASED',"
                    " 5, now() - interval '1 second',"
                    " now() - interval '1 hour')",
                    ("chk-" + run, run, _REPO, _SHA_A, ext))
            conn.commit()
        transport = _StaticTransport(201, {"id": 1})
        outcome = cr.publish_once(self._conn_factory, api_base="http://fake",
                                  transport=transport, token="t",
                                  max_attempts=5)
        self.assertEqual(outcome, "idle")
        self.assertEqual(transport.calls, 0)     # ZERO HTTP on reap-only
        state, last_error, _a, _i, _v = self._fetch_row(ext)
        self.assertEqual(state, "TERMINAL")
        self.assertEqual(last_error, "MAX_ATTEMPTS")

    def test_version_increase_resets_budget_same_version_does_not(self):
        run = "gh-" + "4" * 24
        ext = "mergepilot/" + run
        self._seed_outbox(run, external=ext)
        # exhaust to TERMINAL (collapse backoff between rounds)
        for _ in range(2):
            outcome = cr.publish_once(
                self._conn_factory, api_base="http://fake",
                transport=_StaticTransport(503, sequence=[(503, {}),
                                                          (503, {})]),
                token="t", max_attempts=2)
            if outcome == "retry":
                with self._admin_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE github_check_outbox SET next_retry_at ="
                            " now() - interval '1 second' "
                            "WHERE external_id = %s", (ext,))
                    conn.commit()
        state, _le, _a, _i, _v = self._fetch_row(ext)
        self.assertEqual(state, "TERMINAL")
        # SAME-triple upsert: no revival, no reset
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(gd._UPSERT_CHECK_SQL,
                            ("chk-x", run, _REPO, 7, _SHA_A, ext,
                             "in_progress", None))
            conn.commit()
        state, _le, attempts, _i, version = self._fetch_row(ext)
        self.assertEqual(state, "TERMINAL")
        self.assertEqual(attempts, 2)
        self.assertEqual(version, 1)
        # REAL change (SHA drift): fresh budget, PENDING, version+1
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(gd._UPSERT_CHECK_SQL,
                            ("chk-x", run, _REPO, 7, _SHA_B, ext,
                             "in_progress", None))
            conn.commit()
        state, last_error, attempts, _i, version = self._fetch_row(ext)
        self.assertEqual(state, "PENDING")
        self.assertIsNone(last_error)
        self.assertEqual(attempts, 0)
        self.assertEqual(version, 2)


if __name__ == "__main__":
    unittest.main()
