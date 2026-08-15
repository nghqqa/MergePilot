#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostgreSQL read-only snapshot source for ISOLATED_LIVE mode (Phase 2).

``PostgresSnapshotSource`` is a ``SnapshotSource`` that reads a single run's
state from a PostgreSQL database and assembles a DemoBundle on the fly. It is
**strictly read-only**:

- The FIRST statement after connecting is an explicit
  ``BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY``. All identity
  checks (database/role/read-only/server/role/catalog/environment-marker probes)
  run INSIDE that transaction. On success it ``ROLLBACK``s; on any error it
  ``ROLLBACK``s and closes the connection.
- Only ``SELECT`` queries are issued inside the read-only transaction.
- ``psycopg2`` is imported lazily inside ``read_snapshot`` so that REPLAY /
  FILE_FIXTURE deployments never need a database driver installed.
- The DSN is treated as a secret: it never appears in ``repr``, ``str``,
  exception messages, or logs. On ANY ``psycopg2``/libpq error the re-raised
  message carries ONLY a stable error ``code`` and the exception type name —
  the raw libpq/psycopg2 message (which can echo the connection string on
  connect failure) is NEVER included, not even after redaction.

Environment identity is established via a dedicated single-row
``environment_identity`` table (see
:data:`ENVIRONMENT_MARKER_CONTRACT` and
``migrations/001_environment_identity.sql``), NOT via
``controller_offsets.sync_token``. The marker is mandatory; the source never
guesses the environment from hostname.

The assembled bundle declares ``demo_mode = "ISOLATED_LIVE"`` and a
``bundle_sha256`` computed by the shared :mod:`integrity` helpers, so it flows
through the existing schema/integrity validation in :mod:`live_poller`
unchanged.

This is a P2 code implementation candidate: local review only, not pushed, not
merged. MergePilot-Test isolated verification has NOT been performed.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time

# Add tools/demo_console to sys.path so the shared helpers import cleanly when
# this module is run directly or imported by the test suite.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from integrity import compute_bundle_sha256  # noqa: E402
from live_poller import SnapshotSource  # noqa: E402
from one_click_startup import (  # noqa: E402
    canonicalize_server_address,
    canonicalize_server_address_list,
)


# ── run_id safety ──────────────────────────────────────────────────────────
# run_id is the only caller-supplied value that reaches a SQL query. It must
# match a strict allowlist before it is ever used. Even though every query is
# parameterized (%s), validating the shape up front gives a clean, stable
# error code (RUN_ID_INVALID) rather than an empty result set the caller might
# mistake for "no data yet".
_RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


# ── Stable error codes ─────────────────────────────────────────────────────
# The canonical, machine-readable error codes this source can emit. Each is
# carried on a PostgresSourceError instance via the ``.code`` attribute so the
# poller can surface a stable string regardless of the exception class name.
STABLE_ERROR_CODES = frozenset({
    "PSYCOPG2_MISSING",
    "RUN_ID_INVALID",
    "RUN_NOT_FOUND",
    "WRONG_DATABASE",
    "WRONG_ROLE",
    "WRONG_SERVER",
    "ENVIRONMENT_ID_MISMATCH",
    "ENVIRONMENT_ID_NOT_VERIFIED",
    "SCHEMA_INCOMPATIBLE",
    "NOT_READ_ONLY",
    "POSTGRES_READ_FAILED",
    "CONFIG_INVALID",
})

CANONICAL_VIEWER_ROLE = "mergepilot_reader"


class PostgresSourceError(Exception):
    """Sanitized error raised by :class:`PostgresSnapshotSource`.

    The message NEVER contains the DSN or any other connection secret. It
    carries a stable machine-readable ``code`` (one of
    :data:`STABLE_ERROR_CODES`) and, where relevant, the public identity fields
    that were being verified. Callers (the poller) read ``e.code`` first and
    fall back to ``type(e).__name__`` only when no ``code`` is present.
    """

    def __init__(self, message: str = "", *, code: str = "POSTGRES_READ_FAILED"):
        super().__init__(message)
        # Coerce unknown codes to the generic read-failed code so the poller
        # always sees a member of STABLE_ERROR_CODES on the .code attribute.
        self.code = code if code in STABLE_ERROR_CODES else "POSTGRES_READ_FAILED"


# Backwards-compatible subclass aliases. Each carries the same stable .code so
# existing tests that assert on the exception type still pass, while new code
# asserts on .code.
class IdentityCheckError(PostgresSourceError):
    """The connected database/user/server/read-only flag did not match."""


class RunIdError(PostgresSourceError):
    """The supplied run_id failed the allowlist check (RUN_ID_INVALID)."""


class RunNotFoundError(PostgresSourceError):
    """The supplied run_id does not exist in task_runs (RUN_NOT_FOUND)."""


class PostgresQueryError(PostgresSourceError):
    """A read query failed. The original (sanitized) detail is attached."""


class ConfigInvalidError(PostgresSourceError):
    """The source was constructed with invalid configuration (CONFIG_INVALID)."""


# ── Authoritative schema contract ──────────────────────────────────────────
# SCHEMA_CONTRACT is the authoritative mapping of each audit-DB table this
# source reads to the set of columns known to exist in the production schema
# (extracted verbatim from the migration files in tools/audit-db/). It is the
# single source of truth for column existence and is asserted against by the
# migration-contract tests in test_postgres_source.py: every column referenced
# by a SELECT in this module must be present in the contract.
#
# Sources:
#   m3_state.sql     — task_runs, stage_runs, stage_events, dispatch_outbox,
#                      controller_offsets
#   m3b_policy.sql   — mcp_calls, approvals, policy_action_outbox
#   m3b_b4.sql       — run_pr_bindings (+ task_runs APPROVAL_PENDING status)
#   m3c_state.sql    — rollback_runs (+ task_runs verify_attempt/rollback_id/
#                      parent_run_id)
#   m4f1_state.sql   — envelope_store, run_snapshots, revision_bindings,
#                      task_runs trace_id/active_snapshot_id/skill_data_state
SCHEMA_CONTRACT: dict[str, frozenset[str]] = {
    # task_runs accumulates columns across m3_state, m3c_state, and m4f1_state.
    "task_runs": frozenset({
        "run_id", "room_id", "repo", "pr_number", "branch", "status",
        "current_stage", "attempt", "verdict", "last_error", "created_at",
        "updated_at",
        # m3c_state additions
        "verify_attempt", "rollback_id", "parent_run_id",
        # m4f1_state additions
        "trace_id", "active_snapshot_id", "skill_data_state",
    }),
    # stage_runs is the authoritative per-stage execution source (m3_state).
    "stage_runs": frozenset({
        "id", "run_id", "stage", "agent", "attempt", "status", "started_at",
        "completed_at", "evidence_path", "verdict", "detail",
    }),
    # stage_events is the Matrix event audit log (m3_state). Used only for
    # event provenance/counts, not as the authoritative stage list.
    "stage_events": frozenset({
        "event_id", "room_id", "run_id", "sender", "event_type", "stage",
        "body_sha256", "raw_body", "status", "error", "received_at",
        "processed_at",
    }),
    "dispatch_outbox": frozenset({
        "id", "idempotency_key", "run_id", "room_id", "target_agent",
        "target_stage", "attempt", "body", "status", "matrix_event_id",
        "retry_count", "next_retry_at", "last_error", "created_at",
        "dispatched_at",
    }),
    "controller_offsets": frozenset({
        "consumer_name", "sync_token", "updated_at",
    }),
    # mcp_calls (m3b_policy + m3b_b4 execution_id).
    "mcp_calls": frozenset({
        "request_id", "correlation_id", "phase", "ts", "caller_agent", "tool",
        "decision", "reason_code", "policy_version", "policy_hash",
        "ticket_id", "args_hash", "target_repo", "target_branch",
        "result_status", "http_status", "git_sha", "run_id", "error",
        # m3b_b4 addition
        "execution_id",
    }),
    "approvals": frozenset({
        "ticket_id", "run_id", "action", "repo", "pr_number", "target_branch",
        "expected_head_sha", "revert_commit_sha", "status", "approved_by",
        "approved_at", "expires_at", "used_at", "result_sha", "error",
        "created_at",
        # m3b_b4 additions
        "binding_id", "attempt_no", "canonical_payload", "args_hash",
        "execution_id", "executing_at", "approval_expires_at",
        "exec_ttl_hours",
    }),
    "policy_action_outbox": frozenset({
        "id", "ticket_id", "run_id", "action", "repo", "pr_number",
        "target_branch", "args_hash", "idempotency_key", "status", "attempts",
        "next_retry_at", "result_sha", "matrix_event_id", "error",
        "created_at", "dispatched_at", "completed_at",
        # m3b_b4 addition
        "lease_expires_at",
    }),
    # run_pr_bindings (m3b_b4).
    "run_pr_bindings": frozenset({
        "binding_id", "run_id", "repo", "pr_number", "fix_branch",
        "base_branch", "head_sha", "recorded_at",
    }),
    # rollback_runs (m3c_state).
    "rollback_runs": frozenset({
        "rollback_id", "parent_run_id", "revert_run_id", "reverted_merge_sha",
        "repo", "pr_number", "trigger_event_id", "status", "fail_reason",
        "merge_parent_sha", "revert_branch", "revert_pr_number",
        "revert_ticket_id", "revert_result_sha", "reverify_verdict",
        "reverify_event_id", "diff_summary", "created_at", "updated_at",
    }),
    # m4f1_state tables (read for provenance; many deliberately NOT read).
    "envelope_store": frozenset({
        "content_digest", "content_bytes", "content_json", "content_type",
        "size_bytes", "created_at",
    }),
    "run_snapshots": frozenset({
        "snapshot_id", "run_id", "repo", "pr_number", "base_sha", "head_sha",
        "manifest_digest", "incomplete", "created_at",
    }),
    "revision_bindings": frozenset({
        "binding_id", "run_id", "repo", "pr_number", "base_sha", "head_sha",
        "source_call_id", "source_evidence_digest", "recorded_at",
    }),
    "purge_requests": frozenset({
        "purge_id", "run_id", "target_state", "status", "requested_by",
        "requested_at", "purging_at", "completed_at", "error",
    }),
    "snapshot_job_outbox": frozenset({
        "job_id", "run_id", "snapshot_id", "revision_binding_id",
        "idempotency_key", "status", "claim_id", "leased_by",
        "lease_expires_at", "last_heartbeat_at", "attempts", "max_attempts",
        "next_retry_at", "error", "created_at", "claimed_at", "completed_at",
    }),
    "skill_job_outbox": frozenset({
        "job_id", "run_id", "snapshot_id", "trace_id", "skill_name",
        "skill_version", "attempt", "request_envelope_ref", "idempotency_key",
        "status", "claim_id", "leased_by", "lease_expires_at",
        "last_heartbeat_at", "attempts", "max_attempts", "next_retry_at",
        "result_invocation_id", "error", "created_at", "claimed_at",
        "completed_at",
    }),
    "skill_job_dependencies": frozenset({
        "job_id", "depends_on_job_id",
    }),
    "skill_invocations": frozenset({
        "invocation_id", "run_id", "snapshot_id", "job_id", "trace_id",
        "skill_name", "skill_version", "attempt", "request_id",
        "contract_version", "status", "error_code", "verdict", "input_digest",
        "output_digest", "snapshot_manifest_digest",
        "expected_output_schema_digest", "output_schema_validated",
        "duration_ms", "started_at", "finished_at", "idempotency_key",
    }),
    "skill_version_registry": frozenset({
        "skill_name", "skill_version", "output_schema_digest",
        "request_schema_digest", "registered_at",
    }),
}

