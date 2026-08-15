"""ISOLATED_LIVE MergePilot-Test integration Phase A — Mock/static tests.

All database interaction is injected (FakeCursor / callables); NO WSL /
Docker / PostgreSQL is started, NO real MergePilot-Test DB is contacted, NO
evidence/ is written. EPHEMERAL_PG_VERIFY is never set.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mergepilot_integration as mi  # noqa: E402
from mergepilot_integration import (  # noqa: E402
    IntegrationCleanupError,
    IntegrationGateError,
    MERGEPILOT_TEST_SOURCE_KIND,
    MergePilotTestSnapshotSource,
    assert_argv_safe,
    assert_live_status_contract,
    build_db_authorization_context,
    check_kind_isolation,
    check_server_version_window,
    freeze_execution_fingerprint,
    integration_cleanup,
    observe_producer_window,
    redact_text,
    recheck_execution_fingerprint,
    run_db_prerequisite_checks,
    validate_db_authorization_context,
    window_retry_allowed,
)

import evidence_manifest as em  # noqa: E402
from live_poller import LivePoller  # noqa: E402
from integrity import compute_bundle_sha256  # noqa: E402
from serve import create_server  # noqa: E402

# ── Shared fixtures ──────────────────────────────────────────────────────────

GOOD_CTX = dict(
    database="mergepilot_audit",
    current_user="mergepilot_reader",
    application_name="mergepilot_isolated_live_reader",
    server_version_num=160003,
    marker_value="mergepilot-test-app",
    server_address="172.17.0.2",
    server_port=5432,
    captured_at="2026-08-14T00:00:00Z",
)


def _ctx(**overrides):
    c = dict(GOOD_CTX)
    c.update(overrides)
    return c


class FakeCursor:
    """Scripted cursor: ``queue`` items are rows or lists of rows per execute."""

    def __init__(self, script):
        # script: list of results; each result is: ("one", row) for
        # fetchone or ("all", rows) for fetchall, consumed in execute order.
        self.script = list(script)
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if not self.script:
            raise AssertionError("script exhausted")
        kind, value = self.script.pop(0)
        assert kind == "one", "fetchone on an all-scripted step"
        return value

    def fetchall(self):
        if not self.script:
            raise AssertionError("script exhausted")
        kind, value = self.script.pop(0)
        assert kind == "all", "fetchall on a one-scripted step"
        return value


def _prereq_script(*, role_row=(False, False, False, False, False),
                   owner_count=0, marker_rows=None, tables=None,
                   acl_row=(True, False, False, False, False)):
    marker_rows = [("mergepilot-test-app",)] if marker_rows is None else marker_rows
    tables = list(mi.PRIVILEGE_CHECKED_TABLES) if tables is None else tables
    from postgres_source import PRIVILEGE_CHECKED_TABLES
    script = [
        ("one", role_row),
        ("one", (owner_count,)),
        ("all", marker_rows),
        ("all", [(t,) for t in tables]),
    ] + [("one", acl_row) for _ in PRIVILEGE_CHECKED_TABLES]
    return script


def _gate(testcase, fn, *args, code, **kwargs):
    with testcase.assertRaises(IntegrationGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertEqual(cm.exception.code, code, msg=str(cm.exception))
    return cm.exception


def _egate(testcase, fn, *args, code, **kwargs):
    with testcase.assertRaises(em.EvidenceGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertTrue(cm.exception.code.endswith(":" + code),
                        msg=cm.exception.code)
    return cm.exception


# ── Version window (inclusive; deliberate divergence) ────────────────────────

class TestVersionWindow(unittest.TestCase):

    def test_120000_inclusive_pass(self):
        self.assertTrue(check_server_version_window(120000))

    def test_180000_inclusive_pass(self):
        self.assertTrue(check_server_version_window(180000))

    def test_119999_rejected(self):
        _gate(self, check_server_version_window, 119999, code="WRONG_SERVER")

    def test_180001_rejected(self):
        _gate(self, check_server_version_window, 180001, code="WRONG_SERVER")

    def test_non_int_rejected(self):
        _gate(self, check_server_version_window, "160003", code="WRONG_SERVER")
        _gate(self, check_server_version_window, True, code="WRONG_SERVER")

    def test_divergence_from_phase_b_is_intentional(self):
        # Phase-B container gate: 120000 <= v < 180000 (upper-exclusive).
        # Integration gate (this module): inclusive on both boundaries.
        self.assertEqual(mi.VERSION_WINDOW_MIN, 120000)
        self.assertEqual(mi.VERSION_WINDOW_MAX, 180000)


# ── DB authorization context ─────────────────────────────────────────────────

class TestDbAuthorizationContext(unittest.TestCase):

    def test_valid_context_passes(self):
        validate_db_authorization_context(_ctx())

    def test_missing_field_rejected_no_inference(self):
        for key in ("database", "current_user", "application_name",
                    "server_version_num", "marker_value", "server_address",
                    "server_port", "captured_at"):
            c = _ctx()
            del c[key]
            _gate(self, validate_db_authorization_context, c,
                  code="AUTH_CONTEXT_INVALID")

    def test_wrong_database_rejected(self):
        _gate(self, validate_db_authorization_context,
              _ctx(database="other_db"), code="WRONG_DATABASE")

    def test_wrong_user_rejected(self):
        _gate(self, validate_db_authorization_context,
              _ctx(current_user="postgres"), code="WRONG_ROLE")

    def test_marker_mismatch_rejected(self):
        _gate(self, validate_db_authorization_context,
              _ctx(marker_value="wrong"), code="ENVIRONMENT_ID_MISMATCH")

    def test_version_outside_window_rejected(self):
        _gate(self, validate_db_authorization_context,
              _ctx(server_version_num=180001), code="WRONG_SERVER")

    def test_null_server_address_rejected(self):
        _gate(self, validate_db_authorization_context,
              _ctx(server_address="NULL"), code="WRONG_SERVER")
        # Empty string fails the generic non-empty scalar gate first.
        _gate(self, validate_db_authorization_context,
              _ctx(server_address=""), code="AUTH_CONTEXT_INVALID")

    def test_bad_port_rejected(self):
        _gate(self, validate_db_authorization_context,
              _ctx(server_port=0), code="WRONG_SERVER")
        _gate(self, validate_db_authorization_context,
              _ctx(server_port=70000), code="WRONG_SERVER")

    def test_container_fields_rejected_in_db_context(self):
        c = _ctx(image_digest="pgvector/pgvector@sha256:" + "a" * 64)
        _gate(self, validate_db_authorization_context, c,
              code="AUTH_CONTEXT_INVALID")

    def test_builder_validates_and_does_not_infer(self):
        ctx = build_db_authorization_context(**_ctx())
        self.assertEqual(ctx["database"], "mergepilot_audit")

    def test_builder_rejects_bad_values(self):
        bad = _ctx(server_version_num=119999)
        _gate(self, build_db_authorization_context, **bad,
              code="WRONG_SERVER")

    def test_deep_copy_isolated_from_caller(self):
        original = _ctx()
        copied = mi.deep_copy_context(original)
        original["marker_value"] = "tampered"
        self.assertEqual(copied["marker_value"], "mergepilot-test-app")


# ── Prerequisite probes (CHECK-ONLY) ─────────────────────────────────────────

class TestPrerequisiteChecks(unittest.TestCase):

    def test_all_probes_pass(self):
        cur = FakeCursor(_prereq_script())
        out = run_db_prerequisite_checks(cur)
        self.assertEqual(out["database"], "mergepilot_audit")
        self.assertEqual(out["current_user"], "mergepilot_reader")

    def test_zero_connections_on_validate_only_paths(self):
        # Pure validation functions issue NO cursor executes at all.
        cur = FakeCursor([])
        validate_db_authorization_context(_ctx())
        check_server_version_window(160003)
        self.assertEqual(cur.executed, [])

    def test_reader_role_absent(self):
        cur = FakeCursor(_prereq_script(role_row=None))
        _gate(self, run_db_prerequisite_checks, cur,
              code="DB_PREREQUISITE_MISSING")

    def test_reader_role_not_hardened_per_attribute(self):
        flags = (False, False, False, False, False)
        for i in range(5):
            bad = list(flags)
            bad[i] = True
            cur = FakeCursor(_prereq_script(role_row=tuple(bad)))
            _gate(self, run_db_prerequisite_checks, cur,
                  code="DB_PREREQUISITE_MISSING")

    def test_reader_in_owner_role_rejected(self):
        cur = FakeCursor(_prereq_script(owner_count=1))
        _gate(self, run_db_prerequisite_checks, cur,
              code="DB_PREREQUISITE_MISSING")

    def test_marker_zero_rows(self):
        cur = FakeCursor(_prereq_script(marker_rows=[]))
        _gate(self, run_db_prerequisite_checks, cur,
              code="ENVIRONMENT_ID_NOT_VERIFIED")

    def test_marker_two_rows(self):
        cur = FakeCursor(_prereq_script(
            marker_rows=[("mergepilot-test-app",), ("other",)]))
        _gate(self, run_db_prerequisite_checks, cur,
              code="ENVIRONMENT_ID_NOT_VERIFIED")

    def test_marker_value_mismatch(self):
        cur = FakeCursor(_prereq_script(marker_rows=[("wrong",)]))
        _gate(self, run_db_prerequisite_checks, cur,
              code="ENVIRONMENT_ID_MISMATCH")

    def test_missing_table_rejected(self):
        tables = [t for t in mi.PRIVILEGE_CHECKED_TABLES if t != "audit_events"]
        cur = FakeCursor(_prereq_script(tables=tables))
        _gate(self, run_db_prerequisite_checks, cur,
              code="DB_PREREQUISITE_MISSING")

    def test_acl_select_false_rejected(self):
        cur = FakeCursor(_prereq_script(acl_row=(False, False, False, False, False)))
        _gate(self, run_db_prerequisite_checks, cur,
              code="DB_PREREQUISITE_MISSING")

    def test_acl_any_write_true_rejected(self):
        base = [True, False, False, False, False]
        for i in range(1, 5):
            bad = list(base)
            bad[i] = True
            cur = FakeCursor(_prereq_script(acl_row=tuple(bad)))
            _gate(self, run_db_prerequisite_checks, cur,
                  code="DB_PREREQUISITE_MISSING")

    def test_emitted_sql_is_read_only_only(self):
        cur = FakeCursor(_prereq_script())
        run_db_prerequisite_checks(cur)
        forbidden_openers = ("CREATE", "GRANT", "REVOKE", "ALTER",
                             "INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP")
        for sql, _params in cur.executed:
            opener = sql.lstrip().split(None, 1)[0].upper()
            self.assertIn(opener, ("SELECT", "SHOW"))
            self.assertNotIn(opener, forbidden_openers)

    def test_assert_read_only_sql_rejects_mutations(self):
        for bad in ("INSERT INTO x VALUES (1)", "GRANT SELECT ON t TO r",
                    "CREATE ROLE x", "ALTER ROLE x", "DELETE FROM t",
                    "UPDATE t SET a=1", "TRUNCATE t", "DROP TABLE t"):
            _gate(self, mi._assert_read_only_sql, bad, code="CONFIG_INVALID")


# ── Fingerprint freeze / recheck ─────────────────────────────────────────────

class TestFingerprint(unittest.TestCase):

    def test_freeze_copies_and_validates(self):
        fp = freeze_execution_fingerprint(_ctx())
        self.assertEqual(fp["server_port"], 5432)
        self.assertNotIn("captured_at", fp)  # frozen keys only

    def test_freeze_missing_key_rejected(self):
        bad = _ctx()
        del bad["marker_value"]
        _gate(self, freeze_execution_fingerprint, bad,
              code="AUTH_CONTEXT_INVALID")

    def test_recheck_all_match_passes(self):
        before = freeze_execution_fingerprint(_ctx())
        after = recheck_execution_fingerprint(
            before, lambda: _ctx())
        self.assertEqual(after["database"], "mergepilot_audit")

    def test_recheck_drift_per_field(self):
        for key, value in (("database", "other"), ("current_user", "postgres"),
                           ("application_name", "x"), ("server_version_num", 1),
                           ("marker_value", "m"), ("server_address", "1.2.3.4"),
                           ("server_port", 9999)):
            before = freeze_execution_fingerprint(_ctx())
            _gate(self, recheck_execution_fingerprint, before,
                  lambda k=key, v=value: _ctx(**{k: v}),
                  code="ENVIRONMENT_FINGERPRINT_CHANGED")

    def test_recheck_probe_failure(self):
        before = freeze_execution_fingerprint(_ctx())
        def boom():
            raise RuntimeError("probe down")
        _gate(self, recheck_execution_fingerprint, before, boom,
              code="ENVIRONMENT_RECHECK_FAILED")


# ── Producer observation window ──────────────────────────────────────────────

class TestProducerWindow(unittest.TestCase):

    @staticmethod
    def _events(actions, task_id="run-x"):
        return [{"task_id": task_id, "action": a, "ts": "t"} for a in actions]

    def _run(self, events, timeout=300):
        polls = {"n": 0}
        def poll(run_id):
            polls["n"] += 1
            return events
        clock = {"t": 0.0}
        def clock_fn():
            return clock["t"]
        def sleep_fn(_s):
            clock["t"] += 1.0
        return observe_producer_window(
            run_id="run-x", poll_query=poll, timeout_seconds=timeout,
            clock_fn=clock_fn, sleep_fn=sleep_fn)

    def test_full_sequence_success(self):
        out = self._run(self._events(["review", "fix", "verify", "merge",
                                      "close_pr"]))
        self.assertTrue(out["succeeded"])
        self.assertEqual(out["observed_actions"],
                         list(mi.PRODUCER_ACTION_SEQUENCE))
        self.assertEqual(out["narrow_flag"],
                         "mergepilot_test_audit_producer_observed")
        # Producer contracts stay NOT_VERIFIED even on success.
        self.assertEqual(out["producer_contracts"]["audit_producer_contract"],
                         "NOT_VERIFIED")
        self.assertEqual(out["producer_contracts"]["revision_producer_contract"],
                         "NOT_VERIFIED")

    def test_timeout_returns_stable_code(self):
        out = self._run(self._events(["review", "fix"]))
        self.assertFalse(out["succeeded"])
        self.assertEqual(out["error_code"], "PRODUCER_WINDOW_TIMEOUT")

    def test_unrelated_task_id_not_counted(self):
        events = self._events(["review", "fix", "verify", "merge", "close_pr"],
                              task_id="OTHER") + \
                 self._events(["review"])
        out = self._run(events)
        self.assertFalse(out["succeeded"])
        self.assertEqual(out["error_code"], "PRODUCER_WINDOW_TIMEOUT")

    def test_out_of_order_sequence_times_out(self):
        out = self._run(self._events(["fix", "review", "verify", "merge",
                                      "close_pr"]))
        self.assertFalse(out["succeeded"])

    def test_no_fabrication_zero_polls_before_start(self):
        # The window only ever READS via poll_query; it never writes events.
        calls = []
        def poll(run_id):
            calls.append(run_id)
            return self._events(["review", "fix", "verify", "merge",
                                 "close_pr"])
        out = observe_producer_window(
            run_id="run-x", poll_query=poll, timeout_seconds=300,
            clock_fn=lambda: 0.0, sleep_fn=lambda s: None)
        self.assertTrue(out["succeeded"])
        self.assertTrue(all(c == "run-x" for c in calls))

    def test_timeout_bounds(self):
        for bad in (59, 901, 0, -1):
            _gate(self, observe_producer_window, run_id="r",
                  poll_query=lambda rid: [], timeout_seconds=bad,
                  clock_fn=lambda: 0.0, sleep_fn=lambda s: None,
                  code="CONFIG_INVALID")
        for good in (60, 900):
            out = observe_producer_window(
                run_id="r", poll_query=lambda rid: [], timeout_seconds=good,
                clock_fn=lambda t=[0.0]: (t.__setitem__(0, t[0] + good + 1)
                                          or t[0]), sleep_fn=lambda s: None)
            self.assertFalse(out["succeeded"])

    def test_retry_rules(self):
        failed = {"attempted": True, "succeeded": False,
                  "error_code": "PRODUCER_WINDOW_TIMEOUT",
                  "run_id": "run-1", "retry_count": 0}
        self.assertTrue(window_retry_allowed(failed, new_run_id="run-2"))
        self.assertFalse(window_retry_allowed(failed, new_run_id="run-1"),
                         "retry requires a NEW run_id")
        twice = dict(failed, retry_count=1)
        self.assertFalse(window_retry_allowed(twice, new_run_id="run-2"),
                         "at most one retry")
        identity_fail = dict(failed, error_code="WRONG_ROLE")
        self.assertFalse(window_retry_allowed(identity_fail, new_run_id="run-2"),
                         "identity-gate failures are not retryable")


# ── Kind isolation + status contract ─────────────────────────────────────────

class TestKindAndStatus(unittest.TestCase):

    def test_kind_isolation_ok(self):
        check_kind_isolation(MERGEPILOT_TEST_SOURCE_KIND,
                             MERGEPILOT_TEST_SOURCE_KIND)

    def test_kind_mismatch_both_directions(self):
        _gate(self, check_kind_isolation, MERGEPILOT_TEST_SOURCE_KIND,
              "POSTGRES_ISOLATED", code="KIND_MISMATCH")
        _gate(self, check_kind_isolation, "POSTGRES_ISOLATED",
              MERGEPILOT_TEST_SOURCE_KIND, code="KIND_MISMATCH")

    def test_unknown_kind_rejected(self):
        _gate(self, check_kind_isolation, "MYSQL", "MYSQL",
              code="KIND_MISMATCH")

    def test_status_contract_ok(self):
        assert_live_status_contract({
            "source_kind": MERGEPILOT_TEST_SOURCE_KIND,
            "source_read_only": True, "not_production": True,
            "production_resource_accessed": None,
            "production_resource_access_status": "NOT_MEASURED",
            "github_writes_enabled": False, "agent_control_enabled": False,
            "runtime_consumes_rag_context": False,
            "dynamic_pages_consume_live_api": True,
        })

    def test_status_contract_violations(self):
        base = {
            "source_kind": MERGEPILOT_TEST_SOURCE_KIND,
            "source_read_only": True, "not_production": True,
            "production_resource_accessed": None,
            "production_resource_access_status": "NOT_MEASURED",
            "github_writes_enabled": False, "agent_control_enabled": False,
            "runtime_consumes_rag_context": False,
            "dynamic_pages_consume_live_api": True,
        }
        for key, bad in (("source_kind", "POSTGRES_ISOLATED"),
                         ("source_read_only", False),
                         ("not_production", False),
                         ("production_resource_accessed", "yes"),
                         ("github_writes_enabled", True),
                         ("dynamic_pages_consume_live_api", False)):
            status = dict(base)
            status[key] = bad
            _gate(self, assert_live_status_contract, status,
                  code="STATUS_CONTRACT_VIOLATION")


# ── Mock snapshot source ─────────────────────────────────────────────────────

class TestSnapshotSource(unittest.TestCase):

    def test_kind_and_read_only(self):
        src = MergePilotTestSnapshotSource(lambda: b"{}", "run-x")
        self.assertEqual(src.kind, MERGEPILOT_TEST_SOURCE_KIND)
        self.assertTrue(src.read_only)

    def test_provider_must_be_callable(self):
        _gate(self, MergePilotTestSnapshotSource, "not-callable", "run-x",
              code="CONFIG_INVALID")

    def test_empty_run_id_rejected(self):
        _gate(self, MergePilotTestSnapshotSource, lambda: b"{}", "",
              code="CONFIG_INVALID")

    def test_read_and_close(self):
        src = MergePilotTestSnapshotSource(lambda: b"payload", "run-x")
        self.assertEqual(src.read_snapshot(), b"payload")
        src.close()
        _gate(self, src.read_snapshot, code="SOURCE_CLOSED")


# ── HTTP integration (real server + poller, mocked source) ───────────────────

class TestHttpIntegration(unittest.TestCase):

    def _bundle_bytes(self):
        bundle = {
            "schema_version": "mergepilot.demo-bundle.v1",
            "run": "run-http",
            "repo": "test/repo-alpha",
            "demo_mode": "ISOLATED_LIVE",
            "generated_at": "2026-08-14T00:00:00Z",
            "final_status": "PASS",
            "workflow_stages": [
                {"stage": "review", "agent_role": "reviewer",
                 "status": "COMPLETED"},
                {"stage": "fix", "agent_role": "fixer",
                 "status": "COMPLETED"},
                {"stage": "verify", "agent_role": "verifier",
                 "status": "COMPLETED", "verdict": "PASS"},
            ],
            "source_commit": "b" * 40,
            "verification_commit": None,
            "verification_commit_status": "NOT_AVAILABLE",
            "pr": {"number": 42, "base_sha": "a" * 40, "head_sha": "b" * 40,
                   "title": "t"},
            "findings": [],
            "fixes": [],
            "spans": [],
            "agents": [],
            "evidence_files": [],
            "rag_advisories": [],
            "rollback_events": [],
            "residue": {"pycache": 0},
            "secret_leaks": 0,
            "topology": {"nodes": []},
            "verifier_result": {"status": "PASS"},
            "benchmark_summary": {
                "benchmark_phase": "development",
                "confirmatory_all_ok": True,
                "dataset_version": "v0",
                "quality_gate_pass": True,
                "runtime_consumes_rag_context": False,
                "unique_case_count": 0,
                "workflow_utility_status":
                    "NOT_MEASURABLE_WITH_CURRENT_RUNTIME",
            },
        }
        bundle["bundle_sha256"] = compute_bundle_sha256(bundle)
        return json.dumps(bundle, sort_keys=True).encode("utf-8")

    def test_live_http_chain(self):
        payload = self._bundle_bytes()
        src = MergePilotTestSnapshotSource(lambda: payload, "run-http")
        poller = LivePoller(src, poll_interval=2.0)
        poller.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if poller.get_view()["state"] == "LIVE":
                break
            time.sleep(0.2)
        else:
            poller.stop(); poller.join(timeout=5)
            self.fail("poller never reached LIVE")
        server = create_server("127.0.0.1", 0, "ISOLATED_LIVE", poller=poller)
        host, port = server.server_address[:2]
        base = "http://127.0.0.1:%d" % port
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            # snapshot: 200 + sha recomputable
            with urllib.request.urlopen(base + "/api/live/snapshot",
                                        timeout=10) as r:
                self.assertEqual(r.status, 200)
                snap = json.loads(r.read().decode("utf-8"))
            self.assertEqual(
                snap["bundle_sha256"],
                compute_bundle_sha256({k: v for k, v in snap.items()
                                       if k != "bundle_sha256"}))
            # status: 200 + full hard-negative contract + real source kind
            with urllib.request.urlopen(base + "/api/live/status",
                                        timeout=10) as r:
                self.assertEqual(r.status, 200)
                status = json.loads(r.read().decode("utf-8"))
            self.assertEqual(status["source_kind"],
                             MERGEPILOT_TEST_SOURCE_KIND)
            assert_live_status_contract(status)
            # write methods: 405
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                req = urllib.request.Request(base + "/api/live/snapshot",
                                             method=method)
                try:
                    urllib.request.urlopen(req, timeout=10)
                    self.fail("%s should be 405" % method)
                except urllib.error.HTTPError as he:
                    self.assertEqual(he.code, 405)
        finally:
            integration_cleanup(server, poller, [src])
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(poller.is_alive())


# ── Cleanup with dual stable codes ───────────────────────────────────────────

class TestCleanup(unittest.TestCase):

    def test_clean_success(self):
        srv = mock.Mock()
        poller = mock.Mock()
        poller.is_alive.return_value = False
        src = mock.Mock()
        integration_cleanup(srv, poller, [src])
        srv.shutdown.assert_called_once()
        srv.server_close.assert_called_once()
        poller.stop.assert_called_once()
        src.close.assert_called_once()

    def test_http_shutdown_failure_code(self):
        srv = mock.Mock()
        srv.shutdown.side_effect = RuntimeError("boom")
        with self.assertRaises(IntegrationCleanupError) as cm:
            integration_cleanup(http_server=srv)
        self.assertIn("HTTP_SHUTDOWN_FAILED", cm.exception.cleanup_codes)

    def test_poller_stop_failure_and_alive_codes(self):
        poller = mock.Mock()
        poller.stop.side_effect = RuntimeError("boom")
        with self.assertRaises(IntegrationCleanupError) as cm:
            integration_cleanup(poller=poller)
        self.assertIn("POLLER_STOP_FAILED", cm.exception.cleanup_codes)
        alive = mock.Mock()
        alive.is_alive.return_value = True
        with self.assertRaises(IntegrationCleanupError) as cm2:
            integration_cleanup(poller=alive)
        self.assertIn("POLLER_STILL_ALIVE", cm2.exception.cleanup_codes)

    def test_source_close_failure_code(self):
        src = mock.Mock()
        src.close.side_effect = RuntimeError("boom")
        with self.assertRaises(IntegrationCleanupError) as cm:
            integration_cleanup(sources=[src])
        self.assertIn("SOURCE_CLOSE_FAILED", cm.exception.cleanup_codes)

    def test_multiple_codes_all_preserved(self):
        srv = mock.Mock()
        srv.shutdown.side_effect = RuntimeError("b")
        poller = mock.Mock()
        poller.stop.side_effect = RuntimeError("b")
        src = mock.Mock()
        src.close.side_effect = RuntimeError("b")
        with self.assertRaises(IntegrationCleanupError) as cm:
            integration_cleanup(srv, poller, [src])
        self.assertEqual(cm.exception.primary_code, "CLEANUP_RESIDUE")
        for code in ("HTTP_SHUTDOWN_FAILED", "POLLER_STOP_FAILED",
                     "SOURCE_CLOSE_FAILED"):
            self.assertIn(code, cm.exception.cleanup_codes)


# ── argv / secret safety ─────────────────────────────────────────────────────

class TestArgvAndSecrets(unittest.TestCase):

    def test_argv_rejects_secrets(self):
        _gate(self, assert_argv_safe, ["cmd", "supersecret123"],
              ["supersecret123"], code="ARGV_SECRET_LEAK")

    def test_argv_rejects_full_dsn(self):
        _gate(self, assert_argv_safe,
              ["psql", "postgresql://user:pw@host/db"],
              code="ARGV_SECRET_LEAK")

    def test_argv_rejects_sql_password_literal(self):
        _gate(self, assert_argv_safe,
              ["psql", "-c", "CREATE ROLE x PASSWORD 'abc'"],
              code="ARGV_SECRET_LEAK")

    def test_argv_accepts_clean(self):
        assert_argv_safe(["git", "status", "--porcelain"], ["not-present"])

    def test_redact_text(self):
        text = "connect postgresql://u:pw@h/db password=hunter2abc PASSWORD 'zz'"
        out = redact_text(text)
        self.assertNotIn("pw@h", out)
        self.assertNotIn("hunter2abc", out)
        self.assertNotIn("'zz'", out)
        self.assertIn("***REDACTED***", out)

    def test_redact_tokens(self):
        out = redact_text("token ghp_" + "a" * 36)
        self.assertNotIn("ghp_", out)

    def test_source_repr_has_no_dsn(self):
        src = MergePilotTestSnapshotSource(lambda: b"{}", "run-x")
        self.assertNotIn("password=", repr(src))
        self.assertNotIn("postgresql://", repr(src))


# ── Provenance schema 1.0/1.1 isolation ──────────────────────────────────────

class TestProvenanceSchemaIsolation(unittest.TestCase):

    SHA_1 = "a" * 40
    SHA_2 = "b" * 40
    TREE_1 = "c" * 40

    def _integration_manifest(self, **overrides):
        m = em.build_manifest(
            evidence_id="mt-integration-rec-1",
            generated_at="2026-08-14T00:00:00Z",
            evidence_provenance_mode="MERGEPILOT_TEST_INTEGRATION_RECORD",
            execution_provenance={
                "execution_commit": self.SHA_1,
                "execution_tree_oid": self.TREE_1,
                "execution_worktree_clean": True,
                "execution_worktree_porcelain": "",
                "execution_ref": "feat/x",
                "execution_remote_ref_oid": self.SHA_1,
                "captured_at": "2026-08-14T00:00:00Z",
            },
            merge_commit=self.SHA_2,
            parent_commits=[self.SHA_2, self.SHA_1],
            m7_closed={"object": "d" * 40, "peeled": "e" * 40,
                       "unchanged": True},
            image_digest="",
            local_image_id="",
        )
        m["schema_version"] = "1.1"
        m["db_authorization_context"] = dict(GOOD_CTX)
        m["producer_observation"] = {
            "attempted": True, "succeeded": True, "error_code": "",
            "run_id": "run-x", "window_started_at": 0.0,
            "window_ended_at": 1.0,
            "observed_actions": list(mi.PRODUCER_ACTION_SEQUENCE),
            "retry_count": 0,
        }
        m.update(overrides)
        return m

    def test_integration_manifest_1_1_valid(self):
        em.validate_manifest(self._integration_manifest())

    def test_integration_mode_with_schema_1_0_rejected(self):
        m = self._integration_manifest()
        m["schema_version"] = "1.0"
        _egate(self, em.validate_manifest, m, code="PROVENANCE_MISMATCH")

    def test_historical_mode_with_schema_1_1_rejected(self):
        m = self._integration_manifest()
        m["schema_version"] = "1.1"
        m["evidence_provenance_mode"] = "HISTORICAL_PHASE_B_RECORD"
        # historical requires its own commit + doc ref; the schema pairing
        # fires regardless — assert PROVENANCE_MISMATCH family.
        _egate(self, em.validate_manifest, m, code="PROVENANCE_MISMATCH")

    def test_container_fields_in_integration_rejected(self):
        m = self._integration_manifest(
            image_digest="pgvector/pgvector@sha256:" + "a" * 64)
        _egate(self, em.detect_cross_mode_reuse, m,
               code="PROVENANCE_MISMATCH")

    def test_integration_fields_in_historical_rejected(self):
        m = {
            "evidence_provenance_mode": "HISTORICAL_PHASE_B_RECORD",
            "db_authorization_context": dict(GOOD_CTX),
        }
        _egate(self, em.detect_cross_mode_reuse, m,
               code="PROVENANCE_MISMATCH")

    def test_historical_artifact_identity_frozen(self):
        em.validate_historical_artifact_identity(
            em.HISTORICAL_ARTIFACT_REL_PATH, em.HISTORICAL_ARTIFACT_SHA256)
        _egate(self, em.validate_historical_artifact_identity,
               "other/path.json", em.HISTORICAL_ARTIFACT_SHA256,
               code="PROVENANCE_MISMATCH")
        _egate(self, em.validate_historical_artifact_identity,
               em.HISTORICAL_ARTIFACT_REL_PATH, "0" * 64,
               code="PROVENANCE_MISMATCH")

    def test_historical_mode_still_requires_schema_1_0(self):
        self.assertEqual(em.MODE_REQUIRED_SCHEMA["HISTORICAL_PHASE_B_RECORD"],
                         "1.0")
        self.assertEqual(
            em.MODE_REQUIRED_SCHEMA["MERGEPILOT_TEST_INTEGRATION_RECORD"],
            "1.1")

    def test_unknown_schema_version_still_rejected(self):
        m = self._integration_manifest()
        m["schema_version"] = "1.2"
        _egate(self, em.validate_manifest, m, code="SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()
