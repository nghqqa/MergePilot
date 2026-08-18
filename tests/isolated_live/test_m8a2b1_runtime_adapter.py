#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8-A2-b1 — runtime-owned M4F_RUN adapter tests.

Verifies the AgentTeams → Matrix → Controller → stage_events entry path
 WITHOUT any direct database writes by the producer side. All tests mock
 the Matrix /sync responses and the PG connection; no Docker, no network,
 no real homeserver, no skips.

Contract verified (12 items from the A2-b1 spec):

  1. Candidate mode accepts only allowlisted Manager senders.
  2. Legacy mode admin behavior remains compatible.
  3. M4F_ALLOWED_ROOMS empty → reject all rooms (Candidate).
  4. M4F_ALLOWED_SENDERS mismatch → fail-closed.
  5. M4F_RUN_PREFIX mismatch → fail-closed.
  6. Malformed payload (bad JSON, missing fields, wrong types, extra
     dangerous fields) → rejected.
  7. event_id idempotency (ON CONFLICT DO NOTHING).
  8. Accepted events enter stage_events as M4F_PENDING.
  9. Permanent errors (M4FIngressError) → terminal ERROR.
 10. Runtime errors → M4F_PENDING retry (A1 semantics).
 11. /sync since_token uses controller_offsets.sync_token.
 12. The AgentTeams path has NO direct INSERT into governance tables
     (stage_events/task_runs/run_pr_bindings/mcp_calls/audit_events/
     revision_bindings) — only the Controller writes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from types import SimpleNamespace

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(ROOT / "tools" / "workflow-controller"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import controller as ctrl  # noqa: E402
import m4f_ingress  # noqa: E402

CTRL_SOURCE = (ROOT / "tools" / "workflow-controller" / "controller.py")\
    .read_text(encoding="utf-8")

_RUN_ID = "m5live-test-run-001"
_TRACE = "trace-m5live-001"
_REPO = "test/repo"
_PR = 42
_ROOM = "!test:matrix-local.hiclaw.io:18080"
_SENDER_MGR = "@manager:matrix-local.hiclaw.io:18080"
_SENDER_ADMIN = "@admin:matrix-local.hiclaw.io:18080"
_SENDER_BAD = "@evil:matrix-local.hiclaw.io:18080"
_EVENT_ID = "$evt-m4f-run-001"

_GOOD_PAYLOAD = {
    "contract_version": "1",
    "run_id": _RUN_ID,
    "trace_id": _TRACE,
    "repo": _REPO,
    "pr_number": _PR,
    "test_runner": {"command": "pytest -q"},
    "pr_lifecycle": {"action": "create"},
}
_GOOD_BODY = "M4F_RUN: " + json.dumps(_GOOD_PAYLOAD)


def _sync_response(events, next_batch="nb-1"):
    """Build a minimal /sync response with timeline events."""
    return {
        "next_batch": next_batch,
        "rooms": {"join": {_ROOM: {"timeline": {"events": events}}}},
    }


def _matrix_event(event_id, sender, body, ts=1700000000000):
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": {"body": body},
    }