# audit_events is a legacy/external table (referenced by query_audit.sql) keyed
# by task_id rather than run_id. It is included so the contract test sees its
# columns, but it is intentionally NOT in the authoritative M3/M4 migration set
# above; we surface only aggregate counts from it.
SCHEMA_CONTRACT["audit_events"] = frozenset({
    "id", "task_id", "action", "ts", "actor", "detail",
})

# environment_identity is the ISOLATED_LIVE environment marker table
# (migrations/001_environment_identity.sql). It is a single-row table with a
# single column (environment_id); the reader role gets SELECT only.
SCHEMA_CONTRACT["environment_identity"] = frozenset({
    "environment_id", "created_at",
})


# ── Environment identity marker contract ────────────────────────────────────
# ENVIRONMENT_MARKER_CONTRACT is the authoritative description of the trusted
# environment identity marker table. It replaces the earlier scheme that
# overloaded controller_offsets.sync_token. The marker is now a dedicated
# single-row table (environment_identity) whose only column is environment_id.
#
# Contract:
#   - Table name: environment_identity
#   - Exactly one row, with column: environment_id (TEXT)
#   - Viewer (reader role) has SELECT only (no INSERT/UPDATE/DELETE/TRUNCATE)
#   - Probe query: SELECT environment_id FROM environment_identity LIMIT 1
#
# Failure modes (all fail-closed → ENVIRONMENT_ID_NOT_VERIFIED except a value
# mismatch → ENVIRONMENT_ID_MISMATCH):
#   - Missing table            → ENVIRONMENT_ID_NOT_VERIFIED
#   - 0 rows                   → ENVIRONMENT_ID_NOT_VERIFIED
#   - >1 rows                  → ENVIRONMENT_ID_NOT_VERIFIED
#   - value != expected        → ENVIRONMENT_ID_MISMATCH
ENVIRONMENT_MARKER_CONTRACT = {
    "table_name": "environment_identity",
    "probe_sql": "SELECT environment_id FROM environment_identity LIMIT 1",
    "expected_row_count": 1,
    "required_columns": frozenset({"environment_id"}),
    "viewer_privileges": frozenset({"SELECT"}),
    "revoked_privileges": frozenset(
        {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}
    ),
}


# ── SQL column reference extraction (for migration-contract tests) ──────────
# Regex that pulls ``table.column`` and bare ``column`` references out of a
# SELECT statement so the contract test can verify every referenced column
# exists in SCHEMA_CONTRACT. It deliberately stays conservative: it only looks
# at qualified (alias.column) references and the explicit column lists.
_QUALIFIED_COL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def _referenced_table_columns(sql: str) -> list[tuple[str, str]]:
    """Return ``(alias_or_table, column)`` pairs for qualified refs in ``sql``.

    Only qualified references (``rb.base_sha``, ``t.run_id``) are returned; the
    migration-contract test maps the alias back to a real table via the FROM
    clause. Bare column references are resolved by the test against the
    single-table SELECTs they appear in.
    """
    return _QUALIFIED_COL_RE.findall(sql or "")


# All SELECT statements this module issues, as ``(label, sql_template)`` pairs.
# The migration-contract test introspects this list (via the query methods) and
# verifies every referenced column is present in SCHEMA_CONTRACT. Keeping the
# templates here means the contract test does not have to re-derive SQL from a
# live DB.
def _all_select_templates() -> list[tuple[str, str]]:
    """Return the canonical SELECT templates this source issues (label, sql).

    Used by the migration-contract test to assert every column referenced is
    present in :data:`SCHEMA_CONTRACT`. The templates mirror the query methods
    one-to-one; if a query method changes, update its template here too.
    """
    return [
        ("task_run", _TASK_RUN_SQL),
        ("stage_runs", _STAGE_RUNS_SQL),
        ("stage_events", _STAGE_EVENTS_SQL),
        ("revision_bindings", _REVISION_BINDINGS_SQL),
        ("run_pr_bindings", _RUN_PR_BINDINGS_SQL),
        ("mcp_calls", _MCP_CALLS_SQL),
        ("rollback_runs", _ROLLBACK_RUNS_SQL),
        ("audit_events_total", _AUDIT_TOTAL_SQL),
        ("audit_events_by_action", _AUDIT_BY_ACTION_SQL),
        ("environment_marker", _ENV_MARKER_SQL),
    ]


# ── SQL templates (all parameterized; run_id is the only parameter) ────────
# Kept as module-level constants so the migration-contract test can parse them
# without instantiating a source. Each template lists its columns explicitly;
# never ``SELECT *``.
_TASK_RUN_SQL = (
    "SELECT run_id, repo, pr_number, branch, status, current_stage, "
    "attempt, verdict, last_error, created_at, updated_at, trace_id "
    "FROM task_runs WHERE run_id = %s"
)

# stage_runs is the AUTHORITATIVE per-stage execution source. Deterministic
# selection: ORDER BY stage ASC, attempt DESC, id ASC means that for a given
# stage the row with the highest attempt number (and, within that, the highest
# id) sorts first — so the latest attempt per stage is picked deterministically.
_STAGE_RUNS_SQL = (
    "SELECT id, run_id, stage, agent, attempt, status, started_at, "
    "completed_at, verdict, detail "
    "FROM stage_runs WHERE run_id = %s "
    "ORDER BY stage, attempt DESC, id"
)

# stage_events is used ONLY for event provenance/counts (not the stage list).
_STAGE_EVENTS_SQL = (
    "SELECT event_id, run_id, sender, event_type, stage, status, error, "
    "received_at, processed_at FROM stage_events WHERE run_id = %s "
    "ORDER BY received_at"
)

# revision_bindings holds the authoritative immutable base/head SHA for the run.
_REVISION_BINDINGS_SQL = (
    "SELECT rb.binding_id, rb.run_id, rb.repo, rb.pr_number, "
    "rb.base_sha, rb.head_sha, rb.recorded_at "
    "FROM revision_bindings rb WHERE rb.run_id = %s"
)

# run_pr_bindings holds the PR identity (repo/pr/branches). base_sha is NOT
# here (it lives in revision_bindings per the M4-F1 contract).
_RUN_PR_BINDINGS_SQL = (
    "SELECT repo, pr_number, fix_branch, base_branch, head_sha, recorded_at "
    "FROM run_pr_bindings WHERE run_id = %s"
)

_MCP_CALLS_SQL = (
    "SELECT request_id, correlation_id, phase, ts, caller_agent, tool, "
    "decision, reason_code, target_repo, target_branch, result_status, "
    "git_sha, error FROM mcp_calls WHERE run_id = %s ORDER BY ts"
)

_ROLLBACK_RUNS_SQL = (
    "SELECT rollback_id, parent_run_id, revert_run_id, reverted_merge_sha, "
    "repo, pr_number, status, fail_reason, revert_result_sha, "
    "reverify_verdict, created_at, updated_at "
    "FROM rollback_runs WHERE parent_run_id = %s OR revert_run_id = %s "
    "ORDER BY created_at"
)

_AUDIT_TOTAL_SQL = "SELECT count(*) FROM audit_events WHERE task_id = %s"

_AUDIT_BY_ACTION_SQL = (
    "SELECT action, count(*) FROM audit_events WHERE task_id = %s "
    "GROUP BY action ORDER BY action"
)

# Environment marker probe. The source looks for a trusted marker row in the
# dedicated environment_identity table. The probe is
#   SELECT environment_id FROM environment_identity LIMIT 1
# and the result is compared against the caller-supplied expected_environment_id.
# A missing table, 0 rows, or more than 1 row is treated as
# ENVIRONMENT_ID_NOT_VERIFIED (fail-closed). A value mismatch is
# ENVIRONMENT_ID_MISMATCH. See ENVIRONMENT_MARKER_CONTRACT for the contract.
_ENV_MARKER_SQL = ENVIRONMENT_MARKER_CONTRACT["probe_sql"]


