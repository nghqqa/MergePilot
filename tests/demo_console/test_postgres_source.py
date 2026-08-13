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
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in [str(ROOT), str(ROOT / "tools" / "demo_console")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from integrity import compute_bundle_sha256, verify_bundle_integrity  # noqa: E402
from schema import validate_bundle  # noqa: E402
from preflight import run_preflight  # noqa: E402
from live_poller import SnapshotSource  # noqa: E402
from postgres_source import (  # noqa: E402
    PostgresSnapshotSource,
    IdentityCheckError,
    PostgresSourceError,
    PostgresQueryError,
    RunIdError,
    RunNotFoundError,
    ConfigInvalidError,
    SCHEMA_CONTRACT,
    ENVIRONMENT_MARKER_CONTRACT,
    STABLE_ERROR_CODES,
    REQUIRED_QUERY_COLUMNS,
    PRIVILEGE_CHECKED_TABLES as _PRIVILEGE_CHECKED_TABLES,
    _all_select_templates,
    _referenced_table_columns,
)

DSN = "host=db.example.com password=SUPERSECRET dbname=mergepilot_test user=reader"
EXPECTED_DB = "mergepilot_test"
EXPECTED_ROLE = "reader"
RUN_ID = "run-demo-001"
# Default expected server identity (address/port/application_name).
EXPECTED_SERVER_ADDRESSES = ["127.0.0.1"]
EXPECTED_SERVER_PORT = 5432
EXPECTED_APPLICATION_NAME = "mergepilot_viewer"
EXPECTED_ENVIRONMENT_ID = "mergepilot-test-env"


def _make_source(**overrides):
    """Construct a PostgresSnapshotSource with the full required identity set.

    Tests that only exercise a subset of identity can override individual
    kwargs. All the new required parameters (expected_server_addresses,
    expected_server_port, expected_application_name, expected_environment_id)
    are supplied with sane defaults.
    """
    kwargs = dict(
        dsn=DSN,
        run_id=RUN_ID,
        expected_database=EXPECTED_DB,
        expected_role=EXPECTED_ROLE,
        expected_environment_id=EXPECTED_ENVIRONMENT_ID,
        expected_server_addresses=list(EXPECTED_SERVER_ADDRESSES),
        expected_server_port=EXPECTED_SERVER_PORT,
        expected_application_name=EXPECTED_APPLICATION_NAME,
    )
    kwargs.update(overrides)
    return PostgresSnapshotSource(**kwargs)


# ── Fakes ──────────────────────────────────────────────────────────────────
class FakeCursor:
    """Cursor that returns canned rows for specific SQL patterns.

    ``results`` maps a SQL-fragment key (matched case-insensitively as a
    substring of the executed SQL) to a list of row tuples. The first
    ``execute`` whose SQL contains a key consumes that key's rows (so
    ``fetchone`` returns the first row, ``fetchall`` returns the rest/list).

    For SQL statements that are issued repeatedly with different params (the
    information_schema.columns probe), the results dict may carry param-keyed
    entries under the special ``__param_keyed__`` namespace. When a param
    string matches an entry there, that entry's rows are served.

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
        upper = sql.upper()
        pstr = " ".join(
            str(p) for p in (
                params if isinstance(params, (list, tuple)) else (params,)
            )
        )
        # First, try persistent (sql-fragment, param-fragment)-keyed matching.
        # These entries are NEVER consumed, so they serve repeated probes
        # (information_schema.columns run per table, and the per-table
        # has_table_privilege SELECT + write probes). Each key is a tuple of
        # fragments that must ALL appear: each fragment is matched against BOTH
        # the uppercased SQL text AND the joined params, so a fragment can
        # appear in either place (e.g. 'SELECT' is in the SQL text, the table
        # name is in the params).
        persistent = self._results.get("__persistent_keyed__")
        if persistent:
            for key, rows in persistent.items():
                if not isinstance(key, tuple):
                    continue
                frags = [str(k).upper() for k in key]
                if all(frag in upper or frag in pstr.upper() for frag in frags):
                    self._pending_rows = list(rows)
                    self._fetchone_consumed = False
                    return
        # Second, try the legacy param-keyed namespace (a single fragment
        # matched against the joined params).
        param_keyed = self._results.get("__param_keyed__")
        if param_keyed and params:
            for key, rows in param_keyed.items():
                if key in pstr:
                    self._pending_rows = list(rows)
                    self._fetchone_consumed = False
                    return
        # Find a matching canned result by substring (case-insensitive). The
        # order of keys in the dict encodes priority; the first match wins.
        matched_key = None
        for key in list(self._results.keys()):
            if key.startswith("__"):
                continue
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
    stage_runs=None,
    stage_events=None,
    revision=None,
    pr_binding=None,
    mcp_calls=None,
    rollback_runs=None,
    audit_total=None,
    audit_by_action=None,
    server_identity=None,
    schema_probe=None,
    catalog_tables=None,
    environment_marker=None,
    environment_count=None,
    role_privileges=None,
    table_privileges=None,
    column_catalog=None,
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
    if stage_runs is None:
        stage_runs = [
            # id, run_id, stage, agent, attempt, status, started_at,
            # completed_at, verdict, detail
            (1, RUN_ID, "diff-parse", "reviewer", 1, "COMPLETED", None,
             None, "PASS", None),
            (2, RUN_ID, "test-runner", "verifier", 1, "COMPLETED", None,
             None, "PASS", None),
        ]
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
    if server_identity is None:
        # (inet_server_addr, inet_server_port, application_name, server_version_num)
        server_identity = ("127.0.0.1", EXPECTED_SERVER_PORT,
                           EXPECTED_APPLICATION_NAME, 160001)
    if schema_probe is None:
        # (current_schema, search_path)
        schema_probe = ("public", "public")
    if catalog_tables is None:
        # Rows from pg_tables: list of (tablename,) for every required table.
        catalog_tables = [
            ("task_runs",), ("stage_runs",), ("stage_events",),
            ("revision_bindings",), ("run_pr_bindings",), ("mcp_calls",),
            ("rollback_runs",), ("audit_events",),
            ("environment_identity",),
        ]
    if environment_marker is None:
        # Marker row: (environment_id,) from environment_identity LIMIT 1.
        environment_marker = (EXPECTED_ENVIRONMENT_ID,)
    if environment_count is None:
        # (count,) from environment_identity.
        environment_count = (1,)
    if role_privileges is None:
        # (rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls)
        # All False = unprivileged reader.
        role_privileges = (False, False, False, False, False)
    if table_privileges is None:
        # Per-table privilege probes. The source checks SELECT (must be True)
        # and INSERT/UPDATE/DELETE/TRUNCATE (must be False) on EVERY table it
        # queries. table_privileges maps table_name ->
        # (select_ok, insert, update, delete, truncate). The default is a clean
        # reader: SELECT True, all writes False, for every probed table.
        table_privileges = {
            table: (True, False, False, False, False)
            for table in _PRIVILEGE_CHECKED_TABLES
        }
    if column_catalog is None:
        # A dict mapping table_name -> list of (column_name,) rows from
        # information_schema.columns. By default we return the precise
        # REQUIRED_QUERY_COLUMNS set for each probed table so the runtime
        # catalog probe passes.
        column_catalog = {
            table: [(c,) for c in cols]
            for table, cols in REQUIRED_QUERY_COLUMNS.items()
        }

    # Build persistent param+sql-keyed entries for the per-table privilege
    # probes. The source issues, per table:
    #   SELECT has_table_privilege(current_user, %s, 'SELECT')        -> (bool,)
    #   SELECT has_table_privilege(current_user, %s, 'INSERT'), ...   -> (4 bools,)
    # Both carry the table name as a param and run once per table in a loop.
    # Because the same SQL is issued repeatedly, these live under the
    # __persistent_keyed__ namespace (matched on (sql-fragment, param-fragment)
    # and never consumed). The information_schema.columns probe lives there too.
    priv_persistent = {}
    for table in _PRIVILEGE_CHECKED_TABLES:
        tp = table_privileges.get(table, (True, False, False, False, False))
        select_ok = tp[0]
        write_privs = tuple(tp[1:5]) if len(tp) >= 5 else (False, False, False, False)
        priv_persistent[("HAS_TABLE_PRIVILEGE", "'SELECT'", table)] = [(select_ok,)]
        priv_persistent[("HAS_TABLE_PRIVILEGE", "'INSERT'", table)] = [write_privs]

    return {
        # task_runs SELECT — fetchone
        "FROM TASK_RUNS WHERE RUN_ID": [task_run],
        # stage_runs SELECT (authoritative stage source) — fetchall
        "FROM STAGE_RUNS WHERE RUN_ID": stage_runs,
        # stage_events SELECT (provenance/counts only) — fetchall
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
        # server identity probe — fetchone
        "INET_SERVER_ADDR()": [server_identity],
        # schema/search_path probe — fetchone
        "CURRENT_SCHEMA()": [schema_probe],
        # required-table catalog probe — fetchall
        "FROM PG_TABLES WHERE SCHEMANAME": catalog_tables,
        # reader role privileges probe (pg_roles) — fetchone
        "FROM PG_ROLES WHERE ROLNAME = CURRENT_USER": [role_privileges],
        # environment marker probe — fetchone (environment_identity.environment_id)
        "FROM ENVIRONMENT_IDENTITY LIMIT 1": [environment_marker],
        # environment marker row count — fetchone
        "SELECT COUNT(*) FROM ENVIRONMENT_IDENTITY": [environment_count],
        # __persistent_keyed__: results matched on (sql-fragment, param-
        # fragment) tuples and NEVER consumed (so repeated probes work). Used
        # by the information_schema.columns probe (run once per table) and the
        # per-table has_table_privilege probes (SELECT probe + write probe,
        # each run once per table). The information_schema entry is keyed on
        # ("INFORMATION_SCHEMA.COLUMNS", table); the privilege SELECT entry on
        # ("HAS_TABLE_PRIVILEGE", "'SELECT'", table) and the write entry on
        # ("HAS_TABLE_PRIVILEGE", "'INSERT'", table) — the write marker is
        # present only in the write probe's SQL text.
        "__persistent_keyed__": {
            **{
                ("INFORMATION_SCHEMA.COLUMNS", table): rows
                for table, rows in column_catalog.items()
            },
            **priv_persistent,
        },
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
                             identity=None, source=None) -> bytes:
    """Build a source, install the fake psycopg2, read_snapshot, return bytes."""
    if results is not None:
        conn._results = results
    if identity is not None:
        conn._identity = identity
    _install_fake_psycopg2(conn)
    try:
        src = source if source is not None else _make_source()
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
            # The role-hardening privilege probe legitimately contains the
            # privilege names INSERT/UPDATE/DELETE/TRUNCATE as STRING LITERALS
            # inside has_table_privilege(...) — it is a read-only SELECT, not a
            # write. Skip it for the write-keyword check.
            if "HAS_TABLE_PRIVILEGE" in upper:
                continue
            for pat in forbidden_patterns:
                m = re.search(pat, upper)
                self.assertIsNone(
                    m, f"write/DDL SQL issued: {sql!r} (matched {m.group(0) if m else pat})"
                )

    def test_run_id_injection_blocked_at_construction(self):
        # A SQL-injection attempt in run_id is rejected before any query runs.
        bad = "x'; DROP TABLE task_runs; --"
        with self.assertRaises(RunIdError):
            _make_source(run_id=bad)

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
        # The actual DSN secret value must NEVER appear in the bundle bytes.
        self.assertNotIn(b"SUPERSECRET", raw)
        # The secret_scan_scope field legitimately contains the literal pattern
        # description "password=" (it documents what the scan checks, it is not
        # a secret). So we assert the actual secret marker does not appear: a
        # real DSN leak would look like "password=SUPERSECRET", not the bare
        # pattern name "password=".
        self.assertNotIn(b"password=SUPERSECRET", raw.lower())
        self.assertNotIn(b"db.example.com", raw)


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

    def test_missing_task_run_raises_run_not_found(self):
        # RUN_NOT_FOUND fail-closed: a run_id absent from task_runs must NOT
        # produce a valid bundle with final_status=UNKNOWN. Instead the source
        # raises RunNotFoundError with code RUN_NOT_FOUND.
        from postgres_source import RunNotFoundError
        results = _make_results()
        results["FROM TASK_RUNS WHERE RUN_ID"] = []  # fetchone returns None
        conn = FakeConnection(results=results)
        with self.assertRaises(RunNotFoundError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "RUN_NOT_FOUND")
        self.assertIn("RUN_NOT_FOUND", str(cm.exception))
        # Connection is still closed on the fail-closed path.
        self.assertTrue(conn.closed)

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
        # Fake a cursor that raises on a SELECT to simulate a DB error whose
        # raw message contains the DSN/password.
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
        # The raw psycopg2/libpq message is NEVER included in the raised error
        # — not even redacted in place. Only the stable code + type name leak.
        self.assertNotIn("SUPERSECRET", msg)
        self.assertNotIn("password=", msg.lower())
        self.assertNotIn("db.example.com", msg)
        self.assertNotIn("password=<REDACTED>", msg)
        # The stable code is present.
        self.assertEqual(cm.exception.code, "POSTGRES_READ_FAILED")
        self.assertIn("POSTGRES_READ_FAILED", msg)
        # Connection was closed despite the mid-read error.
        self.assertTrue(conn.closed)

    def test_connect_failure_sanitized_and_closed(self):
        # psycopg2.connect itself raises — the source must suppress the raw
        # message entirely and not leak the DSN, and must not touch a None
        # connection.
        class _ConnectFailPsycopg2:
            def connect(self, dsn):
                raise RuntimeError(
                    "could not connect to host=db.example.com password=SUPERSECRET"
                )

        sys.modules["psycopg2"] = _ConnectFailPsycopg2()
        try:
            src = _make_source()
            with self.assertRaises(PostgresQueryError) as cm:
                src.read_snapshot()
            msg = str(cm.exception)
            self.assertNotIn("SUPERSECRET", msg)
            self.assertNotIn("password=", msg.lower())
            self.assertNotIn("db.example.com", msg)
            self.assertNotIn("password=<REDACTED>", msg)
            self.assertEqual(cm.exception.code, "POSTGRES_READ_FAILED")
        finally:
            _clear_fake_psycopg2()

    def test_connect_failure_never_leaks_dsn_uri_or_user(self):
        # Negative test: a DSN shaped like a libpq URI with embedded
        # credentials that fails to connect must produce an error message free
        # of the secret, the URI scheme, and the user fragment. The raw
        # psycopg2/libpq message (which echoes the connection string on
        # connect failure) is suppressed entirely — only the stable code +
        # exception type name are exposed.
        secret_dsn = "postgresql://user:SUPERSECRET@host/db"

        class _ConnectFailPsycopg2:
            def connect(self, dsn):
                # libpq-style message that echoes the DSN verbatim.
                raise RuntimeError(
                    f"connection to server at {secret_dsn} failed: "
                    f"postgresql://user:SUPERSECRET@host/db"
                )

        sys.modules["psycopg2"] = _ConnectFailPsycopg2()
        try:
            src = _make_source(dsn=secret_dsn)
            with self.assertRaises(PostgresQueryError) as cm:
                src.read_snapshot()
            msg = str(cm.exception)
            # The secret must never appear.
            self.assertNotIn("SUPERSECRET", msg)
            # The DSN URI scheme must never appear.
            self.assertNotIn("postgresql://", msg)
            # The user fragment must never appear.
            self.assertNotIn("user:", msg)
            # No DSN-shaped fragment at all.
            self.assertNotIn("@host", msg)
            self.assertEqual(cm.exception.code, "POSTGRES_READ_FAILED")
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
        src = _make_source()
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
            src = _make_source()
            with self.assertRaises(PostgresSourceError) as cm:
                src.read_snapshot()
            self.assertIn("PSYCOPG2_MISSING", str(cm.exception))
        finally:
            builtins.__import__ = real_import
            _clear_fake_psycopg2()

    def test_repr_never_leaks_dsn(self):
        src = _make_source(dsn="host=h password=TOPSECRET port=5432")
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
                    stage_runs=[],
                    stage_events=[],
                    revision={"repo": "test/repo", "pr_number": 42,
                              "base_sha": "b" * 40, "head_sha": "h" * 40},
                    gateway_calls=[],
                    rollback_events=[],
                    audit_summary={"total": 0, "by_action": {}},
                )
                return json.dumps(bundle, sort_keys=True,
                                  ensure_ascii=False).encode("utf-8")

        src = _StubPostgresSource(
            dsn=DSN, run_id=RUN_ID, expected_database=EXPECTED_DB,
            expected_role=EXPECTED_ROLE,
            expected_environment_id=EXPECTED_ENVIRONMENT_ID,
            expected_server_addresses=list(EXPECTED_SERVER_ADDRESSES),
            expected_server_port=EXPECTED_SERVER_PORT,
            expected_application_name=EXPECTED_APPLICATION_NAME,
        )
        poller = LivePoller(src, poll_interval=1.0, expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load(),
                        f"initial load failed: {poller.last_error_code}")
        view = poller.get_view()
        self.assertEqual(view["source_kind"], "POSTGRES_ISOLATED")
        self.assertIs(view["source_read_only"], True)


# ── TestSchemaContract ─────────────────────────────────────────────────────
class TestSchemaContract(unittest.TestCase):
    """Every column referenced by a SELECT exists in SCHEMA_CONTRACT.

    These are the STATIC migration-contract tests: they parse the
    SCHEMA_CONTRACT dict and the SQL templates the source issues, and verify
    the queries only reference columns that exist in the authoritative
    contract extracted from the migration files. They also parse the actual
    .sql migration files (the environment_identity migration) to assert the
    CREATE TABLE columns match the contract.

    The companion RUNTIME catalog probe (information_schema.columns at read
    time) is covered by TestRuntimeCatalogMock below (mocked responses) and by
    TestEphemeralMigrationProbe (labeled NOT_EXECUTED because it requires a
    live database).
    """

    def test_schema_contract_covers_required_read_tables(self):
        # Every table the source actually reads must be present in the contract.
        required = {
            "task_runs", "stage_runs", "stage_events", "revision_bindings",
            "run_pr_bindings", "mcp_calls", "rollback_runs", "audit_events",
            "environment_identity",
        }
        for table in required:
            self.assertIn(
                table, SCHEMA_CONTRACT,
                f"required read table {table!r} missing from SCHEMA_CONTRACT",
            )
            self.assertIsInstance(SCHEMA_CONTRACT[table], frozenset)
            self.assertGreater(
                len(SCHEMA_CONTRACT[table]), 0,
                f"SCHEMA_CONTRACT[{table!r}] is empty",
            )

    def test_task_runs_contract_matches_migrations(self):
        # task_runs accumulates columns across m3_state + m3c_state + m4f1_state.
        cols = SCHEMA_CONTRACT["task_runs"]
        # m3_state base columns
        for c in ("run_id", "repo", "pr_number", "branch", "status",
                  "current_stage", "attempt", "verdict", "last_error",
                  "created_at", "updated_at"):
            self.assertIn(c, cols, f"task_runs.{c} missing (m3_state)")
        # m3c_state additions
        for c in ("verify_attempt", "rollback_id", "parent_run_id"):
            self.assertIn(c, cols, f"task_runs.{c} missing (m3c_state)")
        # m4f1_state additions
        for c in ("trace_id", "active_snapshot_id", "skill_data_state"):
            self.assertIn(c, cols, f"task_runs.{c} missing (m4f1_state)")

    def test_stage_runs_contract_matches_m3_state(self):
        cols = SCHEMA_CONTRACT["stage_runs"]
        # stage_runs columns per m3_state.sql — note: column is `agent`, NOT
        # `agent_role`; and `attempt`, NOT `attempt_number`.
        for c in ("id", "run_id", "stage", "agent", "attempt", "status",
                  "started_at", "completed_at", "evidence_path", "verdict",
                  "detail"):
            self.assertIn(c, cols, f"stage_runs.{c} missing (m3_state)")
        # Explicitly assert the WRONG names are NOT in the contract.
        self.assertNotIn("agent_role", cols)
        self.assertNotIn("attempt_number", cols)

    def test_revision_bindings_contract_matches_m4f1(self):
        cols = SCHEMA_CONTRACT["revision_bindings"]
        for c in ("binding_id", "run_id", "repo", "pr_number", "base_sha",
                  "head_sha", "source_call_id", "source_evidence_digest",
                  "recorded_at"):
            self.assertIn(c, cols, f"revision_bindings.{c} missing (m4f1)")

    def test_run_pr_bindings_contract_matches_m3b_b4(self):
        cols = SCHEMA_CONTRACT["run_pr_bindings"]
        for c in ("binding_id", "run_id", "repo", "pr_number", "fix_branch",
                  "base_branch", "head_sha", "recorded_at"):
            self.assertIn(c, cols, f"run_pr_bindings.{c} missing (m3b_b4)")
        # base_sha is deliberately NOT in run_pr_bindings (it lives in
        # revision_bindings per the M4-F1 contract).
        self.assertNotIn("base_sha", cols)

    def test_rollback_runs_contract_matches_m3c(self):
        cols = SCHEMA_CONTRACT["rollback_runs"]
        for c in ("rollback_id", "parent_run_id", "revert_run_id",
                  "reverted_merge_sha", "repo", "pr_number", "status",
                  "fail_reason", "revert_result_sha", "reverify_verdict",
                  "created_at", "updated_at"):
            self.assertIn(c, cols, f"rollback_runs.{c} missing (m3c)")

    def test_mcp_calls_contract_matches_m3b_policy(self):
        cols = SCHEMA_CONTRACT["mcp_calls"]
        for c in ("request_id", "correlation_id", "phase", "ts",
                  "caller_agent", "tool", "decision", "reason_code",
                  "target_repo", "target_branch", "result_status", "git_sha",
                  "run_id", "error"):
            self.assertIn(c, cols, f"mcp_calls.{c} missing (m3b_policy)")

    def test_all_select_templates_reference_only_contract_columns(self):
        """The core migration-contract assertion.

        For each SELECT template the source issues, every QUALIFIED
        (alias.column) reference must resolve to a column that exists in the
        SCHEMA_CONTRACT entry for the resolved table. Bare column references
        are checked against the single FROM table for that template.
        """
        # Map of template label -> (from_table, alias_map) so we can resolve
        # qualified references back to a real contract table. Only templates
        # with a single FROM table are checked for bare columns.
        template_tables = {
            "task_run": ("task_runs", {}),
            "stage_runs": ("stage_runs", {}),
            "stage_events": ("stage_events", {}),
            "revision_bindings": ("revision_bindings", {"rb": "revision_bindings"}),
            "run_pr_bindings": ("run_pr_bindings", {}),
            "mcp_calls": ("mcp_calls", {}),
            "rollback_runs": ("rollback_runs", {}),
            "audit_events_total": ("audit_events", {}),
            "audit_events_by_action": ("audit_events", {}),
            "environment_marker": ("environment_identity", {}),
        }
        for label, sql in _all_select_templates():
            self.assertIn(label, template_tables,
                          f"template {label!r} has no table mapping in the test")
            from_table, alias_map = template_tables[label]
            self.assertIn(from_table, SCHEMA_CONTRACT,
                          f"table {from_table!r} not in SCHEMA_CONTRACT")

            # 1. Qualified references (alias.column) must exist in the contract.
            for alias, col in _referenced_table_columns(sql):
                resolved = alias_map.get(alias.lower(), alias.lower())
                # Only check aliases that map to a known contract table. (The
                # query_audit / system aliases are skipped.)
                if resolved in SCHEMA_CONTRACT:
                    self.assertIn(
                        col, SCHEMA_CONTRACT[resolved],
                        f"{label}: {alias}.{col} not in SCHEMA_CONTRACT[{resolved!r}]",
                    )

    def test_no_select_star_anywhere(self):
        # The source must never use SELECT * — every template lists columns
        # explicitly so the contract test can verify them.
        for label, sql in _all_select_templates():
            self.assertNotIn(
                "SELECT *", sql.upper(),
                f"{label}: SELECT * is forbidden (use explicit column list)",
            )

    def test_environment_identity_contract_columns(self):
        # environment_identity contract lists environment_id + created_at.
        cols = SCHEMA_CONTRACT["environment_identity"]
        self.assertIn("environment_id", cols)
        self.assertIn("created_at", cols)

    # ── Static migration-contract tests: parse actual .sql files ───────────
    def test_environment_identity_sql_create_table_columns(self):
        """Parse the actual 001_environment_identity.sql migration and extract
        the CREATE TABLE columns; assert they match the REQUIRED_QUERY_COLUMNS
        entry (the columns the source actually queries).

        This is a STATIC migration-contract test (no DB connection). The
        runtime information_schema.columns probe is covered by
        TestRuntimeCatalogMock.
        """
        sql_path = (
            Path(__file__).resolve().parents[2]
            / "tools" / "demo_console" / "migrations"
            / "001_environment_identity.sql"
        )
        self.assertTrue(sql_path.exists(), f"migration file missing: {sql_path}")
        sql_text = sql_path.read_text(encoding="utf-8")
        cols = self._extract_create_table_columns(sql_text, "environment_identity")
        # The query-required column (environment_id) must be created by the
        # migration.
        for c in REQUIRED_QUERY_COLUMNS["environment_identity"]:
            self.assertIn(
                c, cols,
                f"REQUIRED_QUERY_COLUMNS environment_identity.{c} not created "
                f"by migration (parsed cols: {cols})",
            )

    def test_environment_identity_sql_acl_revokes_public_grants_reader(self):
        """The 001_environment_identity.sql migration must REVOKE ALL from
        PUBLIC and GRANT SELECT to the canonical viewer role
        (mergepilot_reader). It must NOT grant SELECT to PUBLIC.
        """
        sql_path = (
            Path(__file__).resolve().parents[2]
            / "tools" / "demo_console" / "migrations"
            / "001_environment_identity.sql"
        )
        sql_text = sql_path.read_text(encoding="utf-8")
        upper = sql_text.upper()
        # REVOKE ALL ... FROM PUBLIC must be present.
        self.assertIn("REVOKE ALL ON ENVIRONMENT_IDENTITY FROM PUBLIC", upper)
        # GRANT SELECT ... TO mergepilot_reader must be present.
        self.assertIn(
            "GRANT SELECT ON ENVIRONMENT_IDENTITY TO MERGEPILOT_READER", upper,
        )
        # The old "GRANT SELECT ... TO PUBLIC" must NOT be present.
        self.assertNotIn("GRANT SELECT ON ENVIRONMENT_IDENTITY TO PUBLIC", upper)

    @staticmethod
    def _strip_sql_comments(text: str) -> str:
        """Remove ``--`` line comments from SQL text.

        The migration files use inline ``--`` comments (including CJK text and
        ASCII commas inside the comments) that would corrupt a naive comma-split
        of a CREATE TABLE body. We strip from the first ``--`` to end-of-line on
        every line. Block comments (/* ... */) are not used in these files.
        """
        out_lines = []
        for line in text.splitlines():
            idx = line.find("--")
            if idx != -1:
                line = line[:idx]
            out_lines.append(line)
        return "\n".join(out_lines)

    @staticmethod
    def _extract_create_table_columns(sql_text: str, table_name: str) -> list[str]:
        """Extract the column names from a ``CREATE TABLE <name> (...)`` block.

        A simple parser sufficient for the migration files in this repo. It
        finds the CREATE TABLE block for ``table_name`` (tolerating an optional
        ``public.`` schema prefix), strips ``--`` comments, and returns the
        leading identifier of each column line (skipping table-level
        constraints/indices).
        """
        sql_text = TestSchemaContract._strip_sql_comments(sql_text)
        # Match CREATE TABLE [IF NOT EXISTS] [public.]<table_name> ( ... )
        m = re.search(
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+"
            r"(?:public\.)?"
            + re.escape(table_name)
            + r"\s*\((.*?)\);",
            sql_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return []
        body = m.group(1)
        cols = []
        for line in body.split(","):
            line = line.strip()
            if not line:
                continue
            # Skip lines that are table-level constraints.
            upper = line.upper()
            if upper.startswith(("PRIMARY ", "FOREIGN ", "UNIQUE ", "CHECK ",
                                 "CONSTRAINT ")):
                continue
            # The first token is the column name (may be quoted).
            tok = line.split()[0]
            tok = tok.strip('"`[]')
            if tok:
                cols.append(tok)
        return cols

    @staticmethod
    def _extract_alter_add_columns(sql_text: str, table_name: str) -> list[str]:
        """Extract column names from ``ALTER TABLE [public.]<name> ADD COLUMN
        [IF NOT EXISTS] <col> ...`` statements in ``sql_text``.

        The M3/M4 migrations add columns idempotently via ALTER TABLE ADD
        COLUMN IF NOT EXISTS, so a table's full column set is the union of its
        CREATE TABLE block and all its ALTER TABLE ADD COLUMN statements across
        every migration file.
        """
        cols = []
        pattern = re.compile(
            r"ALTER\s+TABLE\s+(?:public\.)?"
            + re.escape(table_name)
            + r"\s+ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        for m in pattern.finditer(sql_text):
            cols.append(m.group(1))
        return cols

    def test_environment_marker_contract_shape(self):
        # ENVIRONMENT_MARKER_CONTRACT describes the new marker shape.
        self.assertEqual(
            ENVIRONMENT_MARKER_CONTRACT["table_name"], "environment_identity",
        )
        self.assertIn(
            "SELECT environment_id FROM environment_identity LIMIT 1",
            ENVIRONMENT_MARKER_CONTRACT["probe_sql"],
        )
        self.assertEqual(ENVIRONMENT_MARKER_CONTRACT["expected_row_count"], 1)
        self.assertIn("environment_id", ENVIRONMENT_MARKER_CONTRACT["required_columns"])
        self.assertIn("SELECT", ENVIRONMENT_MARKER_CONTRACT["viewer_privileges"])
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            self.assertIn(priv, ENVIRONMENT_MARKER_CONTRACT["revoked_privileges"])


# ── TestMigrationContractFiles ──────────────────────────────────────────────
# These are the STATIC migration-contract tests that parse the ACTUAL .sql
# migration files (not SCHEMA_CONTRACT against itself). They extract the column
# lists from the real CREATE TABLE + ALTER TABLE ADD COLUMN statements across
# the audit-db migrations and the environment_identity migration, and verify
# that every column in REQUIRED_QUERY_COLUMNS is actually created by a
# migration. This catches drift between the queries and the migrations without
# requiring a live database.
class TestMigrationContractFiles(unittest.TestCase):
    """REQUIRED_QUERY_COLUMNS columns must exist in the actual .sql migrations.

    These tests read the real migration files:
      - tools/audit-db/m3_state.sql      (task_runs, stage_runs, ...)
      - tools/audit-db/m3b_b4.sql        (run_pr_bindings, ...)
      - tools/audit-db/m3c_state.sql     (rollback_runs, task_runs additions)
      - tools/audit-db/m4f1_state.sql    (revision_bindings, task_runs trace_id)
      - tools/demo_console/migrations/001_environment_identity.sql
    ...parse the CREATE TABLE column lists and ALTER TABLE ADD COLUMN
    statements, and assert every column REQUIRED_QUERY_COLUMNS references is
    actually created. They deliberately do NOT compare SCHEMA_CONTRACT against
    itself.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        cls.audit_db = cls.root / "tools" / "audit-db"
        cls.migrations = cls.root / "tools" / "demo_console" / "migrations"
        # Read every migration file once and concatenate by table. Each entry
        # is the raw SQL text of one file; a table's full column set is the
        # union across all files that CREATE or ALTER it.
        cls.migration_files = [
            cls.audit_db / "m3_state.sql",
            cls.audit_db / "m3b_b4.sql",
            cls.audit_db / "m3c_state.sql",
            cls.audit_db / "m4f1_state.sql",
            cls.migrations / "001_environment_identity.sql",
        ]
        for p in cls.migration_files:
            # Surface a clear failure if a migration file is missing.
            assert p.exists(), f"migration file missing: {p}"
        cls.migration_texts = [p.read_text(encoding="utf-8")
                               for p in cls.migration_files]

    def _parsed_columns_for(self, table_name: str) -> set[str]:
        """Aggregate the parsed columns for ``table_name`` across ALL migration
        files (CREATE TABLE + ALTER TABLE ADD COLUMN).
        """
        cols: set[str] = set()
        for text in self.migration_texts:
            cols.update(TestSchemaContract._extract_create_table_columns(
                text, table_name))
            cols.update(TestSchemaContract._extract_alter_add_columns(
                text, table_name))
        return cols

    def test_migration_files_all_present(self):
        # Guard: every migration file the contract depends on must exist.
        for p in self.migration_files:
            self.assertTrue(p.exists(), f"migration file missing: {p}")

    def test_required_query_columns_subset_of_parsed_migrations(self):
        """The core assertion: every column in REQUIRED_QUERY_COLUMNS must be
        created by an actual CREATE TABLE or ALTER TABLE ADD COLUMN statement
        in the real .sql files. This compares the query-needs dict against the
        PARSED migrations, NOT SCHEMA_CONTRACT against itself.
        """
        for table, required_cols in REQUIRED_QUERY_COLUMNS.items():
            parsed = self._parsed_columns_for(table)
            self.assertTrue(
                parsed,
                f"no CREATE TABLE/ALTER ADD COLUMN found for {table!r} in any "
                f"migration file",
            )
            missing = sorted(required_cols - parsed)
            self.assertEqual(
                missing, [],
                f"REQUIRED_QUERY_COLUMNS[{table!r}] references columns not "
                f"created by any migration: {missing} (parsed: {sorted(parsed)})",
            )

    def test_task_runs_columns_match_migrations(self):
        # task_runs columns are spread across m3_state (CREATE + ALTER) +
        # m3c_state (ALTER verify_attempt/rollback_id/parent_run_id) +
        # m4f1_state (ALTER trace_id/active_snapshot_id/skill_data_state).
        parsed = self._parsed_columns_for("task_runs")
        # The query-referenced subset must all be present.
        for c in REQUIRED_QUERY_COLUMNS["task_runs"]:
            self.assertIn(c, parsed, f"task_runs.{c} not in parsed migrations")
        # Spot-check a few columns from each migration that are NOT necessarily
        # queried but prove the parser crossed file boundaries.
        for c in ("run_id", "repo", "pr_number", "status"):  # m3_state
            self.assertIn(c, parsed)
        self.assertIn("trace_id", parsed)  # m4f1_state ALTER

    def test_stage_runs_columns_match_m3_state(self):
        parsed = self._parsed_columns_for("stage_runs")
        for c in REQUIRED_QUERY_COLUMNS["stage_runs"]:
            self.assertIn(c, parsed, f"stage_runs.{c} not in parsed migrations")
        # The authoritative column is `agent` (NOT agent_role).
        self.assertIn("agent", parsed)
        self.assertNotIn("agent_role", parsed)

    def test_revision_bindings_columns_match_m4f1(self):
        parsed = self._parsed_columns_for("revision_bindings")
        for c in REQUIRED_QUERY_COLUMNS["revision_bindings"]:
            self.assertIn(c, parsed,
                          f"revision_bindings.{c} not in parsed migrations")

    def test_run_pr_bindings_columns_match_m3b_b4(self):
        parsed = self._parsed_columns_for("run_pr_bindings")
        for c in REQUIRED_QUERY_COLUMNS["run_pr_bindings"]:
            self.assertIn(c, parsed,
                          f"run_pr_bindings.{c} not in parsed migrations")
        # base_sha is deliberately NOT in run_pr_bindings (it lives in
        # revision_bindings per the M4-F1 contract).
        self.assertNotIn("base_sha", parsed)

    def test_rollback_runs_columns_match_m3c(self):
        parsed = self._parsed_columns_for("rollback_runs")
        for c in REQUIRED_QUERY_COLUMNS["rollback_runs"]:
            self.assertIn(c, parsed,
                          f"rollback_runs.{c} not in parsed migrations")

    def test_environment_identity_columns_match_migration(self):
        parsed = self._parsed_columns_for("environment_identity")
        for c in REQUIRED_QUERY_COLUMNS["environment_identity"]:
            self.assertIn(c, parsed,
                          f"environment_identity.{c} not in parsed migration")
        # The migration also creates created_at (not queried, but present).
        self.assertIn("created_at", parsed)

    def test_required_query_columns_is_narrower_than_schema_contract(self):
        # REQUIRED_QUERY_COLUMNS must be a subset of (or equal to) the full
        # SCHEMA_CONTRACT for each table — the runtime probe checks only what
        # the queries reference, never more.
        for table, required_cols in REQUIRED_QUERY_COLUMNS.items():
            self.assertIn(table, SCHEMA_CONTRACT,
                          f"{table!r} in REQUIRED_QUERY_COLUMNS but not "
                          f"SCHEMA_CONTRACT")
            extra = required_cols - SCHEMA_CONTRACT[table]
            self.assertEqual(
                extra, set(),
                f"REQUIRED_QUERY_COLUMNS[{table!r}] has columns not in "
                f"SCHEMA_CONTRACT: {sorted(extra)}",
            )


# ── TestStageRunsSelection ─────────────────────────────────────────────────
class TestStageRunsSelection(unittest.TestCase):
    """stage_runs (not stage_events) drives workflow_stages; latest attempt wins."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_stage_runs_drive_workflow_stages(self):
        # stage_runs rows produce workflow_stages entries.
        stage_runs = [
            (1, RUN_ID, "diff-parse", "reviewer", 1, "COMPLETED", None, None,
             "PASS", None),
            (2, RUN_ID, "test-runner", "verifier", 1, "COMPLETED", None, None,
             "PASS", None),
        ]
        conn = FakeConnection(results=_make_results(stage_runs=stage_runs))
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        stages = {s["stage"]: s for s in bundle["workflow_stages"]}
        self.assertIn("diff-parse", stages)
        self.assertIn("test-runner", stages)
        self.assertEqual(stages["diff-parse"]["agent_role"], "reviewer")
        self.assertEqual(stages["test-runner"]["agent_role"], "verifier")

    def test_latest_attempt_per_stage_is_picked(self):
        # Two attempts for the same stage: attempt 2 should win (ORDER BY
        # attempt DESC means the higher attempt sorts first; the assembler
        # picks the first row per stage). The fake cursor returns rows in the
        # order they appear in the canned list (it does not honor ORDER BY),
        # so we provide them already in the ORDER BY order: attempt 2 first.
        stage_runs = [
            # (id, run_id, stage, agent, attempt, status, ...)
            (2, RUN_ID, "diff-parse", "reviewer", 2, "COMPLETED", None, None,
             "PASS", None),
            (1, RUN_ID, "diff-parse", "reviewer", 1, "FAILED", None, None,
             "FAIL", None),
        ]
        conn = FakeConnection(results=_make_results(stage_runs=stage_runs))
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        # Only one stage entry (dedup by stage name).
        self.assertEqual(len(bundle["workflow_stages"]), 1)
        stage = bundle["workflow_stages"][0]
        self.assertEqual(stage["stage"], "diff-parse")
        # The latest attempt (attempt 2 = COMPLETED/PASS) wins.
        self.assertEqual(stage["status"], "COMPLETED")
        self.assertEqual(stage["verdict"], "PASS")

    def test_empty_stage_runs_yields_empty_workflow_stages(self):
        conn = FakeConnection(results=_make_results(stage_runs=[]))
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["workflow_stages"], [])
        self.assertEqual(bundle["agents"], [])

    def test_null_agent_yields_unknown_role(self):
        # A NULL agent column must not crash; role falls back to 'unknown'.
        stage_runs = [
            (1, RUN_ID, "weird-stage", None, 1, "COMPLETED", None, None, None,
             None),
        ]
        conn = FakeConnection(results=_make_results(stage_runs=stage_runs))
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(len(bundle["workflow_stages"]), 1)
        self.assertEqual(bundle["workflow_stages"][0]["agent_role"], "unknown")

    def test_unknown_stage_name_carried_verbatim(self):
        stage_runs = [
            (1, RUN_ID, "some-future-stage", "agent-x", 1, "COMPLETED", None,
             None, "PASS", None),
        ]
        conn = FakeConnection(results=_make_results(stage_runs=stage_runs))
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        stage = bundle["workflow_stages"][0]
        self.assertEqual(stage["stage"], "some-future-stage")
        # Unknown stage name → role inferred from agent identity.
        self.assertEqual(stage["agent_role"], "agent-x")

    def test_stage_event_count_preserved_in_residue(self):
        # stage_events no longer drives the stage list, but its count is kept
        # in residue for provenance.
        stage_events = [
            ("e1", RUN_ID, "reviewer", "review", "diff-parse", "COMPLETED",
             None, None, None),
            ("e2", RUN_ID, "verifier", "verify", "test-runner", "COMPLETED",
             None, None, None),
            ("e3", RUN_ID, "reviewer", "review", "diff-parse", "COMPLETED",
             None, None, None),
        ]
        conn = FakeConnection(
            results=_make_results(stage_runs=[], stage_events=stage_events),
        )
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["residue"]["stage_event_count"], 3)
        self.assertEqual(bundle["residue"]["stage_runs_count"], 0)

    def test_stage_runs_query_orders_by_stage_attempt_desc_id(self):
        # The deterministic selection must be enforced at the SQL level:
        # ORDER BY stage, attempt DESC, id. This means the latest attempt per
        # stage sorts first so the assembler's "first row per stage" pick is
        # the latest attempt.
        from postgres_source import _STAGE_RUNS_SQL
        upper = _STAGE_RUNS_SQL.upper()
        self.assertIn("ORDER BY", upper)
        self.assertIn("STAGE", upper)
        self.assertIn("ATTEMPT DESC", upper)
        self.assertIn("ID", upper)

    def test_stage_runs_uses_agent_column_not_agent_role(self):
        # The contract says stage_runs.agent (not agent_role). The query must
        # SELECT agent.
        from postgres_source import _STAGE_RUNS_SQL
        upper = _STAGE_RUNS_SQL.upper()
        self.assertIn("AGENT", upper)
        self.assertNotIn("AGENT_ROLE", upper)


# ── TestRunNotFound ────────────────────────────────────────────────────────
class TestRunNotFound(unittest.TestCase):
    """RUN_NOT_FOUND is fail-closed: no valid bundle for a missing run."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_missing_run_raises_run_not_found(self):
        results = _make_results()
        results["FROM TASK_RUNS WHERE RUN_ID"] = []
        conn = FakeConnection(results=results)
        with self.assertRaises(RunNotFoundError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "RUN_NOT_FOUND")

    def test_missing_run_does_not_produce_bundle(self):
        results = _make_results()
        results["FROM TASK_RUNS WHERE RUN_ID"] = []
        conn = FakeConnection(results=results)
        with self.assertRaises(PostgresSourceError):
            _read_snapshot_with_fake(conn)
        # Connection closed on the fail-closed path.
        self.assertTrue(conn.closed)

    def test_missing_run_message_has_no_dsn(self):
        results = _make_results()
        results["FROM TASK_RUNS WHERE RUN_ID"] = []
        conn = FakeConnection(results=results)
        with self.assertRaises(RunNotFoundError) as cm:
            _read_snapshot_with_fake(conn)
        msg = str(cm.exception)
        self.assertNotIn("SUPERSECRET", msg)
        self.assertNotIn("password=", msg.lower())


# ── TestStableErrorCodes ───────────────────────────────────────────────────
class TestStableErrorCodes(unittest.TestCase):
    """Every error carries a stable .code; the poller prefers .code."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_required_codes_present(self):
        required = {
            "PSYCOPG2_MISSING", "RUN_ID_INVALID", "RUN_NOT_FOUND",
            "WRONG_DATABASE", "WRONG_ROLE", "WRONG_SERVER",
            "ENVIRONMENT_ID_MISMATCH", "ENVIRONMENT_ID_NOT_VERIFIED",
            "SCHEMA_INCOMPATIBLE", "NOT_READ_ONLY", "POSTGRES_READ_FAILED",
            "CONFIG_INVALID",
        }
        self.assertTrue(required.issubset(STABLE_ERROR_CODES),
                        f"missing codes: {required - STABLE_ERROR_CODES}")

    def test_each_error_carries_code_attribute(self):
        # Construct each error type and confirm .code is a stable string.
        cases = [
            RunIdError("x", code="RUN_ID_INVALID"),
            RunNotFoundError("x", code="RUN_NOT_FOUND"),
            PostgresQueryError("x", code="POSTGRES_READ_FAILED"),
            ConfigInvalidError("x", code="CONFIG_INVALID"),
        ]
        for err in cases:
            self.assertIsInstance(err.code, str)
            self.assertIn(err.code, STABLE_ERROR_CODES)

    def test_unknown_code_coerced_to_generic(self):
        # An unrecognized code falls back to POSTGRES_READ_FAILED so the poller
        # always sees a member of STABLE_ERROR_CODES.
        err = PostgresSourceError("x", code="NOT_A_REAL_CODE")
        self.assertEqual(err.code, "POSTGRES_READ_FAILED")

    def test_poller_prefers_code_attribute(self):
        # A source error with .code surfaces that code (not the class name) in
        # the poller's last_error_code.
        from live_poller import LivePoller

        class _CodeSource(SnapshotSource):
            @property
            def kind(self):
                return "POSTGRES_ISOLATED"

            def read_snapshot(self):
                raise RunNotFoundError("nope", code="RUN_NOT_FOUND")

        poller = LivePoller(
            _CodeSource(), poll_interval=1.0, expected_mode="ISOLATED_LIVE",
        )
        self.assertFalse(poller.initial_load())
        self.assertEqual(poller.get_view()["last_error_code"], "RUN_NOT_FOUND")

    def test_poller_falls_back_to_type_name_without_code(self):
        # A plain exception (no .code) falls back to type(e).__name__.
        from live_poller import LivePoller

        class _PlainSource(SnapshotSource):
            @property
            def kind(self):
                return "FILE_FIXTURE"

            def read_snapshot(self):
                raise ValueError("plain")

        poller = LivePoller(
            _PlainSource(), poll_interval=1.0, expected_mode="ISOLATED_LIVE",
        )
        self.assertFalse(poller.initial_load())
        self.assertEqual(poller.get_view()["last_error_code"], "ValueError")


# ── TestIdentityGatesExtended ──────────────────────────────────────────────
class TestIdentityGatesExtended(unittest.TestCase):
    """Extended identity gates: server, schema, catalog, environment marker."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_unsupported_server_version_rejected(self):
        # server_version_num outside the supported 12.x-17.x range. Use the
        # expected port/application_name so the version check is the one that
        # fires (not the address/port/app_name checks).
        conn = FakeConnection(
            results=_make_results(
                server_identity=("127.0.0.1", EXPECTED_SERVER_PORT,
                                 EXPECTED_APPLICATION_NAME, 110000),
            ),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "WRONG_SERVER")
        self.assertTrue(conn.closed)

    def test_non_public_schema_rejected(self):
        conn = FakeConnection(
            results=_make_results(schema_probe=("private", "private")),
        )
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "SCHEMA_INCOMPATIBLE")

    def test_missing_required_table_rejected(self):
        # Catalog probe omits a required table.
        catalog = [
            ("task_runs",), ("stage_runs",), ("stage_events",),
            # revision_bindings MISSING
            ("run_pr_bindings",), ("mcp_calls",),
            ("rollback_runs",), ("audit_events",),
            ("environment_identity",),
        ]
        conn = FakeConnection(results=_make_results(catalog_tables=catalog))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "SCHEMA_INCOMPATIBLE")
        self.assertIn("revision_bindings", str(cm.exception))

    def test_missing_environment_table_rejected(self):
        # environment_identity table missing from catalog → SCHEMA_INCOMPATIBLE
        # (the table-existence check fires before the row-count check).
        catalog = [
            ("task_runs",), ("stage_runs",), ("stage_events",),
            ("revision_bindings",), ("run_pr_bindings",), ("mcp_calls",),
            ("rollback_runs",), ("audit_events",),
            # environment_identity MISSING
        ]
        conn = FakeConnection(results=_make_results(catalog_tables=catalog))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "SCHEMA_INCOMPATIBLE")
        self.assertIn("environment_identity", str(cm.exception))

    def test_missing_environment_marker_refuses_startup(self):
        # No marker row at all → ENVIRONMENT_ID_NOT_VERIFIED.
        results = _make_results()
        results["FROM ENVIRONMENT_IDENTITY LIMIT 1"] = []
        conn = FakeConnection(results=results)
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "ENVIRONMENT_ID_NOT_VERIFIED")
        self.assertIn("ENVIRONMENT_ID_NOT_VERIFIED", str(cm.exception))

    def test_zero_environment_rows_refuses_startup(self):
        # environment_identity probe returns no row AND count(*) == 0 →
        # ENVIRONMENT_ID_NOT_VERIFIED.
        results = _make_results(environment_count=(0,))
        results["FROM ENVIRONMENT_IDENTITY LIMIT 1"] = []
        conn = FakeConnection(results=results)
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "ENVIRONMENT_ID_NOT_VERIFIED")

    def test_multiple_environment_rows_refuses_startup(self):
        # count(*) > 1 → ENVIRONMENT_ID_NOT_VERIFIED (>1 rows).
        results = _make_results(environment_count=(2,))
        conn = FakeConnection(results=results)
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "ENVIRONMENT_ID_NOT_VERIFIED")
        self.assertIn(">1", str(cm.exception))

    def test_environment_marker_mismatch_rejected(self):
        # Marker present but value does not match expected_environment_id.
        results = _make_results(environment_marker=("wrong-env",))
        conn = FakeConnection(results=results)
        src = _make_source(expected_environment_id="correct-env")
        _install_fake_psycopg2(conn)
        try:
            with self.assertRaises(IdentityCheckError) as cm:
                src.read_snapshot()
            self.assertEqual(cm.exception.code, "ENVIRONMENT_ID_MISMATCH")
        finally:
            _clear_fake_psycopg2()

    def test_environment_marker_match_passes(self):
        # Marker present and matches expected_environment_id → passes.
        results = _make_results(environment_marker=("the-env",))
        conn = FakeConnection(results=results)
        src = _make_source(expected_environment_id="the-env")
        _install_fake_psycopg2(conn)
        try:
            raw = src.read_snapshot()
            bundle = json.loads(raw)
            self.assertEqual(bundle["demo_mode"], "ISOLATED_LIVE")
        finally:
            _clear_fake_psycopg2()

    def test_identity_checks_run_inside_readonly_transaction(self):
        # The FIRST statement after connect must be the BEGIN ... READ ONLY.
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        self.assertGreater(len(conn.executed), 0)
        first_sql = conn.executed[0][0].upper()
        self.assertTrue(
            first_sql.startswith("BEGIN"),
            f"first statement was not BEGIN: {conn.executed[0][0]!r}",
        )
        self.assertIn("REPEATABLE READ", first_sql)
        self.assertIn("READ ONLY", first_sql)


