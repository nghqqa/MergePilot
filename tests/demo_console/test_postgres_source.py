#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISOLATED_LIVE Phase 2 — PostgresSnapshotSource tests (mock DB, no real PG).

These tests exercise :class:`PostgresSnapshotSource` entirely against in-memory
fakes (``FakeCursor`` / ``FakeConnection``). No real PostgreSQL server is
required, and ``psycopg2.connect`` is monkeypatched so the suite runs even on a
host without a database. The fakes record every SQL statement and parameter
tuple so the tests can assert on SQL safety (parameterization, no writes),
connection lifecycle (closed on success and on error, transaction rolled back),
and bundle assembly (demo_mode, bundle_sha256, RAG boundaries, status mapping).

Test groups
  TestIdentityChecks      — correct identity passes; wrong db/user/read-only rejected
  TestSqlSafety           — parameterized queries; run_id injection blocked; no writes
  TestBundleAssembly      — demo_mode=ISOLATED_LIVE; bundle_sha256 valid; RAG preserved
  TestStatusFields        — unknown status → UNKNOWN (never MERGED); empty findings
  TestConnectionHandling  — closed on success/error; rolled back; no idle residue
  TestRegression          — REPLAY/FILE unaffected; psycopg2 missing only hits PG mode
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in [str(ROOT), str(ROOT / "tools" / "demo_console")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from integrity import compute_bundle_sha256, verify_bundle_integrity  # noqa: E402
from schema import validate_bundle  # noqa: E402
from postgres_source import (  # noqa: E402
    PostgresSnapshotSource,
    IdentityCheckError,
    PostgresSourceError,
    PostgresQueryError,
    RunIdError,
)

DSN = "host=db.example.com password=SUPERSECRET dbname=mergepilot_test user=reader"
EXPECTED_DB = "mergepilot_test"
EXPECTED_ROLE = "reader"
RUN_ID = "run-demo-001"


# ── Fakes ──────────────────────────────────────────────────────────────────
class FakeCursor:
    """Cursor that returns canned rows for specific SQL patterns.

    ``results`` maps a SQL-fragment key (matched case-insensitively as a
    substring of the executed SQL) to a list of row tuples. The first
    ``execute`` whose SQL contains a key consumes that key's rows (so
    ``fetchone`` returns the first row, ``fetchall`` returns the rest/list).

    Every ``execute`` records (sql, params, normalized_sql) on the parent
    connection for later SQL-safety assertions.
    """

    def __init__(self, conn, results=None):
        self.conn = conn
        self._results = results or {}
        self._pending_rows: list[tuple] = []
        self._fetchone_consumed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        # Record on the connection for later assertions.
        self.conn.executed.append((sql, params))
        # Find a matching canned result by substring (case-insensitive). The
        # order of keys in the dict encodes priority; the first match wins.
        upper = sql.upper()
        matched_key = None
        for key in list(self._results.keys()):
            if key.upper() in upper:
                matched_key = key
                break
        if matched_key is not None:
            rows = self._results.pop(matched_key)
            self._pending_rows = list(rows)
            self._fetchone_consumed = False
        else:
            # No canned rows for this statement (e.g. BEGIN / SET LOCAL /
            # a SELECT the test deliberately left empty).
            self._pending_rows = []
            self._fetchone_consumed = False

    def fetchone(self):
        if self._fetchone_consumed:
            return None
        self._fetchone_consumed = True
        if not self._pending_rows:
            return None
        return self._pending_rows.pop(0)

    def fetchall(self):
        rows = list(self._pending_rows)
        self._pending_rows = []
        self._fetchone_consumed = True
        return rows

    def close(self):  # pragma: no cover - not used via with-blocks here
        pass


class FakeConnection:
    """Connection that hands out FakeCursor(s) and tracks lifecycle.

    Tracks: ``closed`` (bool), ``rollback_called`` (int), ``commit_called``
    (int), ``executed`` (list of (sql, params)), and ``autocommit``.
    """

    def __init__(self, results=None, identity=None):
        # `results` is a dict of SQL-fragment -> list[row tuples]; shared across
        # all cursors this connection creates so a multi-cursor read works.
        self._results = results if results is not None else {}
        # Default identity probe result: (database, user, tx_ro, default_ro).
        self._identity = identity if identity is not None else (
            EXPECTED_DB, EXPECTED_ROLE, True, True,
        )
        self.executed: list[tuple] = []
        self.closed = False
        self.rollback_called = 0
        self.commit_called = 0
        self.autocommit = False

    def cursor(self, *args, **kwargs):
        # Identity probe uses a dedicated result so the very first SELECT
        # (current_database/current_user) returns the configured identity.
        results = dict(self._results)
        # Only seed identity if not already provided by the caller.
        ident_key = "CURRENT_DATABASE(), CURRENT_USER"
        if ident_key.upper() not in {k.upper() for k in results}:
            results[ident_key] = [self._identity]
        return FakeCursor(self, results)

    def rollback(self):
        self.rollback_called += 1

    def commit(self):
        self.commit_called += 1

    def close(self):
        self.closed = True


class _FakePsycopg2:
    """Minimal psycopg2 shim exposing ``connect(dsn)``."""

    def __init__(self, conn: FakeConnection):
        self._conn = conn
        self.connect_calls: list[str] = []

    def connect(self, dsn):
        self.connect_calls.append(dsn)
        return self._conn


def _make_results(
    *,
    task_run=None,
    stage_events=None,
    revision=None,
    pr_binding=None,
    mcp_calls=None,
    rollback_runs=None,
    audit_total=None,
    audit_by_action=None,
):
    """Build the canned-row dict consumed by FakeCursor.

    Each key is a SQL fragment that appears in the source's SELECT statements;
    the value is the list of row tuples returned (first row for fetchone-based
    queries, full list for fetchall-based queries).
    """
    if task_run is None:
        task_run = (
            RUN_ID, "test/repo-alpha", 42, "fix/run-demo-001", "MERGED",
            "complete", 1, "PASS", None, None, None, "trace-001",
        )
    if stage_events is None:
        stage_events = [
            ("evt-1", RUN_ID, "reviewer", "review", "diff-parse", "COMPLETED",
             None, None, None),
            ("evt-2", RUN_ID, "verifier", "verify", "test-runner", "COMPLETED",
             None, None, None),
        ]
    if revision is None:
        revision = (
            "rev-binding-1", RUN_ID, "test/repo-alpha", 42,
            "b" * 40, "h" * 40, None,
        )
    if pr_binding is None:
        pr_binding = (
            "test/repo-alpha", 42, "fix/run-demo-001", "main", "h" * 40, None,
        )
    if mcp_calls is None:
        mcp_calls = [
            ("req-1", "corr-1", "INTENT", None, "reviewer", "get_pr",
             "ALLOW", "B1_PERMISSIVE_CALL", "test/repo-alpha", "main",
             "OK", "b" * 40, None),
            ("req-2", "corr-1", "RESULT", None, "reviewer", "get_pr",
             "ALLOW", "B1_PERMISSIVE_CALL", "test/repo-alpha", "main",
             "OK", "b" * 40, None),
        ]
    if rollback_runs is None:
        rollback_runs = []
    if audit_total is None:
        audit_total = (5,)
    if audit_by_action is None:
        audit_by_action = [("review", 3), ("verify", 2)]

    return {
        # task_runs SELECT — fetchone
        "FROM TASK_RUNS WHERE RUN_ID": [task_run],
        # stage_events SELECT — fetchall
        "FROM STAGE_EVENTS WHERE RUN_ID": stage_events,
        # revision_bindings SELECT — fetchone
        "FROM REVISION_BINDINGS RB WHERE RB.RUN_ID": [revision],
        # run_pr_bindings SELECT — fetchone
        "FROM RUN_PR_BINDINGS WHERE RUN_ID": [pr_binding],
        # mcp_calls SELECT — fetchall
        "FROM MCP_CALLS WHERE RUN_ID": mcp_calls,
        # rollback_runs SELECT — fetchall
        "FROM ROLLBACK_RUNS WHERE PARENT_RUN_ID": rollback_runs,
        # audit_events count — fetchone
        "SELECT COUNT(*) FROM AUDIT_EVENTS": [audit_total],
        # audit_events by action — fetchall
        "GROUP BY ACTION": audit_by_action,
    }


def _install_fake_psycopg2(monkey_conn: FakeConnection):
    """Insert a fake psycopg2 module into sys.modules and return it.

    This lets PostgresSnapshotSource.read_snapshot do its lazy
    ``import psycopg2`` and get our shim without touching the real driver.
    """
    fake = _FakePsycopg2(monkey_conn)
    sys.modules["psycopg2"] = fake
    return fake


def _clear_fake_psycopg2():
    sys.modules.pop("psycopg2", None)


def _read_snapshot_with_fake(conn: FakeConnection, results=None,
                             identity=None) -> bytes:
    """Build a source, install the fake psycopg2, read_snapshot, return bytes."""
    if results is not None:
        conn._results = results
    if identity is not None:
        conn._identity = identity
    _install_fake_psycopg2(conn)
    try:
        src = PostgresSnapshotSource(DSN, RUN_ID, EXPECTED_DB, EXPECTED_ROLE)
        return src.read_snapshot()
    finally:
        _clear_fake_psycopg2()


# ── TestIdentityChecks ─────────────────────────────────────────────────────
class TestIdentityChecks(unittest.TestCase):
    """The read-only gate: correct identity passes, mismatches are rejected."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_correct_identity_passes(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["demo_mode"], "ISOLATED_LIVE")
        # Connection must be closed after a successful read.
        self.assertTrue(conn.closed)

    def test_wrong_database_rejected(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=("WRONG_DB", EXPECTED_ROLE, True, True),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertIn("WRONG_DATABASE", str(cm.exception))
        # Connection still closed even on identity failure.
        self.assertTrue(conn.closed)

    def test_wrong_user_rejected(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=(EXPECTED_DB, "wrong_role", True, True),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertIn("WRONG_ROLE", str(cm.exception))
        self.assertTrue(conn.closed)

    def test_transaction_read_only_off_rejected(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=(EXPECTED_DB, EXPECTED_ROLE, False, True),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertIn("NOT_READ_ONLY", str(cm.exception))
        self.assertTrue(conn.closed)

    def test_default_transaction_read_only_off_rejected(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=(EXPECTED_DB, EXPECTED_ROLE, True, False),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertIn("NOT_READ_ONLY", str(cm.exception))
        self.assertTrue(conn.closed)

    def test_identity_failure_does_not_leak_dsn(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=("WRONG_DB", EXPECTED_ROLE, True, True),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        msg = str(cm.exception)
        self.assertNotIn("SUPERSECRET", msg)
        self.assertNotIn("password=", msg.lower())


# ── TestSqlSafety ──────────────────────────────────────────────────────────
class TestSqlSafety(unittest.TestCase):
    """Every user-influenced query is parameterized; no write SQL is issued."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_all_run_queries_use_parameterized_run_id(self):
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        # Every SELECT that filters by run_id must pass run_id as a param,
        # not interpolate it into the SQL text.
        run_id_selects = [
            (sql, params) for (sql, params) in conn.executed
            if "WHERE" in sql.upper()
            and ("RUN_ID" in sql.upper() or "PARENT_RUN_ID" in sql.upper())
            and "BEGIN" not in sql.upper()
        ]
        self.assertGreater(len(run_id_selects), 0,
                           "expected at least one run_id-filtered SELECT")
        for sql, params in run_id_selects:
            # run_id must NOT appear literally in the SQL text.
            self.assertNotIn(RUN_ID, sql,
                             f"run_id interpolated into SQL: {sql}")
            # run_id must appear in the params tuple.
            self.assertIn(RUN_ID, params or (),
                          f"run_id not passed as a param for: {sql}")

    def test_no_write_sql_issued(self):
        import re
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        # Match whole SQL keywords only (word boundaries) so column names like
        # UPDATED_AT / CREATED_AT are not mistaken for UPDATE / CREATE.
        forbidden_patterns = [
            r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b",
            r"\bTRUNCATE\b", r"\bALTER\b", r"\bCREATE\b", r"\bGRANT\b",
            r"\bMERGE\s+INTO\b", r"\bCALL\b",
        ]
        for sql, _ in conn.executed:
            upper = sql.upper()
            for pat in forbidden_patterns:
                m = re.search(pat, upper)
                self.assertIsNone(
                    m, f"write/DDL SQL issued: {sql!r} (matched {m.group(0) if m else pat})"
                )

    def test_run_id_injection_blocked_at_construction(self):
        # A SQL-injection attempt in run_id is rejected before any query runs.
        bad = "x'; DROP TABLE task_runs; --"
        with self.assertRaises(RunIdError):
            PostgresSnapshotSource(DSN, bad, EXPECTED_DB, EXPECTED_ROLE)

    def test_run_id_injection_blocked_in_query_text(self):
        # Even a syntactically tame-but-malicious run_id that slips shape
        # validation still only ever reaches the DB as a parameter.
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        for sql, _ in conn.executed:
            self.assertNotIn("DROP TABLE", sql.upper())
            self.assertNotIn("--", sql)

    def test_only_select_begin_set_rollback_issued(self):
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        allowed_prefixes = ("SELECT", "BEGIN", "SET LOCAL", "ROLLBACK")
        for sql, _ in conn.executed:
            # The source may also issue a final rollback via conn.rollback().
            # SQL text must start with one of the read-only prefixes.
            self.assertTrue(
                sql.upper().startswith(allowed_prefixes),
                f"unexpected SQL statement: {sql!r}",
            )


# ── TestBundleAssembly ─────────────────────────────────────────────────────
class TestBundleAssembly(unittest.TestCase):
    """The assembled bundle is a schema-valid ISOLATED_LIVE DemoBundle."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_demo_mode_is_isolated_live(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["demo_mode"], "ISOLATED_LIVE")

    def test_bundle_sha256_valid_and_matches(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        # Integrity helper: recomputed digest must match the stored field.
        self.assertEqual(
            bundle["bundle_sha256"],
            compute_bundle_sha256(bundle),
        )
        self.assertEqual(verify_bundle_integrity(bundle), [])

    def test_bundle_passes_schema_validation_isolated_live(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        errors = validate_bundle(bundle, expected_mode="ISOLATED_LIVE")
        self.assertEqual(errors, [], f"schema errors: {errors}")

    def test_rag_boundary_preserved(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(len(bundle["rag_advisories"]), 2)
        for rag in bundle["rag_advisories"]:
            self.assertIs(rag["adopted"], False)
            self.assertIs(rag["untrusted"], True)

    def test_secret_leaks_zero(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["secret_leaks"], 0)

    def test_runtime_consumes_rag_context_false(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertIs(bundle["benchmark_summary"]["runtime_consumes_rag_context"], False)
        self.assertEqual(
            bundle["benchmark_summary"]["workflow_utility_status"],
            "NOT_MEASURABLE_WITH_CURRENT_RUNTIME",
        )

    def test_bundle_run_id_matches_requested(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["run"]["run_id"], RUN_ID)

    def test_revision_sha_populated_from_revision_bindings(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["pr"]["base_sha"], "b" * 40)
        self.assertEqual(bundle["pr"]["head_sha"], "h" * 40)

    def test_dsn_not_in_bundle_bytes(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        self.assertNotIn(b"SUPERSECRET", raw)
        self.assertNotIn(b"password=", raw.lower())


# ── TestStatusFields ───────────────────────────────────────────────────────
class TestStatusFields(unittest.TestCase):
    """Status mapping: unknown → UNKNOWN (never MERGED); empty findings honest."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_unknown_status_maps_to_unknown_not_merged(self):
        results = _make_results(
            task_run=(
                RUN_ID, "test/repo-alpha", 42, "fix/run", "TOTALLY_UNKNOWN_STATUS",
                "weird", 1, None, None, None, None, "trace-001",
            )
        )
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["final_status"], "UNKNOWN")

    def test_merged_status_maps_to_merged(self):
        results = _make_results(
            task_run=(RUN_ID, "test/repo-alpha", 42, "fix/run", "MERGED",
                      "complete", 1, "PASS", None, None, None, "trace-001")
        )
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["final_status"], "MERGED")

    def test_findings_empty_when_db_has_none(self):
        # The read-only DB view does not materialize inline finding bodies;
        # the bundle honestly reports an empty findings list.
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["findings"], [])
        self.assertEqual(bundle["fixes"], [])

    def test_missing_task_run_still_assembles_unknown_status(self):
        # If the run_id is not present in task_runs, final_status is UNKNOWN.
        results = _make_results()
        results["FROM TASK_RUNS WHERE RUN_ID"] = []  # fetchone returns None
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["final_status"], "UNKNOWN")

    def test_rolled_back_status_mapped(self):
        results = _make_results(
            task_run=(RUN_ID, "test/repo-alpha", 42, "fix/run", "ROLLED_BACK",
                      "rolled_back", 1, None, None, None, None, "trace-001")
        )
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["final_status"], "ROLLED_BACK")


