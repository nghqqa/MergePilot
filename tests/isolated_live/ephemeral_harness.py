#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISOLATED_LIVE PostgreSQL ephemeral verification harness (Phase A scaffolding).

This module is the Phase A code implementation candidate for the ISOLATED_LIVE
PostgreSQL Ephemeral Verification harness described in
``docs/ISOLATED-LIVE-PG-Ephemeral-Verification-Design.md``.

**Phase A scope**: harness scaffolding ONLY. No Docker execution, no real
PostgreSQL connection, no evidence output. The functions defined here are the
building blocks a Phase B executor would call against an authorized ephemeral
container. They are pure / deterministic / safe to import in any environment.

Status: ``NOT_EXECUTED``. The env gate (:func:`check_execution_auth`) requires
both ``EPHEMERAL_PG_VERIFY=1`` AND the MergePilot-Test daemon to be reachable.
Even when authorized, Phase A does NOT run the container — that is Phase B.

Security invariants enforced by this module:
  - All subprocess invocations use **array arguments** (never ``shell=True``).
    ``build_migration_commands`` and ``build_cleanup_commands`` return lists of
    argv arrays so a caller can pass them directly to ``subprocess.run``.
  - The reader-role password is a **function argument**, never a module
    constant, never placed in ``repr``/``str``/logs. ``redact_secrets`` strips
    ``password=...`` patterns from any text before it is logged.
  - Every container name is validated by :func:`validate_container_name` before
    it reaches a ``docker rm``. Path traversal and shell metacharacters are
    rejected so a malformed label can never escape the argv boundary.
  - File reads use ``with``. The digest algorithm is pure-stdlib (hashlib).

This is a Phase A code implementation candidate: local review only, not pushed,
not merged. Ephemeral PostgreSQL execution has NOT been performed.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