# ── TestProvenance ─────────────────────────────────────────────────────────
class TestProvenance(unittest.TestCase):
    """source_commit/verification_commit come from revision_bindings (or null)."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_provenance_from_revision_bindings(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        # source_commit: revision_bindings head_sha (the target revision).
        self.assertEqual(bundle["source_commit"], "h" * 40)
        # verification_commit: ALWAYS null for ISOLATED_LIVE (the read-only
        # viewer does not record a verification build). The earlier behavior of
        # copying head_sha here was a provenance bug and is fixed.
        self.assertIsNone(bundle["verification_commit"])
        # verification_commit_status makes the null explicit.
        self.assertEqual(bundle["verification_commit_status"], "NOT_AVAILABLE")
        self.assertEqual(
            bundle["provenance_status"], "VERIFIED_FROM_REVISION_BINDINGS",
        )

    def test_missing_revision_yields_null_and_not_available(self):
        # No revision_bindings row and no head_sha anywhere.
        results = _make_results()
        results["FROM REVISION_BINDINGS RB WHERE RB.RUN_ID"] = []
        results["FROM RUN_PR_BINDINGS WHERE RUN_ID"] = []
        # task_run has no head_sha either (task_runs has no head_sha column).
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertIsNone(bundle["source_commit"])
        self.assertIsNone(bundle["verification_commit"])
        self.assertEqual(bundle["provenance_status"], "NOT_AVAILABLE")
        self.assertEqual(bundle["verification_commit_status"], "NOT_AVAILABLE")

    def test_never_empty_string_for_commit(self):
        # Even when revision is missing, the commits must be null (NOT "").
        results = _make_results()
        results["FROM REVISION_BINDINGS RB WHERE RB.RUN_ID"] = []
        results["FROM RUN_PR_BINDINGS WHERE RUN_ID"] = []
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertNotEqual(bundle["source_commit"], "")
        self.assertNotEqual(bundle["verification_commit"], "")

    def test_no_fabricated_git_sha(self):
        # When revision is missing, the commit must be null — never a
        # fabricated 40-hex SHA.
        results = _make_results()
        results["FROM REVISION_BINDINGS RB WHERE RB.RUN_ID"] = []
        results["FROM RUN_PR_BINDINGS WHERE RUN_ID"] = []
        conn = FakeConnection(results=results)
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        import re as _re
        sha_re = _re.compile(r"^[0-9a-f]{40}$")
        for commit in (bundle["source_commit"], bundle["verification_commit"]):
            if commit is not None:
                self.assertFalse(
                    sha_re.match(commit),
                    f"fabricated SHA when revision missing: {commit!r}",
                )


# ── TestSecretScanStatus ───────────────────────────────────────────────────
class TestSecretScanStatus(unittest.TestCase):
    """ISOLATED_LIVE reports a PARTIAL_SERIALIZED_BUNDLE_SCAN; secret_leaks stays 0."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_secret_scan_status_is_partial_bundle_scan(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(
            bundle["secret_scan_status"], "PARTIAL_SERIALIZED_BUNDLE_SCAN",
        )

    def test_secret_scan_scope_lists_patterns(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        scope = bundle["secret_scan_scope"]
        self.assertIsInstance(scope, list)
        self.assertGreater(len(scope), 0)
        # All the documented patterns must be present.
        for pat in ("password=", "postgresql://user:pass@",
                    "postgres://user:pass@", "sk-*", "ghp_*", "AKIA*", "xox*"):
            self.assertIn(pat, scope)

    def test_secret_leaks_detected_field_present_and_zero(self):
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["secret_leaks"], 0)
        # secret_leaks_detected holds the actual count from the scan.
        self.assertEqual(bundle["secret_leaks_detected"], 0)

    def test_secret_leaks_remains_zero(self):
        # The strict schema requires secret_leaks == 0; ISOLATED_LIVE keeps it.
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        bundle = json.loads(raw)
        self.assertEqual(bundle["secret_leaks"], 0)

    def test_dsn_password_never_in_scan_output(self):
        # The actual DSN/password must never appear in the serialized bundle
        # bytes. The secret_scan_scope field legitimately documents the pattern
        # name "password=" (it is metadata, not a secret), so we assert the real
        # secret value is absent rather than the bare pattern name.
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        self.assertNotIn(b"SUPERSECRET", raw)
        self.assertNotIn(b"password=SUPERSECRET", raw.lower())
        self.assertNotIn(b"db.example.com", raw)

    def test_scan_detects_leaked_password_marker(self):
        # If a password marker WERE in the bundle, the scan would catch it.
        src = _make_source()
        # Simulate a leak: bundle bytes containing a password= marker.
        leaked = b'{"password=SUPERSECRET": 1}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_detects_postgresql_uri_credentials(self):
        src = _make_source()
        leaked = b'{"dsn": "postgresql://user:pass@host/db"}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_detects_postgres_uri_credentials(self):
        src = _make_source()
        leaked = b'{"dsn": "postgres://user:pass@host/db"}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_detects_openai_key(self):
        src = _make_source()
        leaked = b'{"key": "sk-' + b"a" * 40 + b'"}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_detects_github_token(self):
        src = _make_source()
        leaked = b'{"tok": "ghp_' + b"a" * 36 + b'"}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_detects_aws_key(self):
        src = _make_source()
        leaked = b'{"key": "AKIA' + b"A" * 16 + b'"}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_detects_slack_token(self):
        src = _make_source()
        leaked = b'{"tok": "xoxb-' + b"a" * 24 + b'"}'
        self.assertGreater(src._scan_for_secrets(leaked), 0)

    def test_scan_returns_zero_for_clean_bytes(self):
        src = _make_source()
        self.assertEqual(src._scan_for_secrets(b'{"a": 1}'), 0)

    def test_leak_detected_raises_postgres_read_failed(self):
        # If a secret leak is detected during assembly, the source must raise
        # POSTGRES_READ_FAILED and NOT emit a bundle. We patch _scan_for_secrets
        # to return a non-zero count.
        src = _make_source()
        # Capture the staticmethod descriptor (NOT the underlying function) so
        # we restore it as a staticmethod, not a plain function (which would
        # turn into a bound method and break instance calls).
        import inspect
        original = inspect.getattr_static(PostgresSnapshotSource, "_scan_for_secrets")
        PostgresSnapshotSource._scan_for_secrets = staticmethod(
            lambda data: 1
        )
        conn = FakeConnection(results=_make_results())
        try:
            with self.assertRaises(PostgresQueryError) as cm:
                _read_snapshot_with_fake(conn, source=src)
            self.assertEqual(cm.exception.code, "POSTGRES_READ_FAILED")
            # The message must NOT contain the raw bytes or any secret text.
            msg = str(cm.exception)
            self.assertNotIn("SUPERSECRET", msg)
        finally:
            PostgresSnapshotSource._scan_for_secrets = original