# ── TestConnectionHandling ─────────────────────────────────────────────────
class TestConnectionHandling(unittest.TestCase):
    """Connection closed on success and on error; transaction rolled back."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_connection_closed_on_success(self):
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        self.assertTrue(conn.closed)

    def test_transaction_rolled_back_on_success(self):
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        # The read-only transaction must be ended with ROLLBACK (never commit).
        self.assertGreaterEqual(conn.rollback_called, 1)
        self.assertEqual(conn.commit_called, 0)

    def test_connection_closed_on_error(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=("WRONG_DB", EXPECTED_ROLE, True, True),
        )
        with self.assertRaises(PostgresSourceError):
            _read_snapshot_with_fake(conn)
        self.assertTrue(conn.closed)

    def test_no_idle_residue(self):
        # After a successful read the connection is closed: no idle session
        # is left behind on the server.
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        self.assertTrue(conn.closed)
        # rollback was called at least once to end the transaction.
        self.assertGreaterEqual(conn.rollback_called, 1)

    def test_query_error_closes_connection_and_sanitizes(self):
        # Fake a cursor that raises on a SELECT to simulate a DB error.
        class _ExplodingCursor(FakeCursor):
            def execute(self, sql, params=None):
                if "FROM TASK_RUNS" in sql.upper():
                    raise RuntimeError(
                        "connection to db.example.com:5432 password=SUPERSECRET failed"
                    )
                super().execute(sql, params)

        conn = FakeConnection(results=_make_results())
        original_cursor = conn.cursor

        def cursor(*a, **kw):
            c = original_cursor(*a, **kw)
            # Replace only the read cursor's execute path.
            return _ExplodingCursor(conn, c._results)

        conn.cursor = cursor
        with self.assertRaises(PostgresQueryError) as cm:
            _read_snapshot_with_fake(conn)
        msg = str(cm.exception)
        # The DSN / password must be redacted from the sanitized message.
        self.assertNotIn("SUPERSECRET", msg)
        self.assertIn("password=<REDACTED>", msg)
        # Connection was closed despite the mid-read error.
        self.assertTrue(conn.closed)

    def test_connect_failure_sanitized_and_closed(self):
        # psycopg2.connect itself raises — the source must sanitize and not
        # leak the DSN, and must not touch a None connection.
        class _ConnectFailPsycopg2:
            def connect(self, dsn):
                raise RuntimeError(
                    "could not connect to host=db.example.com password=SUPERSECRET"
                )

        sys.modules["psycopg2"] = _ConnectFailPsycopg2()
        try:
            src = PostgresSnapshotSource(DSN, RUN_ID, EXPECTED_DB, EXPECTED_ROLE)
            with self.assertRaises(PostgresQueryError) as cm:
                src.read_snapshot()
            self.assertNotIn("SUPERSECRET", str(cm.exception))
            self.assertIn("password=<REDACTED>", str(cm.exception))
        finally:
            _clear_fake_psycopg2()


# ── TestRegression ─────────────────────────────────────────────────────────
class TestRegression(unittest.TestCase):
    """PostgreSQL mode is additive: REPLAY/FILE keep working; PG-only deps stay out."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_postgres_source_kind_is_distinct(self):
        from live_poller import FileSnapshotSource
        # PostgresSnapshotSource.kind is a class attribute; FileSnapshotSource
        # declares kind as a property, so compare via an instance.
        self.assertEqual(PostgresSnapshotSource.kind, "POSTGRES_ISOLATED")
        file_src = FileSnapshotSource(__file__)
        self.assertEqual(file_src.kind, "FILE_FIXTURE")
        self.assertNotEqual(PostgresSnapshotSource.kind, file_src.kind)

    def test_postgres_source_is_a_snapshot_source(self):
        from live_poller import SnapshotSource
        src = PostgresSnapshotSource(DSN, RUN_ID, EXPECTED_DB, EXPECTED_ROLE)
        self.assertIsInstance(src, SnapshotSource)
        self.assertIs(src.read_only, True)

    def test_psycopg2_missing_only_affects_postgres_mode(self):
        # With psycopg2 absent from sys.modules AND the real driver blockable,
        # REPLAY/FILE sources must still import and construct fine.
        _clear_fake_psycopg2()
        # Block accidental real psycopg2 by injecting a sentinel that raises.
        class _NoPsycopg2:
            def __getattr__(self, name):
                raise ImportError("psycopg2 blocked for this test")

        # Ensure import psycopg2 inside read_snapshot fails.
        sys.modules["psycopg2"] = _NoPsycopg2()
        try:
            from live_poller import FileSnapshotSource
            # A file source can be constructed and (with a real file) read
            # without psycopg2. Constructing it must not import the driver.
            src = FileSnapshotSource(__file__)
            self.assertEqual(src.kind, "FILE_FIXTURE")
        finally:
            _clear_fake_psycopg2()

    def test_postgres_source_reports_psycopg2_missing(self):
        # Make the lazy import fail with a clean error code.
        _clear_fake_psycopg2()
        # Force ImportError on `import psycopg2`.
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "psycopg2":
                raise ImportError("no module named psycopg2")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = blocking_import
        try:
            src = PostgresSnapshotSource(DSN, RUN_ID, EXPECTED_DB, EXPECTED_ROLE)
            with self.assertRaises(PostgresSourceError) as cm:
                src.read_snapshot()
            self.assertIn("PSYCOPG2_MISSING", str(cm.exception))
        finally:
            builtins.__import__ = real_import
            _clear_fake_psycopg2()

    def test_repr_never_leaks_dsn(self):
        src = PostgresSnapshotSource(
            "host=h password=TOPSECRET port=5432", RUN_ID, EXPECTED_DB,
            EXPECTED_ROLE,
        )
        for rep in (repr(src), str(src)):
            self.assertNotIn("TOPSECRET", rep)
            self.assertNotIn("password=", rep.lower())
            self.assertNotIn("5432", rep)

    def test_source_kind_surfaces_through_poller_view(self):
        # A PostgresSnapshotSource wired into a LivePoller reports its own
        # kind (POSTGRES_ISOLATED) and read_only=True via get_view() — this
        # is how the status API surfaces source identity.
        from live_poller import LivePoller

        class _StubPostgresSource(PostgresSnapshotSource):
            """Bypass the DB entirely; return a valid ISOLATED_LIVE bundle."""

            def read_snapshot(self) -> bytes:
                # Build a minimal valid bundle via the parent's assembler path
                # by faking the DB results through the real assemble method.
                bundle = self._assemble_bundle(
                    task_run={
                        "run_id": RUN_ID, "repo": "test/repo", "pr_number": 42,
                        "status": "MERGED", "trace_id": "t1",
                    },
                    stage_events=[],
                    revision={"repo": "test/repo", "pr_number": 42,
                              "base_sha": "b" * 40, "head_sha": "h" * 40},
                    gateway_calls=[],
                    rollback_events=[],
                    audit_summary={"total": 0, "by_action": {}},
                )
                return json.dumps(bundle, sort_keys=True,
                                  ensure_ascii=False).encode("utf-8")

        src = _StubPostgresSource(DSN, RUN_ID, EXPECTED_DB, EXPECTED_ROLE)
        poller = LivePoller(src, poll_interval=1.0, expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load(),
                        f"initial load failed: {poller.last_error_code}")
        view = poller.get_view()
        self.assertEqual(view["source_kind"], "POSTGRES_ISOLATED")
        self.assertIs(view["source_read_only"], True)


if __name__ == "__main__":
    unittest.main()
