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
    "a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
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
# Phase B hardening (Fix 2): the authorization gate verifies, in order:
#   1. EPHEMERAL_PG_VERIFY == "1" (exact) — no Docker probe if unset
#   2. MergePilot-Test exists in `wsl -l -v` AND is initially Running
#   3. Ubuntu-22.04 state is recorded but NEVER invoked
#   4. all Docker commands go through `wsl -u root -d MergePilot-Test -- docker`
#   5. DOCKER_HOST is empty or a local unix socket
#   6. docker context endpoint == unix:///var/run/docker.sock (no TCP/SSH/remote)
#   7. daemon fingerprint (Server ID, Name, Root Dir, Version) is present
#   8. IMAGE_DIGEST is cached locally
# The result carries a structured, secret-free fingerprint for pre/post compare.
_APPROVED_ENDPOINT = "unix:///var/run/docker.sock"


def _run_wsl_text(argv: list, timeout: int = 15) -> tuple[int, str, str] | None:
    """Run a wsl.exe command, return (rc, stdout, stderr) or None on OS error."""
    try:
        cp = subprocess.run(
            ["wsl.exe"] + argv,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    return (
        cp.returncode,
        cp.stdout.decode("utf-8", "replace") if cp.stdout else "",
        cp.stderr.decode("utf-8", "replace") if cp.stderr else "",
    )


def _wsl_distro_states() -> dict:
    """Return {distro_name: state} from `wsl -l -v` (read-only). Empty on error.

    Robust parsing (Fix 1, second review round):
    - Handles UTF-16LE/NUL-style output from wsl.exe (strips NUL bytes; the
      caller's _run_wsl_text already decodes, but defensive NUL-strip is kept).
    - Strips an optional leading ``*`` (marks the default distro) and
      surrounding whitespace before parsing — so a default distro line like
      ``* MergePilot-Test  Stopped  2`` parses with name ``MergePilot-Test``,
      not ``* MergePilot-Test``.
    - Header-agnostic: does NOT require an English ``NAME`` header; it simply
      ignores any line whose trailing three tokens are not
      ``<name> <Running|Stopped> <version>``. This means malformed/header/blank
      lines are ignored rather than mis-parsed.
    - distro names may contain spaces (the state is the second-to-last token,
      version the last; the name is everything before).
    - Fail-closed: if parsing is unreliable for a line, that line contributes
      nothing (no guessing).
    """
    res = _run_wsl_text(["-l", "-v"])
    if res is None:
        return {}
    _rc, out, _err = res
    states: dict = {}
    for line in out.splitlines():
        # Strip UTF-16 NULs defensively, then whitespace.
        clean = line.replace("\x00", "").strip()
        if not clean:
            continue
        # Remove a leading '*' (default-distro marker) plus following space(s).
        if clean.startswith("*"):
            clean = clean.lstrip("*").strip()
        parts = clean.split()
        # Require at least 3 tokens: <name...> <state> <version>.
        if len(parts) < 3:
            continue
        state = parts[-2]
        # Accept ONLY Running|Stopped (case-insensitive) as the state token.
        if state.lower() not in ("running", "stopped"):
            continue
        # Version (last token) must be an integer.
        if not parts[-1].isdigit():
            continue
        name = " ".join(parts[:-2]).strip()
        if not name:
            continue
        # Preserve the canonical casing of the state as emitted.
        states[name] = state
    return states


def check_execution_auth() -> dict:
    """Return whether the ephemeral harness is authorized to execute.

    Phase B authorization (Fix 2) — all of the following must hold, checked in
    order, fail-closed at the first miss (NO Docker command runs if an earlier
    gate fails):

      1. ``EPHEMERAL_PG_VERIFY == "1"`` (exact string; "0"/"true"/unset refuse).
      2. MergePilot-Test is present in ``wsl -l -v`` AND its initial state is
         ``Running`` (a Stopped distro is NOT implicitly started; the harness
         refuses). Ubuntu-22.04 state is recorded but NEVER invoked.
      3. The Docker context endpoint is exactly ``unix:///var/run/docker.sock``
         (TCP/SSH/remote endpoints are refused). ``DOCKER_HOST`` (read inside
         the distro) must be empty or a local unix socket.
      4. The daemon fingerprint (Server ID, Name, Docker Root Dir, Version) is
         present and returned for pre/post-execution comparison.
      5. ``IMAGE_DIGEST`` is cached locally (no pull).

    Returns a dict with at least ``{"authorized": bool, "reason": str}``. When
    authorized, it also carries ``"fingerprint"`` (a secret-free dict) for the
    executor to save pre-execution and compare after cleanup. The reason is
    safe to log. This function NEVER raises — it returns ``authorized=False``.
    """
    flag = os.environ.get("EPHEMERAL_PG_VERIFY", "")
    if flag != "1":
        return {
            "authorized": False,
            "reason": (
                "EPHEMERAL_PG_VERIFY is not set to '1' (got %r); ephemeral "
                "execution is NOT authorized; no Docker command was issued"
                % flag
            ),
        }

    # Gate 2: distro states (read-only; does NOT start any distro).
    states = _wsl_distro_states()
    ubuntu_state = states.get("Ubuntu-22.04", "UNKNOWN")
    if AUTHORIZED_DAEMON not in states:
        return {
            "authorized": False,
            "reason": ("%s not present in `wsl -l -v`; NOT authorized "
                       "(Ubuntu-22.04 state recorded: %s, never invoked)"
                       % (AUTHORIZED_DAEMON, ubuntu_state)),
            "ubuntu_state": ubuntu_state,
        }
    if states[AUTHORIZED_DAEMON] != "Running":
        return {
            "authorized": False,
            "reason": ("%s initial state is %s (must be Running); NOT "
                       "authorized — distro is NOT implicitly started"
                       % (AUTHORIZED_DAEMON, states[AUTHORIZED_DAEMON])),
            "ubuntu_state": ubuntu_state,
        }

    # Gate 3: Docker endpoint via the authorized distro.
    ep_res = _run_wsl_text(
        ["-u", "root", "-d", AUTHORIZED_DAEMON, "--",
         "docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        timeout=15)
    if ep_res is None:
        return {"authorized": False,
                "reason": "could not probe docker context endpoint (wsl/docker error)",
                "ubuntu_state": ubuntu_state}
    ep_rc, ep_out, _ep_err = ep_res
    if ep_rc != 0:
        return {"authorized": False,
                "reason": "docker context inspect failed (rc=%d)" % ep_rc,
                "ubuntu_state": ubuntu_state}
    endpoint = ep_out.strip()
    if endpoint != _APPROVED_ENDPOINT:
        return {"authorized": False,
                "reason": ("docker endpoint is %s, must be %s (no TCP/SSH/remote)"
                           % (endpoint, _APPROVED_ENDPOINT)),
                "ubuntu_state": ubuntu_state}
    # DOCKER_HOST inside the distro (must be empty or local unix socket).
    dh_res = _run_wsl_text(
        ["-u", "root", "-d", AUTHORIZED_DAEMON, "--",
         "bash", "-c", "echo \"${DOCKER_HOST:-}\""],
        timeout=10)
    if dh_res is None:
        return {"authorized": False,
                "reason": "could not read DOCKER_HOST in distro",
                "ubuntu_state": ubuntu_state}
    _dh_rc, dh_out, _dh_err = dh_res
    docker_host = dh_out.strip()
    # DOCKER_HOST allowlist (Fix 2, second review): ONLY empty or the exact
    # approved local socket. Reject any other unix socket (e.g. /tmp/docker.sock,
    # /var/run/other.sock), tcp://, ssh://, npipe://, and whitespace variants.
    if docker_host not in ("", _APPROVED_ENDPOINT):
        return {"authorized": False,
                "reason": ("DOCKER_HOST=%r is not empty or exactly %s "
                           "(no other socket/tcp/ssh/npipe)" % (docker_host, _APPROVED_ENDPOINT)),
                "ubuntu_state": ubuntu_state}

    # Gate 4: daemon fingerprint.
    info_res = _run_wsl_text(
        ["-u", "root", "-d", AUTHORIZED_DAEMON, "--", "docker", "info"],
        timeout=15)
    if info_res is None:
        return {"authorized": False,
                "reason": "could not probe docker info (wsl/docker error)",
                "ubuntu_state": ubuntu_state}
    info_rc, info_out, _info_err = info_res
    if info_rc != 0:
        return {"authorized": False,
                "reason": "docker info failed (rc=%d)" % info_rc,
                "ubuntu_state": ubuntu_state}
    fingerprint = _parse_daemon_fingerprint(info_out)
    missing = [k for k, v in fingerprint.items() if not v]
    if missing:
        return {"authorized": False,
                "reason": "daemon fingerprint missing fields: %s" % missing,
                "ubuntu_state": ubuntu_state}

    # Gate 5: IMAGE_DIGEST cached + capture the local Image ID (Fix 1, final review).
    img_res = _run_wsl_text(
        ["-u", "root", "-d", AUTHORIZED_DAEMON, "--",
         "docker", "image", "inspect", IMAGE_DIGEST, "--format", "{{.Id}}"],
        timeout=15)
    if img_res is None:
        return {"authorized": False,
                "reason": "could not probe cached image digest (wsl/docker error)",
                "ubuntu_state": ubuntu_state}
    img_rc, img_out, _img_err = img_res
    if img_rc != 0:
        return {"authorized": False,
                "reason": "approved image digest not cached locally (no pull)",
                "ubuntu_state": ubuntu_state}
    image_id = img_out.strip()
    # Must be a valid sha256 Image ID (non-empty, sha256:<hex> or <hex>).
    if not image_id or not re.match(r"^(sha256:)?[0-9a-f]{12,64}$", image_id):
        return {"authorized": False,
                "reason": "image inspect returned an invalid Image ID",
                "ubuntu_state": ubuntu_state}

    return {
        "authorized": True,
        "reason": "EPHEMERAL_PG_VERIFY=1, %s Running, endpoint %s, fingerprint ok, "
                  "image cached" % (AUTHORIZED_DAEMON, _APPROVED_ENDPOINT),
        "fingerprint": fingerprint,
        "ubuntu_state": ubuntu_state,
        "authorized_distro_state": states[AUTHORIZED_DAEMON],
        "endpoint": endpoint,
        "docker_host": docker_host,
        "image_digest": IMAGE_DIGEST,
        "image_id": image_id,
    }


def _parse_daemon_fingerprint(info_text: str) -> dict:
    """Extract a secret-free fingerprint from `docker info` output."""
    fp = {"server_id": "", "name": "", "docker_root_dir": "", "version": ""}
    for line in info_text.splitlines():
        s = line.strip()
        if s.startswith("ID:"):
            fp["server_id"] = s.split(":", 1)[1].strip()
        elif s.startswith("Server Version:"):
            fp["version"] = s.split(":", 1)[1].strip()
        elif s.startswith("Docker Root Dir:"):
            fp["docker_root_dir"] = s.split(":", 1)[1].strip()
        elif s.startswith("Name:") and not fp["name"]:
            fp["name"] = s.split(":", 1)[1].strip()
    return fp


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
    # Delegate to the structured parts generator (Fix 7); the parts are built
    # structurally so the Option A bind_revision ordering is unambiguous, and
    # their concatenation is byte-identical to the historical monolithic seed.
    before, option_b, after = build_seed_sql_parts()
    return before + option_b + after


def build_seed_sql_parts():
    """Return the seed SQL as three structured parts (Phase B, Fix 7).

    This replaces the fragile text-parsing split. The parts are built
    STRUCTURALLY (not by parsing the final SQL) so that the Option A
    ``bind_revision()`` ordering is unambiguous:

    - ``before_bind_sql``: environment marker + task_runs/run_pr_bindings/
      mcp_calls for run-eph-ok (the provenance rows ``bind_revision`` requires).
      Does NOT contain the ``revision_bindings`` INSERT.
    - ``option_b_revision_sql``: the direct-admin ``revision_bindings`` INSERT
      (Option B fallback). Applied ONLY if Option A fails. Contains the
      ``-- revision_producer_contract = NOT_VERIFIED`` marker.
    - ``after_bind_sql``: stage_runs/stage_events/audit_events for run-eph-ok,
      then runs 2-5 (unknown/no-rev/rollback/missing). Does NOT contain the
      ``revision_bindings`` INSERT.

    ``build_seed_sql()`` returns ``before + option_b + after`` so the existing
    static-contract tests stay byte-identical.
    """
    digest = compute_revision_digest(
        source_call_id="mcp-eph-001",
        correlation_id="corr-eph-001",
        tool="create_pull_request",
        target_repo="test/repo-alpha",
        run_id="run-eph-ok",
        git_sha="a" * 40,
        result_status="OK",
    )
    base_sha = "a" * 40
    head_sha = "b" * 40
    revert_merge_sha = "d" * 40

    # ── before_bind_sql ──
    before_bind_sql = (
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
        % (ENVIRONMENT_ID_EPHEMERAL, head_sha, base_sha)
    )

    # ── option_b_revision_sql (Option B fallback; NOT applied if Option A OK) ──
    option_b_revision_sql = (
        "-- revision_bindings (direct-admin fallback; Option B).\n"
        "-- revision_producer_contract = NOT_VERIFIED when this path is used.\n"
        "INSERT INTO revision_bindings (binding_id, run_id, repo, pr_number,\n"
        "    base_sha, head_sha, source_call_id, source_evidence_digest)\n"
        "VALUES ('rev-eph-ok-0000000000000000000000000000', 'run-eph-ok',\n"
        "    'test/repo-alpha', 42, '%s', '%s', 'mcp-eph-001', '%s');\n"
        "\n"
        % (base_sha, head_sha, digest)
    )

    # ── after_bind_sql ──
    after_bind_sql = (
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
            head_sha,           # audit_events[0] review
            head_sha,           # audit_events[1] fix
            head_sha,           # audit_events[2] verify
            head_sha,           # audit_events[3] merge
            head_sha,           # audit_events[4] close_pr
            revert_merge_sha,
        )
    )
    return before_bind_sql, option_b_revision_sql, after_bind_sql


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