# ── REQUIRED_QUERY_COLUMNS — precise per-table column mapping ────────────────
# REQUIRED_QUERY_COLUMNS maps each table this source reads to EXACTLY the set of
# columns actually referenced by the SELECT queries above (and the environment
# marker probe). This is intentionally narrower than SCHEMA_CONTRACT, which
# documents every column the migrations create. The runtime
# information_schema.columns probe checks ONLY these columns: a migration that
# adds a new column the source does not read must NOT fail the read (a column the
# query never references can be absent-or-present without affecting correctness).
#
# Source of truth: each entry is the literal column list from the corresponding
# _*_SQL template (and _ENV_MARKER_SQL). If a query's SELECT list changes, this
# entry must be updated to match.
REQUIRED_QUERY_COLUMNS: dict[str, frozenset[str]] = {
    # _TASK_RUN_SQL: run_id, repo, pr_number, branch, status, current_stage,
    #   attempt, verdict, last_error, created_at, updated_at, trace_id
    "task_runs": frozenset({
        "run_id", "repo", "pr_number", "branch", "status", "current_stage",
        "attempt", "verdict", "last_error", "created_at", "updated_at",
        "trace_id",
    }),
    # _STAGE_RUNS_SQL: id, run_id, stage, agent, attempt, status, started_at,
    #   completed_at, verdict, detail
    "stage_runs": frozenset({
        "id", "run_id", "stage", "agent", "attempt", "status", "started_at",
        "completed_at", "verdict", "detail",
    }),
    # _STAGE_EVENTS_SQL: event_id, run_id, sender, event_type, stage, status,
    #   error, received_at, processed_at
    "stage_events": frozenset({
        "event_id", "run_id", "sender", "event_type", "stage", "status",
        "error", "received_at", "processed_at",
    }),
    # _REVISION_BINDINGS_SQL (alias rb): binding_id, run_id, repo, pr_number,
    #   base_sha, head_sha, recorded_at
    "revision_bindings": frozenset({
        "binding_id", "run_id", "repo", "pr_number", "base_sha", "head_sha",
        "recorded_at",
    }),
    # _RUN_PR_BINDINGS_SQL: repo, pr_number, fix_branch, base_branch, head_sha,
    #   recorded_at
    "run_pr_bindings": frozenset({
        "repo", "pr_number", "fix_branch", "base_branch", "head_sha",
        "recorded_at",
    }),
    # _MCP_CALLS_SQL: request_id, correlation_id, phase, ts, caller_agent,
    #   tool, decision, reason_code, target_repo, target_branch,
    #   result_status, git_sha, error
    "mcp_calls": frozenset({
        "request_id", "correlation_id", "phase", "ts", "caller_agent", "tool",
        "decision", "reason_code", "target_repo", "target_branch",
        "result_status", "git_sha", "error",
    }),
    # _ROLLBACK_RUNS_SQL: rollback_id, parent_run_id, revert_run_id,
    #   reverted_merge_sha, repo, pr_number, status, fail_reason,
    #   revert_result_sha, reverify_verdict, created_at, updated_at
    "rollback_runs": frozenset({
        "rollback_id", "parent_run_id", "revert_run_id", "reverted_merge_sha",
        "repo", "pr_number", "status", "fail_reason", "revert_result_sha",
        "reverify_verdict", "created_at", "updated_at",
    }),
    # _AUDIT_TOTAL_SQL + _AUDIT_BY_ACTION_SQL: the aggregate queries reference
    #   only task_id (WHERE filter) and action (SELECT/GROUP BY). count(*) does
    #   not name a column.
    "audit_events": frozenset({"task_id", "action"}),
    # _ENV_MARKER_SQL: environment_id
    "environment_identity": frozenset({"environment_id"}),
}


# Tables whose per-table privileges must be probed at read time (the connected
# reader role must have SELECT and must NOT have INSERT/UPDATE/DELETE/TRUNCATE
# on any of them). This is the exhaustive list of tables the source actually
# queries; it is checked in addition to the pg_roles privileged-attribute probe.
PRIVILEGE_CHECKED_TABLES = (
    "task_runs",
    "stage_runs",
    "stage_events",
    "revision_bindings",
    "run_pr_bindings",
    "mcp_calls",
    "rollback_runs",
    "audit_events",
    "environment_identity",
)