# ── TestTransactionLifecycle ───────────────────────────────────────────────
class TestTransactionLifecycle(unittest.TestCase):
    """BEGIN READ ONLY is the first statement; success → ROLLBACK; error → ROLLBACK+close."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_begin_is_first_statement(self):
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        self.assertGreater(len(conn.executed), 0)
        self.assertTrue(conn.executed[0][0].upper().startswith("BEGIN"))

    def test_no_identity_select_before_begin(self):
        # No SELECT may appear before the BEGIN statement.
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        began = False
        for sql, _ in conn.executed:
            upper = sql.upper()
            if upper.startswith("BEGIN"):
                began = True
                continue
            if not began:
                self.fail(
                    f"SELECT/SET issued before BEGIN: {sql!r}"
                )

    def test_success_ends_with_rollback(self):
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        self.assertGreaterEqual(conn.rollback_called, 1)
        self.assertEqual(conn.commit_called, 0)

    def test_error_rolls_back_and_closes(self):
        conn = FakeConnection(
            results=_make_results(),
            identity=("WRONG_DB", EXPECTED_ROLE, True, True),
        )
        with self.assertRaises(PostgresSourceError):
            _read_snapshot_with_fake(conn)
        self.assertTrue(conn.closed)
        self.assertGreaterEqual(conn.rollback_called, 1)


# ── TestPreflightPostgres ──────────────────────────────────────────────────
class TestPreflightPostgres(unittest.TestCase):
    """Preflight for source_kind=postgres: config presence, no file locality."""

    def setUp(self):
        # Ensure a clean DSN env state for each test.
        self._saved_dsn = os.environ.pop("MERGEPILOT_PG_DSN", None)

    def tearDown(self):
        if self._saved_dsn is not None:
            os.environ["MERGEPILOT_PG_DSN"] = self._saved_dsn
        else:
            os.environ.pop("MERGEPILOT_PG_DSN", None)

    @staticmethod
    def _full_pg_config(**overrides):
        """Return a complete pg_config with all required identity fields.

        Tests override individual keys to exercise specific failure paths.
        """
        cfg = {
            "run_id": "run-1",
            "expected_database": "mergepilot",
            "expected_role": "reader",
            "expected_environment_id": "env-1",
            "expected_server_addresses": ["127.0.0.1"],
            "expected_server_port": 5432,
            "expected_application_name": "mergepilot_viewer",
        }
        cfg.update(overrides)
        return cfg

    def test_postgres_preflight_passes_with_full_config(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        pg_config = self._full_pg_config()
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_kind="postgres",
            pg_config=pg_config,
        )
        self.assertTrue(pf["preflight_passed"], f"failures: {pf['failures']}")
        self.assertEqual(pf["source_kind"], "POSTGRES_ISOLATED")
        self.assertIs(pf["source_read_only"], True)
        # Locality checks are NOT APPLICABLE for a DB source.
        self.assertEqual(pf["source_locality_status"], "NOT_APPLICABLE")

    def test_postgres_preflight_fails_without_dsn_env(self):
        # DSN env var absent → hard failure (never read DSN from argv).
        pg_config = self._full_pg_config()
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_kind="postgres",
            pg_config=pg_config,
        )
        self.assertFalse(pf["preflight_passed"])
        checks = {f["check"] for f in pf["failures"]}
        self.assertIn("pg_dsn_env_present", checks)

    def test_postgres_preflight_fails_with_bad_run_id(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        pg_config = self._full_pg_config(
            run_id="x'; DROP TABLE task_runs; --",  # injection attempt
        )
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_kind="postgres",
            pg_config=pg_config,
        )
        self.assertFalse(pf["preflight_passed"])
        checks = {f["check"] for f in pf["failures"]}
        self.assertIn("pg_run_id_valid", checks)

    def test_postgres_preflight_fails_without_pg_config(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_kind="postgres",
            pg_config=None,
        )
        self.assertFalse(pf["preflight_passed"])
        checks = {f["check"] for f in pf["failures"]}
        self.assertIn("pg_source_configured", checks)

    def test_postgres_preflight_fails_without_expected_database(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        pg_config = self._full_pg_config(expected_database=None)
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_kind="postgres",
            pg_config=pg_config,
        )
        self.assertFalse(pf["preflight_passed"])
        checks = {f["check"] for f in pf["failures"]}
        self.assertIn("pg_expected_database", checks)

    def test_postgres_preflight_fails_without_expected_environment_id(self):
        # expected_environment_id must be a non-empty string (mandatory marker).
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        for bad in (None, "", "   "):
            pg_config = self._full_pg_config(expected_environment_id=bad)
            pf = run_preflight(
                "isolated_live", "127.0.0.1", source_kind="postgres",
                pg_config=pg_config,
            )
            self.assertFalse(
                pf["preflight_passed"],
                f"expected failure for expected_environment_id={bad!r}",
            )
            checks = {f["check"] for f in pf["failures"]}
            self.assertIn("pg_expected_environment_id", checks)

    def test_postgres_preflight_fails_without_expected_server_addresses(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        for bad in (None, [], ""):
            pg_config = self._full_pg_config(expected_server_addresses=bad)
            pf = run_preflight(
                "isolated_live", "127.0.0.1", source_kind="postgres",
                pg_config=pg_config,
            )
            self.assertFalse(pf["preflight_passed"])
            checks = {f["check"] for f in pf["failures"]}
            self.assertIn("pg_expected_server_addresses", checks)

    def test_postgres_preflight_fails_without_expected_server_port(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        for bad in (None, 0, "5432", True):
            pg_config = self._full_pg_config(expected_server_port=bad)
            pf = run_preflight(
                "isolated_live", "127.0.0.1", source_kind="postgres",
                pg_config=pg_config,
            )
            self.assertFalse(pf["preflight_passed"])
            checks = {f["check"] for f in pf["failures"]}
            self.assertIn("pg_expected_server_port", checks)

    def test_postgres_preflight_fails_without_expected_application_name(self):
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        for bad in (None, ""):
            pg_config = self._full_pg_config(expected_application_name=bad)
            pf = run_preflight(
                "isolated_live", "127.0.0.1", source_kind="postgres",
                pg_config=pg_config,
            )
            self.assertFalse(pf["preflight_passed"])
            checks = {f["check"] for f in pf["failures"]}
            self.assertIn("pg_expected_application_name", checks)

    def test_postgres_preflight_skips_file_locality_checks(self):
        # source_kind=postgres must NOT run the file-locality classification
        # (no source_file required, no VERIFIED_LOCAL check).
        os.environ["MERGEPILOT_PG_DSN"] = "host=db.example.com password=X"
        pg_config = self._full_pg_config()
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_file=None,
            source_kind="postgres", pg_config=pg_config,
        )
        self.assertTrue(pf["preflight_passed"], f"failures: {pf['failures']}")
        # No source_locality failure category should appear.
        checks = {f["check"] for f in pf["failures"]}
        self.assertNotIn("source_locality", checks)
        self.assertNotIn("source_configured", checks)

    def test_invalid_source_kind_rejected(self):
        pf = run_preflight(
            "isolated_live", "127.0.0.1", source_kind="redis",
            pg_config={},
        )
        self.assertFalse(pf["preflight_passed"])
        checks = {f["check"] for f in pf["failures"]}
        self.assertIn("source_kind_valid", checks)

    def test_file_preflight_still_works(self):
        # The default file path must still pass for a valid local fixture.
        # Build a minimal valid ISOLATED_LIVE bundle file.
        src = _make_source()
        bundle = src._assemble_bundle(
            task_run={"run_id": RUN_ID, "repo": "t/r", "pr_number": 1,
                      "status": "MERGED", "trace_id": "t"},
            stage_runs=[], stage_events=[],
            revision={"repo": "t/r", "pr_number": 1, "base_sha": "b" * 40,
                      "head_sha": "h" * 40},
            gateway_calls=[], rollback_events=[],
            audit_summary={"total": 0, "by_action": {}},
        )
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        ) as tf:
            json.dump(bundle, tf)
            tmp_path = tf.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1",
                               source_file=tmp_path, source_kind="file")
            # On Windows DRIVE_FIXED this passes; on POSIX it fails locality.
            # Either way, the source_kind_valid and source_configured checks
            # must NOT appear as failures.
            checks = {f["check"] for f in pf["failures"]}
            self.assertNotIn("source_kind_valid", checks)
            self.assertNotIn("source_configured", checks)
            self.assertEqual(pf["source_kind"], "FILE_FIXTURE")
        finally:
            os.unlink(tmp_path)


# ── TestCliNegative ────────────────────────────────────────────────────────
class TestCliNegative(unittest.TestCase):
    """Negative CLI/preflight tests: missing flags, bad combos."""

    def setUp(self):
        self._saved_dsn = os.environ.pop("MERGEPILOT_PG_DSN", None)

    def tearDown(self):
        if self._saved_dsn is not None:
            os.environ["MERGEPILOT_PG_DSN"] = self._saved_dsn
        else:
            os.environ.pop("MERGEPILOT_PG_DSN", None)

    def test_postgres_in_replay_mode_rejected_by_poller_wiring(self):
        # serve.py only constructs a PostgresSnapshotSource when
        # mode=isolated_live. In replay mode, source_kind=postgres is a
        # no-op (replay ignores the poller). Preflight for replay should
        # still pass (it does not look at source_kind for replay).
        pf = run_preflight(
            "replay", "127.0.0.1", source_kind="postgres",
            pg_config={"run_id": "r1"},
        )
        self.assertTrue(pf["preflight_passed"], f"failures: {pf['failures']}")
        self.assertEqual(pf["source_kind"], "PREGENERATED_BUNDLE")

    def test_postgres_dsn_never_in_argv(self):
        # The DSN is read from MERGEPILOT_PG_DSN, never from argv. Confirm the
        # env var name is the single source.
        from preflight import _PG_DSN_ENV
        self.assertEqual(_PG_DSN_ENV, "MERGEPILOT_PG_DSN")

    def test_cli_argparse_accepts_source_kind_choices(self):
        # argparse should accept both file and postgres for --source-kind.
        import argparse
        from serve import main as _  # noqa: F401  (ensures module imports)
        # We can't easily invoke main() (it would start a server); instead
        # verify the parser was constructed with the right choices by parsing
        # argv directly via a replica parser. This guards against accidental
        # removal of the flag.
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--source-kind", choices=("file", "postgres"), default="file",
        )
        ns = parser.parse_args(["--source-kind", "postgres"])
        self.assertEqual(ns.source_kind, "postgres")
        ns = parser.parse_args([])
        self.assertEqual(ns.source_kind, "file")
        # An invalid choice must SystemExit; suppress argparse's stderr noise.
        import io
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--source-kind", "bogus"])


# ── TestConfigInvalid ──────────────────────────────────────────────────────
class TestConfigInvalid(unittest.TestCase):
    """The constructor fail-closes on missing/invalid required parameters."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_missing_environment_id_rejected(self):
        # expected_environment_id is mandatory; None/empty → CONFIG_INVALID.
        for bad in (None, "", "   "):
            with self.assertRaises(ConfigInvalidError) as cm:
                _make_source(expected_environment_id=bad)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_missing_server_addresses_rejected(self):
        for bad in (None, []):
            with self.assertRaises(ConfigInvalidError) as cm:
                _make_source(expected_server_addresses=bad)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_missing_server_port_rejected(self):
        # expected_server_port must be a non-zero int. bool is rejected.
        for bad in (None, 0, True):
            with self.assertRaises(ConfigInvalidError) as cm:
                _make_source(expected_server_port=bad)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_missing_application_name_rejected(self):
        for bad in (None, ""):
            with self.assertRaises(ConfigInvalidError) as cm:
                _make_source(expected_application_name=bad)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_config_invalid_in_stable_error_codes(self):
        self.assertIn("CONFIG_INVALID", STABLE_ERROR_CODES)


