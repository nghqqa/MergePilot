"""ISOLATED_LIVE MergePilot-Test integration (Phase A implementation).

Design-only implementation of the Rev-3 integration design: pure functions,
read-only prerequisite probes, an observation-window orchestrator, a
mock-ready snapshot source, HTTP status-contract assertions, and cleanup
with dual stable error codes. This module NEVER starts WSL/Docker/
PostgreSQL, NEVER opens a real connection, and NEVER fabricates
audit_events or bind_revision output — all database interaction is injected
(``FakeConnection``/``FakeCursor`` in tests; a real connector is a later,
separately authorized round).

Frozen truth boundaries (unchanged by anything in this module):
  MergePilot-Test_database_verified = false
  MergePilot-Test_application_integration_verified = false
  production_verified = false
  revision_producer_contract = NOT_VERIFIED
  audit_producer_contract = NOT_VERIFIED
  M8 remains undefined
"""

from __future__ import annotations

import copy as _copy
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import sys as _sys

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in _sys.path:
    _sys.path.insert(0, str(_HERE))

from postgres_source import (  # noqa: E402
    CANONICAL_VIEWER_ROLE,
    PRIVILEGE_CHECKED_TABLES,
)
from live_poller import SnapshotSource  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────

MERGEPILOT_TEST_SOURCE_KIND = "POSTGRES_MERGEPILOT_TEST"
ISOLATED_SOURCE_KIND = "POSTGRES_ISOLATED"
KNOWN_SOURCE_KINDS = (MERGEPILOT_TEST_SOURCE_KIND, ISOLATED_SOURCE_KIND)

REAL_DB_NAME = "mergepilot_audit"
REAL_DB_MARKER = "mergepilot-test-app"
READER_ROLE = CANONICAL_VIEWER_ROLE  # "mergepilot_reader", exact
APP_NAME = "mergepilot_isolated_live_reader"

# Version window (Rev-3 design fix): INCLUSIVE on both boundaries
# 120000 <= v <= 180000. This is a DELIBERATE divergence from the Phase-B
# disposable-container gate (120000 <= v < 180000, upper-exclusive); the
# divergence is recorded here and in the design document.
VERSION_WINDOW_MIN = 120000
VERSION_WINDOW_MAX = 180000

OWNER_ROLES_READER_MUST_NOT_JOIN = (
    "runtime_owner", "gate_owner", "envelope_maint",
)

PRODUCER_ACTION_SEQUENCE = ("review", "fix", "verify", "merge", "close_pr")
PRODUCER_WINDOW_MIN_SECONDS = 60
PRODUCER_WINDOW_MAX_SECONDS = 900
PRODUCER_WINDOW_DEFAULT_SECONDS = 300

# Frozen producer-contract status — observation NEVER upgrades these.
PRODUCER_CONTRACTS_STATUS = {
    "revision_producer_contract": "NOT_VERIFIED",
    "audit_producer_contract": "NOT_VERIFIED",
}

_DB_CONTEXT_REQUIRED_KEYS = (
    "database", "current_user", "application_name", "server_version_num",
    "marker_value", "server_address", "server_port", "captured_at",
)
_FINGERPRINT_KEYS = (
    "database", "current_user", "application_name", "server_version_num",
    "marker_value", "server_address", "server_port",
)

# Only read-only statement openers may ever be emitted by this module.
_ALLOWED_SQL_OPENERS = ("SELECT", "SHOW")

# Secret patterns for argv safety and redaction.
_DSN_RE = re.compile(r"postgresql?://[^/\s@]+:[^/\s@]+@")
_PASSWORD_KV_RE = re.compile(r"(password\s*=\s*)['\"]?[^\s;&'\"]+", re.IGNORECASE)
_SQL_PASSWORD_RE = re.compile(r"(PASSWORD\s+)'[^']*'", re.IGNORECASE)
_TOKEN_RES = (
    re.compile(r"ghp_[0-9a-zA-Z]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[a-zA-Z0-9]{40}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9-]{10,}"),
)