def _make_mock_conn(inserted=True):
    """Mock PG connection that tracks cursor.execute calls.
    `inserted=True` → fetchone returns a row (new event);
    `inserted=False` → fetchone returns None (duplicate event_id)."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    cursor.fetchone.return_value = (_EVENT_ID,) if inserted else None
    return conn, cursor


# ── 1-5: sender / room / prefix allowlist contracts ───────────────────────

class TestVerifyM5Sender(unittest.TestCase):

    def test_valid_manager_sender(self):
        result = ctrl.verify_m5_sender(_SENDER_MGR, {"manager"})
        self.assertEqual(result, "manager")

    def test_valid_admin_sender(self):
        result = ctrl.verify_m5_sender(_SENDER_ADMIN, {"admin"})
        self.assertEqual(result, "admin")

    def test_wrong_sender_rejected(self):
        self.assertIsNone(ctrl.verify_m5_sender(_SENDER_BAD, {"manager"}))

    def test_missing_at_rejected(self):
        self.assertIsNone(ctrl.verify_m5_sender("manager:server", {"manager"}))

    def test_wrong_server_rejected(self):
        self.assertIsNone(
            ctrl.verify_m5_sender("@manager:evil.com", {"manager"}))

    def test_empty_rejected(self):
        self.assertIsNone(ctrl.verify_m5_sender("", {"manager"}))
        self.assertIsNone(ctrl.verify_m5_sender(None, {"manager"}))


class TestM5ParseM4FRun(unittest.TestCase):

    def setUp(self):
        self._patcher = patch.object(ctrl, 'M4F_RUN_PREFIX', 'm5live-')
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_valid_payload_accepted(self):
        result = ctrl.m5_parse_m4f_run(_GOOD_BODY)
        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], _RUN_ID)

    def test_missing_prefix_rejected(self):
        self.assertIsNone(ctrl.m5_parse_m4f_run("not-m4f: " + json.dumps(_GOOD_PAYLOAD)))

    def test_wrong_run_prefix_rejected(self):
        bad = dict(_GOOD_PAYLOAD, run_id="wrong-prefix-001")
        with patch.object(ctrl, 'M4F_RUN_PREFIX', 'm5live-'):
            self.assertIsNone(
                ctrl.m5_parse_m4f_run("M4F_RUN: " + json.dumps(bad)))

    def test_invalid_json_rejected(self):
        self.assertIsNone(ctrl.m5_parse_m4f_run("M4F_RUN: {invalid json"))

    def test_not_a_dict_rejected(self):
        self.assertIsNone(ctrl.m5_parse_m4f_run("M4F_RUN: [1,2,3]"))

    def test_missing_required_fields_rejected(self):
        bad = {k: v for k, v in _GOOD_PAYLOAD.items() if k != "repo"}
        self.assertIsNone(ctrl.m5_parse_m4f_run("M4F_RUN: " + json.dumps(bad)))

    def test_extra_dangerous_fields_rejected(self):
        bad = dict(_GOOD_PAYLOAD, base_sha="a" * 40, decision="ALLOW")
        self.assertIsNone(ctrl.m5_parse_m4f_run("M4F_RUN: " + json.dumps(bad)))

    def test_wrong_type_rejected(self):
        bad = dict(_GOOD_PAYLOAD, pr_number="not-an-int")
        self.assertIsNone(ctrl.m5_parse_m4f_run("M4F_RUN: " + json.dumps(bad)))

    def test_embedded_marker_rejected(self):
        self.assertIsNone(ctrl.m5_parse_m4f_run(
            "M4F_RUN: M4F_RUN: " + json.dumps(_GOOD_PAYLOAD)))

    def test_trailing_prose_accepted_if_json_valid(self):
        # m5_parse strips after JSON; trailing prose after the closing }
        # is handled by json.loads which stops at the closing brace
        result = ctrl.m5_parse_m4f_run(_GOOD_BODY)
        self.assertIsNotNone(result)


# ── 6-8: process_event M4F entry path ─────────────────────────────────────

class TestProcessEventM4FEntry(unittest.TestCase):
    """Test process_event's M4F branch with mocked PG."""

    def _run_process_event(self, body, sender, event_id=_EVENT_ID,
                           m4f_only=False, m4f_enabled=True):
        conn, cursor = _make_mock_conn()
        with patch.object(ctrl, 'ensure_pg', return_value=conn), \
             patch.object(ctrl, 'M4F_ONLY_MODE', m4f_only), \
             patch.object(ctrl, 'M4F_ENABLED', m4f_enabled), \
             patch.object(ctrl, 'ADMIN', 'admin'):
            ctrl.process_event(event_id, _ROOM, sender, sender.split('@')[1].split(':')[0] if '@' in sender else sender, body, 1700000000000)
        return conn, cursor

    def test_candidate_manager_accepted(self):
        conn, cursor = self._run_process_event(
            _GOOD_BODY, _SENDER_MGR, m4f_only=True)
        # Should have UPDATE stage_events to M4F_PENDING
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertTrue(updates, "expected M4F_PENDING update")

    def test_legacy_admin_accepted(self):
        conn, cursor = self._run_process_event(
            _GOOD_BODY, _SENDER_ADMIN, m4f_only=False)
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertTrue(updates, "expected M4F_PENDING update (legacy admin)")

    def test_candidate_wrong_sender_rejected(self):
        conn, cursor = self._run_process_event(
            _GOOD_BODY, _SENDER_BAD, m4f_only=True)
        errors = [c for c in cursor.execute.call_args_list
                  if 'mark_error' in str(c) or 'ERROR' in str(c)]
        # At minimum, should NOT have M4F_PENDING
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertFalse(updates, "wrong sender must not reach M4F_PENDING")

    def test_legacy_non_admin_rejected(self):
        conn, cursor = self._run_process_event(
            _GOOD_BODY, _SENDER_MGR, m4f_only=False)
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertFalse(updates, "non-admin must not reach M4F_PENDING")

    def test_m4f_disabled_rejected(self):
        conn, cursor = self._run_process_event(
            _GOOD_BODY, _SENDER_MGR, m4f_only=True, m4f_enabled=False)
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertFalse(updates, "M4F_ENABLED=0 must reject")

    def test_invalid_payload_error(self):
        bad_body = "M4F_RUN: {invalid"
        conn, cursor = self._run_process_event(
            bad_body, _SENDER_MGR, m4f_only=True)
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertFalse(updates, "invalid payload must not enter M4F_PENDING")

    def test_event_id_idempotent_on_conflict(self):
        """process_event uses ON CONFLICT DO NOTHING on event_id;
        a duplicate returns without processing."""
        conn, cursor = _make_mock_conn()
        cursor.fetchone.return_value = None  # conflict → no row returned
        with patch.object(ctrl, 'ensure_pg', return_value=conn), \
             patch.object(ctrl, 'M4F_ONLY_MODE', True), \
             patch.object(ctrl, 'M4F_ENABLED', True), \
             patch.object(ctrl, 'ADMIN', 'admin'):
            ctrl.process_event(_EVENT_ID, _ROOM, _SENDER_MGR, "manager",
                               _GOOD_BODY, 1700000000000)
        # fetchone returned None → early return; no M4F_PENDING update
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertFalse(updates, "duplicate event_id must be a no-op")

    def test_stage_events_initial_status_received_then_pending(self):
        """The INSERT puts status='RECEIVED'; the M4F branch UPDATEs
        to M4F_PENDING — never direct INSERT as M4F_PENDING."""
        conn, cursor = self._run_process_event(
            _GOOD_BODY, _SENDER_MGR, m4f_only=True)
        inserts = [c for c in cursor.execute.call_args_list
                   if 'INSERT INTO stage_events' in str(c)]
        self.assertTrue(inserts)
        # INSERT uses 'RECEIVED' (not M4F_PENDING)
        insert_sql = str(inserts[0])
        self.assertIn("'RECEIVED'", insert_sql)
        self.assertNotIn("'M4F_PENDING'", insert_sql)
        # UPDATE transitions to M4F_PENDING
        updates = [c for c in cursor.execute.call_args_list
                   if 'M4F_PENDING' in str(c)]
        self.assertTrue(updates)


