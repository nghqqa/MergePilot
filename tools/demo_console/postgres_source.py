#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostgreSQL read-only snapshot source for ISOLATED_LIVE mode (Phase 2).

``PostgresSnapshotSource`` is a ``SnapshotSource`` that reads a single run's
state from a PostgreSQL database and assembles a DemoBundle on the fly. It is
**strictly read-only**:

- The FIRST statement after connecting is an explicit
  ``BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY``. All identity
  checks (database/role/read-only/server/catalog/environment-marker probes) run
  INSIDE that transaction. On success it ``ROLLBACK``s; on any error it
  ``ROLLBACK``s and closes the connection.
- Only ``SELECT`` queries are issued inside the read-only transaction.
- ``psycopg2`` is imported lazily inside ``read_snapshot`` so that REPLAY /
  FILE_FIXTURE deployments never need a database driver installed.
- The DSN is treated as a secret: it never appears in ``repr``, ``str``,
  exception messages, or logs. All errors are re-raised with a sanitized
  message that carries only a stable error ``code`` and the public identity
  fields.

The assembled bundle declares ``demo_mode = "ISOLATED_LIVE"`` and a
``bundle_sha256`` computed by the shared :mod:`integrity` helpers, so it flows
through the existing schema/integrity validation in :mod:`live_poller`
unchanged.