class IntegrationGateError(Exception):
    """Stable, redacted integration gate failure.

    ``code`` is a bare stable code (``DB_PREREQUISITE_MISSING``,
    ``ENVIRONMENT_FINGERPRINT_CHANGED``, ``PRODUCER_WINDOW_TIMEOUT``,
    ``KIND_MISMATCH``, ...). Messages never contain DSNs, passwords, or raw
    subprocess/SQL output.
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(code + ((" (%s)" % detail) if detail else ""))


class IntegrationCleanupError(Exception):
    """Carries primary + cleanup stable codes; neither swallows the other."""

    def __init__(self, primary_code: str, cleanup_codes: Iterable[str]):
        self.primary_code = primary_code
        self.cleanup_codes = tuple(cleanup_codes)
        super().__init__(
            "primary=%s cleanup=%s"
            % (primary_code, ",".join(self.cleanup_codes) or "none"))


# ── SQL safety ───────────────────────────────────────────────────────────────

def _assert_read_only_sql(sql: str) -> None:
    """Every statement this module emits must start with SELECT/SHOW.

    This enforces the CHECK-ONLY prerequisite contract structurally: no
    CREATE ROLE / GRANT / REVOKE / ALTER / INSERT / UPDATE / DELETE / TRUNCATE
    / DROP statement can ever be produced.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise IntegrationGateError("CONFIG_INVALID", "SQL must be a string")
    first = sql.lstrip().split(None, 1)[0].upper()
    if first not in _ALLOWED_SQL_OPENERS:
        raise IntegrationGateError(
            "CONFIG_INVALID",
            "non-read-only statement opener rejected: %s" % first)


def assert_argv_safe(argv: list, secrets: Iterable[str] = ()) -> None:
    """Reject argv containing secrets, full DSNs, or SQL PASSWORD literals."""
    joined = " ".join(str(t) for t in argv)
    forbidden = []
    for s in secrets:
        if s and s in joined:
            forbidden.append("secret")
            break
    if _DSN_RE.search(joined):
        forbidden.append("full_dsn")
    if _SQL_PASSWORD_RE.search(joined):
        forbidden.append("sql_password_literal")
    if forbidden:
        raise IntegrationGateError("ARGV_SECRET_LEAK", ",".join(forbidden))


def redact_text(text: str) -> str:
    """Best-effort redaction for logs/diagnostics (never a publish path)."""
    if not isinstance(text, str):
        return text
    out = _DSN_RE.sub("postgresql://***:***@", text)
    out = _PASSWORD_KV_RE.sub(
        lambda m: m.group(1) + "***REDACTED***", out)
    out = _SQL_PASSWORD_RE.sub(r"\1'***REDACTED***'", out)
    for pat in _TOKEN_RES:
        out = pat.sub("***REDACTED***", out)
    return out


# ── Server version window (inclusive, per Rev-3 design) ─────────────────────

def check_server_version_window(server_version_num: Any) -> bool:
    """Enforce 120000 <= v <= 180000 (BOTH boundaries inclusive).

    Raises ``WRONG_SERVER`` on any violation (non-int, bool, or outside the
    inclusive window). Deliberate divergence from the Phase-B
    disposable-container gate (upper-exclusive ``< 180000``), recorded in
    the Rev-3 design.
    """
    if not isinstance(server_version_num, int) or \
            isinstance(server_version_num, bool):
        raise IntegrationGateError(
            "WRONG_SERVER", "server_version_num not an int")
    if not (VERSION_WINDOW_MIN <= server_version_num <= VERSION_WINDOW_MAX):
        raise IntegrationGateError(
            "WRONG_SERVER",
            "server_version_num outside inclusive window %d-%d"
            % (VERSION_WINDOW_MIN, VERSION_WINDOW_MAX))
    return True


# ── DB authorization context ─────────────────────────────────────────────────

def build_db_authorization_context(
    *, database: str, current_user: str, application_name: str,
    server_version_num: int, marker_value: str, server_address: str,
    server_port: int, captured_at: str,
) -> dict:
    """Build (and validate) the DB authorization context; no inference.

    All values are measured by the caller at authorization time; this
    function never defaults, infers, or back-fills any field.
    """
    ctx = {
        "database": database,
        "current_user": current_user,
        "application_name": application_name,
        "server_version_num": server_version_num,
        "marker_value": marker_value,
        "server_address": server_address,
        "server_port": server_port,
        "captured_at": captured_at,
    }
    validate_db_authorization_context(ctx)
    return ctx


