#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostgreSQL read-only snapshot source for ISOLATED_LIVE mode (Phase 2).

``PostgresSnapshotSource`` is a ``SnapshotSource`` that reads a single run's
state from a PostgreSQL database and assembles a DemoBundle on the fly. It is
**strictly read-only**:

- It connects, verifies the session is read-only and points at the expected
  database/role, opens a ``REPEATABLE READ READ ONLY`` transaction, issues only
  ``SELECT`` queries, then ``ROLLBACK``s and closes the connection.
- ``psycopg2`` is imported lazily inside ``read_snapshot`` so that REPLAY /
  FILE_FIXTURE deployments never need a database driver installed.
- The DSN is treated as a secret: it never appears in ``repr``, ``str``,
  exception messages, or logs. All errors are re-raised with a sanitized
  message that carries only a stable error code and the public identity fields.

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


class PostgresSourceError(Exception):
    """Sanitized error raised by :class:`PostgresSnapshotSource`.

    The message NEVER contains the DSN or any other connection secret. It
    carries only a stable ``code`` and the public identity fields that were
    being verified. Callers (the poller) surface ``type(e).__name__`` as the
    error code, so subclasses below give stable, machine-readable codes.
    """


class IdentityCheckError(PostgresSourceError):
    """The connected database/user/read-only flag did not match expectations."""


class RunIdError(PostgresSourceError):
    """The supplied run_id failed the allowlist check."""


