"""Phase B real executor for the ISOLATED_LIVE ephemeral PostgreSQL verification.

This module implements the one-shot ephemeral-container session that Phase A
declared but never executed. It is the ONLY component that touches a live
PostgreSQL, and it does so against an authorized disposable Docker container on
the MergePilot-Test WSL distro — never against a production database and never
against the real MergePilot-Test application database.

Hard security invariants (enforced throughout):

* Passwords NEVER appear in argv. The admin password is delivered to the
  container via ``docker run --env-file /dev/stdin`` with the env bytes piped
  through ``subprocess.run(input=...)``. The reader password travels only inside
  ``psql`` SQL piped over stdin (``CREATE ROLE``/``ALTER ROLE``).
* Every argv is checked by :meth:`_assert_argv_safe` BEFORE execution: it must
  not contain the admin password, the reader password, a full DSN, or a SQL
  ``PASSWORD`` literal.
* Every collected stdout/stderr fragment is passed through
  :func:`ephemeral_harness.redact_secrets` IMMEDIATELY on collection — raw
  output is never retained.
* All Docker commands are routed through ``wsl -u root -d MergePilot-Test --``
  (the authorized daemon). No TCP/SSH/remote Docker endpoint is ever used.
* The container binds ONLY ``-p 127.0.0.1::5432`` (IPv4 loopback, auto port).
  No ``0.0.0.0``, ``::``, LAN address, or ``-p 0:5432``.
* Image reference uses ``--pull=never``; the digest-pinned image must already be
  cached (verified by the caller's preflight).
* No persistent volumes are created (``PGDATA=/tmp/pgdata``; ``docker rm -fv``).
* ``host_address``/``host_port`` (Windows psycopg2 DSN) are STRICTLY SEPARATED
  from ``server_address``/``server_port`` (measured via real TCP
  ``inet_server_addr()``/``inet_server_port()`` and used as
  ``expected_server_*``). The random host port is NEVER used as
  ``expected_server_port``.

Boundary honesty: this executor verifies the CONSUMER/read path and (optionally)
the ``bind_revision`` producer contract on a disposable container. It does NOT
verify the MergePilot-Test application database, production, or the controller's
audit-event write path. See the Phase B doc for the precise classification.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path

# ── sys.path setup (mirror ephemeral_harness.py so imports resolve identically) ──
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # tests/isolated_live → repo root
_DEMO_CONSOLE = _REPO_ROOT / "tools" / "demo_console"
for _p in (str(_HERE), str(_REPO_ROOT), str(_DEMO_CONSOLE)):
    import sys
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ephemeral_harness import (  # noqa: E402
    AUTHORIZED_DAEMON,
    CANONICAL_VIEWER_ROLE,
    ENVIRONMENT_ID_EPHEMERAL,
    IMAGE_DIGEST,
    ISOLATED_LIVE_MIGRATIONS,
    MIGRATION_CHAIN,
    build_cleanup_commands,
    build_migration_commands,
    build_prerequisite_role_sql,
    build_reader_role_sql,
    build_seed_sql,
    build_seed_sql_parts,
    check_execution_auth,
    compute_revision_digest,
    redact_secrets,
    validate_container_name,
)

# The image is referenced ONLY by its digest-pinned form (``repo@sha256:...``).
# Per the Phase B re-review (Fix 1): the executor MUST start the container with
# the digest directly — NOT a floating tag with a post-hoc digest check. If the
# digest form cannot be used (CLI mangling), the executor is BLOCKED; there is
# NO tag fallback. The approved Image ID is resolved via a pre-start
# ``docker image inspect IMAGE_DIGEST`` and the running container's ``.Image``
# must equal it exactly.
#
# The ``@sha256:`` digest is forwarded intact through ``wsl.exe ... -- docker
# run`` because the digest is passed as the FINAL argv element (a single token
# with no shell metachars) — the earlier mangling only affected format-string
# templates, not the digest token itself.
_APPROVED_LOCAL_IMAGE_ID: str | None = None  # resolved at start() time

# Phase B base commit: the executor verifies every migration file's working-tree
# content is byte-identical to this commit's git blob (Fix 3). No glob, no
# symlink, no path escape.
PHASE_B_BASE_COMMIT = "7c5630a6f2f6c5049f028312caf895cf8cd2cbc9"

# Application/database constants for the ephemeral session. These mirror the
# production bootstrap convention in tests/m4f1/run_schema_foundation.sh:
# POSTGRES_USER=mergepilot (the bootstrap superuser + object owner referenced by
# the audit-db migrations, e.g. ``ALTER FUNCTION ... OWNER TO mergepilot``),
# POSTGRES_DB=mergepilot_audit. The migrations hard-reference the ``mergepilot``
# role, so it MUST be the bootstrap user (auto-created by the PostgreSQL image
# init when POSTGRES_USER names it). Using the default ``postgres`` user leaves
# ``mergepilot`` absent and migrations fail at ownership GRANTs.
_APPLICATION_NAME = "mergepilot_isolated_live"
_DB_NAME = "mergepilot_audit"
_ADMIN_USER = "mergepilot"
_PSQL_ON_ERROR = ["-v", "ON_ERROR_STOP=1"]
_READINESS_TOTAL_TIMEOUT = 90.0   # seconds, total wait for pg_isready + SELECT 1
_READINESS_POLL_INTERVAL = 1.0    # seconds between readiness polls
_READINESS_CMD_TIMEOUT = 15       # seconds, per readiness subprocess
_RUN_PSQL_DEFAULT_TIMEOUT = 60    # seconds, per psql invocation

# ── Exceptions ──────────────────────────────────────────────────────────────

class EphemeralExecutionError(Exception):
    """A stable, redacted error from the ephemeral executor.

    The ``code`` is a short stable string (safe to log/report). The message is
    passed through :func:`redact_secrets` and NEVER contains the raw subprocess
    output, passwords, DSNs, or full SQL.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(redact_secrets("[%s] %s" % (code, message)))

    @property
    def safe_repr(self) -> str:
        return "EphemeralExecutionError(code=%r)" % (self.code,)


class EphemeralExecutionAndCleanupError(Exception):
    """Carries BOTH a primary and a cleanup error code (Python 3.9 has no
    ExceptionGroup). Neither error swallows the other.

    Attributes
    ----------
    primary_error_code: str
        Stable code of the failure that occurred during execution/testing.
    cleanup_error_code: str
        Stable code of the failure that occurred during cleanup (or ``""`` if
        cleanup succeeded).
    """

    def __init__(self, primary_error_code: str, cleanup_error_code: str):
        self.primary_error_code = primary_error_code
        self.cleanup_error_code = cleanup_error_code
        super().__init__(
            redact_secrets(
                "primary=%s cleanup=%s" % (primary_error_code, cleanup_error_code)
            )
        )


# ── Executor ────────────────────────────────────────────────────────────────