def validate_db_authorization_context(ctx: Any) -> None:
    """Strict validation: exact key set, non-empty scalars, version window."""
    if not isinstance(ctx, dict):
        raise IntegrationGateError(
            "AUTH_CONTEXT_INVALID", "context not a dict")
    for key in _DB_CONTEXT_REQUIRED_KEYS:
        if key not in ctx:
            raise IntegrationGateError(
                "AUTH_CONTEXT_INVALID", "missing field %s" % key)
    # No inference: container-image fields must never appear in a DB context.
    for foreign in ("image_digest", "local_image_id", "daemon_fingerprint"):
        if ctx.get(foreign):
            raise IntegrationGateError(
                "AUTH_CONTEXT_INVALID",
                "container-mode field %s present in DB context" % foreign)
    for key in ("database", "current_user", "application_name",
                "marker_value", "server_address", "captured_at"):
        if not isinstance(ctx[key], str) or not ctx[key]:
            raise IntegrationGateError(
                "AUTH_CONTEXT_INVALID", "field %s empty/not a string" % key)
    if ctx["database"] != REAL_DB_NAME:
        raise IntegrationGateError(
            "WRONG_DATABASE", "database != %s" % REAL_DB_NAME)
    if ctx["current_user"] != READER_ROLE:
        raise IntegrationGateError("WRONG_ROLE", "user != %s" % READER_ROLE)
    if ctx["marker_value"] != REAL_DB_MARKER:
        raise IntegrationGateError(
            "ENVIRONMENT_ID_MISMATCH", "marker != %s" % REAL_DB_MARKER)
    check_server_version_window(ctx["server_version_num"])
    port = ctx["server_port"]
    if not isinstance(port, int) or isinstance(port, bool) or \
            not (0 < port < 65536):
        raise IntegrationGateError(
            "WRONG_SERVER", "server_port invalid")
    if not isinstance(ctx["server_address"], str) or \
            not ctx["server_address"] or ctx["server_address"] == "NULL":
        raise IntegrationGateError(
            "WRONG_SERVER", "server_address NULL/empty (must be real TCP)")


def deep_copy_context(ctx: dict) -> dict:
    """Defensive deep copy — the caller's mutable dict is never retained."""
    return _copy.deepcopy(ctx)


# ── Read-only prerequisite probes (CHECK-ONLY) ───────────────────────────────

def run_db_prerequisite_checks(cursor, *,
                               expected_marker: str = REAL_DB_MARKER,
                               expected_role: str = READER_ROLE) -> dict:
    """Run ALL read-only prerequisite probes; fail-closed on any mismatch.

    Probes (every statement passes :func:`_assert_read_only_sql`):
      1. reader role exists and is hardened (5 privileged attrs false)
      2. reader is NOT a member of runtime_owner/gate_owner/envelope_maint
      3. environment_identity has EXACTLY one row matching expected_marker
      4. all 9 privilege-checked tables exist in schema public
      5. per-table ACL: SELECT=true; INSERT/UPDATE/DELETE/TRUNCATE=false

    This function NEVER issues CREATE ROLE / GRANT / REVOKE / ALTER /
    INSERT / UPDATE / DELETE (structurally enforced). Returns the measured
    identity values for the authorization context.
    """
    # 1. reader hardened
    _assert_read_only_sql(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls FROM pg_roles WHERE rolname = %s")
    cursor.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls FROM pg_roles WHERE rolname = %s", (expected_role,))
    row = cursor.fetchone()
    if row is None:
        raise IntegrationGateError(
            "DB_PREREQUISITE_MISSING", "reader role absent")
    if any(v is not False for v in tuple(row)):
        raise IntegrationGateError(
            "DB_PREREQUISITE_MISSING", "reader role not hardened")

    # 2. membership isolation
    _assert_read_only_sql(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r "
        "ON r.oid = m.roleid WHERE m.member = (SELECT oid FROM pg_roles "
        "WHERE rolname = %s) AND r.rolname IN %s")
    cursor.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r "
        "ON r.oid = m.roleid WHERE m.member = (SELECT oid FROM pg_roles "
        "WHERE rolname = %s) AND r.rolname IN %s",
        (expected_role, OWNER_ROLES_READER_MUST_NOT_JOIN))
    (owner_membership,) = cursor.fetchone()
    if owner_membership != 0:
        raise IntegrationGateError(
            "DB_PREREQUISITE_MISSING", "reader is member of an owner role")

    # 3. marker (exactly one row, value match)
    _assert_read_only_sql(
        "SELECT environment_id FROM environment_identity")
    cursor.execute("SELECT environment_id FROM environment_identity")
    marker_rows = cursor.fetchall()
    if len(marker_rows) != 1:
        raise IntegrationGateError(
            "ENVIRONMENT_ID_NOT_VERIFIED",
            "marker rows = %d (must be exactly 1)" % len(marker_rows))
    if marker_rows[0][0] != expected_marker:
        raise IntegrationGateError("ENVIRONMENT_ID_MISMATCH", "marker value")

    # 4. tables present
    _assert_read_only_sql(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    cursor.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    present = {r[0] for r in cursor.fetchall()}
    missing = [t for t in PRIVILEGE_CHECKED_TABLES if t not in present]
    if missing:
        raise IntegrationGateError(
            "DB_PREREQUISITE_MISSING", "required table(s) absent")

    # 5. per-table ACL
    for table in PRIVILEGE_CHECKED_TABLES:
        _assert_read_only_sql(
            "SELECT has_table_privilege(%s, %s, 'SELECT'), "
            "has_table_privilege(%s, %s, 'INSERT'), "
            "has_table_privilege(%s, %s, 'UPDATE'), "
            "has_table_privilege(%s, %s, 'DELETE'), "
            "has_table_privilege(%s, %s, 'TRUNCATE')")
        cursor.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT'), "
            "has_table_privilege(%s, %s, 'INSERT'), "
            "has_table_privilege(%s, %s, 'UPDATE'), "
            "has_table_privilege(%s, %s, 'DELETE'), "
            "has_table_privilege(%s, %s, 'TRUNCATE')",
            (expected_role, table) * 5)
        perms = tuple(cursor.fetchone())
        if perms != (True, False, False, False, False):
            raise IntegrationGateError(
                "DB_PREREQUISITE_MISSING",
                "ACL contract violated on %s" % table)

    return {
        "database": REAL_DB_NAME,
        "current_user": expected_role,
        "marker_value": expected_marker,
    }