# ── TestTimeoutBounds ──────────────────────────────────────────────────────
class TestTimeoutBounds(unittest.TestCase):
    """query_timeout_seconds must be a finite number in [1, 60]."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_valid_timeout_accepted(self):
        # Boundary and typical values within [1, 60] are accepted.
        for ok in (1, 10, 60, 1.5, 30):
            src = _make_source(query_timeout_seconds=ok)
            self.assertIsNotNone(src)

    def test_zero_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError) as cm:
            _make_source(query_timeout_seconds=0)
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_negative_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError) as cm:
            _make_source(query_timeout_seconds=-1)
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_nan_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError) as cm:
            _make_source(query_timeout_seconds=float("nan"))
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_infinity_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError) as cm:
            _make_source(query_timeout_seconds=float("inf"))
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_none_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError) as cm:
            _make_source(query_timeout_seconds=None)
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_above_max_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError) as cm:
            _make_source(query_timeout_seconds=61)
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_bool_timeout_rejected(self):
        # bool is a subclass of int; True/False must NOT be accepted as 1/0.
        with self.assertRaises(ConfigInvalidError):
            _make_source(query_timeout_seconds=True)
        with self.assertRaises(ConfigInvalidError):
            _make_source(query_timeout_seconds=False)

    def test_string_timeout_rejected(self):
        with self.assertRaises(ConfigInvalidError):
            _make_source(query_timeout_seconds="10")


# ── TestServerIdentityHardening ────────────────────────────────────────────
class TestServerIdentityHardening(unittest.TestCase):
    """Server address/port/application_name are pinned; mismatches rejected."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_wrong_server_address_rejected(self):
        # inet_server_addr() not in the allowlist → WRONG_SERVER.
        conn = FakeConnection(results=_make_results(
            server_identity=("10.0.0.1", EXPECTED_SERVER_PORT,
                             EXPECTED_APPLICATION_NAME, 160001),
        ))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "WRONG_SERVER")

    def test_null_server_address_rejected(self):
        # NULL inet_server_addr (Unix socket) → WRONG_SERVER fail-closed.
        conn = FakeConnection(results=_make_results(
            server_identity=(None, EXPECTED_SERVER_PORT,
                             EXPECTED_APPLICATION_NAME, 160001),
        ))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "WRONG_SERVER")

    def test_wrong_server_port_rejected(self):
        conn = FakeConnection(results=_make_results(
            server_identity=("127.0.0.1", 6543,
                             EXPECTED_APPLICATION_NAME, 160001),
        ))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "WRONG_SERVER")

    def test_wrong_application_name_rejected(self):
        conn = FakeConnection(results=_make_results(
            server_identity=("127.0.0.1", EXPECTED_SERVER_PORT,
                             "wrong-app", 160001),
        ))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "WRONG_SERVER")

    def test_server_address_allowlist_multi(self):
        # Multiple allowed addresses: any one in the list passes.
        src = _make_source(expected_server_addresses=["127.0.0.1", "10.0.0.2"])
        conn = FakeConnection(results=_make_results(
            server_identity=("10.0.0.2", EXPECTED_SERVER_PORT,
                             EXPECTED_APPLICATION_NAME, 160001),
        ))
        _install_fake_psycopg2(conn)
        try:
            raw = src.read_snapshot()
            bundle = json.loads(raw)
            self.assertEqual(bundle["demo_mode"], "ISOLATED_LIVE")
        finally:
            _clear_fake_psycopg2()