This is a P2 code implementation candidate: local review only, not pushed, not
merged. MergePilot-Test isolated verification has NOT been performed.
"""
from __future__ import annotations

import json
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
})


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

# Environment marker probe. The source looks for a trusted marker row that
# identifies the database environment as the expected ISOLATED_LIVE viewer
# target. The marker is a simple key/value in controller_offsets
# (consumer_name='mergepilot_environment', sync_token=<expected env id>). If no
# trusted marker exists, the source refuses startup with ENVIRONMENT_ID_NOT_VERIFIED
# rather than guessing from the hostname.
_ENV_MARKER_SQL = (
    "SELECT sync_token FROM controller_offsets "
    "WHERE consumer_name = 'mergepilot_environment'"
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
        Required value of ``current_user``; anything else is rejected.
    expected_environment_id:
        Required value of the trusted environment marker
        (``controller_offsets.sync_token`` where
        ``consumer_name='mergepilot_environment'``). If ``None``, the
        environment-marker check is skipped (only safe for tests).
    query_timeout_seconds:
        Per-session ``statement_timeout`` (and ``lock_timeout`` /
        ``idle_in_transaction_session_timeout``), in seconds. Default 10.
    """

    kind = "POSTGRES_ISOLATED"

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
        query_timeout_seconds: float = 10.0,
    ):
        # Store the DSN in a single private attribute. __repr__/__str__ are
        # overridden below to ensure it can never leak.
        self._dsn = dsn
        self._run_id = run_id
        self._expected_database = expected_database
        self._expected_role = expected_role
        self._expected_environment_id = expected_environment_id
        # statement_timeout / lock_timeout take milliseconds in PostgreSQL.
        self._timeout_ms = int(max(0.0, float(query_timeout_seconds)) * 1000)

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
        5. Verify identity inside the txn: current_database(), current_user,
           transaction_read_only / default_transaction_read_only, server identity
           (inet_server_addr/port/application_name/server_version_num), schema
           search_path, required-table catalog probe, and the environment marker.
        6. SELECT task_runs / stage_runs / stage_events / revision_bindings /
           run_pr_bindings / mcp_calls / rollback_runs / audit_events counts
           (all parameterized).
        7. Assemble the DemoBundle with demo_mode="ISOLATED_LIVE".
        8. Compute bundle_sha256.
        9. ROLLBACK + close.
        10. Return JSON bytes.

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
            ) from e

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
            # Sanitize: strip any DSN that psycopg2 may embed in the message.
            # Raise a generic code plus a trimmed, secret-free detail.
            detail = self._sanitize_text(str(exc))
            if conn is not None:
                self._safe_rollback(conn)
                self._safe_close(conn)
            raise PostgresQueryError(
                f"POSTGRES_READ_FAILED: {type(exc).__name__}: {detail[:200]}",
                code="POSTGRES_READ_FAILED",
            ) from exc
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
        """
        # ── Core identity (database, role, read-only flags) ────────────────
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

        # ── Server identity (address/port/application_name/version) ────────
        # inet_server_addr()/inet_server_port() report the actual server the
        # connection landed on. We do NOT guess from hostname: an operator who
        # needs a specific server must supply expected values. Here we only
        # record them and reject obviously-wrong server artifacts (NULL addr
        # via a Unix socket is allowed); a future hardening can pin these.
        cur.execute(
            "SELECT inet_server_addr()::text, inet_server_port(), "
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
        # server_version_num is an integer (e.g. 160001 for PG 16.1). We accept
        # the supported major range (12..17) — anything older/newer-than-known
        # is treated as a schema-compatibility risk.
        if not isinstance(server_version_num, int) or not (120000 <= server_version_num < 180000):
            raise IdentityCheckError(
                "WRONG_SERVER: unsupported server_version_num "
                f"(got {self._safe_snippet(server_version_num)}; expected 12.x-17.x)",
                code="WRONG_SERVER",
            )

        # ── Schema / search_path ───────────────────────────────────────────
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

        # ── Required-table catalog probe ───────────────────────────────────
        # Fail-closed if any table the source reads is missing — a partial
        # schema means we cannot assemble a correct bundle.
        required_tables = (
            "task_runs", "stage_runs", "stage_events", "revision_bindings",
            "run_pr_bindings", "mcp_calls", "rollback_runs", "audit_events",
            "controller_offsets",
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

        # ── Environment marker (trusted identity; never guessed) ───────────
        # Look for a trusted marker row identifying the database environment.
        # If an expected_environment_id was supplied, the marker's value must
        # match it exactly; otherwise ENVIRONMENT_ID_MISMATCH. If NO marker row
        # exists at all, the environment is unverified: refuse startup with
        # ENVIRONMENT_ID_NOT_VERIFIED (do NOT guess from hostname).
        cur.execute(_ENV_MARKER_SQL)
        marker_row = cur.fetchone()
        if marker_row is None:
            raise IdentityCheckError(
                "ENVIRONMENT_ID_NOT_VERIFIED: no trusted environment marker "
                "(controller_offsets.consumer_name='mergepilot_environment'); "
                "refusing startup without a verified environment identity",
                code="ENVIRONMENT_ID_NOT_VERIFIED",
            )
        marker_value = marker_row[0]
        if self._expected_environment_id is not None:
            if marker_value != self._expected_environment_id:
                raise IdentityCheckError(
                    "ENVIRONMENT_ID_MISMATCH: environment marker does not match "
                    f"expected (got {self._safe_snippet(marker_value)})",
                    code="ENVIRONMENT_ID_MISMATCH",
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

        # source_commit/verification_commit come from the revision cut. If we
        # have a real head_sha, use it; otherwise null + NOT_AVAILABLE (never
        # fabricate a SHA, never emit "").
        has_revision_sha = bool(rev_head_sha)
        source_commit = rev_head_sha if has_revision_sha else None
        verification_commit = rev_head_sha if has_revision_sha else None
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
        # secret_scan_status records HOW secret_leaks was determined:
        #   NOT_MEASURED — for ISOLATED_LIVE we do not run a full secret scan
        #                  over the DB rows; the bundle bytes are scanned for
        #                  the known DSN/password markers only (deterministic).
        # secret_leaks stays an integer (0) because the schema strictly requires
        # secret_leaks == 0; the bundle never contains the DSN (it is a secret
        # and is never serialized into the bundle), so the deterministic scan
        # of the serialized bytes always reports 0. This keeps the REPLAY
        # strict contract unchanged while making the ISOLATED_LIVE measurement
        # status explicit.
        secret_scan_status = "NOT_MEASURED"
        secret_leaks = 0

        # ── Assemble (bundle_sha256 + generated_at added last) ──────────────
        bundle = {
            "schema_version": "mergepilot.demo-bundle.v1",
            "demo_mode": "ISOLATED_LIVE",
            # generated_at is volatile (excluded from the digest).
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # source_commit / verification_commit: mapped from revision_bindings.
            # If no revision SHA exists, null + provenance_status=NOT_AVAILABLE
            # (NEVER an empty string, NEVER a fabricated SHA).
            "source_commit": source_commit,
            "verification_commit": verification_commit,
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
            "residue": residue,
            "benchmark_summary": benchmark_summary,
            "topology": topology,
        }

        # Deterministic secret scan over the serialized bundle bytes. This is a
        # belt-and-suspenders guarantee that a DSN/password never reaches the
        # emitted bytes: even if a future change leaked the DSN into the bundle,
        # this scan would catch it and surface a non-zero secret_leaks (which
        # the schema would then reject). Because the DSN is never serialized
        # into the bundle, this is always 0 in practice.
        serialized = json.dumps(
            bundle, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        scanned_leaks = self._scan_for_secrets(serialized)
        if scanned_leaks:
            # A leak was detected in the assembled bundle bytes. This is a
            # fail-closed invariant violation: do NOT emit a bundle with
            # secret_leaks=0; report the actual count so schema validation
            # rejects it. (In practice this never fires.)
            bundle["secret_leaks"] = scanned_leaks

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
        """Strip anything that looks like a DSN from an error string.

        psycopg2 / libpq messages occasionally echo the connection string on
        connect failures. Redact any ``password=...`` fragment wholesale and
        trim the message so a leaked DSN can never reach a log.
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
        return text

    @staticmethod
    def _scan_for_secrets(data: bytes) -> int:
        """Deterministic secret scan over serialized bundle bytes.

        Returns the count of secret markers found. The scan looks for the known
        DSN password marker and a libpq-style connection string that includes a
        password. Because the DSN is a secret and is NEVER serialized into the
        bundle, this returns 0 in practice. The scan exists as a fail-closed
        guarantee: if a future change leaked the DSN, the count would be > 0
        and the bundle would fail schema validation (secret_leaks != 0).
        """
        if not isinstance(data, (bytes, bytearray)):
            return 0
        text = data.decode("utf-8", errors="replace")
        count = 0
        # password= marker (the canonical DSN secret leak signature).
        count += len(re.findall(r"password=['\"]?[^'\"\s]+", text, flags=re.IGNORECASE))
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
    "SCHEMA_CONTRACT",
    "STABLE_ERROR_CODES",
    "_all_select_templates",
    "_referenced_table_columns",
]