# ── Fingerprint freeze / recheck ─────────────────────────────────────────────

def freeze_execution_fingerprint(measurements: dict) -> dict:
    """Deep-copy and validate the frozen pre-execution fingerprint."""
    fp = deep_copy_context(measurements)
    for key in _FINGERPRINT_KEYS:
        if key not in fp:
            raise IntegrationGateError(
                "AUTH_CONTEXT_INVALID", "fingerprint missing %s" % key)
    return {k: fp[k] for k in _FINGERPRINT_KEYS}


def recheck_execution_fingerprint(before: dict, measure_fn: Callable[[], dict]) -> dict:
    """Re-measure after execution and compare to the frozen fingerprint.

    ``measure_fn`` performs the (injected) re-measurement. Probe failure →
    ENVIRONMENT_RECHECK_FAILED; any field drift →
    ENVIRONMENT_FINGERPRINT_CHANGED. Returns the after-fingerprint.
    """
    try:
        after = measure_fn()
    except BaseException:
        raise IntegrationGateError(
            "ENVIRONMENT_RECHECK_FAILED", "re-measurement failed") from None
    if not isinstance(after, dict):
        raise IntegrationGateError(
            "ENVIRONMENT_RECHECK_FAILED", "measurement not a dict")
    for key in _FINGERPRINT_KEYS:
        if after.get(key) != before.get(key):
            raise IntegrationGateError(
                "ENVIRONMENT_FINGERPRINT_CHANGED", "field %s drifted" % key)
    return after


# ── Producer observation window (observation, NOT verification) ─────────────

def _validate_window_timeout(timeout_seconds: int) -> int:
    if not isinstance(timeout_seconds, int) or \
            isinstance(timeout_seconds, bool):
        raise IntegrationGateError(
            "CONFIG_INVALID", "timeout must be int")
    if not (PRODUCER_WINDOW_MIN_SECONDS <= timeout_seconds
            <= PRODUCER_WINDOW_MAX_SECONDS):
        raise IntegrationGateError(
            "CONFIG_INVALID",
            "timeout outside %d-%d" % (PRODUCER_WINDOW_MIN_SECONDS,
                                       PRODUCER_WINDOW_MAX_SECONDS))
    return timeout_seconds