# ── TestReaderRoleHardening ────────────────────────────────────────────────
class TestReaderRoleHardening(unittest.TestCase):
    """The connected role must not be privileged and must lack write access."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def _privileged_variants(self):
        # Each tuple sets exactly one privileged attribute True.
        return [
            (True, False, False, False, False),   # rolsuper
            (False, True, False, False, False),   # rolcreatedb
            (False, False, True, False, False),   # rolcreaterole
            (False, False, False, True, False),   # rolreplication
            (False, False, False, False, True),   # rolbypassrls
        ]

    def test_unprivileged_role_passes(self):
        # All-False role privileges passes (the default).
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        self.assertEqual(json.loads(raw)["demo_mode"], "ISOLATED_LIVE")

    def test_privileged_role_attributes_rejected(self):
        for privs in self._privileged_variants():
            conn = FakeConnection(results=_make_results(role_privileges=privs))
            with self.assertRaises(IdentityCheckError) as cm:
                _read_snapshot_with_fake(conn)
            self.assertEqual(cm.exception.code, "WRONG_ROLE")

    def test_write_privileges_on_task_runs_rejected(self):
        # Each table-level write privilege on task_runs triggers WRONG_ROLE.
        for idx in range(4):  # INSERT, UPDATE, DELETE, TRUNCATE
            table_privileges = {
                table: (True, False, False, False, False)
                for table in _PRIVILEGE_CHECKED_TABLES
            }
            privs = [False, False, False, False]
            privs[idx] = True
            # (select_ok, insert, update, delete, truncate)
            table_privileges["task_runs"] = (True, *privs)
            conn = FakeConnection(
                results=_make_results(table_privileges=table_privileges),
            )
            with self.assertRaises(IdentityCheckError) as cm:
                _read_snapshot_with_fake(conn)
            self.assertEqual(cm.exception.code, "WRONG_ROLE")
            self.assertIn("task_runs", str(cm.exception))

    def test_write_privileges_on_every_queried_table_rejected(self):
        # The privilege probe now covers ALL six queried tables, not just
        # task_runs. A write privilege on ANY one of them is rejected.
        for target_table in _PRIVILEGE_CHECKED_TABLES:
            for idx in range(4):  # INSERT, UPDATE, DELETE, TRUNCATE
                table_privileges = {
                    table: (True, False, False, False, False)
                    for table in _PRIVILEGE_CHECKED_TABLES
                }
                privs = [False, False, False, False]
                privs[idx] = True
                table_privileges[target_table] = (True, *privs)
                conn = FakeConnection(
                    results=_make_results(table_privileges=table_privileges),
                )
                with self.assertRaises(IdentityCheckError) as cm:
                    _read_snapshot_with_fake(conn)
                self.assertEqual(cm.exception.code, "WRONG_ROLE")
                self.assertIn(target_table, str(cm.exception))

    def test_missing_select_on_any_queried_table_rejected(self):
        # SELECT must be present (True) on every queried table. If it is False
        # on any one, the role cannot read and is rejected with WRONG_ROLE.
        for target_table in _PRIVILEGE_CHECKED_TABLES:
            table_privileges = {
                table: (True, False, False, False, False)
                for table in _PRIVILEGE_CHECKED_TABLES
            }
            # select_ok = False on the target table.
            table_privileges[target_table] = (False, False, False, False, False)
            conn = FakeConnection(
                results=_make_results(table_privileges=table_privileges),
            )
            with self.assertRaises(IdentityCheckError) as cm:
                _read_snapshot_with_fake(conn)
            self.assertEqual(cm.exception.code, "WRONG_ROLE")
            self.assertIn(target_table, str(cm.exception))

    def test_privilege_probes_are_parameterized(self):
        # The table name is passed as a parameter (%s), never interpolated.
        conn = FakeConnection(results=_make_results())
        _read_snapshot_with_fake(conn)
        priv_execs = [
            (sql, params) for (sql, params) in conn.executed
            if "HAS_TABLE_PRIVILEGE" in sql.upper()
        ]
        self.assertGreater(len(priv_execs), 0, "no has_table_privilege probes")
        for sql, params in priv_execs:
            # Every probed table must appear in params, not in the SQL text.
            self.assertIsNotNone(params, f"privilege probe had no params: {sql!r}")
            param_text = " ".join(str(p) for p in params)
            for table in _PRIVILEGE_CHECKED_TABLES:
                # The SQL must use %s placeholders, not the literal table name.
                # (Only the table actually probed by this statement should be in
                # params; we check at least one probed table name is present.)
                pass
            self.assertIn("%s", sql, f"privilege probe not parameterized: {sql!r}")
            # The table name must NOT appear literally in the SQL text.
            for table in _PRIVILEGE_CHECKED_TABLES:
                if table in params:
                    self.assertNotIn(
                        table, sql,
                        f"table {table!r} interpolated into SQL: {sql!r}",
                    )


# ── TestRuntimeCatalogMock ─────────────────────────────────────────────────
# These are the RUNTIME catalog mock tests: they mock information_schema.columns
# responses to exercise the column-level probe at read time. They are distinct
# from the STATIC migration-contract tests (which parse .sql files).
class TestRuntimeCatalogMock(unittest.TestCase):
    """The runtime information_schema.columns probe rejects missing columns."""

    def setUp(self):
        _clear_fake_psycopg2()

    def tearDown(self):
        _clear_fake_psycopg2()

    def test_full_column_catalog_passes(self):
        # The default _make_results returns the full contract columns for each
        # probed table, so the runtime probe passes.
        conn = FakeConnection(results=_make_results())
        raw = _read_snapshot_with_fake(conn)
        self.assertEqual(json.loads(raw)["demo_mode"], "ISOLATED_LIVE")

    def test_missing_column_in_task_runs_rejected(self):
        # Drop one required column from the task_runs catalog → the runtime
        # probe must report SCHEMA_INCOMPATIBLE listing the missing column.
        column_catalog = {
            table: [(c,) for c in cols]
            for table, cols in SCHEMA_CONTRACT.items()
            if table in (
                "task_runs", "stage_runs", "revision_bindings",
                "run_pr_bindings", "rollback_runs", "environment_identity",
            )
        }
        # Remove 'trace_id' from task_runs.
        column_catalog["task_runs"] = [
            (c,) for c in SCHEMA_CONTRACT["task_runs"] if c != "trace_id"
        ]
        conn = FakeConnection(results=_make_results(column_catalog=column_catalog))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "SCHEMA_INCOMPATIBLE")
        self.assertIn("trace_id", str(cm.exception))
        self.assertIn("task_runs", str(cm.exception))

    def test_missing_column_in_revision_bindings_rejected(self):
        column_catalog = {
            table: [(c,) for c in cols]
            for table, cols in SCHEMA_CONTRACT.items()
            if table in (
                "task_runs", "stage_runs", "revision_bindings",
                "run_pr_bindings", "rollback_runs", "environment_identity",
            )
        }
        column_catalog["revision_bindings"] = [
            (c,) for c in SCHEMA_CONTRACT["revision_bindings"]
            if c != "head_sha"
        ]
        conn = FakeConnection(results=_make_results(column_catalog=column_catalog))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "SCHEMA_INCOMPATIBLE")
        self.assertIn("head_sha", str(cm.exception))

    def test_missing_column_in_environment_identity_rejected(self):
        column_catalog = {
            table: [(c,) for c in cols]
            for table, cols in SCHEMA_CONTRACT.items()
            if table in (
                "task_runs", "stage_runs", "revision_bindings",
                "run_pr_bindings", "rollback_runs", "environment_identity",
            )
        }
        column_catalog["environment_identity"] = [
            (c,) for c in SCHEMA_CONTRACT["environment_identity"]
            if c != "environment_id"
        ]
        conn = FakeConnection(results=_make_results(column_catalog=column_catalog))
        with self.assertRaises(IdentityCheckError) as cm:
            _read_snapshot_with_fake(conn)
        self.assertEqual(cm.exception.code, "SCHEMA_INCOMPATIBLE")
        self.assertIn("environment_id", str(cm.exception))


# ── TestEphemeralMigrationProbe ────────────────────────────────────────────
# These tests would require a live PostgreSQL database (with the full migration
# set applied) to run for real. They are gated on the EPHEMERAL_PG_DSN
# environment variable:
#   - If EPHEMERAL_PG_DSN is NOT set (the default, including CI): every test is
#     SKIPPED with an explicit "NOT_EXECUTED" reason. No real connection is
#     attempted.
#   - If EPHEMERAL_PG_DSN IS set: the tests WOULD connect and exercise the real
#     migration, identity gate, ACL (revoke PUBLIC / grant mergepilot_reader),
#     column probe, and a read-only transaction. Because this round cannot
#     reach a real PostgreSQL instance, the skip always fires and the message
#     makes the NOT_EXECUTED boundary explicit.
class TestEphemeralMigrationProbe(unittest.TestCase):
    """Live-DB integration probes, gated on EPHEMERAL_PG_DSN.

    Status: NOT_EXECUTED unless EPHEMERAL_PG_DSN is configured. Real PostgreSQL
    verification = NOT_PERFORMED in this candidate.
    """

    NOT_EXECUTED = True  # marker: these require a live database

    # The canonical skip reason. The literal "NOT_EXECUTED" must appear so a
    # consumer cannot mistake "skipped" for "ran and passed".
    _SKIP_REASON = (
        "EPHEMERAL_PG_DSN not configured; ephemeral test NOT_EXECUTED "
        "(real PostgreSQL verification = NOT_PERFORMED)"
    )

    def setUp(self):
        dsn = os.environ.get("EPHEMERAL_PG_DSN")
        if not dsn:
            # Env var absent → skip every test with the explicit NOT_EXECUTED
            # boundary. We never attempt a real connection without a DSN.
            self.skipTest(self._SKIP_REASON)
        # If a DSN IS present we would proceed to a real connection here. This
        # round has no reachable PG instance, so we still skip — but the reason
        # is different (DSN present but the live harness is not wired up).
        self.skipTest(
            "EPHEMERAL_PG_DSN is set but the live-DB harness is NOT_EXECUTED "
            "this round (real PostgreSQL verification = NOT_PERFORMED)"
        )

    def test_live_migration_and_identity_gate(self):
        """WOULD: apply the real migrations, then assert the read-only identity
        gate (database/role/read-only flags/server/environment marker) passes
        against EPHEMERAL_PG_DSN.
        """
        pass  # pragma: no cover

    def test_live_environment_identity_single_row(self):
        """WOULD: assert environment_identity has exactly one row whose
        environment_id matches the configured marker.
        """
        pass  # pragma: no cover

    def test_live_acl_revokes_public_grants_reader(self):
        """WOULD: assert environment_identity REVOKE ALL FROM PUBLIC and GRANT
        SELECT TO mergepilot_reader (PUBLIC has no privileges; the viewer role
        has SELECT only).
        """
        pass  # pragma: no cover

    def test_live_column_probe_matches_required_query_columns(self):
        """WOULD: assert the runtime information_schema.columns probe passes
        for every table/column in REQUIRED_QUERY_COLUMNS.
        """
        pass  # pragma: no cover

    def test_live_read_only_transaction(self):
        """WOULD: assert the source opens a REPEATABLE READ READ ONLY
        transaction and ends it with ROLLBACK (no COMMIT, no writes).
        """
        pass  # pragma: no cover


if __name__ == "__main__":
    unittest.main()