class PostgresQueryError(PostgresSourceError):
    """A read query failed. The original (sanitized) detail is attached."""


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
        query_timeout_seconds: float = 10.0,
    ):
        # Store the DSN in a single private attribute. __repr__/__str__ are
        # overridden below to ensure it can never leak.
        self._dsn = dsn
        self._run_id = run_id
        self._expected_database = expected_database
        self._expected_role = expected_role
        # statement_timeout / lock_timeout take milliseconds in PostgreSQL.
        self._timeout_ms = int(max(0.0, float(query_timeout_seconds)) * 1000)

        # Validate run_id shape eagerly so a bad value never reaches a query.
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.match(run_id):
            # Do NOT include the raw value verbatim if it is huge / weird; cap it.
            snippet = self._safe_snippet(run_id)
            raise RunIdError(
                f"RUN_ID_INVALID: run_id must match ^[a-zA-Z0-9_-]+$ (got {snippet})"
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
        3. Verify identity: current_database(), current_user,
           transaction_read_only, default_transaction_read_only.
        4. BEGIN REPEATABLE READ READ ONLY + SET LOCAL timeouts.
        5. SELECT task_runs / stage_events / run_pr_bindings / mcp_calls /
           rollback_runs / audit_events (all parameterized).
        6. Assemble the DemoBundle with demo_mode="ISOLATED_LIVE".
        7. Compute bundle_sha256.
        8. ROLLBACK + close.
        9. Return JSON bytes.

        On ANY error the connection is closed and a sanitized
        :class:`PostgresSourceError` is raised (no DSN in the message).
        """
        # 1. Lazy import. Importing here means REPLAY / FILE deployments do
        #    not require psycopg2 to be installed at all.
        try:
            import psycopg2
        except ImportError as e:
            raise PostgresSourceError(
                "PSYCOPG2_MISSING: psycopg2 is required for the PostgreSQL "
                "snapshot source but is not installed"
            ) from e

        conn = None
        try:
            # 2. Connect. Any connect-time failure is sanitized below.
            conn = psycopg2.connect(self._dsn)

            # 3. Identity verification. Use a bare cursor (no transaction is
            #    open yet, so these run in autocommit-ish read probes).
            with conn.cursor() as cur:
                self._verify_identity(cur)

            # 4. Open an explicit READ ONLY transaction with tight timeouts.
            with conn.cursor() as cur:
                cur.execute(
                    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                self._set_local_timeouts(cur)

                # 5. Read each table. All queries use %s placeholders; run_id
                #    is the only parameter and it was validated in __init__.
                task_run = self._query_task_run(cur)
                stage_events = self._query_stage_events(cur)
                revision = self._query_revision(cur)
                gateway_calls = self._query_mcp_calls(cur)
                rollback_events = self._query_rollback_runs(cur)
                audit_summary = self._query_audit_events(cur)

            # 6/7. Assemble + digest outside the cursor block (no DB needed).
            bundle = self._assemble_bundle(
                task_run=task_run,
                stage_events=stage_events,
                revision=revision,
                gateway_calls=gateway_calls,
                rollback_events=rollback_events,
                audit_summary=audit_summary,
            )

            # 8. End the read-only transaction by rolling it back (we only
            #    ever read; ROLLBACK releases the snapshot cleanly and leaves
            #    no idle-in-transaction residue).
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - rollback best-effort
                pass

            # 9. Serialize. Sort keys for deterministic bytes (matches the
            #    canonical serialization used by compute_bundle_sha256).
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
                f"POSTGRES_READ_FAILED: {type(exc).__name__}: {detail[:200]}"
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
        """Reject wrong database, wrong role, or a non-read-only session."""
        # All four values are server constants / GUCs; no user input, so a
        # plain execute without parameters is safe. The expected values come
        # from constructor arguments that are public identity, not secrets.
        cur.execute(
            "SELECT current_database(), current_user, "
            "current_setting('transaction_read_only')::boolean, "
            "current_setting('default_transaction_read_only')::boolean"
        )
        row = cur.fetchone()
        if row is None:
            raise IdentityCheckError(
                "IDENTITY_CHECK_FAILED: identity probe returned no row"
            )
        database, user, tx_read_only, default_read_only = row

        if database != self._expected_database:
            raise IdentityCheckError(
                "WRONG_DATABASE: connected database does not match expected "
                f"(got {self._safe_snippet(database)})"
            )
        if user != self._expected_role:
            raise IdentityCheckError(
                "WRONG_ROLE: connected role does not match expected "
                f"(got {self._safe_snippet(user)})"
            )
        # Fail-closed: the session must report read-only at BOTH the current
        # transaction and the default level. A writable session is refused
        # even if our explicit BEGIN ... READ ONLY would also constrain it —
        # defense in depth.
        if tx_read_only is not True or default_read_only is not True:
            raise IdentityCheckError(
                "NOT_READ_ONLY: session is writable "
                "(transaction_read_only or default_transaction_read_only is off)"
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
        cur.execute(
            "SELECT run_id, repo, pr_number, branch, status, current_stage, "
            "attempt, verdict, last_error, created_at, updated_at, trace_id "
            "FROM task_runs WHERE run_id = %s",
            (self._run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "run_id", "repo", "pr_number", "branch", "status", "current_stage",
            "attempt", "verdict", "last_error", "created_at", "updated_at",
            "trace_id",
        ]
        return dict(zip(cols, row))

    def _query_stage_events(self, cur) -> list[dict]:
        """SELECT stage_events for the run, ordered by received_at."""
        cur.execute(
            "SELECT event_id, run_id, sender, event_type, stage, status, "
            "error, received_at, processed_at "
            "FROM stage_events WHERE run_id = %s ORDER BY received_at",
            (self._run_id,),
        )
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
        cur.execute(
            "SELECT rb.binding_id, rb.run_id, rb.repo, rb.pr_number, "
            "rb.base_sha, rb.head_sha, rb.recorded_at "
            "FROM revision_bindings rb WHERE rb.run_id = %s",
            (self._run_id,),
        )
        cols = [
            "binding_id", "run_id", "repo", "pr_number", "base_sha",
            "head_sha", "recorded_at",
        ]
        row = cur.fetchone()
        revision = dict(zip(cols, row)) if row else None

        # PR branch identity from run_pr_bindings (fix_branch / base_branch).
        cur.execute(
            "SELECT repo, pr_number, fix_branch, base_branch, head_sha, "
            "recorded_at FROM run_pr_bindings WHERE run_id = %s",
            (self._run_id,),
        )
        pr_cols = ["repo", "pr_number", "fix_branch", "base_branch",
                   "head_sha", "recorded_at"]
        pr_row = cur.fetchone()
        pr_binding = dict(zip(pr_cols, pr_row)) if pr_row else None

        if revision is not None:
            revision["pr_binding"] = pr_binding
        return revision

    def _query_mcp_calls(self, cur) -> list[dict]:
        """SELECT mcp_calls (gateway audit) for the run."""
        cur.execute(
            "SELECT request_id, correlation_id, phase, ts, caller_agent, tool, "
            "decision, reason_code, target_repo, target_branch, result_status, "
            "git_sha, error "
            "FROM mcp_calls WHERE run_id = %s ORDER BY ts",
            (self._run_id,),
        )
        cols = [
            "request_id", "correlation_id", "phase", "ts", "caller_agent",
            "tool", "decision", "reason_code", "target_repo", "target_branch",
            "result_status", "git_sha", "error",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _query_rollback_runs(self, cur) -> list[dict]:
        """SELECT rollback_runs events referencing this run (as parent or revert)."""
        cur.execute(
            "SELECT rollback_id, parent_run_id, revert_run_id, reverted_merge_sha, "
            "repo, pr_number, status, fail_reason, revert_result_sha, "
            "reverify_verdict, created_at, updated_at "
            "FROM rollback_runs WHERE parent_run_id = %s OR revert_run_id = %s "
            "ORDER BY created_at",
            (self._run_id, self._run_id),
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
        cur.execute(
            "SELECT count(*) FROM audit_events WHERE task_id = %s",
            (self._run_id,),
        )
        total_row = cur.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        cur.execute(
            "SELECT action, count(*) FROM audit_events WHERE task_id = %s "
            "GROUP BY action ORDER BY action",
            (self._run_id,),
        )
        by_action = {str(action): int(cnt) for action, cnt in cur.fetchall()}

        return {"total": total, "by_action": by_action}

    # ── Bundle assembly ────────────────────────────────────────────────────
    def _assemble_bundle(
        self,
        *,
        task_run: dict | None,
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
        base_sha = (revision or {}).get("base_sha") or ""
        head_sha = (revision or {}).get("head_sha") or tr.get("head_sha") or ""

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
        # Unknown statuses map to UNKNOWN (NEVER silently to MERGED).
        final_status = self._map_final_status(tr.get("status"))

        # ── workflow_stages / agents from stage_events ──────────────────────
        # stage_events.event_type / stage / status drive a minimal stage list.
        # We do not fabricate agent roles that the DB does not record; unknown
        # roles are carried verbatim and default to "unknown".
        workflow_stages = []
        agents = []
        seen_stages = set()
        for ev in stage_events:
            stage_name = ev.get("stage") or ev.get("event_type") or "unknown"
            if stage_name in seen_stages:
                continue
            seen_stages.add(stage_name)
            status = ev.get("status") or "UNKNOWN"
            sender = ev.get("sender") or "unknown"
            stage_entry = {
                "stage": stage_name,
                "agent_role": self._infer_role(stage_name, sender),
                "status": status,
                "verdict": None,
                "skill_name": stage_name,
                "skill_version": "1",
                "output_schema_validated": False,
            }
            workflow_stages.append(stage_entry)
            agents.append({
                "role": stage_entry["agent_role"],
                "skill": stage_name,
                "status": status,
                "verdict": None,
                "outcome": None,
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

        # ── residue / secret_leaks ──────────────────────────────────────────
        residue = {
            "gateway_audit_summary": gateway_summary,
            "audit_events_summary": audit_summary,
            "stage_event_count": len(stage_events),
        }
        secret_leaks = 0

        # ── Assemble (bundle_sha256 + generated_at added last) ──────────────
        bundle = {
            "schema_version": "mergepilot.demo-bundle.v1",
            "demo_mode": "ISOLATED_LIVE",
            # generated_at is volatile (excluded from the digest).
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # source_commit / verification_commit: the DB viewer has no git
            # working copy; record NOT_MEASURED rather than fabricate a SHA.
            "source_commit": "",
            "verification_commit": "",
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
            "secret_leaks": secret_leaks,
            "residue": residue,
            "benchmark_summary": benchmark_summary,
            "topology": topology,
        }

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
    def _infer_role(stage_name: str, sender: str) -> str:
        """Best-effort agent_role inference for a stage_event row.

        Falls back to the sender, then 'unknown'. This is display metadata
        only — it does not drive any write or decision.
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
        if isinstance(sender, str) and sender in role_map:
            return role_map[sender]
        if isinstance(sender, str) and sender:
            return sender
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
    "PostgresQueryError",
]