# ── sys.path setup ─────────────────────────────────────────────────────────
# Make the repo root importable so the canonical viewer role constant is
# imported from its authoritative source (tools/demo_console/postgres_source.py)
# rather than redefined here. Drift between this harness and the snapshot
# source's role name would silently break the identity gate.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # tests/isolated_live → repo root
_DEMO_CONSOLE = _REPO_ROOT / "tools" / "demo_console"
for _p in (str(_REPO_ROOT), str(_DEMO_CONSOLE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from postgres_source import CANONICAL_VIEWER_ROLE  # noqa: E402


# ── Image + daemon authorization ───────────────────────────────────────────
# Digest-pinned pgvector image (same as tests/m4f1/run_schema_foundation.sh).
# The full sha256 digest MUST be present — a floating tag would defeat the
# reproducibility guarantee.
IMAGE_DIGEST = (
    "pgvector/pgvector@sha256:"
    "a36250871de0833b8757561c72f2477ef1dd1101afa4a617fb552e0de514c6b"
)

# The ONLY Docker daemon authorized to run the ephemeral container. Production
# daemons (Ubuntu-22.04) must never be touched. See the design doc §1.
AUTHORIZED_DAEMON = "MergePilot-Test"

# ── Canonical role / environment contract ──────────────────────────────────
# Re-exported from postgres_source so there is exactly one source of truth.
# A test below asserts this equals the literal "mergepilot_reader".

# Prerequisite roles created in Phase 0, BEFORE any audit-db migration (mirrors
# run_schema_foundation.sh:43). Both are NOLOGIN — they are group/login roles
# for the policy/approval subsystems, not connection roles.
PREREQUISITE_ROLES = [
    "policy_gateway_l2 NOLOGIN",
    "mergepilot_approver NOLOGIN",
]

# ISOLATED_LIVE viewer-role migrations (Phase 3). 001 creates the environment
# marker table + GRANTs SELECT to the reader role; 002 GRANTs SELECT on all 9
# queried tables and REVOKEs writes. These run AFTER the reader role exists.
ISOLATED_LIVE_MIGRATIONS = [
    "001_environment_identity.sql",
    "002_mergepilot_reader_acl.sql",
]

# The deterministic environment marker inserted into environment_identity. The
# snapshot source compares this value exactly against the table row at read
# time; a mismatch is fail-closed ENVIRONMENT_ID_MISMATCH.
ENVIRONMENT_ID_EPHEMERAL = "mergepilot-test-ephemeral"


# ── Migration chain ────────────────────────────────────────────────────────
# Ordered (filename, description) pairs for the audit-db migration applications.
# Per the design §1 and run_schema_foundation.sh, m4f1_state and m4f1_hotfix_1
# are each applied TWICE (idempotency verification). That yields 13 entries:
#   9 base (init..m3c_state) + 2× m4f1_state + 2× m4f1_hotfix_1 = 13
# (11 distinct files). The two ISOLATED_LIVE migrations (001/002) are a SEPARATE
# Phase-3 step here (see ISOLATED_LIVE_MIGRATIONS), NOT part of this chain, so
# the total migration-file count is 15 (13 audit-db + 2 ISOLATED_LIVE). The
# authoritative audit-db count is 13.
MIGRATION_CHAIN = [
    ("init.sql", "Base schema bootstrap (extensions, initial tables)"),
    ("m3_state.sql", "M3-A workflow controller state (task_runs/stage_runs/...)"),
    ("m3b_policy.sql", "M3-B policy gateway (mcp_calls, approvals, outbox)"),
    ("m3b_b4.sql", "M3-B b4 run_pr_bindings + APPROVAL_PENDING status"),
    ("m3b_b4c.sql", "M3-B b4c run_pr_bindings refinement"),
    ("m3b_b4c1.sql", "M3-B b4c1 run_pr_bindings refinement"),
    ("m3b_b4c1_1.sql", "M3-B b4c1_1 run_pr_bindings refinement"),
    ("m3b_b4d1.sql", "M3-B b4d1 run_pr_bindings refinement"),
    ("m3c_state.sql", "M3-C state-aware failure handling + rollback_runs"),
    ("m4f1_state.sql", "M4-F1 contract v2.8 (round 1)"),
    ("m4f1_state.sql", "M4-F1 contract v2.8 (round 2 — idempotency)"),
    ("m4f1_hotfix_1.sql", "M4-F post-release P1 hotfix (round 1)"),
    ("m4f1_hotfix_1.sql", "M4-F post-release P1 hotfix (round 2 — idempotency)"),
]


# ── Validation regexes ─────────────────────────────────────────────────────
# Container names: lowercase alnum + dash, anchored. Mirrors Docker's own name
# rule but is intentionally stricter (no leading/trailing dash, no underscore)
# so a path-traversal or shell-metachar payload can never reach a docker argv.
# Example valid: "m6rag-eph-1234567890".
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

# Anything that could break out of a single argv token or reference a path is
# forbidden outright, even though argv arrays already neutralize shell
# metacharacters — defense in depth.
_FORBIDDEN_SUBSTRINGS = (";", "|", "&", "`", "$", "(", ")", "{", "}", "<", ">",
                         "*", "?", "\n", "\r", "\t", " ", "..", "/", "\\")

# password=... (DSN fragment or conninfo) must never survive into a log line.
# Matches password='<...>' or password=<word> in either URI or keyword form.
_PASSWORD_RE = re.compile(
    r"(password=)(?:'[^']*'|[^\s;&]+)", re.IGNORECASE,
)
# SQL CREATE ROLE ... LOGIN PASSWORD '<...>' form. Matches the bare SQL keyword
# (PASSWORD followed by a space and a single-quoted literal) so the reader-role
# bootstrap SQL can also be scrubbed before logging.
_SQL_PASSWORD_RE = re.compile(
    r"(PASSWORD\s+)'([^']*)'", re.IGNORECASE,
)


# ── Execution gate ─────────────────────────────────────────────────────────
def check_execution_auth() -> dict:
    """Return whether the ephemeral harness is authorized to execute.

    Authorization requires BOTH:
      1. ``EPHEMERAL_PG_VERIFY=1`` in the environment (explicit opt-in; the
         string "1" exactly — "0", "true", "yes", unset, etc. are all NOT
         authorized). This is a two-key rule: an accidental env var alone must
         never start a container.
      2. The authorized Docker daemon (``MergePilot-Test``) is reachable as
         root. This is checked via ``wsl -u root -d MergePilot-Test docker
         info``. Production daemons (Ubuntu-22.04) are never probed.

    Returns a dict ``{"authorized": bool, "reason": str}``. The reason is safe
    to log (it never contains a secret). This function NEVER raises on a
    daemon-check failure — it returns ``authorized=False`` so the caller can
    skip cleanly.

    Phase A note: this function is defined and callable, but Phase A does NOT
    start a container even when authorized. Container startup is Phase B.
    """
    flag = os.environ.get("EPHEMERAL_PG_VERIFY", "")
    if flag != "1":
        return {
            "authorized": False,
            "reason": (
                "EPHEMERAL_PG_VERIFY is not set to '1' (got %r); ephemeral "
                "execution is NOT authorized" % flag
            ),
        }

    # Daemon reachability probe. Array arguments only — never shell=True.
    # We probe the authorized WSL distribution explicitly so a default-distro
    # docker can never satisfy the check.
    try:
        completed = subprocess.run(
            ["wsl", "-u", "root", "-d", AUTHORIZED_DAEMON, "docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Fail fast — a hung daemon probe must not stall the test suite.
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return {
            "authorized": False,
            "reason": "wsl executable not found; cannot reach %s daemon"
                      % AUTHORIZED_DAEMON,
        }
    except subprocess.TimeoutExpired:
        return {
            "authorized": False,
            "reason": "timed out probing %s daemon" % AUTHORIZED_DAEMON,
        }
    except OSError as exc:
        # Defensive: never let a subprocess probe failure propagate. The gate
        # is fail-closed (authorized=False).
        return {
            "authorized": False,
            "reason": "could not probe %s daemon: %s"
                      % (AUTHORIZED_DAEMON, type(exc).__name__),
        }

    if completed.returncode != 0:
        return {
            "authorized": False,
            "reason": "%s daemon not reachable (docker info rc=%d)"
                      % (AUTHORIZED_DAEMON, completed.returncode),
        }

    return {
        "authorized": True,
        "reason": "EPHEMERAL_PG_VERIFY=1 and %s daemon reachable"
                  % AUTHORIZED_DAEMON,
    }


# ── Command builders (all return lists of argv arrays) ─────────────────────
def build_migration_commands(container: str, db_name: str, user: str,
                             root_path: str) -> list:
    """Return one argv array per migration application in MIGRATION_CHAIN.

    Each returned element is a list of strings suitable for
    ``subprocess.run(element, input=sql_bytes)`` — never a shell string.
    The migration SQL is piped to psql via **stdin** (``-f -``) rather than
    a host file path. The host file path would not exist inside the container;
    using stdin avoids the host-path-as-container-path problem entirely.

    The caller (Phase B executor) reads each migration file's bytes on the
    HOST side and passes them to ``subprocess.run(cmd, input=sql_bytes,
    check=True)``. This function returns the argv only — the caller is
    responsible for reading the file and providing ``input=``.

    Parameters
    ----------
    container:
        Target container name.
    db_name, user:
        Connection target.
    root_path:
        Repo root; used by the caller to locate the SQL files on the host.

    Returns
    -------
    list of list[str]
        ``len == len(MIGRATION_CHAIN)`` (13 audit-db applications).
        Each element ends with ``"-f", "-"`` (read SQL from stdin).
    """
    commands = []
    for _filename, _desc in MIGRATION_CHAIN:
        # psql argv: docker exec -i <container> psql -U <user> -d <db>
        #   -v ON_ERROR_STOP=1 -f -  (read SQL from stdin)
        # The caller reads the host file and passes bytes via input=.
        # No host path appears in the argv.
        commands.append([
            "docker", "exec", "-i", container,
            "psql",
            "-U", user,
            "-d", db_name,
            "-v", "ON_ERROR_STOP=1",
            "-f", "-",
        ])
    return commands


def build_prerequisite_role_sql() -> str:
    """Return the Phase 0 prerequisite-role SQL (idempotent).

    Creates ``policy_gateway_l2`` and ``mergepilot_approver`` as NOLOGIN roles
    if they do not already exist. Mirrors run_schema_foundation.sh:43 exactly.
    This SQL runs BEFORE any audit-db migration (the migrations reference these
    roles in triggers/ownership).
    """
    return (
        "DO $d$ BEGIN "
        "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') "
        "THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF; "
        "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') "
        "THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF; "
        "END $d$;"
    )


def build_reader_role_sql(password: str) -> str:
    """Return the Phase 2 ``CREATE ROLE mergepilot_reader`` SQL.

    The password is interpolated into the SQL as a quoted literal. This is the
    ONLY place the password touches a string, and the returned SQL is meant to
    be piped to psql over stdin (never logged). The caller MUST NOT log the
    return value; :func:`redact_secrets` exists to scrub it if logging is
    unavoidable.

    The role is hardened with every privileged attribute OFF (NOINHERIT,
    NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS) and its
    default transaction is READ ONLY. This matches the design §1 Phase 2.
    """
    # Single-quote-escape the password per PostgreSQL E''/'' literal rules:
    # double internal single-quotes and backslashes. This prevents SQL
    # injection via the password value itself.
    escaped = password.replace("\\", "\\\\").replace("'", "''")
    return (
        "CREATE ROLE %s\n"
        "    LOGIN PASSWORD '%s'\n"
        "    NOINHERIT\n"
        "    NOSUPERUSER\n"
        "    NOCREATEDB\n"
        "    NOCREATEROLE\n"
        "    NOREPLICATION\n"
        "    NOBYPASSRLS;\n"
        "ALTER ROLE %s\n"
        "    SET default_transaction_read_only = on;\n"
        % (CANONICAL_VIEWER_ROLE, escaped, CANONICAL_VIEWER_ROLE)
    )


def build_seed_sql() -> str:
    """Return deterministic synthetic seed INSERTs for all 5 runs.

    Returns a single SQL script (string) that populates the 5 runs defined in
    the design §3. Every value is synthetic. All FKs resolve, all SHAs are
    40-char lowercase hex, all digests are 64-char lowercase hex, and every
    NOT NULL / CHECK constraint is satisfied.

    The 5 runs:
      1. ``run-eph-ok``      — success (status=PASS, full chain)
      2. ``run-eph-unknown`` — unknown status (status=NULL)
      3. ``run-eph-no-rev``  — missing revision binding
      4. ``run-eph-rollback``— rollback (status=ROLLED_BACK, rollback_runs row)
      5. ``run-eph-missing`` — no task_runs row (deliberately absent)

    Revision binding for run-eph-ok is created via a DIRECT-ADMIN INSERT (the
    design's Option B fallback). The preferred path is ``bind_revision()``;
    when that cannot be called, the harness records
    ``revision_producer_contract=NOT_VERIFIED``. This seed uses the fallback so
    the consumer/read path is exercised regardless.

    run-eph-ok also seeds 5 immutable ``audit_events`` rows (one per closed-loop
    step: review/fix/verify/merge/close_pr), matching the design §3 Run-1 table.
    These exercise the read path on the 9th queried table; they do NOT verify
    the audit producer contract (no controller write path is invoked).
    """
    # Pre-compute the source_evidence_digest for run-eph-ok so the
    # revision_bindings row is internally consistent (the value a
    # bind_revision() call would have recomputed and demanded).
    digest = compute_revision_digest(
        source_call_id="mcp-eph-001",
        correlation_id="corr-eph-001",
        tool="create_pull_request",
        target_repo="test/repo-alpha",
        run_id="run-eph-ok",
        git_sha="a" * 40,  # base_sha (matches mcp_calls.git_sha)
        result_status="OK",
    )

    # 40-char hex SHAs (deterministic, distinct per slot).
    base_sha = "a" * 40            # run-eph-ok base (== mcp_calls.git_sha)
    head_sha = "b" * 40            # run-eph-ok head
    revert_merge_sha = "d" * 40    # run-eph-rollback reverted merge

    return (
        "-- Deterministic synthetic seed (Phase A; all values synthetic).\n"
        "-- environment marker (single row; unique index enforces)\n"
        "INSERT INTO environment_identity (environment_id) VALUES\n"
        "    ('%s')\n"
        "ON CONFLICT DO NOTHING;\n"
        "\n"
        "-- ── Run 1: run-eph-ok (success) ──\n"
        "INSERT INTO task_runs (run_id, room_id, repo, pr_number, branch, status,\n"
        "    current_stage, attempt, verdict, skill_data_state)\n"
        "VALUES ('run-eph-ok', 'room-eph-ok', 'test/repo-alpha', 42, 'fix/run-eph-ok',\n"
        "    'PASS', 'verify', 1, 'PASS', 'ACTIVE');\n"
        "\n"
        "INSERT INTO run_pr_bindings (binding_id, run_id, repo, pr_number,\n"
        "    fix_branch, base_branch, head_sha)\n"
        "VALUES ('prb-eph-ok', 'run-eph-ok', 'test/repo-alpha', 42,\n"
        "    'fix/run-eph-ok', 'main', '%s');\n"
        "\n"
        "INSERT INTO mcp_calls (request_id, correlation_id, phase, caller_agent,\n"
        "    tool, decision, result_status, run_id, target_repo, git_sha, error)\n"
        "VALUES ('mcp-eph-001', 'corr-eph-001', 'RESULT', 'coordinator',\n"
        "    'create_pull_request', 'ALLOW', 'OK', 'run-eph-ok',\n"
        "    'test/repo-alpha', '%s', NULL);\n"
        "\n"
        "-- revision_bindings (direct-admin fallback; Option B).\n"
        "-- revision_producer_contract = NOT_VERIFIED when this path is used.\n"
        "INSERT INTO revision_bindings (binding_id, run_id, repo, pr_number,\n"
        "    base_sha, head_sha, source_call_id, source_evidence_digest)\n"
        "VALUES ('rev-eph-ok-0000000000000000000000000000', 'run-eph-ok',\n"
        "    'test/repo-alpha', 42, '%s', '%s', 'mcp-eph-001', '%s');\n"
        "\n"
        "INSERT INTO stage_runs (run_id, stage, agent, attempt, status, verdict)\n"
        "VALUES ('run-eph-ok', 'review', 'reviewer', 1, 'COMPLETED', NULL),\n"
        "       ('run-eph-ok', 'fix',    'fixer',    1, 'COMPLETED', NULL),\n"
        "       ('run-eph-ok', 'verify', 'verifier', 1, 'COMPLETED', 'PASS');\n"
        "\n"
        "INSERT INTO stage_events (event_id, room_id, run_id, event_type, stage,\n"
        "    status, sender)\n"
        "VALUES ('evt-eph-ok-r1', 'room-eph-ok', 'run-eph-ok', 'M4F_REVIEW_DISPATCH',\n"
        "        'review', 'PROCESSED', 'controller'),\n"
        "       ('evt-eph-ok-r2', 'room-eph-ok', 'run-eph-ok', 'M4F_FIX_DISPATCH',\n"
        "        'fix', 'PROCESSED', 'controller'),\n"
        "       ('evt-eph-ok-r3', 'room-eph-ok', 'run-eph-ok', 'M4F_VERIFY_DISPATCH',\n"
        "        'verify', 'PROCESSED', 'controller');\n"
        "\n"
        "-- audit_events: one immutable row per closed-loop step for run-eph-ok.\n"
        "-- DDL (init.sql:52): task_id/agent/action/target/detail/sha/via,\n"
        "-- ts DEFAULT now(). task_id mirrors run_id (design §3 audit_events.task_id\n"
        "-- vs run_id). agent per init.sql comment (reviewer/fixer/verifier/manager/\n"
        "-- system); via per init.sql comment (github-mcp/sast-scan/matrix/pg).\n"
        "-- sha reuses the synthetic head_sha for merge/close_pr (the commit the\n"
        "-- reader surfaces as source_commit); review/fix/verify carry the same\n"
        "-- head since this synthetic run has a single commit.\n"
        "INSERT INTO audit_events (task_id, agent, action, target, detail, sha, via)\n"
        "VALUES ('run-eph-ok', 'reviewer', 'review',   'test/repo-alpha#42',\n"
        "        'synthetic review completed', '%s', 'github-mcp'),\n"
        "       ('run-eph-ok', 'fixer',    'fix',      'test/repo-alpha#42',\n"
        "        'synthetic fix applied',    '%s', 'github-mcp'),\n"
        "       ('run-eph-ok', 'verifier', 'verify',   'test/repo-alpha#42',\n"
        "        'synthetic verify passed',  '%s', 'sast-scan'),\n"
        "       ('run-eph-ok', 'manager',  'merge',    'test/repo-alpha#42',\n"
        "        'synthetic merge succeeded','%s', 'github-mcp'),\n"
        "       ('run-eph-ok', 'system',   'close_pr', 'test/repo-alpha#42',\n"
        "        'synthetic pr closed',     '%s', 'github-mcp');\n"
        "\n"
        "-- ── Run 2: run-eph-unknown (status=NULL) ──\n"
        "INSERT INTO task_runs (run_id, status)\n"
        "VALUES ('run-eph-unknown', NULL);\n"
        "\n"
        "-- ── Run 3: run-eph-no-rev (PASS, no revision binding, no mcp_call) ──\n"
        "INSERT INTO task_runs (run_id, repo, pr_number, status, skill_data_state)\n"
        "VALUES ('run-eph-no-rev', 'test/repo-alpha', 7, 'PASS', 'ACTIVE');\n"
        "\n"
        "-- ── Run 4: run-eph-rollback (status=ROLLED_BACK) ──\n"
        "INSERT INTO task_runs (run_id, room_id, repo, pr_number, status,\n"
        "    skill_data_state)\n"
        "VALUES ('run-eph-rollback', 'room-eph-rb', 'test/repo-alpha', 42,\n"
        "    'ROLLED_BACK', 'ACTIVE');\n"
        "\n"
        "INSERT INTO stage_events (event_id, room_id, run_id, event_type, stage,\n"
        "    status, sender, error)\n"
        "VALUES ('evt-eph-rb1', 'room-eph-rb', 'run-eph-rollback',\n"
        "        'POST_MERGE_VERIFY_FAILED', 'verify', 'PROCESSED', 'verifier',\n"
        "        'test failure');\n"
        "\n"
        "-- rollback_runs.status='REVERTED' (valid CHECK value; NOT 'COMPLETED').\n"
        "INSERT INTO rollback_runs (rollback_id, parent_run_id, reverted_merge_sha,\n"
        "    repo, pr_number, trigger_event_id, status, fail_reason)\n"
        "VALUES ('rb-eph-rb1', 'run-eph-rollback', '%s',\n"
        "    'test/repo-alpha', 42, 'evt-eph-rb1', 'REVERTED', 'test_failure');\n"
        "\n"
        "-- ── Run 5 (missing) ──\n"
        "-- No task_runs row inserted for the missing run; the reader source\n"
        "-- must return RUN_NOT_FOUND for its run_id. (No INSERT follows.)\n"
        % (
            ENVIRONMENT_ID_EPHEMERAL,
            head_sha,
            base_sha,           # mcp_calls.git_sha == base_sha
            base_sha,           # revision_bindings.base_sha
            head_sha,           # revision_bindings.head_sha
            digest,             # source_evidence_digest
            head_sha,           # audit_events[0] review  — synthetic head_sha
            head_sha,           # audit_events[1] fix     — synthetic head_sha
            head_sha,           # audit_events[2] verify  — synthetic head_sha
            head_sha,           # audit_events[3] merge   — synthetic head_sha
            head_sha,           # audit_events[4] close_pr— synthetic head_sha
            revert_merge_sha,
        )
    )


def build_cleanup_commands(container_name: str, label: str) -> list:
    """Return docker cleanup argv arrays for a container + its label.

    Returns a list of argv lists (never shell strings). The caller runs each in
    order. Every command is validated: the container name must pass
    :func:`validate_container_name`, and the label is checked for the same
    forbidden substrings. A malformed name raises ``ValueError`` BEFORE any
    docker process is spawned.

    Cleanup commands:
      1. ``docker rm -f <container_name>``   (remove the exact container)
      2. ``docker ps -a --filter name=<container_name>``  (verify none remain)
      3. ``docker network ls --filter label=<label>``     (verify no networks)
      4. ``docker network prune -f --filter label=<label>`` (remove labeled nets)
    """
    if not validate_container_name(container_name):
        raise ValueError(
            "refusing to build cleanup for invalid container name: %r"
            % (container_name,)
        )
    # Label is a free-form string but must not carry shell/path escape chars.
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in label:
            raise ValueError(
                "refusing to build cleanup: label contains forbidden substring"
            )

    return [
        ["docker", "rm", "-f", container_name],
        ["docker", "ps", "-a", "--filter", "name=" + container_name],
        ["docker", "network", "ls", "--filter", "label=" + label],
        ["docker", "network", "prune", "-f", "--filter", "label=" + label],
    ]


# ── Validation / redaction helpers ─────────────────────────────────────────
def validate_container_name(name: str) -> bool:
    r"""Return True iff ``name`` is a safe container name.

    Rules (defense in depth — argv arrays already neutralize shell
    metacharacters, but we reject them anyway):
      - non-empty str
      - matches ``^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$`` (2..64 chars)
      - contains no path traversal (``..``, ``/``, ``\``)
      - contains no shell metacharacters or whitespace
    """
    if not isinstance(name, str) or not name:
        return False
    if not _CONTAINER_NAME_RE.match(name):
        return False
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in name:
            return False
    return True


def redact_secrets(text: str) -> str:
    """Replace ``password=<...>`` patterns with ``password=***REDACTED***``.

    Used before logging any string that may have ingested a DSN fragment or the
    reader-role bootstrap SQL. The match is case-insensitive and covers:
      - ``password='...'`` (conninfo quoted) and ``password=word`` (URI/keyword)
      - ``PASSWORD '...'`` (SQL ``CREATE ROLE ... LOGIN PASSWORD '...'``)
    """
    if not isinstance(text, str):
        return text
    out = _PASSWORD_RE.sub(r"\1***REDACTED***", text)
    out = _SQL_PASSWORD_RE.sub(r"\1'***REDACTED***'", out)
    return out


# ── Server identity + digest (Phase B placeholders / pure helpers) ─────────
def measure_server_identity(admin_dsn: str) -> dict:
    """NOT EXECUTED in Phase A. Returns a placeholder dict.

    In Phase B this would connect with ``admin_dsn`` and run::

        SELECT inet_server_addr()::text, inet_server_port()

    to measure the actual server the container published, then freeze those
    values as ``expected_server_addresses`` / ``expected_server_port`` for the
    ``PostgresSnapshotSource`` constructor. The design (§4 "Server Identity
    Design") explicitly forbids hard-coding ``127.0.0.1``.

    Phase A: returns ``{"executed": False, "reason": "NOT_EXECUTED"}`` without
    opening any connection. The ``admin_dsn`` is accepted by signature only and
    is NEVER stored on the module or logged.
    """
    return {
        "executed": False,
        "reason": "NOT_EXECUTED",
        "server_addr": None,
        "server_port": None,
    }


def _canon_str(value: str) -> str:
    """Python mirror of PostgreSQL ``public._canon_str(text)``.

    Algorithm (from m4f1_state.sql):
      - NULL  → ``-1:``
      - else  → ``<octet_length_in_utf8>:<value>``

    ``octet_length`` is the byte length of the UTF-8 encoding (NOT the
    character count). This matches ``octet_length(v)::text || ':' || v``.
    """
    if value is None:
        return "-1:"
    encoded = value.encode("utf-8")
    return "%d:%s" % (len(encoded), value)


def compute_revision_digest(source_call_id: str, correlation_id: str,
                            tool: str, target_repo: str, run_id: str,
                            git_sha: str, result_status: str) -> str:
    """Compute the ``source_evidence_digest`` from the bind_revision algorithm.

    Mirrors the canonical recomputation in ``public.bind_revision`` (m4f1_state
    .sql, step 4):

    .. code-block:: sql

        v_recomputed := encode(digest(
            _canon_str(p_source_call_id) ||
            _canon_str(v_mc.correlation_id) ||
            _canon_str(v_mc.tool) ||
            _canon_str(v_mc.target_repo) ||
            _canon_str(v_mc.run_id) ||
            _canon_str(v_mc.git_sha) ||
            _canon_str(v_mc.result_status),
            'sha256'), 'hex');

    where ``digest(..., 'sha256')`` is ``pgcrypto``'s raw digest and
    ``_canon_str`` is the length-prefixed canonical string (NULL → ``-1:``).

    Returns the 64-char lowercase hex SHA256 of the canonical concatenation.
    Pure and deterministic: identical inputs always yield identical output.
    """
    canon = (
        _canon_str(source_call_id)
        + _canon_str(correlation_id)
        + _canon_str(tool)
        + _canon_str(target_repo)
        + _canon_str(run_id)
        + _canon_str(git_sha)
        + _canon_str(result_status)
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