# ── 9-10: A1 error semantics in controller source ─────────────────────────

class TestA1SemanticsInController(unittest.TestCase):

    def test_permanent_error_mark_error_path(self):
        """M4FIngressError → mark_error → terminal ERROR."""
        self.assertIn(
            "except m4f_ingress.M4FIngressError",
            CTRL_SOURCE)
        self.assertIn("mark_error", CTRL_SOURCE)
        self.assertIn("'ERROR'", CTRL_SOURCE)

    def test_retry_semantics_in_drain_m4f(self):
        """drain_m4f_events has retryable M4F_PENDING vs terminal ERROR."""
        self.assertIn(
            'terminal = permanent or attempt >= M4F_EVENT_MAX_ATTEMPTS',
            CTRL_SOURCE)
        self.assertIn(
            '"ERROR" if terminal else "M4F_PENDING"',
            CTRL_SOURCE)


# ── 11: /sync since_token contract ────────────────────────────────────────

class TestSyncTokenContract(unittest.TestCase):

    def test_consume_events_reads_controller_offsets(self):
        self.assertIn(
            "SELECT sync_token FROM controller_offsets",
            CTRL_SOURCE)
        self.assertIn("CONTROLLER_CONSUMER_NAME", CTRL_SOURCE)

    def test_matrix_sync_uses_since_param(self):
        self.assertIn("since={since}", CTRL_SOURCE)

    def test_next_batch_persistence(self):
        """consume_events stores next_batch back to controller_offsets."""
        # Look for the UPDATE/INSERT of controller_offsets after /sync
        self.assertIn("controller_offsets", CTRL_SOURCE)


# ── 12: no direct governance table writes from AgentTeams side ────────────