def observe_producer_window(*, run_id: str, poll_query: Callable[[str], list],
                            timeout_seconds: int = PRODUCER_WINDOW_DEFAULT_SECONDS,
                            clock_fn=time.monotonic,
                            sleep_fn=time.sleep) -> dict:
    """Observe a controlled closed loop; NEVER fabricate events.

    ``poll_query(run_id)`` returns event dicts as read by the reader role
    (task_id, action, ...). ONLY events with ``task_id == run_id`` count;
    success requires the strict action sequence review→fix→verify→merge→
    close_pr observed in order before the deadline. Timeout →
    ``PRODUCER_WINDOW_TIMEOUT``. Producer contracts stay NOT_VERIFIED
    regardless of the outcome (:data:`PRODUCER_CONTRACTS_STATUS`).
    """
    _validate_window_timeout(timeout_seconds)
    started = clock_fn()
    deadline = started + timeout_seconds
    observed: list = []
    while True:
        events = poll_query(run_id) or []
        linked = [e for e in events if e.get("task_id") == run_id]
        observed = _ordered_subsequence(linked)
        if observed == list(PRODUCER_ACTION_SEQUENCE):
            return {
                "attempted": True, "succeeded": True, "error_code": "",
                "run_id": run_id, "retry_count": 0,
                "window_started_at": started,
                "window_ended_at": clock_fn(),
                "observed_actions": list(PRODUCER_ACTION_SEQUENCE),
                "narrow_flag": "mergepilot_test_audit_producer_observed",
                "producer_contracts": dict(PRODUCER_CONTRACTS_STATUS),
            }
        if clock_fn() >= deadline:
            return {
                "attempted": True, "succeeded": False,
                "error_code": "PRODUCER_WINDOW_TIMEOUT",
                "run_id": run_id, "retry_count": 0,
                "window_started_at": started,
                "window_ended_at": clock_fn(),
                "observed_actions": observed,
                "narrow_flag": "",
                "producer_contracts": dict(PRODUCER_CONTRACTS_STATUS),
            }
        sleep_fn(0)  # injected; tests drive time explicitly


def _ordered_subsequence(events: list) -> list:
    """Longest prefix-matching subsequence of PRODUCER_ACTION_SEQUENCE."""
    result: list = []
    idx = 0
    seq = PRODUCER_ACTION_SEQUENCE
    for e in events:
        action = e.get("action")
        if idx < len(seq) and action == seq[idx]:
            result.append(action)
            idx += 1
    return result


def window_retry_allowed(previous: dict, *, new_run_id: str) -> bool:
    """Retry rule: one retry, NEW run_id, timeout/sequence only."""
    if not isinstance(previous, dict) or previous.get("attempted") is not True:
        return False
    if previous.get("retry_count", 0) != 0:
        return False
    if previous.get("error_code") not in (
            "PRODUCER_WINDOW_TIMEOUT", "SEQUENCE_INCOMPLETE"):
        return False
    if not new_run_id or new_run_id == previous.get("run_id"):
        return False
    return True


# ── Mock-ready snapshot source + kind isolation ─────────────────────────────

class MergePilotTestSnapshotSource(SnapshotSource):
    """SnapshotSource with kind POSTGRES_MERGEPILOT_TEST (mock-ready).

    ``bundle_provider`` is a callable returning the (already assembled)
    DemoBundle JSON bytes. NO real connector: a DSN-based implementation is
    a later, separately authorized round. ``read_snapshot`` never fabricates
    audit_events or revision data.
    """

    kind = MERGEPILOT_TEST_SOURCE_KIND

    def __init__(self, bundle_provider: Callable[[], bytes], run_id: str):
        if not callable(bundle_provider):
            raise IntegrationGateError(
                "CONFIG_INVALID", "bundle_provider must be callable")
        if not isinstance(run_id, str) or not run_id:
            raise IntegrationGateError(
                "CONFIG_INVALID", "run_id must be a non-empty string")
        self._provider = bundle_provider
        self._run_id = run_id
        self.closed = False

    @property
    def read_only(self) -> bool:
        return True

    def read_snapshot(self) -> bytes:
        if self.closed:
            raise IntegrationGateError("SOURCE_CLOSED", "source already closed")
        return self._provider()

    def close(self) -> None:
        self.closed = True


def check_kind_isolation(source_kind: str, recorded_kind: Any) -> None:
    """Reject kind mixing in BOTH directions (KIND_MISMATCH).

    ``source_kind`` must be a known kind; ``recorded_kind`` (e.g. a bundle's
    recorded source kind) must equal it exactly. POSTGRES_MERGEPILOT_TEST
    data may never be presented as POSTGRES_ISOLATED and vice versa.
    """
    if source_kind not in KNOWN_SOURCE_KINDS:
        raise IntegrationGateError(
            "KIND_MISMATCH", "unknown source kind")
    if recorded_kind != source_kind:
        raise IntegrationGateError(
            "KIND_MISMATCH",
            "recorded kind does not match source kind")