class PostgresSnapshotSource(SnapshotSource):
    """Read-only PostgreSQL snapshot source producing an ISOLATED_LIVE bundle.

    Parameters
    ----------
    dsn:
        psycopg2 connection string. Treated as a secret — never logged,
        never placed in ``repr``/``str``/exceptions.
    run_id:
        The task_runs.run_id to read. Must match ``^[a-zA-Z0-9_-]+$``.
    expected_database:
        Required value of ``current_database()``; anything else is rejected.
    expected_role:
        Required value of ``current_user``; anything else is rejected. This is
        a mandatory parameter with no default: the canonical viewer role for
        ISOLATED_LIVE is the fixed ``mergepilot_reader`` (the migration grants
        SELECT to exactly that role). The source verifies
        ``current_user == expected_role`` exactly.
    expected_environment_id:
        Required value of the trusted environment marker
        (``environment_identity.environment_id``, single row). MUST be a
        non-empty string (constructor and preflight fail-closed otherwise).
    expected_server_addresses:
        Required list of allowed ``inet_server_addr()`` values (e.g.
        ``["127.0.0.1"]``). The connected server's address must be in this
        set. MUST be a non-empty list at construction time.
    expected_server_port:
        Required value of ``inet_server_port()``. MUST be a non-zero int.
    expected_application_name:
        Required value of ``current_setting('application_name')``. MUST be a
        non-empty string.
    query_timeout_seconds:
        Per-session ``statement_timeout`` (and ``lock_timeout`` /
        ``idle_in_transaction_session_timeout``), in seconds. Must be a
        finite number in the range ``[1, 60]``; 0, negative, NaN, Infinity,
        and None are rejected with CONFIG_INVALID. Default 10.
    """

    kind = "POSTGRES_ISOLATED"

    # Bounds for query_timeout_seconds. Must be a finite number in [1, 60].
    _TIMEOUT_MIN_SECONDS = 1
    _TIMEOUT_MAX_SECONDS = 60

    @property
    def read_only(self) -> bool:
        # This source only ever issues SELECT inside a READ ONLY transaction.
        return True

    def __init__(
        self,
        dsn: str,
        run_id: str,
        expected_database: str,
        expected_role: str,
        expected_environment_id: str | None = None,
        expected_server_addresses: list[str] | None = None,
        expected_server_port: int | None = None,
        expected_application_name: str | None = None,
        query_timeout_seconds: float = 10.0,
    ):
        # Store the DSN in a single private attribute. __repr__/__str__ are
        # overridden below to ensure it can never leak.
        self._dsn = dsn
        self._run_id = run_id

        # ── expected_database must be a non-empty string ───────────────────
        # The canonical viewer role for ISOLATED_LIVE is the fixed
        # ``mergepilot_reader`` (the migration grants SELECT to exactly that
        # role). expected_database is mandatory; a None/empty value is a
        # configuration error (CONFIG_INVALID), not a soft skip.
        if not isinstance(expected_database, str) or not expected_database.strip():
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_database must be a non-empty "
                "string (the target database name is mandatory)",
                code="CONFIG_INVALID",
            )
        self._expected_database = expected_database

        # ── expected_role must be a non-empty string ───────────────────────
        # The canonical viewer role for ISOLATED_LIVE is the fixed
        # ``mergepilot_reader`` (the migration grants SELECT to exactly that
        # role). expected_role is mandatory and is verified against
        # ``current_user`` at read time; a None/empty value is a configuration
        # error (CONFIG_INVALID), not a soft skip. The source verifies
        # ``current_user == expected_role`` exactly (no prefix/wildcard match).
        # Only the canonical CANONICAL_VIEWER_ROLE ("mergepilot_reader") is
        # accepted; any other role name is rejected with CONFIG_INVALID.
        if not isinstance(expected_role, str) or not expected_role.strip():
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_role must be a non-empty "
                "string (the canonical viewer role is %s; the "
                "role is mandatory and must match current_user exactly)" % CANONICAL_VIEWER_ROLE,
                code="CONFIG_INVALID",
            )
        # Strict equality: reject any whitespace-padded variant
        if expected_role != CANONICAL_VIEWER_ROLE:
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_role must be exactly '%s'; got '%s'" % (
                    CANONICAL_VIEWER_ROLE, expected_role),
                code="CONFIG_INVALID",
            )
        self._expected_role = expected_role

        # ── expected_environment_id must be a non-empty string ─────────────
        # The environment marker is mandatory: the source never guesses the
        # environment identity from hostname. A None/empty/whitespace-only value
        # is a configuration error (CONFIG_INVALID), not a soft skip.
        if not isinstance(expected_environment_id, str) or not expected_environment_id.strip():
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_environment_id must be a non-empty "
                "string (environment marker is mandatory; never guessed)",
                code="CONFIG_INVALID",
            )
        self._expected_environment_id = expected_environment_id

        # ── Server identity hardening ──────────────────────────────────────
        # expected_server_addresses: a non-empty list of allowed addresses.
        # Each entry is CANONICALIZED through the shared one-contract
        # canonicalizer (retry v3 Fix 1): a bare ``172.18.0.2`` and a
        # single-host ``172.18.0.2/32`` are the SAME address — PostgreSQL's
        # ``inet_server_addr()`` text form may carry the netmask suffix
        # depending on build. Hostnames/aliases, IPv6, non-/32 CIDR and
        # malformed values are rejected here (fail-closed CONFIG_INVALID),
        # BEFORE any connection is opened.
        if not isinstance(expected_server_addresses, list) or not expected_server_addresses:
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_server_addresses must be a non-empty "
                "list (e.g. ['127.0.0.1'])",
                code="CONFIG_INVALID",
            )
        try:
            self._expected_server_addresses = \
                canonicalize_server_address_list(expected_server_addresses)
        except ValueError as exc:
            raise ConfigInvalidError(str(exc), code="CONFIG_INVALID") \
                from None

        # expected_server_port: a non-zero int.
        if not isinstance(expected_server_port, int) or isinstance(
            expected_server_port, bool
        ) or expected_server_port == 0:
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_server_port must be a non-zero int",
                code="CONFIG_INVALID",
            )
        self._expected_server_port = expected_server_port

        # expected_application_name: a non-empty string.
        if not isinstance(expected_application_name, str) or not expected_application_name:
            raise ConfigInvalidError(
                "CONFIG_INVALID: expected_application_name must be a non-empty "
                "string",
                code="CONFIG_INVALID",
            )
        self._expected_application_name = expected_application_name

        # ── Timeout bounds ─────────────────────────────────────────────────
        # query_timeout_seconds must be a finite number in [1, 60]. Reject 0,
        # negative, NaN, Infinity, and None with CONFIG_INVALID.
        self._timeout_ms = self._validate_timeout(query_timeout_seconds)

        # Validate run_id shape eagerly so a bad value never reaches a query.
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.match(run_id):
            # Do NOT include the raw value verbatim if it is huge / weird; cap it.
            snippet = self._safe_snippet(run_id)
            raise RunIdError(
                f"RUN_ID_INVALID: run_id must match ^[a-zA-Z0-9_-]+$ (got {snippet})",
                code="RUN_ID_INVALID",
            )

    # ── Secret hygiene ─────────────────────────────────────────────────────
    @staticmethod
    def _safe_snippet(value) -> str:
        """Return a short, safe representation of a value for error messages."""
        try:
            text = str(value)
        except Exception:  # pragma: no cover - defensive
            return "<unprintable>"
        if len(text) > 40:
            text = text[:40] + "..."
        return repr(text)

    @classmethod
    def _validate_timeout(cls, query_timeout_seconds) -> int:
        """Validate query_timeout_seconds and return the timeout in milliseconds.

        Must be a finite number in ``[1, 60]``. Rejects None, bool, strings
        (even numeric strings), non-numeric values, 0, negatives, NaN, and
        Infinity with CONFIG_INVALID. Returns the integer milliseconds suitable
        for ``SET LOCAL statement_timeout``.
        """
        # bool is a subclass of int; reject it explicitly so True/False are
        # never silently accepted as 1/0.
        if isinstance(query_timeout_seconds, bool) or query_timeout_seconds is None:
            raise ConfigInvalidError(
                "CONFIG_INVALID: query_timeout_seconds must be a finite "
                f"number in [1, 60] (got {query_timeout_seconds!r})",
                code="CONFIG_INVALID",
            )
        # Reject strings explicitly: even a numeric string like "10" is not an
        # accepted timeout (the contract requires an actual number). This also
        # avoids the surprising float("10") == 10.0 acceptance path.
        if isinstance(query_timeout_seconds, str):
            raise ConfigInvalidError(
                "CONFIG_INVALID: query_timeout_seconds must be a finite "
                f"number in [1, 60] (got {query_timeout_seconds!r})",
                code="CONFIG_INVALID",
            )
        # Only accept int/float at this point.
        if not isinstance(query_timeout_seconds, (int, float)):
            raise ConfigInvalidError(
                "CONFIG_INVALID: query_timeout_seconds must be a finite "
                f"number in [1, 60] (got {query_timeout_seconds!r})",
                code="CONFIG_INVALID",
            )
        timeout = float(query_timeout_seconds)
        # Reject NaN and Infinity (math.isfinite handles both).
        if math.isnan(timeout) or math.isinf(timeout):
            raise ConfigInvalidError(
                "CONFIG_INVALID: query_timeout_seconds must be a finite "
                f"number in [1, 60] (got {query_timeout_seconds!r})",
                code="CONFIG_INVALID",
            )
        if timeout < cls._TIMEOUT_MIN_SECONDS or timeout > cls._TIMEOUT_MAX_SECONDS:
            raise ConfigInvalidError(
                "CONFIG_INVALID: query_timeout_seconds must be in [1, 60] "
                f"(got {query_timeout_seconds!r})",
                code="CONFIG_INVALID",
            )
        return int(timeout * 1000)

    def __repr__(self) -> str:
        # Never expose the DSN. Expose only the public, non-secret identity.
        return (
            f"PostgresSnapshotSource(run_id={self._run_id!r}, "
            f"expected_database={self._expected_database!r}, "
            f"expected_role={self._expected_role!r}, kind={self.kind!r})"
        )

    __str__ = __repr__

    # ── Public interface ───────────────────────────────────────────────────
    def read_snapshot(self) -> bytes:
        """Read a single run from PostgreSQL and return a DemoBundle as JSON bytes.

        Steps (see module docstring for the rationale):

        1. Lazily import psycopg2 (so REPLAY/FILE mode never needs it).
        2. Connect.
        3. IMMEDIATELY open an explicit READ ONLY transaction (BEGIN ... REPEATABLE
           READ READ ONLY) as the first statement. All identity/catalog checks
           happen INSIDE this transaction.
        4. SET LOCAL timeouts.
        5. Verify identity inside the txn (see :meth:`_verify_identity`):
           a. current_database(), current_user, read-only flags
           b. server identity: inet_server_addr() (must be in the configured
              allowlist; NULL → WRONG_SERVER), inet_server_port(),
              application_name, server_version_num
           c. schema / search_path (must be public)
           d. required-table catalog presence
           e. reader role hardening: pg_roles privileged-attribute probe +
              table-level write-privilege probe on task_runs
           f. schema compatibility runtime catalog probe (column-level)
           g. environment marker: environment_identity.environment_id (exactly
              one row, value must match expected_environment_id)
        6. SELECT task_runs / stage_runs / stage_events / revision_bindings /
           run_pr_bindings / mcp_calls / rollback_runs / audit_events counts
           (all parameterized).
        7. Assemble the DemoBundle with demo_mode="ISOLATED_LIVE".
        8. Scan serialized bundle bytes for secrets; raise if any leak detected.
        9. Compute bundle_sha256.
        10. ROLLBACK + close.
        11. Return JSON bytes.

        On ANY error the transaction is rolled back, the connection is closed,
        and a sanitized :class:`PostgresSourceError` (with a stable ``.code``)
        is raised (no DSN in the message).
        """
        # 1. Lazy import. Importing here means REPLAY / FILE deployments do
        #    not require psycopg2 to be installed at all.
        try:
            import psycopg2
        except ImportError as e:
            raise PostgresSourceError(
                "PSYCOPG2_MISSING: psycopg2 is required for the PostgreSQL "
                "snapshot source but is not installed",
                code="PSYCOPG2_MISSING",
            ) from None

        conn = None
        try:
            # 2. Connect. Any connect-time failure is sanitized below.
            conn = psycopg2.connect(self._dsn)

            # 3/4. Open an explicit READ ONLY transaction with tight timeouts AS
            #      THE FIRST statement after connecting. Identity checks run
            #      inside this transaction so a snapshot is established before
            #      any probe.
            with conn.cursor() as cur:
                cur.execute(
                    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                self._set_local_timeouts(cur)

                # 5. Identity verification inside the read-only transaction.
                self._verify_identity(cur)

                # 6. Read each table. All queries use %s placeholders; run_id
                #    is the only parameter and it was validated in __init__.
                task_run = self._query_task_run(cur)
                # RUN_NOT_FOUND fail-closed: a missing run must NOT produce a
                # valid bundle with final_status=UNKNOWN. Raise immediately.
                if task_run is None:
                    raise RunNotFoundError(
                        f"RUN_NOT_FOUND: no task_runs row for run_id "
                        f"{self._safe_snippet(self._run_id)}",
                        code="RUN_NOT_FOUND",
                    )
                stage_runs = self._query_stage_runs(cur)
                stage_events = self._query_stage_events(cur)
                revision = self._query_revision(cur)
                gateway_calls = self._query_mcp_calls(cur)
                rollback_events = self._query_rollback_runs(cur)
                audit_summary = self._query_audit_events(cur)

            # 7/8. Assemble + digest outside the cursor block (no DB needed).
            bundle = self._assemble_bundle(
                task_run=task_run,
                stage_runs=stage_runs,
                stage_events=stage_events,
                revision=revision,
                gateway_calls=gateway_calls,
                rollback_events=rollback_events,
                audit_summary=audit_summary,
            )

            # 9. End the read-only transaction by rolling it back (we only
            #    ever read; ROLLBACK releases the snapshot cleanly and leaves
            #    no idle-in-transaction residue).
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - rollback best-effort
                pass

            # 10. Serialize. Sort keys for deterministic bytes (matches the
            #     canonical serialization used by compute_bundle_sha256).
            return json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode(
                "utf-8"
            )

        except PostgresSourceError:
            # Already sanitized. Just ensure cleanup happens.
            if conn is not None:
                self._safe_rollback(conn)
                self._safe_close(conn)
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize everything else
            # NEVER include the raw exception message from psycopg2/libpq in the
            # re-raised error: those messages can echo the connection string
            # (DSN) on connect failures, and even after regex redaction a
            # fragment (e.g. the URI scheme "postgresql://") could survive. The
            # safe, stable surface is the error CODE plus the exception TYPE
            # name only. The original exception is NOT chained (``from None``)
            # so the raw psycopg2/libpq message and traceback never reach the
            # public exception's ``__cause__``/``__context__`` and cannot be
            # surfaced by ``traceback.format_exception`` on the raised error.
            if conn is not None:
                self._safe_rollback(conn)
                self._safe_close(conn)
            raise PostgresQueryError(
                f"POSTGRES_READ_FAILED: {type(exc).__name__} "
                f"(see server logs; raw detail suppressed to protect the DSN)",
                code="POSTGRES_READ_FAILED",
            ) from None
        finally:
            # Defensive belt-and-suspenders close. If we returned normally the
            # connection was already closed in the success path's rollback,
            # but a second close() is a harmless no-op on a closed connection
            # and guarantees no leaked idle session on any code path.
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # pragma: no cover - close best-effort
                    pass

    # ── Identity verification ──────────────────────────────────────────────
    def _verify_identity(self, cur) -> None:
        """Reject wrong database/role/server, a non-read-only session, a schema
        incompatible with the contract, or an unverified environment marker.

        Every check here runs INSIDE the read-only transaction opened by
        :meth:`read_snapshot`, so all probes see the same snapshot.

        Order of checks:
          1. Core identity (database, role, read-only flags)
          2. Server identity (address/port/application_name/version)
          3. Schema / search_path
          4. Required-table catalog presence
          5. Reader role hardening (pg_roles privileged-attribute probe +
             per-table privilege probe over ALL nine queried tables:
             SELECT required, INSERT/UPDATE/DELETE/TRUNCATE forbidden)
          6. Schema compatibility runtime catalog probe (column-level, against
             REQUIRED_QUERY_COLUMNS)
          7. Environment marker (trusted identity; never guessed)
        """
        # ── 1. Core identity (database, role, read-only flags) ─────────────
        cur.execute(
            "SELECT current_database(), current_user, "
            "current_setting('transaction_read_only')::boolean, "
            "current_setting('default_transaction_read_only')::boolean"
        )
        row = cur.fetchone()
        if row is None:
            raise IdentityCheckError(
                "IDENTITY_CHECK_FAILED: identity probe returned no row",
                code="POSTGRES_READ_FAILED",
            )
        database, user, tx_read_only, default_read_only = row

        if database != self._expected_database:
            raise IdentityCheckError(
                "WRONG_DATABASE: connected database does not match expected "
                f"(got {self._safe_snippet(database)})",
                code="WRONG_DATABASE",
            )
        if user != self._expected_role:
            raise IdentityCheckError(
                "WRONG_ROLE: connected role does not match expected "
                f"(got {self._safe_snippet(user)})",
                code="WRONG_ROLE",
            )
        # Fail-closed: the session must report read-only at BOTH the current
        # transaction and the default level. A writable session is refused
        # even if our explicit BEGIN ... READ ONLY would also constrain it —
        # defense in depth.
        if tx_read_only is not True or default_read_only is not True:
            raise IdentityCheckError(
                "NOT_READ_ONLY: session is writable "
                "(transaction_read_only or default_transaction_read_only is off)",
                code="NOT_READ_ONLY",
            )

        # ── 2. Server identity (address/port/application_name/version) ─────
        # inet_server_addr()/inet_server_port() report the actual server the
        # connection landed on. We do NOT guess from hostname: the operator
        # supplies expected_server_addresses / expected_server_port /
        # expected_application_name, and a mismatch is fail-closed WRONG_SERVER.
        # Retry v3 Fix 1: the SQL measures ``host(inet_server_addr())`` — the
        # bare host text — instead of casting the inet value to text (whose
        # form may carry a ``/32`` netmask suffix depending on build). The
        # measured value is ADDITIONALLY canonicalized in Python (defensive
        # twin of the same shared contract), so both sides of the comparison
        # are normalized: ``172.18.0.2`` and ``172.18.0.2/32`` are the same.
        cur.execute(
            "SELECT host(inet_server_addr()), inet_server_port(), "
            "current_setting('application_name'), "
            "current_setting('server_version_num')::int"
        )
        srv = cur.fetchone()
        if srv is None:
            raise IdentityCheckError(
                "WRONG_SERVER: server identity probe returned no row",
                code="WRONG_SERVER",
            )
        server_addr, server_port, application_name, server_version_num = srv

        # NULL inet_server_addr (e.g. a Unix socket) → WRONG_SERVER fail-closed.
        # We never accept an unidentifiable server address.
        if not isinstance(server_addr, str) or not server_addr:
            raise IdentityCheckError(
                "WRONG_SERVER: inet_server_addr() returned NULL "
                "(Unix socket / unidentifiable server); a concrete IP address "
                "matching the configured allowlist is required",
                code="WRONG_SERVER",
            )
        try:
            server_addr_canonical = canonicalize_server_address(server_addr)
        except ValueError:
            raise IdentityCheckError(
                "WRONG_SERVER: measured server address %r is not a "
                "canonicalizable IPv4 host" % server_addr[:40],
                code="WRONG_SERVER",
            ) from None
        if server_addr_canonical not in self._expected_server_addresses:
            raise IdentityCheckError(
                "WRONG_SERVER: inet_server_addr() not in expected "
                f"allowlist (got {self._safe_snippet(server_addr)})",
                code="WRONG_SERVER",
            )
        if server_port != self._expected_server_port:
            raise IdentityCheckError(
                "WRONG_SERVER: inet_server_port() mismatch "
                f"(got {self._safe_snippet(server_port)})",
                code="WRONG_SERVER",
            )
        if application_name != self._expected_application_name:
            raise IdentityCheckError(
                "WRONG_SERVER: application_name mismatch "
                f"(got {self._safe_snippet(application_name)})",
                code="WRONG_SERVER",
            )
        # server_version_num is an integer (e.g. 160001 for PG 16.1). We accept
        # the supported major range (12..17) — anything older/newer-than-known
        # is treated as a schema-compatibility risk.
        if not isinstance(server_version_num, int) or not (120000 <= server_version_num < 180000):
            raise IdentityCheckError(
                "WRONG_SERVER: unsupported server_version_num "
                f"(got {self._safe_snippet(server_version_num)}; expected 12.x-17.x)",
                code="WRONG_SERVER",
            )

        # ── 3. Schema / search_path ────────────────────────────────────────
        # The read-only viewer must read from the public schema (where the M3/M4
        # migration tables live). A non-public search_path is a sign of a
        # misconfigured or hostile role.
        cur.execute("SELECT current_schema(), current_setting('search_path')")
        sch = cur.fetchone()
        if sch is None:
            raise IdentityCheckError(
                "SCHEMA_INCOMPATIBLE: schema probe returned no row",
                code="SCHEMA_INCOMPATIBLE",
            )
        current_schema_name, search_path = sch
        if not isinstance(current_schema_name, str) or current_schema_name.lower() != "public":
            raise IdentityCheckError(
                "SCHEMA_INCOMPATIBLE: current_schema must be 'public' "
                f"(got {self._safe_snippet(current_schema_name)})",
                code="SCHEMA_INCOMPATIBLE",
            )

        # ── 4. Required-table catalog presence ─────────────────────────────
        # Fail-closed if any table the source reads is missing — a partial
        # schema means we cannot assemble a correct bundle. This is the
        # coarse table-existence check; the column-level probe follows.
        required_tables = (
            "task_runs", "stage_runs", "stage_events", "revision_bindings",
            "run_pr_bindings", "mcp_calls", "rollback_runs", "audit_events",
            "environment_identity",
        )
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        present = {row[0] for row in cur.fetchall()}
        missing = [t for t in required_tables if t not in present]
        if missing:
            raise IdentityCheckError(
                "SCHEMA_INCOMPATIBLE: required tables missing from public schema: "
                f"{sorted(missing)}",
                code="SCHEMA_INCOMPATIBLE",
            )

        # ── 5. Reader role hardening ───────────────────────────────────────
        # The connected role must NOT be a privileged role. Fail-closed if any
        # of rolsuper/rolcreatedb/rolcreaterole/rolreplication/rolbypassrls is
        # true — the viewer must be an unprivileged reader.
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        role_row = cur.fetchone()
        if role_row is None:
            raise IdentityCheckError(
                "WRONG_ROLE: could not probe current role privileges in pg_roles",
                code="WRONG_ROLE",
            )
        # Any True privileged attribute → WRONG_ROLE (role has privileged access).
        for attr_val in role_row:
            if attr_val is True:
                raise IdentityCheckError(
                    "WRONG_ROLE: connected role has a privileged attribute "
                    "(rolsuper/rolcreatedb/rolcreaterole/rolreplication/"
                    "rolbypassrls); only an unprivileged reader is allowed",
                    code="WRONG_ROLE",
                )

        # Table-level privilege probe: the viewer must have SELECT and must NOT
        # have INSERT/UPDATE/DELETE/TRUNCATE on ANY table the source queries.
        # We probe every table in PRIVILEGE_CHECKED_TABLES (the exhaustive list
        # of tables this source reads), each via a parameterized query so the
        # table name is never interpolated into the SQL text.
        for table in PRIVILEGE_CHECKED_TABLES:
            # SELECT must be present (True); the viewer must be able to read.
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT')",
                (table,),
            )
            sel_row = cur.fetchone()
            if sel_row is None or sel_row[0] is not True:
                raise IdentityCheckError(
                    "WRONG_ROLE: connected role lacks SELECT on "
                    f"{table!r}; the read-only viewer must be able to read "
                    "every queried table",
                    code="WRONG_ROLE",
                )
            # INSERT/UPDATE/DELETE/TRUNCATE must all be absent (False). Any True
            # → the role has write access and is too privileged.
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT'), "
                "has_table_privilege(current_user, %s, 'UPDATE'), "
                "has_table_privilege(current_user, %s, 'DELETE'), "
                "has_table_privilege(current_user, %s, 'TRUNCATE')",
                (table, table, table, table),
            )
            priv_row = cur.fetchone()
            if priv_row is None:
                raise IdentityCheckError(
                    "WRONG_ROLE: could not probe table-level privileges on "
                    f"{table!r}",
                    code="WRONG_ROLE",
                )
            for priv_val in priv_row:
                if priv_val is True:
                    raise IdentityCheckError(
                        "WRONG_ROLE: connected role has write privileges on "
                        f"{table!r} (INSERT/UPDATE/DELETE/TRUNCATE); only "
                        "SELECT is allowed for the read-only viewer",
                        code="WRONG_ROLE",
                    )

        # ── 6. Schema compatibility — runtime catalog probe ────────────────
        # For each required table, compare the actual columns (from
        # information_schema.columns) against the required_columns listed in
        # SCHEMA_CONTRACT. Missing columns → SCHEMA_INCOMPATIBLE with the
        # missing column names listed in the detail.
        self._verify_schema_columns(cur)

        # ── 7. Environment marker (trusted identity; never guessed) ────────
        # Probe the environment_identity table. The contract requires exactly
        # one row whose environment_id matches the caller-supplied
        # expected_environment_id. Missing table (handled by step 4), 0 rows,
        # or >1 rows → ENVIRONMENT_ID_NOT_VERIFIED. A value mismatch →
        # ENVIRONMENT_ID_MISMATCH. We never guess the environment from hostname.
        cur.execute(_ENV_MARKER_SQL)
        marker_row = cur.fetchone()
        # A 0-row result yields marker_row None here (fetchone on an empty set).
        if marker_row is None:
            raise IdentityCheckError(
                "ENVIRONMENT_ID_NOT_VERIFIED: no environment_identity row "
                "(0 rows); refusing startup without a verified environment "
                "identity",
                code="ENVIRONMENT_ID_NOT_VERIFIED",
            )
        marker_value = marker_row[0]
        # Count rows to detect >1 (the unique index should prevent it, but
        # fail-closed regardless). LIMIT 1 above only returns one row, so we
        # issue a count to verify the table is single-row.
        cur.execute("SELECT count(*) FROM environment_identity")
        count_row = cur.fetchone()
        row_count = int(count_row[0]) if count_row and count_row[0] is not None else 0
        if row_count == 0:
            raise IdentityCheckError(
                "ENVIRONMENT_ID_NOT_VERIFIED: environment_identity has 0 rows; "
                "refusing startup without a verified environment identity",
                code="ENVIRONMENT_ID_NOT_VERIFIED",
            )
        if row_count > 1:
            raise IdentityCheckError(
                "ENVIRONMENT_ID_NOT_VERIFIED: environment_identity has >1 rows "
                f"({row_count}); a single marker row is required",
                code="ENVIRONMENT_ID_NOT_VERIFIED",
            )
        if marker_value != self._expected_environment_id:
            raise IdentityCheckError(
                "ENVIRONMENT_ID_MISMATCH: environment_identity.environment_id "
                "does not match expected",
                code="ENVIRONMENT_ID_MISMATCH",
            )

    def _verify_schema_columns(self, cur) -> None:
        """Runtime catalog probe: every required column must exist.

        For each table in :data:`REQUIRED_QUERY_COLUMNS`, SELECT column_name
        from information_schema.columns and compare against the precise set of
        columns the source's queries actually reference. Missing columns →
        SCHEMA_INCOMPATIBLE with the missing column names listed in the detail.

        Only the columns a query references are checked — a column the source
        never reads may be absent without failing the read. This keeps the probe
        decoupled from the full migration column set (SCHEMA_CONTRACT) and
        coupled only to what the SELECTs in this module actually need.
        """
        for table in REQUIRED_QUERY_COLUMNS:
            required_cols = REQUIRED_QUERY_COLUMNS[table]
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            )
            actual = {str(row[0]) for row in cur.fetchall()}
            missing = sorted(required_cols - actual)
            if missing:
                raise IdentityCheckError(
                    f"SCHEMA_INCOMPATIBLE: table {table!r} is missing required "
                    f"columns: {missing}",
                    code="SCHEMA_INCOMPATIBLE",
                )

    # ── Transaction setup ──────────────────────────────────────────────────
    def _set_local_timeouts(self, cur) -> None:
        """SET LOCAL statement_timeout / lock_timeout / idle_in_transaction.

        ``SET LOCAL`` only affects the current transaction, which is already
        open (we just BEGIN'd). The values are integers we control, formatted
        via %-formatting into a fixed SQL shape with no user interpolation —
        they are not subject to injection (they are bounded ints derived from
        the constructor's query_timeout_seconds).
        """
        ms = self._timeout_ms
        cur.execute("SET LOCAL statement_timeout = %s" % ms)
        cur.execute("SET LOCAL lock_timeout = %s" % ms)
        cur.execute(
            "SET LOCAL idle_in_transaction_session_timeout = %s" % ms
        )

    # ── Per-table read queries (all parameterized) ─────────────────────────
    def _query_task_run(self, cur) -> dict | None:
        """SELECT the single task_runs row for self._run_id."""
        cur.execute(_TASK_RUN_SQL, (self._run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "run_id", "repo", "pr_number", "branch", "status", "current_stage",
            "attempt", "verdict", "last_error", "created_at", "updated_at",
            "trace_id",
        ]
        return dict(zip(cols, row))

    def _query_stage_runs(self, cur) -> list[dict]:
        """SELECT stage_runs (authoritative per-stage execution) for the run.

        Deterministic selection: rows arrive ordered by ``stage, attempt DESC,
        id``. The caller (:meth:`_assemble_bundle`) picks the first row per
        stage (i.e. the latest attempt) when building workflow_stages.
        """
        cur.execute(_STAGE_RUNS_SQL, (self._run_id,))
        cols = [
            "id", "run_id", "stage", "agent", "attempt", "status",
            "started_at", "completed_at", "verdict", "detail",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _query_stage_events(self, cur) -> list[dict]:
        """SELECT stage_events for the run (event provenance/counts only)."""
        cur.execute(_STAGE_EVENTS_SQL, (self._run_id,))
        cols = [
            "event_id", "run_id", "sender", "event_type", "stage", "status",
            "error", "received_at", "processed_at",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _query_revision(self, cur) -> dict | None:
        """SELECT revision_bindings + run_pr_bindings for revision info.

        revision_bindings holds the immutable base/head SHA for the run (the
        authoritative revision cut). run_pr_bindings holds the PR identity
        (repo/pr_number/branches). base_sha is NOT in run_pr_bindings per the
        M4-F1 contract — it lives in revision_bindings.
        """
        cur.execute(_REVISION_BINDINGS_SQL, (self._run_id,))
        cols = [
            "binding_id", "run_id", "repo", "pr_number", "base_sha",
            "head_sha", "recorded_at",
        ]
        row = cur.fetchone()
        revision = dict(zip(cols, row)) if row else None

        # PR branch identity from run_pr_bindings (fix_branch / base_branch).
        cur.execute(_RUN_PR_BINDINGS_SQL, (self._run_id,))
        pr_cols = ["repo", "pr_number", "fix_branch", "base_branch",
                   "head_sha", "recorded_at"]
        pr_row = cur.fetchone()
        pr_binding = dict(zip(pr_cols, pr_row)) if pr_row else None

        if revision is not None:
            revision["pr_binding"] = pr_binding
        return revision

    def _query_mcp_calls(self, cur) -> list[dict]:
        """SELECT mcp_calls (gateway audit) for the run."""
        cur.execute(_MCP_CALLS_SQL, (self._run_id,))
        cols = [
            "request_id", "correlation_id", "phase", "ts", "caller_agent",
            "tool", "decision", "reason_code", "target_repo", "target_branch",
            "result_status", "git_sha", "error",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _query_rollback_runs(self, cur) -> list[dict]:
        """SELECT rollback_runs events referencing this run (as parent or revert)."""
        cur.execute(
            _ROLLBACK_RUNS_SQL, (self._run_id, self._run_id),
        )
        cols = [
            "rollback_id", "parent_run_id", "revert_run_id", "reverted_merge_sha",
            "repo", "pr_number", "status", "fail_reason", "revert_result_sha",
            "reverify_verdict", "created_at", "updated_at",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _query_audit_events(self, cur) -> dict:
        """SELECT an audit_events summary for the run.

        audit_events is keyed by task_id (not run_id). We treat run_id as the
        task_id alias for this read-only viewer. Only aggregate counts are
        returned — full event bodies are NOT part of the bundle.
        """
        cur.execute(_AUDIT_TOTAL_SQL, (self._run_id,))
        total_row = cur.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        cur.execute(_AUDIT_BY_ACTION_SQL, (self._run_id,))
        by_action = {str(action): int(cnt) for action, cnt in cur.fetchall()}

        return {"total": total, "by_action": by_action}

    # ── Bundle assembly ────────────────────────────────────────────────────
    def _assemble_bundle(
        self,
        *,
        task_run: dict | None,
        stage_runs: list[dict],
        stage_events: list[dict],
        revision: dict | None,
        gateway_calls: list[dict],
        rollback_events: list[dict],
        audit_summary: dict,
    ) -> dict:
        """Assemble a DemoBundle dict with demo_mode='ISOLATED_LIVE'.

        Maps the read DB rows onto the mergepilot.demo-bundle.v1 schema. Fields
        that this source cannot measure from the DB are filled with explicit
        NOT_MEASURED / empty markers (see the design doc for the matrix).
        """
        run_id = self._run_id
        tr = task_run or {}

        # ── PR + run identity (from revision_bindings / run_pr_bindings) ────
        repo = tr.get("repo") or (revision or {}).get("repo") or ""
        pr_number = tr.get("pr_number") or (revision or {}).get("pr_number")

        # Provenance: map base_sha/head_sha from revision_bindings (authoritative).
        # Never output an empty string for source_commit/verification_commit: if
        # the SHA is genuinely missing, use null and provenance_status=NOT_AVAILABLE.
        rev_base_sha = (revision or {}).get("base_sha")
        rev_head_sha = (revision or {}).get("head_sha")
        pr_head_sha = rev_head_sha or tr.get("head_sha")
        base_sha = rev_base_sha or ""
        head_sha = pr_head_sha or ""

        # ── Provenance fix ──────────────────────────────────────────────────
        # source_commit: the target revision's head_sha from revision_bindings
        #   (the revision the run was cut against). This is the authoritative
        #   "source" commit.
        # verification_commit: ALWAYS null for ISOLATED_LIVE. The read-only DB
        #   viewer does NOT perform or record a verification build/test run, so
        #   it cannot truthfully report a verification commit. Earlier versions
        #   incorrectly copied head_sha here; that conflated the target
        #   revision with a verified artifact.
        # verification_commit_status: "NOT_AVAILABLE" makes the absence explicit
        #   so consumers cannot mistake null for "not yet populated".
        # provenance_status: VERIFIED_FROM_REVISION_BINDINGS when a head_sha is
        #   available; NOT_AVAILABLE otherwise.
        has_revision_sha = bool(rev_head_sha)
        source_commit = rev_head_sha if has_revision_sha else None
        verification_commit = None
        verification_commit_status = "NOT_AVAILABLE"
        if has_revision_sha:
            provenance_status = "VERIFIED_FROM_REVISION_BINDINGS"
        else:
            provenance_status = "NOT_AVAILABLE"

        pr = {
            "number": pr_number if pr_number is not None else 0,
            "title": "",  # NOT_MEASURED: title is not stored in the audit DB
            "base_sha": base_sha,
            "head_sha": head_sha,
        }

        trace_id = tr.get("trace_id") or ""
        run = {
            "run_id": run_id,
            "trace_id": trace_id,
            "entrypoint": "controller.process_event",  # stable default
        }

        # ── final_status: map task_runs.status to the bundle's final_status ─
        # Unknown statuses map to UNKNOWN (NEVER silently to MERGED). Note:
        # RUN_NOT_FOUND is handled in read_snapshot before assembly, so a
        # missing run never reaches here.
        final_status = self._map_final_status(tr.get("status"))

        # ── workflow_stages / agents from stage_runs (authoritative) ────────
        # stage_runs is the authoritative per-stage execution source. Rows
        # arrive ordered by (stage, attempt DESC, id); we pick the FIRST row
        # per stage = the latest attempt. stage_events only contributes an
        # event count (provenance), not stage entries.
        workflow_stages = []
        agents = []
        seen_stages: set[str] = set()
        for sr in stage_runs:
            stage_name = sr.get("stage") or "unknown"
            if stage_name in seen_stages:
                continue
            seen_stages.add(stage_name)
            status = self._map_stage_status(sr.get("status"))
            agent_field = sr.get("agent")
            agent_role = self._infer_role(stage_name, agent_field)
            stage_entry = {
                "stage": stage_name,
                "agent_role": agent_role,
                "status": status,
                "verdict": sr.get("verdict"),
                "skill_name": stage_name,
                "skill_version": "1",
                "output_schema_validated": False,
            }
            workflow_stages.append(stage_entry)
            agents.append({
                "role": agent_role,
                "skill": stage_name,
                "status": status,
                "verdict": sr.get("verdict"),
                "outcome": sr.get("detail"),
            })

        # ── findings: the audit DB stores findings under a different task_id
        #    space and without the full inline payload the bundle's REPLAY
        #    findings carry. For the isolated viewer we surface an EMPTY list
        #    (the honest "no inline findings materialized from DB" state)
        #    unless the audit_events summary indicates reviewer findings, in
        #    which case we still do NOT fabricate finding bodies — empty is
        #    the truthful representation. This preserves the RAG boundary
        #    (adopted=False, untrusted=True) below.
        findings: list[dict] = []
        fixes: list[dict] = []

        # ── verifier_result: not directly stored; default UNKNOWN ───────────
        verifier_result = {
            "verdict": "UNKNOWN",
            "tests_run": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "duration_ms": 0,
        }

        # ── rag_advisories: preserved RAG boundary (adopted=False,untrusted) ─
        # The DB does not expose RAG hit contents to this read-only viewer, so
        # both roles report status "not_measured" with zero hits while keeping
        # the mandatory authenticity flags.
        rag_advisories = [
            {
                "agent_role": "reviewer",
                "status": "not_measured",
                "hit_count": 0,
                "fallback_reason": "RAG hits not exposed by read-only DB view",
                "adopted": False,
                "untrusted": True,
                "cases": [],
            },
            {
                "agent_role": "fixer",
                "status": "not_measured",
                "hit_count": 0,
                "fallback_reason": "RAG hits not exposed by read-only DB view",
                "adopted": False,
                "untrusted": True,
                "cases": [],
            },
        ]

        # ── spans: trace_id only; the DB does not store OTel span bodies ────
        spans: list[dict] = []

        # ── rollback_events: from rollback_runs ─────────────────────────────
        rollback_events_out = [
            {
                "rollback_id": rb.get("rollback_id"),
                "parent_run_id": rb.get("parent_run_id"),
                "revert_run_id": rb.get("revert_run_id"),
                "reverted_merge_sha": rb.get("reverted_merge_sha"),
                "status": rb.get("status") or "UNKNOWN",
                "fail_reason": rb.get("fail_reason"),
                "reverify_verdict": rb.get("reverify_verdict"),
                "created_at": self._iso(rb.get("created_at")),
            }
            for rb in rollback_events
        ]

        # ── gateway audit (mcp_calls) → residue / topology provenance ───────
        # We do NOT surface raw request bodies. We count ALLOW/DENY/ERROR.
        gateway_summary = {"allow": 0, "deny": 0, "error": 0, "total": 0}
        for call in gateway_calls:
            decision = (call.get("decision") or "").upper()
            gateway_summary["total"] += 1
            if decision == "ALLOW":
                gateway_summary["allow"] += 1
            elif decision == "DENY":
                gateway_summary["deny"] += 1
            elif decision == "ERROR":
                gateway_summary["error"] += 1

        # ── evidence_files: the DB is the source, not files on disk ─────────
        evidence_files: list[dict] = []

        # ── benchmark_summary: NOT_MEASURABLE (matches REPLAY contract) ─────
        benchmark_summary = {
            "dataset_version": "",
            "unique_case_count": 0,
            "quality_gate_pass": None,
            "confirmatory_all_ok": None,
            "runtime_consumes_rag_context": False,
            "workflow_utility_status": "NOT_MEASURABLE_WITH_CURRENT_RUNTIME",
            "benchmark_phase": "",
        }

        # ── topology ────────────────────────────────────────────────────────
        topology = {
            "policy_gateway": "mcp_calls",
            "github_upstream": "",
            "case_retrieval": "",
            "pr_lifecycle": "",
            "hiclaw_live": False,
        }

        # ── residue / stage provenance ──────────────────────────────────────
        residue = {
            "gateway_audit_summary": gateway_summary,
            "audit_events_summary": audit_summary,
            # stage_runs is the authoritative execution source; stage_events
            # only contributes event provenance/counts.
            "stage_runs_count": len(stage_runs),
            "stage_event_count": len(stage_events),
        }

        # ── Secret measurement ──────────────────────────────────────────────
        # secret_scan_status records HOW secret_leaks was determined. The
        # ISOLATED_LIVE source runs a PARTIAL_SERIALIZED_BUNDLE_SCAN: a
        # deterministic regex scan over the serialized bundle bytes for a fixed
        # set of known secret markers (DSN password=, postgresql://user:pass@,
        # postgres://user:pass@, sk-*, ghp_*, AKIA*, xox*). It is NOT a full
        # content scan over every DB row — only the serialized bundle output.
        #
        # secret_scan_scope lists exactly which patterns are checked so a
        # consumer cannot mistake the partial scan for a full scan.
        #
        # secret_leaks_detected holds the actual count from the scan (should be
        # 0; if it is non-zero we raise POSTGRES_READ_FAILED and do NOT emit a
        # bundle).
        #
        # secret_leaks stays an integer (0) because the schema strictly requires
        # secret_leaks == 0. For ISOLATED_LIVE it equals secret_leaks_detected
        # (both are 0 when the scan is clean); if the scan detects any leak we
        # refuse to emit a bundle at all, so secret_leaks is only ever 0 in a
        # returned bundle.
        secret_scan_status = "PARTIAL_SERIALIZED_BUNDLE_SCAN"
        secret_scan_scope = [
            "password=",
            "postgresql://user:pass@",
            "postgres://user:pass@",
            "sk-*",
            "ghp_*",
            "AKIA*",
            "xox*",
        ]
        secret_leaks = 0

        # ── Assemble (bundle_sha256 + generated_at added last) ──────────────
        # NOTE: secret_scan_scope is intentionally NOT added to the bundle yet.
        # The deterministic secret scan runs over the serialized bundle bytes
        # FIRST; only after the scan is clean do we attach the scope metadata.
        # This avoids a false positive where the literal pattern descriptions in
        # secret_scan_scope (e.g. "postgresql://user:pass@") would match the
        # scan regexes themselves.
        bundle = {
            "schema_version": "mergepilot.demo-bundle.v1",
            "demo_mode": "ISOLATED_LIVE",
            # generated_at is volatile (excluded from the digest).
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # source_commit: mapped from revision_bindings.head_sha (the target
            # revision). If no revision SHA exists, null + provenance_status=
            # NOT_AVAILABLE (NEVER an empty string, NEVER a fabricated SHA).
            "source_commit": source_commit,
            # verification_commit: ALWAYS null for ISOLATED_LIVE — the read-only
            # viewer does not record a verification build. verification_commit_
            # status="NOT_AVAILABLE" makes the absence explicit.
            "verification_commit": verification_commit,
            "verification_commit_status": verification_commit_status,
            # provenance_status records where the commit SHAs came from (or
            # that they are unavailable). ISOLATED_LIVE-only field; REPLAY
            # bundles do not emit it (schema does not require it).
            "provenance_status": provenance_status,
            "repo": repo,
            "pr": pr,
            "run": run,
            "final_status": final_status,
            "workflow_stages": workflow_stages,
            "agents": agents,
            "findings": findings,
            "fixes": fixes,
            "verifier_result": verifier_result,
            "rag_advisories": rag_advisories,
            "spans": spans,
            "rollback_events": rollback_events_out,
            "evidence_files": evidence_files,
            # secret_scan_status records the measurement approach (ISOLATED_LIVE
            # only); secret_leaks stays 0 to honor the strict schema contract.
            "secret_leaks": secret_leaks,
            "secret_scan_status": secret_scan_status,
            "secret_leaks_detected": secret_leaks,
            "residue": residue,
            "benchmark_summary": benchmark_summary,
            "topology": topology,
        }

        # Deterministic secret scan over the serialized bundle bytes. This is a
        # belt-and-suspenders guarantee that a secret never reaches the emitted
        # bytes: even if a future change leaked the DSN or a token into the
        # bundle, this scan would catch it. Because secrets are never serialized
        # into the bundle, this is always 0 in practice. The scan runs BEFORE
        # secret_scan_scope is attached so the literal pattern descriptions do
        # not trigger a false positive.
        serialized = json.dumps(
            bundle, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        scanned_leaks = self._scan_for_secrets(serialized)
        # Record the actual detected count on the bundle for transparency.
        bundle["secret_leaks_detected"] = scanned_leaks
        if scanned_leaks:
            # A leak was detected in the assembled bundle bytes. Fail-closed:
            # do NOT emit a bundle containing a secret. Raise POSTGRES_READ_
            # FAILED with a sanitized, stable-code-only message (no raw bytes,
            # no DSN, no token text).
            raise PostgresQueryError(
                "POSTGRES_READ_FAILED: secret leak detected in serialized "
                f"bundle bytes ({scanned_leaks} match(es)); refusing to emit "
                "a bundle containing a secret",
                code="POSTGRES_READ_FAILED",
            )

        # Now that the scan is clean, attach the secret_scan_scope metadata
        # (the literal pattern descriptions). This field is metadata only; it
        # is excluded from the scan above to avoid a self-match false positive.
        bundle["secret_scan_scope"] = list(secret_scan_scope)

        # Compute the digest over canonical JSON excluding volatile fields.
        bundle["bundle_sha256"] = compute_bundle_sha256(bundle)
        return bundle

    # ── Helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _map_final_status(db_status) -> str:
        """Map task_runs.status to the bundle's final_status enum.

        Unknown / missing statuses map to UNKNOWN — NEVER to MERGED. MERGED is
        only reported when the DB explicitly records it.
        """
        # Known task_runs.status values (per m3_state / m3b_b4 / m3c):
        # SUBMITTED, RUNNING, PASS, FAIL, HOLD, MERGED, ROLLED_BACK,
        # APPROVAL_PENDING.
        mapping = {
            "MERGED": "MERGED",
            "PASS": "PASS",
            "FAIL": "FAIL",
            "HOLD": "HOLD",
            "ROLLED_BACK": "ROLLED_BACK",
            "RUNNING": "RUNNING",
            "SUBMITTED": "SUBMITTED",
            "APPROVAL_PENDING": "APPROVAL_PENDING",
        }
        if not isinstance(db_status, str):
            return "UNKNOWN"
        return mapping.get(db_status.upper(), "UNKNOWN")

    @staticmethod
    def _map_stage_status(db_status) -> str:
        """Map stage_runs.status to a bundle workflow_stage status.

        stage_runs uses PENDING_DISPATCH / RUNNING / COMPLETED / FAILED style
        values. Unknown values are carried through mapped to UNKNOWN (never
        silently to COMPLETED).
        """
        if not isinstance(db_status, str):
            return "UNKNOWN"
        mapping = {
            "PENDING_DISPATCH": "PENDING",
            "RUNNING": "RUNNING",
            "COMPLETED": "COMPLETED",
            "SUCCEEDED": "COMPLETED",
            "FAILED": "FAILED",
            "ERROR": "FAILED",
            "SKIPPED": "SKIPPED",
        }
        return mapping.get(db_status.upper(), "UNKNOWN")

    @staticmethod
    def _infer_role(stage_name: str, agent_field) -> str:
        """Best-effort agent_role inference for a stage_runs row.

        stage_runs has an ``agent`` column (the agent identity), not an
        ``agent_role`` column. We map known stage names to roles, fall back to
        the agent identity, then 'unknown'. NULL agent → 'unknown'. This is
        display metadata only — it does not drive any write or decision.
        """
        role_map = {
            "diff-parse": "reviewer",
            "risk-classify": "reviewer",
            "sast-scan": "reviewer",
            "test-runner": "verifier",
            "case-retrieval": "reviewer",
            "pr-lifecycle": "fixer",
            "review": "reviewer",
            "fix": "fixer",
            "verify": "verifier",
        }
        if isinstance(stage_name, str) and stage_name in role_map:
            return role_map[stage_name]
        if isinstance(agent_field, str) and agent_field:
            # The agent identity (e.g. 'reviewer-agent') may itself encode a
            # known role keyword.
            for key, role in role_map.items():
                if key in agent_field:
                    return role
            return agent_field
        return "unknown"

    @staticmethod
    def _iso(value) -> str:
        """Render a datetime/timestamp as ISO 8601 (or empty string)."""
        if value is None:
            return ""
        # psycopg2 returns datetimes; isoformat() is the stable rendering.
        iso = getattr(value, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:  # pragma: no cover - defensive
                return str(value)
        return str(value)

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """Strip anything that looks like a secret from an error string.

        This is a DEFENSE-IN-DEPTH helper. The primary DSN-secrecy boundary is
        that :meth:`read_snapshot` NEVER includes the raw psycopg2/libpq
        exception message in the re-raised :class:`PostgresSourceError` — it
        surfaces only the stable error ``code`` and the exception type name.
        This helper exists so that if any internal log path or future caller
        ever formats a libpq message (which can echo the connection string on
        connect failures), the result is still safe: it redacts
        ``password=...`` fragments, ``postgresql://user:pass@`` /
        ``postgres://user:pass@`` URIs with embedded credentials, and
        token-like markers.
        """
        if not isinstance(text, str):
            try:
                text = str(text)
            except Exception:  # pragma: no cover - defensive
                return "<unprintable>"
        # Redact password=... (with or without quotes) up to the next space/end.
        text = re.sub(
            r"password=['\"]?[^'\"\s]+",
            "password=<REDACTED>",
            text,
            flags=re.IGNORECASE,
        )
        # Redact libpq URI credentials (postgresql://user:pass@... and
        # postgres://user:pass@...).
        text = re.sub(
            r"(postgres(?:ql)?://)([^:/@\s]+):([^:/@\s]+)@",
            r"\1<REDACTED>:@",
            text,
            flags=re.IGNORECASE,
        )
        # Redact token-like markers so a leaked key never appears verbatim.
        text = re.sub(r"\bsk-[A-Za-z0-9]{20,}", "sk-<REDACTED>", text)
        text = re.sub(r"\bghp_[A-Za-z0-9]{36,}", "ghp_<REDACTED>", text)
        text = re.sub(r"\bAKIA[0-9A-Z]{16}", "AKIA<REDACTED>", text)
        text = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "xox-<REDACTED>", text)
        return text

    @staticmethod
    def _scan_for_secrets(data: bytes) -> int:
        """Deterministic secret scan over serialized bundle bytes.

        Returns the count of secret markers found. The scan looks for a fixed
        set of known secret markers (DSN password=, postgresql://user:pass@,
        postgres://user:pass@, sk-*, ghp_*, AKIA*, xox*). Because secrets are
        NEVER serialized into the bundle, this returns 0 in practice. The scan
        exists as a fail-closed guarantee: if a future change leaked a secret,
        the count would be > 0 and the source would raise POSTGRES_READ_FAILED.
        """
        if not isinstance(data, (bytes, bytearray)):
            return 0
        text = data.decode("utf-8", errors="replace")
        count = 0
        # password= marker (the canonical DSN secret leak signature).
        count += len(re.findall(
            r"password=['\"]?[^'\"\s]+", text, flags=re.IGNORECASE,
        ))
        # postgresql://user:pass@ (libpq URI with embedded credentials).
        count += len(re.findall(
            r"postgresql://[^:/@\s]+:[^:/@\s]+@", text, flags=re.IGNORECASE,
        ))
        # postgres://user:pass@ (alternate libpq URI scheme).
        count += len(re.findall(
            r"postgres://[^:/@\s]+:[^:/@\s]+@", text, flags=re.IGNORECASE,
        ))
        # OpenAI-style API keys (sk-...).
        count += len(re.findall(r"\bsk-[A-Za-z0-9]{20,}", text))
        # GitHub personal access tokens (ghp_...).
        count += len(re.findall(r"\bghp_[A-Za-z0-9]{36,}", text))
        # AWS access key IDs (AKIA...).
        count += len(re.findall(r"\bAKIA[0-9A-Z]{16}", text))
        # Slack tokens (xox...).
        count += len(re.findall(r"\bxox[baprs]-[A-Za-z0-9-]{10,}", text))
        return count

    @staticmethod
    def _safe_rollback(conn) -> None:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - rollback best-effort
            pass

    @staticmethod
    def _safe_close(conn) -> None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close best-effort
            pass


__all__ = [
    "PostgresSnapshotSource",
    "PostgresSourceError",
    "IdentityCheckError",
    "RunIdError",
    "RunNotFoundError",
    "PostgresQueryError",
    "ConfigInvalidError",
    "SCHEMA_CONTRACT",
    "ENVIRONMENT_MARKER_CONTRACT",
    "STABLE_ERROR_CODES",
    "REQUIRED_QUERY_COLUMNS",
    "PRIVILEGE_CHECKED_TABLES",
    "_all_select_templates",
    "_referenced_table_columns",
]