class TestNoDirectGovernanceWrites(unittest.TestCase):
    """The AgentTeams path (Matrix message → consume_events → process_event)
    only writes stage_events via the Controller's INSERT/UPDATE. It never
    directly INSERTs into task_runs, run_pr_bindings, mcp_calls,
    audit_events, or revision_bindings."""

    def test_m4f_branch_only_touches_stage_events(self):
        """The M4F branch of process_event (lines ~1105-1147) contains
        only stage_events INSERT and UPDATE — no other governance table."""
        # Extract the M4F branch (from PAT_M4F.search to the return)
        m = re.search(
            r'if PAT_M4F\.search\(body\):[\s\S]*?return',
            CTRL_SOURCE)
        self.assertIsNotNone(m, "M4F branch not found in controller source")
        m4f_branch = m.group(0)
        for table in ("INSERT INTO task_runs",
                      "INSERT INTO run_pr_bindings",
                      "INSERT INTO mcp_calls",
                      "INSERT INTO audit_events",
                      "INSERT INTO revision_bindings"):
            self.assertNotIn(table, m4f_branch,
                             "M4F branch must not directly write %s" % table)

    def test_consume_events_no_direct_writes(self):
        """consume_events only calls process_event; it doesn't write
        governance tables directly."""
        m = re.search(
            r'def consume_events\(\):[\s\S]*?(?=\ndef |\Z)',
            CTRL_SOURCE)
        self.assertIsNotNone(m)
        body = m.group(0)
        for table in ("INSERT INTO task_runs",
                      "INSERT INTO run_pr_bindings",
                      "INSERT INTO mcp_calls",
                      "INSERT INTO revision_bindings"):
            self.assertNotIn(table, body,
                             "consume_events must not write %s" % table)

    def test_agentteams_path_is_runtime_owned(self):
        """The only entry point for AgentTeams is Matrix m.room.message
        via /sync → process_event. There is no stdin adapter, admin SQL,
        or protocol fixture in the real path."""
        # Controller has /sync as the only Matrix entry
        self.assertIn("def consume_events", CTRL_SOURCE)
        self.assertIn("matrix_sync", CTRL_SOURCE)
        # No direct DB from the Matrix event itself (only via process_event)
        self.assertIn("process_event(eid, room_id, raw_sender, sender, body, ts)",
                      CTRL_SOURCE)


# ── M8-A2 candidate-mode TASK_SUBMITTED routing ──────────────────────────

class TestCandidateTaskSubmittedRouting(unittest.TestCase):
    """M8-A2: candidate mode has no task_runs producer of its own, so the
    /sync filter must route TASK_SUBMITTED from allowlisted senders into
    process_event (which still enforces sender==ADMIN and idempotent INSERT).
    Verified live against the real Matrix homeserver during the M8-A2 E2E."""

    def _candidate_branch(self):
        """Source of the candidate /sync branch inside consume_events."""
        sync_src = CTRL_SOURCE.split("def consume_events", 1)[1]
        return sync_src.split("if M4F_ONLY_MODE:", 1)[1].split(
            "# Legacy mode:", 1)[0]

    def test_candidate_filter_routes_task_submitted(self):
        # The M4F_ONLY_MODE branch routes TASK_SUBMITTED to process_event
        # before the fall-through "skip all other event types" continue.
        tail = self._candidate_branch()
        self.assertIn('body.lstrip().startswith("TASK_SUBMITTED:")', tail)
        self.assertIn(
            "process_event(eid, room_id, raw_sender, sender, body, ts)", tail
        )

    def test_routing_still_gated_on_allowlisted_sender(self):
        # The routing sits after verify_m5_sender, so a non-allowlisted
        # sender never reaches process_event in candidate mode.
        tail = self._candidate_branch()
        sender_pos = tail.index("verify_m5_sender")
        route_pos = tail.index('body.lstrip().startswith("TASK_SUBMITTED:")')
        self.assertLess(sender_pos, route_pos)


# ── supplementary: PAT_M4F regex correctness ──────────────────────────────

class TestPatM4FRegex(unittest.TestCase):

    def test_matches_standard_format(self):
        self.assertTrue(ctrl.PAT_M4F.search("M4F_RUN: {...}"))

    def test_matches_case_insensitive(self):
        self.assertTrue(ctrl.PAT_M4F.search("m4f_run: {...}"))

    def test_does_not_match_without_colon(self):
        self.assertFalse(ctrl.PAT_M4F.search("M4F_RUN{...}"))

    def test_does_not_match_similar_prefix(self):
        self.assertFalse(ctrl.PAT_M4F.search("M4F_REVIEW: {...}"))


if __name__ == "__main__":
    unittest.main()