def assert_live_status_contract(status: dict) -> None:
    """Assert the hard-negative live status contract (real-DB projection)."""
    if not isinstance(status, dict):
        raise IntegrationGateError(
            "STATUS_CONTRACT_VIOLATION", "status not a dict")
    checks = (
        ("source_kind", MERGEPILOT_TEST_SOURCE_KIND),
        ("source_read_only", True),
        ("not_production", True),
        ("production_resource_accessed", None),
        ("production_resource_access_status", "NOT_MEASURED"),
        ("github_writes_enabled", False),
        ("agent_control_enabled", False),
        ("runtime_consumes_rag_context", False),
        ("dynamic_pages_consume_live_api", False),
    )
    for key, expected in checks:
        actual = status.get(key, "<missing>")
        # bool identity matters (True is not 1); compare strictly.
        if type(actual) is not type(expected) or actual != expected:
            raise IntegrationGateError(
                "STATUS_CONTRACT_VIOLATION", "field %s" % key)


# ── Cleanup with dual stable codes ───────────────────────────────────────────

def integration_cleanup(http_server=None, poller=None,
                        sources: Iterable = ()) -> None:
    """Stop HTTP server, poller, and close sources with stable error codes.

    Failure semantics mirror the Phase-B executor: a failed component's
    reference is RETAINED for retry; every failure becomes a stable code
    (HTTP_SHUTDOWN_FAILED / POLLER_STOP_FAILED / POLLER_STILL_ALIVE /
    SOURCE_CLOSE_FAILED); nothing is swallowed; a single
    :class:`IntegrationCleanupError` carries primary "CLEANUP_RESIDUE" plus
    all cleanup codes.
    """
    codes: list = []
    if http_server is not None:
        http_ok = True
        try:
            http_server.shutdown()
        except BaseException:
            http_ok = False
            codes.append("HTTP_SHUTDOWN_FAILED")
        try:
            http_server.server_close()
        except BaseException:
            http_ok = False
            codes.append("HTTP_SHUTDOWN_FAILED")
        # Reference retention on failure is the CALLER's contract: we do not
        # null the object here; the caller keeps it for a retry.
        del http_ok
    if poller is not None:
        poller_ok = True
        try:
            poller.stop()
            poller.join(timeout=5)
            if poller.is_alive():
                poller_ok = False
                codes.append("POLLER_STILL_ALIVE")
        except BaseException:
            poller_ok = False
            codes.append("POLLER_STOP_FAILED")
        del poller_ok
    for src in list(sources):
        try:
            src.close()
        except BaseException:
            codes.append("SOURCE_CLOSE_FAILED")
    if codes:
        raise IntegrationCleanupError("CLEANUP_RESIDUE", codes)


__all__ = [
    "APP_NAME",
    "ISOLATED_SOURCE_KIND",
    "IntegrationCleanupError",
    "IntegrationGateError",
    "KNOWN_SOURCE_KINDS",
    "MERGEPILOT_TEST_SOURCE_KIND",
    "OWNER_ROLES_READER_MUST_NOT_JOIN",
    "PRODUCER_ACTION_SEQUENCE",
    "PRODUCER_CONTRACTS_STATUS",
    "PRODUCER_WINDOW_DEFAULT_SECONDS",
    "PRODUCER_WINDOW_MAX_SECONDS",
    "PRODUCER_WINDOW_MIN_SECONDS",
    "REAL_DB_MARKER",
    "REAL_DB_NAME",
    "READER_ROLE",
    "VERSION_WINDOW_MAX",
    "VERSION_WINDOW_MIN",
    "MergePilotTestSnapshotSource",
    "assert_argv_safe",
    "assert_live_status_contract",
    "build_db_authorization_context",
    "check_kind_isolation",
    "check_server_version_window",
    "deep_copy_context",
    "freeze_execution_fingerprint",
    "integration_cleanup",
    "observe_producer_window",
    "redact_text",
    "recheck_execution_fingerprint",
    "run_db_prerequisite_checks",
    "validate_db_authorization_context",
    "window_retry_allowed",
]