class EphemeralExecutor:
    """One-shot ephemeral PostgreSQL session on the MergePilot-Test daemon.

    Construction stores configuration ONLY — it does NOT touch Docker. The
    caller drives the lifecycle via :meth:`start_and_prepare`,
    :meth:`run_verification`, and :meth:`cleanup_and_verify`.

    A single instance MUST be used for one session. :meth:`cleanup_and_verify`
    is idempotent and tolerates a partially-started session (so it is safe to
    register via ``unittest.TestCase.addClassCleanup`` before any resource is
    created).
    """

    def __init__(self, repo_root: str, authorization_context: dict | None = None):
        import copy as _copy
        self._repo_root = Path(repo_root).resolve()
        # Authorization context (Fix 3, second review + Fix 1 final review): the
        # structured result of check_execution_auth(). Deep-copied so the
        # executor never holds the caller's mutable dict reference and never
        # mutates the caller's dict. start() refuses to touch Docker unless this
        # is present, authorized, and complete.
        self._authorization_context: dict | None = (
            _copy.deepcopy(authorization_context)
            if isinstance(authorization_context, dict) else None
        )
        self._container_name: str | None = None
        self._label: str | None = None
        self._container_id: str | None = None
        self._admin_password: str | None = None   # ephemeral, never in argv
        self._reader_password: str | None = None  # ephemeral, never in argv
        self._host_address: str | None = None     # 127.0.0.1 (Windows DSN)
        self._host_port: int | None = None        # auto-assigned (Windows DSN)
        self._server_address: str | None = None   # inet_server_addr (expected)
        self._server_port: int | None = None      # inet_server_port (expected)
        # Collected, pre-redacted log lines (for the no-password-in-logs test).
        self._collected_logs: list[str] = []
        self._started = False
        self._cleaned = False
        # Fix 2 (final review): True only after the first real WSL/Docker command
        # runs. cleanup's environment post-recheck is ONLY allowed when True, so
        # an AUTH_CONTEXT_INVALID (pre-Docker) failure never implicitly starts a
        # distro during cleanup.
        self._environment_touched = False
        # Anonymous volumes associated with this session's container (recorded
        # before removal, verified absent after). Fix 4.
        self._anonymous_volumes: list[str] = []
        # Set of operations applied during prepare() (for migration count audit).
        self.operations_applied: list[str] = []
        # Reader source + HTTP server handles (for cleanup during shutdown).
        self._http_server = None
        self._poller = None
        self._reader_sources: list = []  # open psycopg2-backed sources to close
        # Option A outcome.
        self.bind_revision_outcome: dict = {
            "option_a_attempted": False,
            "option_a_succeeded": False,
            "option_a_error_code": "",
            "ephemeral_bind_revision_contract_verified": False,
        }

    # ── log collection (immediate redaction) ────────────────────────────────

    def _log(self, raw: str) -> None:
        """Record a log fragment AFTER immediate redaction. Raw is dropped."""
        if raw is None:
            return
        if not isinstance(raw, str):
            raw = str(raw)
        self._collected_logs.append(redact_secrets(raw))

    @property
    def collected_logs(self) -> list[str]:
        """Return a copy of the redacted log lines."""
        return list(self._collected_logs)

    # ── argv safety invariant ───────────────────────────────────────────────

    def _assert_argv_safe(self, argv: list[str]) -> None:
        """Reject any argv containing a secret before execution.

        Forbids: the admin password, the reader password, a full DSN, or a SQL
        ``PASSWORD '...'`` literal. Defense in depth on top of argv arrays.
        """
        joined = " ".join(argv)
        forbidden = []
        if self._admin_password and self._admin_password in joined:
            forbidden.append("admin_password")
        if self._reader_password and self._reader_password in joined:
            forbidden.append("reader_password")
        # Full DSN forms
        if re.search(r"postgresql?://[^/\s@]+:[^/\s@]+@", joined):
            forbidden.append("full_dsn")
        # SQL PASSWORD literal
        if re.search(r"PASSWORD\s+'[^']*'", joined, re.IGNORECASE):
            forbidden.append("sql_password_literal")
        if forbidden:
            raise EphemeralExecutionError(
                "ARGV_SECRET_LEAK",
                "argv rejected; forbidden tokens present: %s" % forbidden,
            )

    # ── subprocess helpers (all argv arrays, no shell=True) ─────────────────

    def _docker(self, args: list[str], *, input_bytes: bytes | None = None,
                timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        """Run a docker command on the authorized MergePilot-Test daemon.

        Routes through ``wsl -u root -d MergePilot-Test -- docker ...``. argv
        arrays only (never ``shell=True``). Output is redacted on collection.
        """
        argv = ["wsl.exe", "-u", "root", "-d", AUTHORIZED_DAEMON, "--", "docker"] + args
        self._assert_argv_safe(argv)
        try:
            cp = subprocess.run(
                argv, input=input_bytes,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._log("docker timeout: %s" % " ".join(args))
            raise EphemeralExecutionError(
                "DOCKER_TIMEOUT",
                "docker %s timed out after %ds" % (args[0], timeout),
            ) from None
        # Immediate redaction on collection.
        out = cp.stdout.decode("utf-8", "replace") if cp.stdout else ""
        err = cp.stderr.decode("utf-8", "replace") if cp.stderr else ""
        self._log("docker %s rc=%d out=%s err=%s" % (args[0], cp.returncode, out[:500], err[:500]))
        if check and cp.returncode != 0:
            raise EphemeralExecutionError(
                "DOCKER_FAILED",
                "docker %s rc=%d (detail redacted)" % (args[0], cp.returncode),
            )
        return cp

    def _psql_via_exec(self, sql: str, *, user: str, timeout: int = _RUN_PSQL_DEFAULT_TIMEOUT) -> str:
        """Pipe SQL to psql inside the container via stdin (``-f -``).

        The SQL bytes are read on the HOST and piped through
        ``subprocess.run(input=...)``; no host path appears in the argv. The
        password is NEVER on the argv — when the reader role is involved, the
        password is embedded in the SQL itself (``CREATE ROLE``/``ALTER ROLE``).
        """
        argv = [
            "wsl.exe", "-u", "root", "-d", AUTHORIZED_DAEMON, "--",
            "docker", "exec", "-i", self._container_name,
            "psql", "-U", user, "-d", _DB_NAME,
        ] + _PSQL_ON_ERROR + ["-A", "-t", "-f", "-"]
        self._assert_argv_safe(argv)
        sql_bytes = sql.encode("utf-8") if isinstance(sql, str) else sql
        try:
            cp = subprocess.run(
                argv, input=sql_bytes,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            self._log("psql timeout via exec")
            raise EphemeralExecutionError(
                "PSQL_TIMEOUT",
                "psql (exec) timed out after %ds" % timeout,
            ) from None
        out = cp.stdout.decode("utf-8", "replace") if cp.stdout else ""
        err = cp.stderr.decode("utf-8", "replace") if cp.stderr else ""
        self._log("psql exec rc=%d out=%s err=%s" % (cp.returncode, out[:500], err[:500]))
        if cp.returncode != 0:
            raise EphemeralExecutionError(
                "PSQL_FAILED",
                "psql rc=%d (detail redacted)" % cp.returncode,
            )
        return out

    # ── TCP/psycopg2 helpers (admin + reader, real connections) ─────────────

    def _connect(self, *, password: str, user: str,
                 application_name: str = _APPLICATION_NAME,
                 connect_timeout: int = 5):
        """Open a real TCP psycopg2 connection to the container's host port.

        Uses the IPv4-loopback ``host_address``/``host_port`` (Windows side).
        The password stays local to this call; the DSN is built inline and
        never logged.
        """
        import psycopg2  # lazy import
        dsn = (
            "host=%s port=%d dbname=%s user=%s password=%s "
            "application_name=%s connect_timeout=%d"
            % (self._host_address, self._host_port, _DB_NAME, user,
               password, application_name, connect_timeout)
        )
        try:
            return psycopg2.connect(dsn)
        except Exception as exc:
            # from None suppresses raw libpq chaining (may echo DSN).
            raise EphemeralExecutionError(
                "PG_CONNECT_FAILED",
                "psycopg2 connect failed as %s: %s" % (user, type(exc).__name__),
            ) from None

    # ── lifecycle: start ────────────────────────────────────────────────────

    def _generate_names(self) -> None:
        """Generate a unique validated container name + run label."""
        stamp = int(time.time())
        rnd = secrets.token_hex(4)
        name = "m6rag-eph-%d-%s" % (stamp, rnd)
        label = "label-" + name
        if not validate_container_name(name):
            raise EphemeralExecutionError(
                "INVALID_CONTAINER_NAME", "generated name failed validation")
        self._container_name = name
        self._label = label

    def _confirm_no_name_collision(self) -> None:
        cp = self._docker(
            ["ps", "-a", "--filter", "name=^%s$" % self._container_name,
             "--format", "{{.ID}}"],
            check=False)
        if cp.stdout and cp.stdout.decode("utf-8", "replace").strip():
            raise EphemeralExecutionError(
                "CONTAINER_NAME_COLLISION",
                "a container with this name already exists (detail redacted)")

    def start(self) -> None:
        """Start the disposable container and wait for readiness.

        Password transport: ``--env-file /dev/stdin`` with env bytes piped via
        ``subprocess.run(input=...)``. No ``-e POSTGRES_PASSWORD=<secret>`` on
        the argv. ``--pull=never`` and ``--restart=no`` are mandatory.

        Image: the container is started with the digest-pinned ``IMAGE_DIGEST``
        directly (Fix 1). There is NO tag fallback. The approved local Image ID
        is resolved via a pre-start ``docker image inspect IMAGE_DIGEST``; after
        start, the running container's ``.Image`` must equal it exactly AND the
        image's RepoDigests must contain ``IMAGE_DIGEST``.
        """
        if self._started:
            return
        # Fix 3 (second review): refuse to touch Docker unless a valid
        # authorization_context is present. This is checked BEFORE any Docker
        # command (including name-collision / image-id probes).
        self._validate_authorization_context()
        self._generate_names()
        self._admin_password = secrets.token_urlsafe(32)
        self._reader_password = secrets.token_urlsafe(32)
        # Fix 2 (final review): mark that we are about to touch the environment.
        # cleanup's post-recheck is only allowed when this is True.
        self._environment_touched = True
        self._confirm_no_name_collision()
        # Resolve the approved local Image ID BEFORE start (fail-closed).
        self._resolve_approved_image_id()

        # Build the env-file content piped to /dev/stdin. Never on argv.
        # POSTGRES_USER=mergepilot makes the migration-referenced ``mergepilot``
        # role the bootstrap superuser (matches run_schema_foundation.sh).
        env_bytes = (
            "POSTGRES_USER=%s\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=%s\n"
            % (_ADMIN_USER, self._admin_password, _DB_NAME)
        ).encode("utf-8")

        argv = [
            "wsl.exe", "-u", "root", "-d", AUTHORIZED_DAEMON, "--", "docker",
            "run", "-d",
            "--pull=never",
            "--restart=no",
            "--name", self._container_name,
            "--label", "mergepilot.ephemeral=%s" % self._label,
            "-e", "PGDATA=/tmp/pgdata",          # no persistent volume
            "-p", "127.0.0.1::5432",             # IPv4 loopback ONLY, auto port
            "--env-file", "/dev/stdin",
            IMAGE_DIGEST,                        # digest-pinned, NO tag fallback
        ]
        self._assert_argv_safe(argv)
        try:
            cp = subprocess.run(
                argv, input=env_bytes,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, check=False,
            )
        except subprocess.TimeoutExpired:
            raise EphemeralExecutionError(
                "DOCKER_RUN_TIMEOUT", "docker run timed out") from None
        out = redact_secrets(cp.stdout.decode("utf-8", "replace") if cp.stdout else "")
        err = redact_secrets(cp.stderr.decode("utf-8", "replace") if cp.stderr else "")
        self._log("docker run rc=%d out=%s err=%s" % (cp.returncode, out[:500], err[:500]))
        if cp.returncode != 0:
            # digest run failed → BLOCKED; do NOT retry with a tag fallback.
            raise EphemeralExecutionError(
                "DOCKER_RUN_FAILED", "docker run (digest) rc=%d (detail redacted); "
                "no tag fallback permitted" % cp.returncode)
        # Release the env bytes reference immediately (per amendment).
        del env_bytes

        container_id = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise EphemeralExecutionError(
                "BAD_CONTAINER_ID", "docker run did not return a valid 64-hex ID")
        self._container_id = container_id
        self._started = True
        self._verify_image_digest_of_running_container()
        self._resolve_host_port()
        self._wait_ready()

    def _validate_authorization_context(self) -> None:
        """Fix 3 (second review): validate the authorization_context before Docker.

        Requires a context with: authorized=True, a complete fingerprint
        (server_id/name/docker_root_dir/version), authorized_distro_state
        =Running, ubuntu_state present, endpoint == the approved unix socket,
        docker_host == "" or the approved socket, image_digest == IMAGE_DIGEST,
        and image_id a valid sha256 Image ID. Raises AUTH_CONTEXT_INVALID
        (before ANY Docker command) on any missing/mismatched field. The context
        is NEVER modified or inferred by the executor (Fix 1, final review).
        """
        ctx = self._authorization_context
        if not isinstance(ctx, dict) or not ctx.get("authorized"):
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID",
                "authorization_context missing or not authorized; no Docker command")
        fp = ctx.get("fingerprint")
        if not isinstance(fp, dict):
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID", "fingerprint missing in authorization_context")
        for field in ("server_id", "name", "docker_root_dir", "version"):
            if not fp.get(field):
                raise EphemeralExecutionError(
                    "AUTH_CONTEXT_INVALID",
                    "fingerprint field %s missing/empty" % field)
        if ctx.get("authorized_distro_state") != "Running":
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID",
                "authorized distro not Running (got %r)"
                % ctx.get("authorized_distro_state"))
        if "ubuntu_state" not in ctx:
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID", "ubuntu_state missing in context")
        # endpoint / docker_host / image_digest / image_id — strict, non-inferred.
        if ctx.get("endpoint") != "unix:///var/run/docker.sock":
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID",
                "endpoint missing or not the approved unix socket")
        if ctx.get("docker_host") not in ("", "unix:///var/run/docker.sock"):
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID",
                "docker_host not an approved value")
        if ctx.get("image_digest") != IMAGE_DIGEST:
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID",
                "image_digest missing or != approved IMAGE_DIGEST")
        image_id = ctx.get("image_id")
        if not isinstance(image_id, str) or not re.match(
                r"^(sha256:)?[0-9a-f]{12,64}$", image_id):
            raise EphemeralExecutionError(
                "AUTH_CONTEXT_INVALID",
                "image_id missing or not a valid sha256 Image ID")

    def _recheck_environment_fingerprint(self) -> None:
        """Re-probe the environment after cleanup (Fix 2, final review).

        Ordered and fail-closed so it NEVER implicitly starts a distro:
          1. Only runs when ``_environment_touched`` is True (a pre-Docker
             AUTH_CONTEXT_INVALID never reaches here).
          2. Read-only ``wsl -l -v``. If MergePilot-Test is missing or NOT
             Running → ENVIRONMENT_FINGERPRINT_CHANGED and return immediately
             (NO ``wsl -d`` / Docker command is issued).
          3. Confirm Ubuntu-22.04 state unchanged.
          4. Only then re-probe DOCKER_HOST, docker context endpoint, daemon
             fingerprint, and image_id — each must equal the pre-context value.

        Probe command failure → ENVIRONMENT_RECHECK_FAILED.
        Any field change → ENVIRONMENT_FINGERPRINT_CHANGED.
        """
        # Fix 2: if the environment was never touched, do nothing (no implicit
        # distro start). This covers the AUTH_CONTEXT_INVALID pre-Docker path.
        if not self._environment_touched:
            return
        ctx = self._authorization_context
        if not isinstance(ctx, dict) or not ctx.get("fingerprint"):
            return  # nothing to compare
        import ephemeral_harness as _eh
        # Step 1-2: read-only wsl -l -v. MergePilot-Test must still exist + Run.
        states = _eh._wsl_distro_states()
        if AUTHORIZED_DAEMON not in states:
            raise EphemeralExecutionError(
                "ENVIRONMENT_FINGERPRINT_CHANGED",
                "%s missing in wsl -l -v on recheck" % AUTHORIZED_DAEMON)
        if states[AUTHORIZED_DAEMON] != "Running":
            raise EphemeralExecutionError(
                "ENVIRONMENT_FINGERPRINT_CHANGED",
                "%s not Running on recheck (got %s); no wsl -d issued"
                % (AUTHORIZED_DAEMON, states[AUTHORIZED_DAEMON]))
        # Step 3: Ubuntu-22.04 state unchanged.
        post_ubuntu = states.get("Ubuntu-22.04", "UNKNOWN")
        if post_ubuntu != ctx.get("ubuntu_state"):
            raise EphemeralExecutionError(
                "ENVIRONMENT_FINGERPRINT_CHANGED",
                "Ubuntu-22.04 state changed: pre=%r post=%r"
                % (ctx.get("ubuntu_state"), post_ubuntu))
        pre = ctx["fingerprint"]
        # Step 4a: DOCKER_HOST recheck.
        dh_res = _eh._run_wsl_text(
            ["-u", "root", "-d", AUTHORIZED_DAEMON, "--",
             "bash", "-c", "echo \"${DOCKER_HOST:-}\""],
            timeout=10)
        if dh_res is None:
            raise EphemeralExecutionError(
                "ENVIRONMENT_RECHECK_FAILED", "could not re-probe DOCKER_HOST")
        post_dh = dh_res[1].strip()
        if post_dh != ctx.get("docker_host"):
            raise EphemeralExecutionError(
                "ENVIRONMENT_FINGERPRINT_CHANGED",
                "DOCKER_HOST changed: pre=%r post=%r" % (ctx.get("docker_host"), post_dh))
        # Step 4b: docker context endpoint.
        ep_res = _eh._run_wsl_text(
            ["-u", "root", "-d", AUTHORIZED_DAEMON, "--",
             "docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            timeout=15)
        if ep_res is None or ep_res[0] != 0:
            raise EphemeralExecutionError(
                "ENVIRONMENT_RECHECK_FAILED", "could not re-probe endpoint")
        post_endpoint = ep_res[1].strip()
        if post_endpoint != ctx.get("endpoint"):
            raise EphemeralExecutionError(
                "ENVIRONMENT_FINGERPRINT_CHANGED",
                "endpoint changed: pre=%r post=%r" % (ctx.get("endpoint"), post_endpoint))
        # Step 4c: daemon fingerprint.
        info_res = _eh._run_wsl_text(
            ["-u", "root", "-d", AUTHORIZED_DAEMON, "--", "docker", "info"],
            timeout=15)
        if info_res is None:
            raise EphemeralExecutionError(
                "ENVIRONMENT_RECHECK_FAILED", "could not re-probe docker info")
        if info_res[0] != 0:
            raise EphemeralExecutionError(
                "ENVIRONMENT_RECHECK_FAILED", "docker info rc=%d on recheck" % info_res[0])
        post = _eh._parse_daemon_fingerprint(info_res[1])
        for field in ("server_id", "name", "docker_root_dir", "version"):
            if post.get(field) != pre.get(field):
                raise EphemeralExecutionError(
                    "ENVIRONMENT_FINGERPRINT_CHANGED",
                    "%s changed: pre=%r post=%r" % (field, pre.get(field), post.get(field)))
        # Step 4d: image_id recheck (IMAGE_DIGEST still resolves to the same id).
        img_res = _eh._run_wsl_text(
            ["-u", "root", "-d", AUTHORIZED_DAEMON, "--",
             "docker", "image", "inspect", IMAGE_DIGEST, "--format", "{{.Id}}"],
            timeout=15)
        if img_res is None or img_res[0] != 0:
            raise EphemeralExecutionError(
                "ENVIRONMENT_RECHECK_FAILED", "could not re-probe image id")
        post_img = img_res[1].strip()
        if ctx.get("image_id") and post_img != ctx.get("image_id"):
            raise EphemeralExecutionError(
                "ENVIRONMENT_FINGERPRINT_CHANGED",
                "image_id changed: pre=%r post=%r" % (ctx.get("image_id"), post_img))

    def _resolve_approved_image_id(self) -> None:
        """Pre-start: resolve the approved local Image ID via digest inspect.

        ``docker image inspect IMAGE_DIGEST --format {{.Id}}`` returns the local
        content-addressed Image ID. This is stored and compared post-start
        against the running container's ``.Image``. If the digest is not cached,
        this raises IMAGE_NOT_CACHED (fail-closed, no pull).

        Fix 1 (final review): the resolved Image ID must also match the
        ``image_id`` recorded in the authorization_context; a mismatch means the
        cached image changed between authorization and start → IMAGE_DIGEST_MISMATCH.
        """
        global _APPROVED_LOCAL_IMAGE_ID
        cp = self._docker(
            ["image", "inspect", IMAGE_DIGEST, "--format", "{{.Id}}"],
            check=False)
        if cp.returncode != 0:
            raise EphemeralExecutionError(
                "IMAGE_NOT_CACHED",
                "approved digest not present locally (no pull permitted)")
        img_id = cp.stdout.decode("utf-8", "replace").strip()
        if not img_id:
            raise EphemeralExecutionError(
                "IMAGE_ID_EMPTY", "image inspect returned empty Id")
        # Cross-check against the authorization_context image_id.
        ctx = self._authorization_context or {}
        auth_image_id = ctx.get("image_id")
        if auth_image_id and img_id != auth_image_id:
            raise EphemeralExecutionError(
                "IMAGE_DIGEST_MISMATCH",
                "pre-start image id != authorization_context image_id")
        _APPROVED_LOCAL_IMAGE_ID = img_id
        self._log("approved local image id: %s" % img_id)

    def _verify_image_digest_of_running_container(self) -> None:
        """Post-start: verify the running container's image == approved.

        Two checks (both must pass):
        1. The running container's ``.Image`` (Image ID) equals the approved
           local Image ID resolved pre-start.
        2. The image's RepoDigests contain ``IMAGE_DIGEST`` exactly.
        """
        # Check 1: running container Image ID == approved.
        cp = self._docker(
            ["inspect", self._container_id, "--format", "{{.Image}}"],
            check=True)
        running_img = cp.stdout.decode("utf-8", "replace").strip()
        self._log("running container image id: %s" % running_img)
        if running_img != _APPROVED_LOCAL_IMAGE_ID:
            raise EphemeralExecutionError(
                "IMAGE_DIGEST_MISMATCH",
                "running container Image ID != approved local Image ID")
        # Check 2: RepoDigests contains the approved digest exactly.
        cp2 = self._docker(
            ["image", "inspect", IMAGE_DIGEST, "--format",
             "{{json .RepoDigests}}"], check=True)
        repo_digests_raw = cp2.stdout.decode("utf-8", "replace").strip()
        self._log("repo digests: %s" % repo_digests_raw)
        import json as _json
        try:
            repo_digests = _json.loads(repo_digests_raw)
        except Exception:
            raise EphemeralExecutionError(
                "IMAGE_DIGEST_MISMATCH",
                "RepoDigests unparseable") from None
        if IMAGE_DIGEST not in repo_digests:
            raise EphemeralExecutionError(
                "IMAGE_DIGEST_MISMATCH",
                "approved digest not in RepoDigests")

    def _resolve_host_port(self) -> None:
        """Resolve the actual IPv4-loopback host port via ``docker port``."""
        cp = self._docker(
            ["port", self._container_name, "5432"], check=True)
        raw = cp.stdout.decode("utf-8", "replace").strip()
        self._log("docker port: %s" % raw)
        # Expect e.g. "127.0.0.1:32768". Reject 0.0.0.0/::/LAN.
        match = re.match(r"^([\d.]+):(\d+)$", raw)
        if not match:
            raise EphemeralExecutionError(
                "PORT_PARSE_FAILED", "docker port output unparseable (redacted)")
        host_addr, port_s = match.group(1), match.group(2)
        if host_addr != "127.0.0.1":
            raise EphemeralExecutionError(
                "BIND_NOT_LOOPBACK",
                "host address is %s, must be 127.0.0.1" % host_addr)
        port = int(port_s)
        if not (0 < port < 65536):
            raise EphemeralExecutionError("BAD_PORT", "port out of range")
        self._host_address = host_addr
        self._host_port = port

    def _wait_ready(self) -> None:
        """Wait for pg_isready, then confirm via real TCP ``SELECT 1``."""
        deadline = time.monotonic() + _READINESS_TOTAL_TIMEOUT
        last_rc = -1
        # Phase 1: pg_isready via docker exec.
        while time.monotonic() < deadline:
            cp = self._docker(
                ["exec", self._container_name, "pg_isready",
                 "-U", _ADMIN_USER, "-d", _DB_NAME],
                check=False, timeout=_READINESS_CMD_TIMEOUT)
            last_rc = cp.returncode
            if cp.returncode == 0:
                break
            time.sleep(_READINESS_POLL_INTERVAL)
        else:
            raise EphemeralExecutionError(
                "PG_NOT_READY", "pg_isready never succeeded (rc=%d)" % last_rc)
        # Phase 2: real TCP SELECT 1 from Windows.
        while time.monotonic() < deadline:
            try:
                conn = self._connect(password=self._admin_password, user=_ADMIN_USER)
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
                conn.close()
                return
            except EphemeralExecutionError:
                time.sleep(_READINESS_POLL_INTERVAL)
        # Check if the container died during readiness.
        cp = self._docker(["inspect", self._container_name,
                           "--format", "{{.State.Status}}"], check=False)
        status = cp.stdout.decode("utf-8", "replace").strip()
        raise EphemeralExecutionError(
            "TCP_NOT_REACHABLE",
            "could not SELECT 1 via TCP within timeout (container status=%s)" % status)

    # ── lifecycle: prepare (17 bootstrap operations) ────────────────────────

    def measure_server_identity(self) -> None:
        """Measure inet_server_addr/port via a REAL TCP connection.

        This sets ``_server_address``/``_server_port`` for
        ``expected_server_*``. NOT done via ``docker exec`` (Unix socket would
        return NULL for inet_server_addr).

        Retry v3 Fix 1: measures ``host(inet_server_addr())`` — the BARE
        host text — so the measured value is already in canonical form
        (an inet→text cast may carry a build-dependent ``/32`` netmask
        suffix; PostgresSnapshotSource canonicalizes both sides of the
        comparison regardless, keeping Phase B behavior identical).
        """
        conn = self._connect(password=self._admin_password, user=_ADMIN_USER)
        cur = conn.cursor()
        cur.execute("SELECT host(inet_server_addr()), inet_server_port()")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or row[0] is None:
            raise EphemeralExecutionError(
                "SERVER_ADDR_NULL",
                "inet_server_addr() returned NULL (not a TCP connection?)")
        self._server_address = str(row[0])
        self._server_port = int(row[1])
        self._log("measured server identity: addr=%s port=%s"
                  % (self._server_address, self._server_port))

    def _verify_migration_file_integrity(self, filename: str, approved_dir: str) -> Path:
        """Verify a migration file is byte-identical to the base-commit blob.

        Enforces the strict allowlist contract (Fix 3):
        - resolved path must be inside the approved directory (no path escape)
        - must be a regular file (no symlink, no directory)
        - must exist
        - working-tree content hash == git blob hash at PHASE_B_BASE_COMMIT

        On ANY mismatch raises MIGRATION_INTEGRITY_MISMATCH and NO SQL is run.
        Returns the resolved, verified Path.
        """
        approved_root = (self._repo_root / approved_dir).resolve()
        # Build the candidate path and resolve WITHOUT following symlinks first.
        candidate = (self._repo_root / approved_dir / filename)
        # Reject symlinks / non-regular files.
        if candidate.is_symlink():
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "migration %s is a symlink (rejected)" % filename)
        if not candidate.exists():
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "migration %s does not exist" % filename)
        if not candidate.is_file():
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "migration %s is not a regular file" % filename)
        resolved = candidate.resolve()
        # Path-escape check: resolved must be inside approved_root.
        try:
            resolved.relative_to(approved_root)
        except ValueError:
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "migration %s resolved outside approved dir" % filename) from None
        # Filename must be a bare basename (no path separators).
        if filename != Path(filename).name:
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "migration %s is not a bare basename" % filename)
        # Blob hash comparison vs PHASE_B_BASE_COMMIT.
        rel = "%s/%s" % (approved_dir, filename)
        git_path = "%s:%s" % (PHASE_B_BASE_COMMIT, rel.replace("\\", "/"))
        r = subprocess.run(
            ["git", "rev-parse", git_path],
            cwd=str(self._repo_root),
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "could not resolve base-commit blob for %s" % filename)
        base_hash = r.stdout.strip()
        # Working-tree hash (no filters, raw content).
        r2 = subprocess.run(
            ["git", "hash-object", "--no-filters", str(resolved)],
            cwd=str(self._repo_root),
            capture_output=True, text=True, timeout=15)
        if r2.returncode != 0:
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "could not hash working-tree file %s" % filename)
        wt_hash = r2.stdout.strip()
        if wt_hash != base_hash:
            raise EphemeralExecutionError(
                "MIGRATION_INTEGRITY_MISMATCH",
                "migration %s content != base commit (wt=%s base=%s)"
                % (filename, wt_hash[:12], base_hash[:12]))
        return resolved

    def _validate_all_migrations(self) -> dict:
        """Phase V (Fix 4, second review): validate ALL migration files first.

        Verifies every DISTINCT migration file in the allowlist
        (MIGRATION_CHAIN distinct + ISOLATED_LIVE_MIGRATIONS) against
        PHASE_B_BASE_COMMIT BEFORE any database operation. Returns a
        {filename: resolved_path} map for Phase E to reuse (so repeated
        applications of m4f1_state/m4f1_hotfix_1 re-use the same verified path
        without re-hashing). If ANY file fails, MIGRATION_INTEGRITY_MISMATCH is
        raised and NO SQL is run.
        """
        verified: dict = {}
        # Distinct audit-db files (11), preserving MIGRATION_CHAIN order.
        seen_audit = []
        for filename, _desc in MIGRATION_CHAIN:
            if filename not in verified:
                verified[filename] = self._verify_migration_file_integrity(
                    filename, "tools/audit-db")
                seen_audit.append(filename)
        # Distinct ISOLATED_LIVE files (2).
        for filename in ISOLATED_LIVE_MIGRATIONS:
            if filename not in verified:
                verified[filename] = self._verify_migration_file_integrity(
                    filename, "tools/demo_console/migrations")
        return verified

    def prepare(self) -> None:
        """Apply the full bootstrap sequence (17 operations).

        Fix 4 (second review): split into Phase V (validate ALL migrations) and
        Phase E (execute). NO database operation — not even measure_server_identity
        or prerequisite roles — runs until Phase V validates every distinct
        migration file against PHASE_B_BASE_COMMIT. Repeated applications
        (m4f1_state/m4f1_hotfix_1 round 2) reuse the same verified path.
        """
        # Phase V: validate ALL distinct migration files first (no SQL yet).
        verified = self._validate_all_migrations()

        # Phase E: execution (only reached if Phase V fully succeeded).
        # Step 0: measure server identity (for expected_server_*).
        self.measure_server_identity()

        # Phase 0: prerequisite roles.
        self._psql_via_exec(build_prerequisite_role_sql(), user=_ADMIN_USER)
        self.operations_applied.append("phase0_prerequisite_roles")

        # Phase 1: 13 audit-db applications, reusing verified paths.
        for i, (filename, _desc) in enumerate(MIGRATION_CHAIN):
            sql_bytes = verified[filename].read_bytes()
            self._psql_via_exec(sql_bytes.decode("utf-8"), user=_ADMIN_USER)
            self.operations_applied.append("phase1_migration_%s_round%d"
                                           % (filename, self._round_no(filename, i)))

        # Phase 2: reader role (password in the SQL, piped via stdin).
        self._psql_via_exec(
            build_reader_role_sql(self._reader_password), user=_ADMIN_USER)
        self.operations_applied.append("phase2_reader_role")

        # Phase 3: ISOLATED_LIVE applications, reusing verified paths.
        for filename in ISOLATED_LIVE_MIGRATIONS:
            self._psql_via_exec(verified[filename].read_bytes().decode("utf-8"),
                                user=_ADMIN_USER)
            self.operations_applied.append("phase3_migration_%s" % filename)

        # Seed: split so Option A (bind_revision) runs BETWEEN seed part 1
        # (task_runs/run_pr_bindings/mcp_calls — the provenance rows
        # bind_revision requires) and seed part 2 (stage_runs/stage_events/
        # audit_events/rollback/missing). The seed's Option B direct-admin
        # revision_bindings INSERT is applied ONLY if Option A fails (fallback).
        # Uses the STRUCTURED build_seed_sql_parts() generator (Fix 7) — no text
        # parsing.
        seed_before, option_b_revision, seed_after = build_seed_sql_parts()
        self._psql_via_exec(seed_before, user=_ADMIN_USER)
        self.operations_applied.append("seed_part1_before_bind")

        # Option A: bind_revision (admin; reader has no EXECUTE).
        self._attempt_bind_revision_option_a()

        # If Option A failed, apply the Option B fallback row now (does NOT
        # mask the Option A failure — the outcome flag stays false).
        if not self.bind_revision_outcome["option_a_succeeded"]:
            self._psql_via_exec(option_b_revision, user=_ADMIN_USER)
            self.operations_applied.append("seed_option_b_fallback_revision")

        # Seed part 2 (remaining rows for the read path).
        self._psql_via_exec(seed_after, user=_ADMIN_USER)
        self.operations_applied.append("seed_part2_after_bind")

    def start_and_prepare(self) -> None:
        """Start + prepare with cleanup-on-failure that preserves both errors.

        Fix 6: if the primary execution (start/prepare) fails, cleanup runs in
        a finally. If cleanup ALSO fails, raise
        :class:`EphemeralExecutionAndCleanupError` carrying BOTH stable,
        redacted error codes. If cleanup succeeds, the original primary error
        propagates. Neither error swallows the other.
        """
        primary_code = ""
        try:
            self.start()
            self.prepare()
        except EphemeralExecutionError as primary:
            primary_code = primary.code
            # Attempt cleanup; do not swallow its failure.
            cleanup_code = ""
            try:
                self.cleanup_and_verify()
            except EphemeralExecutionError as cleanup_exc:
                cleanup_code = cleanup_exc.code
                raise EphemeralExecutionAndCleanupError(
                    primary_code, cleanup_code) from None
            # Cleanup succeeded — propagate the primary error.
            raise

    @staticmethod
    def _round_no(filename: str, i: int) -> int:
        """Determine idempotency round (1 or 2) for a migration chain entry."""
        # m4f1_state.sql and m4f1_hotfix_1.sql appear twice each.
        names = [f for f, _ in MIGRATION_CHAIN]
        prior = names[:i].count(filename)
        return prior + 1

    def _attempt_bind_revision_option_a(self) -> None:
        """Attempt Option A: admin calls public.bind_revision().

        On success: ``ephemeral_bind_revision_contract_verified = true``.
        On failure: record stable code, fall back to Option B (seed already has
        the direct-admin row), and keep
        ``ephemeral_bind_revision_contract_verified = false`` +
        ``revision_producer_contract = NOT_VERIFIED``. The fallback does NOT
        mask the Option A failure.

        Note: this is NOT a claim that ``revision_producer_contract = VERIFIED``
        (only the narrow ``ephemeral_bind_revision_contract_verified`` flag).
        """
        self.bind_revision_outcome["option_a_attempted"] = True
        digest = compute_revision_digest(
            source_call_id="mcp-eph-001",
            correlation_id="corr-eph-001",
            tool="create_pull_request",
            target_repo="test/repo-alpha",
            run_id="run-eph-ok",
            git_sha="a" * 40,
            result_status="OK",
        )
        head_sha = "b" * 40
        base_sha = "a" * 40
        sql = (
            "SELECT public.bind_revision("
            "'run-eph-ok', 'test/repo-alpha', 42, '%s', '%s', 'mcp-eph-001', '%s');"
            % (head_sha, base_sha, digest)
        )
        try:
            self._psql_via_exec(sql, user=_ADMIN_USER)
            self.bind_revision_outcome["option_a_succeeded"] = True
            self.bind_revision_outcome["ephemeral_bind_revision_contract_verified"] = True
            self.operations_applied.append("option_a_bind_revision_ok")
        except EphemeralExecutionError as exc:
            self.bind_revision_outcome["option_a_succeeded"] = False
            self.bind_revision_outcome["option_a_error_code"] = exc.code
            self.bind_revision_outcome["ephemeral_bind_revision_contract_verified"] = False
            self.operations_applied.append("option_a_bind_revision_failed_fallback_b")

    # ── reader DSN + source construction ────────────────────────────────────

    @property
    def expected_server_addresses(self) -> list[str]:
        return [self._server_address]

    @property
    def expected_server_port(self) -> int:
        return self._server_port

    def make_reader_source(self, run_id: str):
        """Construct a PostgresSnapshotSource against the live reader role.

        The DSN is built inline (not stored, not logged). The source is tracked
        for cleanup.
        """
        from postgres_source import PostgresSnapshotSource
        dsn = (
            "host=%s port=%d dbname=%s user=%s password=%s "
            "application_name=%s connect_timeout=5"
            % (self._host_address, self._host_port, _DB_NAME,
               CANONICAL_VIEWER_ROLE, self._reader_password, _APPLICATION_NAME)
        )
        src = PostgresSnapshotSource(
            dsn=dsn,
            run_id=run_id,
            expected_database=_DB_NAME,
            expected_role=CANONICAL_VIEWER_ROLE,
            expected_environment_id=ENVIRONMENT_ID_EPHEMERAL,
            expected_server_addresses=self.expected_server_addresses,
            expected_server_port=self.expected_server_port,
            expected_application_name=_APPLICATION_NAME,
            query_timeout_seconds=10.0,
        )
        self._reader_sources.append(src)
        return src

    def admin_exec(self, sql: str) -> str:
        """Run admin SQL via psql-via-exec (for negative-test modifications)."""
        return self._psql_via_exec(sql, user=_ADMIN_USER)

    # ── lifecycle: cleanup ──────────────────────────────────────────────────

    def _record_anonymous_volumes(self) -> None:
        """Record this container's anonymous volume names before removal (Fix 4/5).

        Fix 5 (second review): does NOT swallow errors. A non-zero inspect
        returncode → VOLUME_INSPECT_FAILED; invalid JSON → VOLUME_INSPECT_INVALID.
        The caller continues to attempt the ownership-verified `docker rm -fv`,
        but the final cleanup must fail (not mark cleaned).
        """
        if not self._container_id:
            return
        cp = self._docker(
            ["inspect", self._container_id, "--format", "{{json .Mounts}}"],
            check=False)
        if cp.returncode != 0:
            raise EphemeralExecutionError(
                "VOLUME_INSPECT_FAILED",
                "volume mount inspect rc=%d" % cp.returncode)
        raw = cp.stdout.decode("utf-8", "replace").strip() or "[]"
        import json as _json
        try:
            mounts = _json.loads(raw)
        except Exception:
            raise EphemeralExecutionError(
                "VOLUME_INSPECT_INVALID",
                "volume mount JSON unparseable") from None
        self._anonymous_volumes = [
            m.get("Name", "") for m in mounts
            if m.get("Type") == "volume" and m.get("Name")
        ]
        self._log("anonymous volumes: %s" % self._anonymous_volumes)

    def _verify_container_ownership(self) -> bool:
        """Verify the container's ID, name, AND label all match this session.

        Fix 4: before removing anything, confirm the container is THIS
        session's resource (all three of ID/name/label). Returns False (and
        records RESOURCE_OWNERSHIP_MISMATCH) if any mismatch — removal is then
        skipped. Never removes a resource that does not belong to this session.
        """
        if not self._container_id:
            return False  # nothing started; nothing to own
        # Use a separate, returncode-checked existence query so a daemon/inspect
        # FAILURE is never confused with "container absent" (Fix 5, second review).
        try:
            cp_exist = self._docker(
                ["inspect", self._container_id, "--format", "{{.Id}}"],
                check=False)
        except EphemeralExecutionError as exc:
            # daemon/inspect command failed → must NOT be treated as absent.
            raise EphemeralExecutionError(
                "DOCKER_INSPECT_FAILED",
                "ownership existence probe failed: %s" % exc.code) from None
        if cp_exist.returncode != 0:
            # A clean "No such object" from docker inspect is a non-zero rc with
            # a specific stderr; if the daemon itself errored, surface it.
            err = (cp_exist.stderr or b"").decode("utf-8", "replace").lower()
            if "no such" in err:
                return False  # genuinely absent — nothing to own
            raise EphemeralExecutionError(
                "DOCKER_INSPECT_FAILED",
                "ownership existence probe rc=%d (not 'no such')" % cp_exist.returncode)
        # Container exists — now read its identity fields for the triple match.
        try:
            cp = self._docker(
                ["inspect", self._container_id,
                 "--format", "{{.Id}}|{{.Name}}|{{json .Config.Labels}}"],
                check=False)
        except EphemeralExecutionError as exc:
            raise EphemeralExecutionError(
                "DOCKER_INSPECT_FAILED",
                "ownership identity probe failed: %s" % exc.code) from None
        if cp.returncode != 0:
            raise EphemeralExecutionError(
                "DOCKER_INSPECT_FAILED",
                "ownership identity probe rc=%d" % cp.returncode)
        raw = cp.stdout.decode("utf-8", "replace").strip()
        parts = raw.split("|", 2)
        if len(parts) != 3:
            return False
        cid, cname, labels_json = parts
        # ID match (docker prepends sha256: sometimes; compare the hex tail).
        cid_hex = cid.split(":")[-1] if ":" in cid else cid
        if cid_hex != self._container_id and cid != self._container_id:
            return False
        # Name match (docker prefixes '/').
        if cname.lstrip("/") != self._container_name:
            return False
        # Label match (the named label key "mergepilot.ephemeral" == self._label).
        import json as _json
        try:
            labels = _json.loads(labels_json) if labels_json else {}
        except Exception:
            return False
        if labels.get("mergepilot.ephemeral") != self._label:
            return False
        return True

    def cleanup_and_verify(self) -> None:
        """Idempotent cleanup. Tolerates partial startup.

        Fix 4 hardening:
        - Before removal, verify the container's ID + name + label all match
          this session (RESOURCE_OWNERSHIP_MISMATCH otherwise → no removal).
        - Record anonymous volumes, remove by container ID, verify each gone.
        - Residue check: container ID absent, exact name absent, exact label
          absent, recorded volumes absent, host port closed.
        - check=False commands have their returncode explicitly handled (a
          failed command is a cleanup failure, NOT "resource absent").

        HTTP server / poller / reader sources are closed first. Raises on
        cleanup failure.
        """
        if self._cleaned:
            return
        cleanup_errors: list[str] = []
        # Close HTTP server (Fix 3, final review): do NOT swallow errors. Keep
        # the reference on failure so a later cleanup retry can re-attempt; only
        # null it when BOTH shutdown and server_close succeed.
        if self._http_server is not None:
            http_ok = True
            try:
                self._http_server.shutdown()
            except Exception:
                http_ok = False
                cleanup_errors.append("HTTP_SHUTDOWN_FAILED")
            try:
                self._http_server.server_close()
            except Exception:
                http_ok = False
                cleanup_errors.append("HTTP_SHUTDOWN_FAILED")
            if http_ok:
                self._http_server = None
        # Close poller (Fix 3): keep the reference on failure for retry.
        if self._poller is not None:
            poller_ok = True
            try:
                self._poller.stop()
                self._poller.join(timeout=5)
                if self._poller.is_alive():
                    poller_ok = False
                    cleanup_errors.append("POLLER_STILL_ALIVE")
            except Exception:
                poller_ok = False
                cleanup_errors.append("POLLER_STOP_FAILED")
            if poller_ok:
                self._poller = None
        # Close reader sources (Fix 3): keep FAILED sources for retry; remove
        # only successfully-closed ones.
        remaining_sources = []
        for src in self._reader_sources:
            try:
                close = getattr(src, "close", None)
                if callable(close):
                    close()
            except Exception:
                cleanup_errors.append("SOURCE_CLOSE_FAILED")
                remaining_sources.append(src)  # keep for retry
        self._reader_sources = remaining_sources

        # Ownership verification + anonymous volume recording. Per Fix 5, volume
        # recording errors are collected (not swallowed) but removal is still
        # attempted so a session cleans up as much as possible.
        owned = False
        try:
            owned = self._verify_container_ownership()
        except EphemeralExecutionError as exc:
            cleanup_errors.append(exc.code)  # DOCKER_INSPECT_FAILED etc.
        if self._container_id and not owned and "DOCKER_INSPECT_FAILED" not in cleanup_errors:
            # Distinguish "gone" from "mismatch". If the container is still
            # present but does not match, that is RESOURCE_OWNERSHIP_MISMATCH.
            try:
                cp = self._docker(["inspect", self._container_id, "--format", "{{.Id}}"],
                                  check=False)
                if cp.returncode == 0:
                    cleanup_errors.append("RESOURCE_OWNERSHIP_MISMATCH")
            except EphemeralExecutionError as exc:
                cleanup_errors.append(exc.code)
        if owned:
            try:
                self._record_anonymous_volumes()
            except EphemeralExecutionError as exc:
                cleanup_errors.append(exc.code)  # VOLUME_INSPECT_*
            # Remove by CONTAINER ID (not name), with -v for anonymous volumes.
            try:
                cp_rm = self._docker(["rm", "-fv", self._container_id],
                                     check=False, timeout=60)
                if cp_rm.returncode != 0:
                    cleanup_errors.append("DOCKER_RM_FAILED")
            except EphemeralExecutionError as exc:
                cleanup_errors.append(exc.code)

        # Wait for release (container + port unpublished are async). Up to ~5s.
        for _ in range(10):
            gone = True
            if self._container_id:
                try:
                    cp = self._docker(["ps", "-aq", "--filter", "id=%s" % self._container_id],
                                      check=False)
                    if cp.returncode != 0:
                        cleanup_errors.append("DOCKER_PS_FAILED")
                    elif cp.stdout and cp.stdout.decode("utf-8", "replace").strip():
                        gone = False
                except EphemeralExecutionError as exc:
                    cleanup_errors.append(exc.code)
            port_closed = True
            if self._host_port:
                s = socket.socket(); s.settimeout(1)
                try:
                    s.connect(("127.0.0.1", self._host_port))
                    port_closed = False
                except OSError:
                    port_closed = True
                finally:
                    s.close()
            if gone and port_closed:
                break
            time.sleep(0.5)

        # Residue verification (this session's ID/name/label/volumes only).
        # Each check=False command is explicitly returncode-checked (Fix 5).
        def _residue_query(args, present_code, fail_code):
            try:
                cp = self._docker(args, check=False)
            except EphemeralExecutionError as exc:
                cleanup_errors.append(exc.code or fail_code)
                return
            if cp.returncode != 0:
                cleanup_errors.append(fail_code)
            elif cp.stdout and cp.stdout.decode("utf-8", "replace").strip():
                cleanup_errors.append(present_code)

        if self._container_id:
            _residue_query(["ps", "-aq", "--filter", "id=%s" % self._container_id],
                           "CONTAINER_ID_STILL_PRESENT", "DOCKER_PS_FAILED")
        if self._container_name:
            _residue_query(
                ["ps", "-aq", "--filter", "name=^%s$" % self._container_name],
                "CONTAINER_NAME_STILL_PRESENT", "DOCKER_PS_FAILED")
        if self._label:
            _residue_query(
                ["ps", "-aq", "--filter", "label=%s" % self._label],
                "CONTAINER_LABEL_STILL_PRESENT", "DOCKER_PS_FAILED")
        for vol in self._anonymous_volumes:
            _residue_query(["volume", "ls", "-q", "--filter", "name=^%s$" % vol],
                           "VOLUME_STILL_PRESENT", "DOCKER_VOLUME_LS_FAILED")

        # Host port closed? (final check)
        if self._host_port:
            s = socket.socket(); s.settimeout(2)
            try:
                s.connect(("127.0.0.1", self._host_port))
                cleanup_errors.append("PORT_STILL_OPEN")
            except OSError:
                pass  # closed = good
            finally:
                s.close()

        # Fix 3 (second review): AFTER all Docker resources are handled but
        # BEFORE the external WSL terminate, re-probe the environment and
        # compare fingerprint/endpoint/Ubuntu state to the pre-execution context.
        try:
            self._recheck_environment_fingerprint()
        except EphemeralExecutionError as exc:
            cleanup_errors.append(exc.code)  # ENVIRONMENT_FINGERPRINT_CHANGED / _RECHECK_FAILED

        # Fix 5 (second review): only mark cleaned when EVERYTHING succeeded.
        # On any cleanup_errors, _cleaned stays False so a later retry can run.
        if not cleanup_errors:
            self._cleaned = True
            return
        # _cleaned stays False; raise so the failure is recorded. A subsequent
        # cleanup_and_verify() call is permitted to retry (idempotent on success).
        raise EphemeralExecutionError(
            "CLEANUP_RESIDUE",
            "cleanup residue: %s" % ",".join(sorted(set(cleanup_errors))))

    def __repr__(self) -> str:
        return ("EphemeralExecutor(container_name=%r, started=%r, cleaned=%r)"
                % (self._container_name, self._started, self._cleaned))
