"""ISOLATED_LIVE productization Phase 1 — one-click startup (design-only code).

Builds a docker-compose configuration for the five-service isolated stack
(controller, policy-gateway, PostgreSQL/pgvector, demo-console, preflight),
provides a preflight-check matrix, a safe secret transport, redaction, and a
retryable cleanup entry point. This module NEVER starts WSL/Docker/
PostgreSQL and NEVER opens a real connection — it is pure configuration
generation and gate logic exercised by Mock/static tests.

Phase 1-C additions:
  - Built-image identity registry (immutable digests/IDs recorded at build
    time; floating tags are never authoritative).
  - A Docker-CLI orchestrator (used when no ``docker compose`` plugin
    exists): argv arrays only, ``shell=True`` forbidden, per-service plans
    derived from the compose contract (network, no-port policy, dependency
    order, healthchecks, secret env-file, cleanup).

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
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Callable

# ── Constants ────────────────────────────────────────────────────────────────

# Digest-pinned pgvector image — the ONLY literal remote image this stack may
# use; a floating tag would defeat reproducibility. Same digest as the
# Phase-B ephemeral executor.
PGVECTOR_IMAGE_DIGEST = (
    "pgvector/pgvector@sha256:"
    "a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
)

# Base image for the four BUILT services (cached in the MergePilot-Test
# daemon). Referenced by the root Dockerfiles; pinned by image ID so a
# floating tag can never sneak a different base in.
BUILT_SERVICE_BASE_IMAGE = "python:3.12-slim"
BUILT_SERVICE_BASE_IMAGE_ID = "sha256:54a0b2beae90"  # recorded, immutable form

# Only the demo-console port may be published, and ONLY on loopback.
LOOPBACK_BIND = "127.0.0.1"
DEMO_CONSOLE_PORT = 8600

DB_NAME = "mergepilot_audit"
READER_ROLE = "mergepilot_reader"
ENVIRONMENT_MARKER = "mergepilot-test-ephemeral"
APP_NAME = "mergepilot_isolated_live_reader"
SOURCE_KIND_ISOLATED = "POSTGRES_ISOLATED"

# Phase 1-D retry v3 Fix 3 — controller / policy-gateway runtime contract,
# extracted from the ACTUAL service code (no guessed variable names):
#   controller.py requires: PG_HOST (default 'audit-pg' — must be overridden
#   to the in-network alias 'postgres'), PG_PORT, PG_DATABASE, PG_USER,
#   and the secrets PG_PASS + ADMIN_PW (it refuses to start without them).
#   gateway.py requires: UPSTREAM_URL (an MCP SSE endpoint it can actually
#   reach; the default 'github-mcp' host does not exist in the isolated
#   stack). LISTEN_HOST/LISTEN_PORT have safe defaults.
CONTROLLER_PG_USER = "mergepilot"
GATEWAY_LISTEN_PORT = 8083
# The isolated-stack upstream: an IN-CONTAINER, zero-tool MCP SSE stub that
# the gateway entrypoint starts on container loopback. It is NOT a separate
# service, NOT a host process and NOT a postgres twin — it exists solely so
# the gateway's real lifespan (which demands a reachable upstream) can
# complete inside the isolated network. It serves ZERO tools: the gateway
# proxies nothing and performs no external access.
GATEWAY_ISOLATED_UPSTREAM_URL = "http://127.0.0.1:8084/sse"

# Services and their dependency order (topological).
SERVICE_ORDER = ("postgres", "policy-gateway", "controller", "gh-webhook",
                 "demo-console", "console-edge", "preflight")

# Host-side loopback port for the gh-webhook receiver (M8-GH-3). The ONLY
# other published port is the console-edge on 8600.
GH_WEBHOOK_PORT = 8090

_HEALTHCHECK_INTERVAL = "5s"
_HEALTHCHECK_TIMEOUT = "3s"
_HEALTHCHECK_RETRIES = 10

_SECRET_FILE_NAME = "postgres.env"

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


class StartupGateError(Exception):
    """Stable, redacted startup-gate failure (bare stable code in ``code``)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(code + ((" (%s)" % detail) if detail else ""))


class StartupCleanupError(Exception):
    """Primary + cleanup stable codes; neither swallows the other."""

    def __init__(self, primary_code: str, cleanup_codes):
        self.primary_code = primary_code
        self.cleanup_codes = tuple(cleanup_codes)
        super().__init__("primary=%s cleanup=%s"
                         % (primary_code, ",".join(self.cleanup_codes) or "none"))


# ── Server-address canonicalization (Phase 1-D retry v3 Fix 1) ───────────────

def canonicalize_server_address(value) -> str:
    """Canonicalize ONE measured/expected server address to a bare IPv4.

    Why this exists: PostgreSQL's ``inet_server_addr()`` text form may carry
    a netmask suffix depending on build (``172.18.0.2/32``), while callers
    naturally configure bare addresses (``172.18.0.2``). Both sides of the
    identity comparison are normalized through THIS single shared function,
    so ``172.18.0.2`` and ``172.18.0.2/32`` are the same address. The SQL
    side additionally prefers ``host(inet_server_addr())`` (bare form at the
    source); this function is the defensive Python-side twin of that.

    Parsing uses the standard ``ipaddress`` module — never ``split('/')``
    and never string ``replace``. Fail-closed (``ValueError`` with a stable
    ``CONFIG_INVALID`` prefix):

      - non-string / empty values
      - hostnames and network aliases (``postgres``, ``db.example``)
      - IPv6 addresses (the contract is IPv4-only)
      - CIDR netmasks other than /32 (an address allowlist, not a subnet
        allowlist — ``172.18.0.0/16`` is rejected)
      - malformed values
    """
    if not isinstance(value, str):
        raise ValueError(
            "CONFIG_INVALID: server address must be a string (got %s)"
            % type(value).__name__)
    text = value.strip()
    if not text:
        raise ValueError("CONFIG_INVALID: server address is empty")
    try:
        iface = ipaddress.ip_interface(text)
    except ValueError:
        raise ValueError(
            "CONFIG_INVALID: server address %r is not a valid IPv4 host "
            "(hostnames, network aliases and malformed values are rejected)"
            % text) from None
    if iface.version != 4:
        raise ValueError(
            "CONFIG_INVALID: server address %r is IPv6; the ISOLATED_LIVE "
            "identity contract is IPv4-only" % text)
    if iface.network.prefixlen != 32:
        raise ValueError(
            "CONFIG_INVALID: server address %r carries a CIDR netmask; "
            "only bare single-host addresses (or /32) are allowed" % text)
    return str(iface.ip)


def canonicalize_server_address_list(value) -> list:
    """Canonicalize a comma-separated string OR an iterable to bare IPv4s.

    Returns a new list, de-duplicated preserving first-seen order. Raises
    ``ValueError`` (stable ``CONFIG_INVALID`` prefix) on any invalid entry
    or an empty input. Shared by the compose builder, the Docker-CLI
    orchestrator, the demo-console entrypoint and PostgresSnapshotSource —
    one contract, one implementation.
    """
    if isinstance(value, str):
        items = [seg for seg in (part.strip() for part in value.split(","))
                 if seg]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(
            "CONFIG_INVALID: server address list must be a comma-separated "
            "string or a list (got %s)" % type(value).__name__)
    if not items:
        raise ValueError("CONFIG_INVALID: server address list is empty")
    canonical: list = []
    for item in items:
        addr = canonicalize_server_address(item)
        if addr not in canonical:
            canonical.append(addr)
    return canonical


# ── Redaction + argv safety ──────────────────────────────────────────────────

def redact(text: str) -> str:
    """Best-effort immediate redaction (never a publish path)."""
    if not isinstance(text, str):
        return text
    out = _DSN_RE.sub("postgresql://***:***@", text)
    out = _PASSWORD_KV_RE.sub(lambda m: m.group(1) + "***REDACTED***", out)
    out = _SQL_PASSWORD_RE.sub(r"\1'***REDACTED***'", out)
    for pat in _TOKEN_RES:
        out = pat.sub("***REDACTED***", out)
    return out


def assert_argv_safe(argv, secrets=()) -> None:
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
        raise StartupGateError("ARGV_SECRET_LEAK", ",".join(forbidden))


# ── Secret transport (no argv, no logging) ───────────────────────────────────

class SecretFile:
    """Ephemeral secret file: name carries no secret; content never logged.

    The file name is fixed (``postgres.env``); only the path is passed
    around. ``write`` and ``delete`` are the caller's responsibility; a
    partial startup MUST delete it in cleanup.
    """

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._path = self._dir / _SECRET_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def write(self, admin_password: str, reader_password: str) -> None:
        """Write env-file bytes; the value never appears in argv or logs."""
        if self._path.exists():
            raise StartupGateError("SECRET_FILE_EXISTS",
                                   "refusing to overwrite an existing secret file")
        self._dir.mkdir(parents=True, exist_ok=True)
        content = (
            "POSTGRES_USER=mergepilot\n"
            "POSTGRES_PASSWORD=%s\n"
            "POSTGRES_DB=%s\n"
            "MERGEPILOT_READER_PASSWORD=%s\n"
            % (admin_password, DB_NAME, reader_password)
        )
        self._path.write_text(content, encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # Windows: recorded honestly in capability, not enforced

    def delete(self) -> None:
        """Idempotent delete (retry-safe)."""
        if self._path.exists():
            self._path.unlink()

    def exists(self) -> bool:
        return self._path.exists()


class ControllerSecretFile:
    """Secret env-file for the workflow-controller service (retry v3 Fix 3).

    Carries the two secrets controller.py refuses to start without
    (extracted from its own ``__main__`` gate): ``PG_PASS`` (the PostgreSQL
    admin password — same secret class as POSTGRES_PASSWORD) and ``ADMIN_PW``
    (the Matrix admin password used only by the unreachable-by-design
    Matrix domain; a random per-session value in the isolated stack).

    Same transport guarantees as :class:`SecretFile`: fixed file name
    (``controller.env``), values never in argv/logs, 0600 where enforceable,
    refuses to overwrite, idempotent delete.

    Injection hardening (review-gap Fix 4): both values are FULLY validated
    BEFORE the directory is created or any byte is written — a failed
    validation leaves zero residue. Values must be non-empty strings
    containing no ``\\r``, ``\\n`` or NUL (the only characters that could
    fold into additional env-file lines). Exceptions never carry the secret
    values themselves.
    """

    _NAME = "controller.env"

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._path = self._dir / self._NAME

    @property
    def path(self) -> Path:
        return self._path

    _DSN_KEY = "M4F_SNAPSHOT_DSN"

    @staticmethod
    def _validate_m4f_snapshot_dsn(value) -> None:
        """Fail-closed static validation of the snapshot-worker DSN.

        Contract (M8-A1): non-empty string, strictly single line, no
        CR/LF/NUL/space/tab; scheme exactly ``postgresql``; username
        exactly ``snapshot_worker`` (admin/mergepilot rejected); password
        non-empty; host exactly the isolated service name ``postgres``
        (multi-host rejected); port exactly 5432; path exactly
        ``/mergepilot_audit``; no query, no fragment. Only the canonical
        spelling is accepted (percent-encoded or otherwise equivalent
        non-canonical forms are rejected). Errors name the field and
        reason only — the DSN value never appears in exceptions.
        """
        import urllib.parse
        if not isinstance(value, str):
            raise StartupGateError(
                "CONFIG_INVALID",
                "m4f_snapshot_dsn must be a string (got %s)"
                % type(value).__name__)
        if not value.strip():
            raise StartupGateError("CONFIG_INVALID",
                                   "m4f_snapshot_dsn must be non-empty")
        for ch in ("\r", "\n", "\0", " ", "\t"):
            if ch in value:
                raise StartupGateError(
                    "CONFIG_INVALID",
                    "m4f_snapshot_dsn contains a rejected whitespace or "
                    "control character (env-file injection vector)")
        parts = urllib.parse.urlsplit(value)
        problems = []
        if parts.scheme != "postgresql":
            problems.append("scheme must be postgresql")
        if parts.username != "snapshot_worker":
            problems.append("username must be snapshot_worker")
        if not parts.password:
            problems.append("password must be non-empty")
        if parts.hostname != "postgres":
            problems.append("host must be postgres")
        if parts.port != 5432:
            problems.append("port must be explicitly 5432")
        if parts.path != "/mergepilot_audit":
            problems.append("path must be /mergepilot_audit")
        if parts.query:
            problems.append("query is forbidden")
        if parts.fragment:
            problems.append("fragment is forbidden")
        if "," in (parts.netloc or ""):
            problems.append("multi-host is forbidden")
        if problems:
            raise StartupGateError(
                "CONFIG_INVALID",
                "m4f_snapshot_dsn contract violation: " + "; ".join(problems))

    @staticmethod
    def _validate_secret(name: str, value) -> None:
        """Validate one secret BEFORE any filesystem effect (fail-closed).

        Raises StartupGateError(CONFIG_INVALID) on: non-string, empty /
        whitespace-only, or any value containing CR/LF/NUL (env-file line
        injection vectors). The error message names the FIELD only — the
        value never appears in exceptions or logs.
        """
        if not isinstance(value, str):
            raise StartupGateError(
                "CONFIG_INVALID",
                "%s must be a string (got %s)" % (name, type(value).__name__))
        if not value.strip():
            raise StartupGateError("CONFIG_INVALID",
                                   "%s must be non-empty" % name)
        for idx, ch in enumerate(value):
            if ch in ("\r", "\n", "\0"):
                raise StartupGateError(
                    "CONFIG_INVALID",
                    "%s contains a rejected control character at offset %d "
                    "(CR/LF/NUL are env-file line-injection vectors)"
                    % (name, idx))

    def write(self, pg_pass: str, admin_pw: str,
              m4f_snapshot_dsn: str | None = None) -> None:
        # FULL validation of ALL values first — a failure below leaves no
        # directory and no file behind. The optional third secret rides the
        # SAME env-file (M8-A1 single-secret-file model: exactly two lines
        # without it, exactly three with it; never a second env-file).
        self._validate_secret("pg_pass", pg_pass)
        self._validate_secret("admin_pw", admin_pw)
        if m4f_snapshot_dsn is not None:
            self._validate_m4f_snapshot_dsn(m4f_snapshot_dsn)
        if self._path.exists():
            raise StartupGateError("SECRET_FILE_EXISTS",
                                   "refusing to overwrite an existing "
                                   "controller secret file")
        self._dir.mkdir(parents=True, exist_ok=True)
        content = ("PG_PASS=%s\n"
                   "ADMIN_PW=%s\n" % (pg_pass, admin_pw))
        if m4f_snapshot_dsn is not None:
            content += "%s=%s\n" % (self._DSN_KEY, m4f_snapshot_dsn)
        self._path.write_text(content, encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # Windows: recorded honestly in capability, not enforced

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()

    def exists(self) -> bool:
        return self._path.exists()


class ReaderDsnSecretFile:
    """Secret env-file carrying the reader DSN (1-G stabilization sweep).

    BOTH DSN consumers — demo-console (serve.py reads
    ``MERGEPILOT_PG_DSN`` for the read-only PostgreSQL source) and
    preflight (preflight_entrypoint.py requires it for its real DB
    gates) — refused to start under the shipped orchestration because
    neither compose nor the Docker-CLI plan attached any env-file to
    them, and the DSN may never ride ``-e`` argv. This file is the
    established transport for it: fixed name (``demo_console.env``),
    single ``MERGEPILOT_PG_DSN=`` line, validate-before-write with zero
    residue, values never in argv/logs, 0600 where enforceable, refuses
    to overwrite, idempotent delete.
    """

    _NAME = "demo_console.env"
    _KEY = "MERGEPILOT_PG_DSN"

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._path = self._dir / self._NAME

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _validate_dsn(dsn) -> None:
        """Fail-closed validation BEFORE any filesystem effect.

        The DSN must be a single-line postgresql:// URL without CR/LF/NUL
        (env-file line-injection vectors) or whitespace. The error names
        the field only — the value never appears in exceptions or logs.
        """
        if not isinstance(dsn, str):
            raise StartupGateError(
                "CONFIG_INVALID",
                "reader dsn must be a string (got %s)"
                % type(dsn).__name__)
        if not dsn.strip():
            raise StartupGateError("CONFIG_INVALID",
                                   "reader dsn must be non-empty")
        if not dsn.startswith("postgresql://"):
            raise StartupGateError("CONFIG_INVALID",
                                   "reader dsn must use the postgresql:// "
                                   "scheme")
        for idx, ch in enumerate(dsn):
            if ch in ("\r", "\n", "\0", " ", "\t"):
                raise StartupGateError(
                    "CONFIG_INVALID",
                    "reader dsn contains a rejected character at offset %d "
                    "(CR/LF/NUL/whitespace are env-file injection vectors)"
                    % idx)

    def write(self, dsn: str) -> None:
        self._validate_dsn(dsn)
        if self._path.exists():
            raise StartupGateError("SECRET_FILE_EXISTS",
                                   "refusing to overwrite an existing "
                                   "reader-DSN secret file")
        self._dir.mkdir(parents=True, exist_ok=True)
        content = "%s=%s\n" % (self._KEY, dsn)
        self._path.write_text(content, encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # Windows: recorded honestly in capability, not enforced

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()

    def exists(self) -> bool:
        return self._path.exists()


# ── Demo-console runtime input validation ────────────────────────────────────

def _validate_demo_console_runtime_inputs(demo_console_run_id: str,
                                          demo_console_pg_server_addresses: str
                                          ) -> None:
    """Fail-closed validation of the caller-provided demo-console inputs.

    ``demo_console_run_id``: the seeded task_runs.run_id — REQUIRED, no
    default, no inference (hardcoding a run_id is forbidden).

    ``demo_console_pg_server_addresses``: the postgres container's MEASURED
    bridge IP (comma-separated for multiple entries) — REQUIRED. It must be
    measured AFTER postgres is healthy and injected by the orchestrator;
    hardcoding it (or substituting the network alias ``postgres``) is
    rejected because the expected-identity gate compares it against the
    real ``inet_server_addr()`` observed at connection time. Entries may be
    bare (``172.18.0.2``) or single-host CIDR (``172.18.0.2/32``) — both
    canonicalize to the same bare IPv4 via the shared canonicalizer.
    """
    if not demo_console_run_id or not demo_console_run_id.strip():
        raise StartupGateError(
            "CONFIG_INVALID",
            "demo_console_run_id is required (no default, no inference); "
            "the caller must provide the seeded run_id")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", demo_console_run_id):
        raise StartupGateError(
            "CONFIG_INVALID",
            "demo_console_run_id must match ^[a-zA-Z0-9_-]+$")
    if not demo_console_pg_server_addresses or \
            not demo_console_pg_server_addresses.strip():
        raise StartupGateError(
            "CONFIG_INVALID",
            "demo_console_pg_server_addresses is required (no default, no "
            "inference); the orchestrator must measure the postgres "
            "container's bridge IP after postgres is healthy and inject it "
            "(hardcoding is forbidden)")
    try:
        canonicalize_server_address_list(demo_console_pg_server_addresses)
    except ValueError as exc:
        raise StartupGateError("CONFIG_INVALID", str(exc)) from None


def _demo_console_environment(demo_console_run_id: str,
                              demo_console_pg_server_addresses: str) -> dict:
    """The demo-console container environment (single source of truth).

    Shared by ``build_compose_config`` (compose path) and
    ``plan_orchestrated_start`` (Docker-CLI path) so the two paths can never
    drift. Non-secret values only; the reader DSN travels separately via the
    secret env-file and is never placed here. Server addresses are emitted
    in CANONICAL bare-IPv4 form (``172.18.0.2/32`` → ``172.18.0.2``).
    """
    _validate_demo_console_runtime_inputs(demo_console_run_id,
                                          demo_console_pg_server_addresses)
    canonical_addrs = ",".join(
        canonicalize_server_address_list(demo_console_pg_server_addresses))
    return {
        "MERGEPILOT_MODE": "isolated_live",
        "MERGEPILOT_SOURCE_KIND": "postgres",
        "MERGEPILOT_RUN_ID": demo_console_run_id,
        "MERGEPILOT_EXPECTED_ROLE": READER_ROLE,
        "MERGEPILOT_BIND_CONTEXT": "container",
        "MERGEPILOT_HOST": "0.0.0.0",
        "MERGEPILOT_PORT": str(DEMO_CONSOLE_PORT),
        "MERGEPILOT_PG_EXPECTED_DATABASE": DB_NAME,
        "MERGEPILOT_PG_ENVIRONMENT_ID": ENVIRONMENT_MARKER,
        "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES": canonical_addrs,
        "MERGEPILOT_PG_EXPECTED_SERVER_PORT": "5432",
        "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME": APP_NAME,
    }


_CONTROLLER_ENV_KEYS_BASE = ("PG_PASS", "ADMIN_PW")
_CONTROLLER_ENV_KEY_M4F = "M4F_SNAPSHOT_DSN"


def _validate_controller_env_file_contract(path, *,
                                           m4f_event_machinery: bool) -> None:
    """Unconditionally validate the controller secret env-file contract.

    Strict line-oriented parse (no lenient dotenv semantics): blank lines,
    comment lines, lines without '=', CR/NUL bytes, duplicate keys and
    unknown keys are all rejected. The exact key set depends on the
    feature flag — ``{PG_PASS, ADMIN_PW}`` when the M4F event machinery is
    off (a present-but-unusable M4F_SNAPSHOT_DSN is a purposeless secret
    delivery and is rejected), plus ``M4F_SNAPSHOT_DSN`` exactly once when
    it is on (whose value then passes the full snapshot-worker DSN static
    contract). Errors name the key and reason only — values (secrets)
    never appear in exceptions.
    """
    allowed = set(_CONTROLLER_ENV_KEYS_BASE)
    if m4f_event_machinery:
        allowed.add(_CONTROLLER_ENV_KEY_M4F)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise StartupGateError(
            "CONFIG_INVALID",
            "controller env file unreadable (cannot validate the secret "
            "contract); refusing to plan any start") from None
    if "\r" in raw or "\0" in raw:
        raise StartupGateError(
            "CONFIG_INVALID",
            "controller env file contains CR/NUL (injection vector)")
    seen = {}
    for lineno, line in enumerate(raw.split("\n"), start=1):
        if line == "":
            if lineno == len(raw.split("\n")):
                continue   # trailing newline after the last entry is normal
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller env file line %d: blank lines are forbidden"
                % lineno)
        if line.lstrip().startswith("#"):
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller env file line %d: comment lines are forbidden"
                % lineno)
        if "=" not in line:
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller env file line %d: line lacks '='" % lineno)
        key, value = line.split("=", 1)
        if key not in allowed:
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller env file line %d: unknown or forbidden key"
                " (m4f_event_machinery=%s)" % (lineno, m4f_event_machinery))
        if key in seen:
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller env file line %d: duplicate key" % lineno)
        if not value:
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller env file line %d: empty value" % lineno)
        seen[key] = value
    expected = allowed
    missing = sorted(expected - set(seen))
    if missing:
        raise StartupGateError(
            "CONFIG_INVALID",
            "controller env file missing required key(s): %s "
            "(m4f_event_machinery=%s)" % (missing, m4f_event_machinery))
    if m4f_event_machinery:
        ControllerSecretFile._validate_m4f_snapshot_dsn(
            seen[_CONTROLLER_ENV_KEY_M4F])


def _controller_environment() -> dict:
    """Non-secret controller env (single source of truth, retry v3 Fix 3).

    Variable names are extracted from tools/workflow-controller/controller.py
    (PG_HOST/PG_PORT/PG_DATABASE/PG_USER). The secrets PG_PASS/ADMIN_PW are
    NOT here — they travel via the controller secret env-file.
    """
    return {
        "PG_HOST": "postgres",       # in-network alias; the code default
                                     # 'audit-pg' does not exist in this stack
        "PG_PORT": "5432",
        "PG_DATABASE": DB_NAME,
        "PG_USER": CONTROLLER_PG_USER,
    }


def _gateway_environment() -> dict:
    """Non-secret policy-gateway env (single source of truth, Fix 3).

    UPSTREAM_URL is required by gateway.py's lifespan (it exits after 30
    failed connect attempts). In the isolated stack it points at the
    in-container zero-tool stub — a REAL, reachable endpoint — so the
    gateway genuinely runs and its healthcheck is meaningful. Non-secret:
    it is a loopback URL.
    """
    return {
        "UPSTREAM_URL": GATEWAY_ISOLATED_UPSTREAM_URL,
    }


# ── docker-compose configuration ─────────────────────────────────────────────

def build_compose_config(*, demo_console_port: int = DEMO_CONSOLE_PORT,
                         project_name: str = "mergepilot-isolated",
                         admin_password_secret: str = "<secret-file>",
                         controller_secret: str = "<controller-secret-file>",
                         reader_dsn_secret: str = "<reader-dsn-secret-file>",
                         gh_webhook_secret: str = "<gh-webhook-secret-file>",
                         demo_console_run_id: str = "",
                         demo_console_pg_server_addresses: str = "",
                         m4f_event_machinery: bool = False,
                         ) -> dict:
    """Build the five-service compose configuration as a pure dict.

    Guarantees:
      - the ONLY image reference is the digest-pinned pgvector
        (controller/policy-gateway/demo-console/preflight use build contexts
        with ``pull_policy: never`` — no implicit image pulls)
      - the ONLY published port is demo-console, bound to 127.0.0.1 on the
        HOST side (postgres is NEVER published; inter-service traffic stays
        on the private compose network; the demo-console CONTAINER listens
        on 0.0.0.0 internally — required for Docker bridge routing)
      - explicit service dependency order with healthcheck-gated conditions
      - passwords travel via the env-file (secret file), never argv
      - ``demo_console_run_id`` is REQUIRED (no default, no inference);
        missing/empty/invalid charset -> CONFIG_INVALID (fail-closed)
      - ``demo_console_pg_server_addresses`` is REQUIRED (the MEASURED
        postgres bridge IP; hardcoding or alias substitution is rejected)
    """
    demo_env = _demo_console_environment(
        demo_console_run_id, demo_console_pg_server_addresses)
    if not (0 < demo_console_port < 65536):
        raise StartupGateError("CONFIG_INVALID", "demo_console_port out of range")
    services = {
        "postgres": {
            "image": PGVECTOR_IMAGE_DIGEST,
            "pull_policy": "never",
            "env_file": "<secret-file>",
            "environment": {
                "PGDATA": "/tmp/pgdata",
            },
            "networks": ["isolated"],
            "healthcheck": {
                "test": ["CMD-SHELL",
                         "pg_isready -U mergepilot -d %s" % DB_NAME],
                "interval": _HEALTHCHECK_INTERVAL,
                "timeout": _HEALTHCHECK_TIMEOUT,
                "retries": _HEALTHCHECK_RETRIES,
            },
            # NOT published: no "ports" key on purpose.
        },
        "policy-gateway": {
            "build": {"context": ".", "dockerfile": "Dockerfile.policy-gateway"},
            "pull_policy": "never",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "networks": ["isolated"],
            # Retry v3 Fix 3: UPSTREAM_URL (non-secret) points at the
            # in-container zero-tool stub so the real lifespan completes.
            # The healthcheck probes the gateway's own listen port — uvicorn
            # only binds AFTER the upstream session is established, so
            # healthy means fully up (an exited/retrying gateway never
            # reports healthy; there is no standby state).
            "environment": _gateway_environment(),
            "healthcheck": {
                "test": ["CMD", "python", "/app/healthcheck.py"],
                "interval": _HEALTHCHECK_INTERVAL,
                "timeout": _HEALTHCHECK_TIMEOUT,
                "retries": _HEALTHCHECK_RETRIES,
            },
        },
        "controller": {
            "build": {"context": ".", "dockerfile": "Dockerfile.controller"},
            "pull_policy": "never",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
                "policy-gateway": {"condition": "service_healthy"},
            },
            "networks": ["isolated"],
            # Retry v3 Fix 3: secrets (PG_PASS/ADMIN_PW) travel via the
            # orchestrator-created secret env-file, never compose literals;
            # the non-secret DB coordinates are explicit (the code default
            # PG_HOST='audit-pg' does not exist in this stack).
            # M8-A1: the optional M4F_SNAPSHOT_DSN rides the SAME env-file
            # (third line, written only when the machinery is opted in);
            # M4F_ENABLED itself is a NON-secret flag injected below.
            "env_file": controller_secret,
            "environment": dict(
                _controller_environment(),
                **({"M4F_ENABLED": "1"} if m4f_event_machinery else {})),
            # Real liveness: the controller has no listen port, so the probe
            # is a TCP connect from INSIDE the container to the configured
            # PostgreSQL — passing means the container is alive AND the DB
            # path is up. An exited controller never reports healthy.
            "healthcheck": {
                "test": ["CMD", "python", "/app/healthcheck.py"],
                "interval": _HEALTHCHECK_INTERVAL,
                "timeout": _HEALTHCHECK_TIMEOUT,
                "retries": _HEALTHCHECK_RETRIES,
            },
        },
        "demo-console": {
            "build": {"context": ".", "dockerfile": "Dockerfile.demo-console"},
            "pull_policy": "never",
            "depends_on": {
                # Retry v3: healthy, not merely started — the full stack
                # requires controller (and transitively gateway) to pass
                # their real healthchecks before the console starts.
                "controller": {"condition": "service_healthy"},
            },
            "networks": ["isolated"],
            # Review-gap Fix 3: REAL readiness — loopback HTTP probe of
            # /api/live/status (200 + JSON + POSTGRES_ISOLATED +
            # source_read_only + startup snapshot available).
            "healthcheck": {
                "test": ["CMD", "python", "/app/console_healthcheck.py"],
                "interval": _HEALTHCHECK_INTERVAL,
                "timeout": _HEALTHCHECK_TIMEOUT,
                "retries": _HEALTHCHECK_RETRIES,
            },
            # Entrypoint contract (demo_console_entrypoint.py): ISOLATED_LIVE
            # mode, postgres source, canonical reader role, CALLER-PROVIDED
            # run_id and MEASURED bridge IP, explicit container bind context.
            # REPLAY is refused (no fallback). The container listens on
            # 0.0.0.0 internally (required for Docker bridge routing).
            # 1-G stabilization sweep: the reader DSN (serve.py reads
            # MERGEPILOT_PG_DSN) travels via this orchestrator-created
            # secret env-file — never compose literals, never -e argv.
            # 1-G network design: UNPUBLISHED — Docker drops -p on
            # internal networks and the DSN-bearing console must never
            # gain an external default route; the loopback publish moved
            # to the secretless console-edge.
            "env_file": reader_dsn_secret,
            "environment": demo_env,
            # NOT published: no "ports" key on purpose.
        },
        # M8-GH-3: gh-webhook receiver in the compose config — mirrors the
        # versioned docker-compose.yml block (loopback publish 8090, secret
        # env-file, hardened runtime flags; secrets NEVER in literals).
        "gh-webhook": {
            "build": {"context": ".",
                      "dockerfile": "Dockerfile.gh-webhook"},
            "pull_policy": "never",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "networks": ["isolated", "console-publish"],
            "ports": [
                "%s:%d:%d" % (LOOPBACK_BIND, GH_WEBHOOK_PORT,
                              GH_WEBHOOK_PORT),
            ],
            "env_file": gh_webhook_secret,
            "read_only": True,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "tmpfs": ["/tmp"],
            "healthcheck": {
                "test": ["CMD", "python", "-c",
                         "import socket;"
                         "s=socket.create_connection("
                         "(\"127.0.0.1\",%d),timeout=2);s.close()"
                         % GH_WEBHOOK_PORT],
                "interval": _HEALTHCHECK_INTERVAL,
                "timeout": _HEALTHCHECK_TIMEOUT,
                "retries": _HEALTHCHECK_RETRIES,
            },
            "restart": "no",
        },
        "console-edge": {
            "build": {"context": ".",
                      "dockerfile": "Dockerfile.console-edge"},
            "pull_policy": "never",
            "depends_on": {
                "demo-console": {"condition": "service_healthy"},
            },
            "networks": ["isolated", "console-publish"],
            # 1-G network design: SECRETLESS publication plumbing (not a
            # fifth application service; NOT application integration).
            # The ONLY published port in the stack, loopback-only.
            "ports": [
                "%s:%d:8600" % (LOOPBACK_BIND, demo_console_port),
            ],
            "healthcheck": {
                "test": ["CMD", "python", "/app/console_edge_healthcheck.py"],
                "interval": _HEALTHCHECK_INTERVAL,
                "timeout": _HEALTHCHECK_TIMEOUT,
                "retries": _HEALTHCHECK_RETRIES,
            },
            "restart": "no",
            # No env_file and no environment by design: the edge holds no
            # secrets and its upstream is a code constant.
        },
        "preflight": {
            "build": {"context": ".", "dockerfile": "Dockerfile.preflight"},
            "pull_policy": "never",
            "depends_on": {
                # Review-gap Fix 3: run the gate matrix only after the
                # demo-console is genuinely READY (the matrix's own
                # fail-closed http_endpoint gate still re-verifies).
                "demo-console": {"condition": "service_healthy"},
                # 1-G network design: full-stack readiness includes the
                # publication edge healthy (its healthcheck proves the
                # fixed upstream chain answers 200/POSTGRES_ISOLATED/
                # read-only).
                "console-edge": {"condition": "service_healthy"},
            },
            "networks": ["isolated"],
            # 1-G stabilization sweep: preflight's REAL DB gates connect
            # with the reader DSN (preflight_entrypoint.py reads
            # MERGEPILOT_PG_DSN and exits without it) — same secret
            # env-file transport as demo-console, never argv.
            "env_file": reader_dsn_secret,
            "environment": {
                # preflight reaches PostgreSQL INSIDE the internal network.
                "MERGEPILOT_PG_HOST": "postgres",
                "MERGEPILOT_PG_PORT": "5432",
                "MERGEPILOT_DEMO_CONSOLE_URL": "http://demo-console:8600",
            },
            # The preflight container runs the gate matrix and exits.
            "restart": "no",
        },
    }
    return {
        "name": project_name,
        "_m4f_event_machinery": bool(m4f_event_machinery),
        "services": services,
        "networks": {
            "isolated": {"driver": "bridge", "internal": True},
            "console-publish": {"driver": "bridge"},
        },
        # No volumes: the stack is one-shot; PGDATA lives in-container.
        "volumes": {},
    }


def validate_compose_config(config: dict) -> None:
    """Static contract validation of the compose configuration."""
    if not isinstance(config, dict):
        raise StartupGateError("COMPOSE_INVALID", "config not a dict")
    services = config.get("services", {})
    for name in SERVICE_ORDER:
        if name not in services:
            raise StartupGateError("COMPOSE_INVALID",
                                   "missing service %s" % name)
    # Image discipline: the only literal image is the digest-pinned pgvector.
    pg = services["postgres"]
    if pg.get("image") != PGVECTOR_IMAGE_DIGEST:
        raise StartupGateError("IMAGE_DIGEST_MISMATCH",
                               "postgres image is not digest-pinned")
    for name, svc in services.items():
        pull = svc.get("pull_policy")
        if pull != "never":
            raise StartupGateError("COMPOSE_INVALID",
                                   "service %s allows implicit pull" % name)
        if "image" in svc and name != "postgres":
            raise StartupGateError("COMPOSE_INVALID",
                                   "service %s uses an unpinned image" % name)
    # 1-G network design + M8-GH-3: ONLY console-edge and gh-webhook
    # publish, ONLY on loopback, ONLY on their canonical ports.
    # demo-console must NOT publish (Docker drops -p on internal networks;
    # the DSN-bearing console stays internal-only and the publish moved to
    # the secretless edge).
    _PUBLISHED = {"console-edge": DEMO_CONSOLE_PORT,
                  "gh-webhook": GH_WEBHOOK_PORT}
    for name, svc in services.items():
        ports = svc.get("ports") or []
        if ports and name not in _PUBLISHED:
            raise StartupGateError("BIND_NOT_LOOPBACK",
                                   "service %s publishes ports" % name)
        for p in ports:
            bind = str(p).split(":", 1)[0]
            if bind != LOOPBACK_BIND:
                raise StartupGateError("BIND_NOT_LOOPBACK",
                                       "port bind %r is not %s" % (bind, LOOPBACK_BIND))
            host_port = str(p).split(":", 1)[1].split(":")[0]
            if host_port != str(_PUBLISHED[name]):
                raise StartupGateError("BIND_NOT_LOOPBACK",
                                       "%s publish port must be %d"
                                       % (name, _PUBLISHED[name]))
    if "ports" in services["postgres"]:
        raise StartupGateError("BIND_NOT_LOOPBACK", "postgres publishes ports")
    if "ports" in services["demo-console"]:
        raise StartupGateError("BIND_NOT_LOOPBACK",
                               "demo-console must NOT publish (internal-only; "
                               "use the secretless console-edge)")

    # Network topology (1-G): isolated stays internal-only; the
    # publication bridge is a NORMAL network; ONLY console-edge may join
    # the publication bridge; the edge must be on BOTH networks.
    networks = config.get("networks", {})
    if networks.get("isolated", {}).get("internal") is not True:
        raise StartupGateError("COMPOSE_INVALID",
                               "isolated network must be internal")
    if networks.get("console-publish", {}).get("internal") is True:
        raise StartupGateError("COMPOSE_INVALID",
                               "console-publish must NOT be internal (Docker "
                               "drops port publishing on internal networks)")
    if "console-publish" not in networks:
        raise StartupGateError("COMPOSE_INVALID",
                               "console-publish network missing")
    secret_services = ("postgres", "policy-gateway", "controller",
                       "demo-console", "preflight")
    for name in secret_services:
        nets = set(services[name].get("networks") or [])
        if "console-publish" in nets:
            raise StartupGateError(
                "COMPOSE_INVALID",
                "secret-bearing service %s must not join console-publish"
                % name)
        if "isolated" not in nets:
            raise StartupGateError("COMPOSE_INVALID",
                                   "service %s must join isolated" % name)
    edge_nets = set(services["console-edge"].get("networks") or [])
    if edge_nets != {"isolated", "console-publish"}:
        raise StartupGateError(
            "COMPOSE_INVALID",
            "console-edge must be on exactly [isolated, console-publish]")
    # The edge carries NO secrets of any kind: no env_file, no environment
    # at all (its upstream is a code constant).
    if services["console-edge"].get("env_file"):
        raise StartupGateError("COMPOSE_INVALID",
                               "console-edge must not carry an env_file")
    if services["console-edge"].get("environment"):
        raise StartupGateError("COMPOSE_INVALID",
                               "console-edge must not carry environment "
                               "(fixed-upstream plumbing; secrets/DSN/db "
                               "coordinates forbidden)")
    if "healthcheck" not in services["console-edge"]:
        raise StartupGateError("COMPOSE_INVALID",
                               "console-edge lacks healthcheck")
    # Full-stack readiness requires the edge healthy before preflight.
    edge_cond = ((services["preflight"].get("depends_on") or {})
                 .get("console-edge", {}).get("condition"))
    if edge_cond != "service_healthy":
        raise StartupGateError(
            "COMPOSE_INVALID",
            "preflight must depend on console-edge service_healthy "
            "(full-stack readiness includes the publication edge)")
    # Healthcheck present on postgres.
    if "healthcheck" not in services["postgres"]:
        raise StartupGateError("COMPOSE_INVALID", "postgres lacks healthcheck")
    # Retry v3 Fix 3 (+ review-gap Fix 3): controller, policy-gateway and
    # demo-console must carry REAL healthchecks — an exited or
    # still-retrying service must never be treated as standby/healthy.
    for name in ("controller", "policy-gateway", "demo-console"):
        if "healthcheck" not in services[name]:
            raise StartupGateError("COMPOSE_INVALID",
                                   "%s lacks healthcheck" % name)
    # Healthy-edge dependency conditions (review-gap Fix 3 + v3 Fix 3):
    # the dependent services wait for REAL readiness, never 'started'.
    for svc, dep in (("controller", "policy-gateway"),
                     ("demo-console", "controller"),
                     ("preflight", "demo-console")):
        cond = ((services[svc].get("depends_on") or {})
                .get(dep, {}).get("condition"))
        if cond != "service_healthy":
            raise StartupGateError(
                "COMPOSE_INVALID",
                "%s must depend on %s service_healthy (got %r)"
                % (svc, dep, cond))
    # Retry v3 Fix 3: controller secrets ride the secret env-file (never
    # compose literals); its non-secret DB coordinates must be explicit.
    ctrl = services["controller"]
    if not ctrl.get("env_file"):
        raise StartupGateError("COMPOSE_INVALID",
                               "controller lacks secret env_file")
    ctrl_env = ctrl.get("environment") or {}
    for key, value in _controller_environment().items():
        if ctrl_env.get(key) != value:
            raise StartupGateError(
                "COMPOSE_INVALID",
                "controller env %s must be %r (got %r)"
                % (key, value, ctrl_env.get(key)))
    # M8-A1 opt-in biconditional: M4F_ENABLED appears in the controller
    # environment IFF the machinery was opted in (never a free env var,
    # never a default-on).
    has_m4f = "M4F_ENABLED" in ctrl_env
    if has_m4f != bool(config.get("_m4f_event_machinery", False)):
        raise StartupGateError(
            "COMPOSE_INVALID",
            "controller M4F_ENABLED must match the m4f_event_machinery "
            "opt-in flag (got key=%s)" % has_m4f)
    if has_m4f and ctrl_env.get("M4F_ENABLED") != "1":
        raise StartupGateError(
            "COMPOSE_INVALID", "controller M4F_ENABLED must be '1'")
    for secret_key in ("PG_PASS", "ADMIN_PW"):
        if secret_key in ctrl_env:
            raise StartupGateError(
                "COMPOSE_INVALID",
                "controller secret %s must travel via env_file, never the "
                "compose environment" % secret_key)
    # 1-G stabilization sweep: BOTH reader-DSN consumers must carry the
    # secret env-file (the DSN never appears in compose environment).
    for name in ("demo-console", "preflight"):
        svc = services[name]
        if not svc.get("env_file"):
            raise StartupGateError("COMPOSE_INVALID",
                                   "%s lacks the reader-DSN secret env_file"
                                   % name)
        if "MERGEPILOT_PG_DSN" in (svc.get("environment") or {}):
            raise StartupGateError(
                "COMPOSE_INVALID",
                "%s secret MERGEPILOT_PG_DSN must travel via env_file, "
                "never the compose environment" % name)
    # Retry v3 Fix 3: the gateway must declare its upstream explicitly.
    gw_env = services["policy-gateway"].get("environment") or {}
    if not gw_env.get("UPSTREAM_URL"):
        raise StartupGateError("COMPOSE_INVALID",
                               "policy-gateway lacks UPSTREAM_URL")
    if "AUDIT_DSN" in gw_env or "L2_DSN" in gw_env:
        raise StartupGateError(
            "COMPOSE_INVALID",
            "policy-gateway DSNs are secrets and must travel via env_file")
    # Internal-only network (no external exposure beyond the loopback port).
    net = config.get("networks", {}).get("isolated", {})
    if net.get("internal") is not True:
        raise StartupGateError("COMPOSE_INVALID",
                               "isolated network must be internal")
    # Dependency order: postgres → policy-gateway → controller →
    # demo-console → preflight.
    expected_deps = {
        "postgres": set(),
        "policy-gateway": {"postgres"},
        "controller": {"postgres", "policy-gateway"},
        "demo-console": {"controller"},
        "console-edge": {"demo-console"},
        "preflight": {"demo-console", "console-edge"},
    }
    for name, expected in expected_deps.items():
        deps = set((services[name].get("depends_on") or {}).keys())
        if deps != expected:
            raise StartupGateError(
                "COMPOSE_INVALID",
                "service %s dependencies %s != expected %s"
                % (name, sorted(deps), sorted(expected)))
    # No volumes (one-shot stack).
    if config.get("volumes"):
        raise StartupGateError("COMPOSE_INVALID", "volumes are not allowed")


def compose_dependency_order(config: dict) -> list:
    """Topological service start order derived from depends_on."""
    services = config.get("services", {})
    order: list = []
    resolved: set = set()
    remaining = dict((n, set((s.get("depends_on") or {}).keys()))
                     for n, s in services.items())
    while remaining:
        progressed = False
        for name in sorted(remaining):
            if remaining[name] <= resolved:
                order.append(name)
                resolved.add(name)
                del remaining[name]
                progressed = True
                break
        if not progressed:
            raise StartupGateError("COMPOSE_INVALID",
                                   "circular or unresolvable service dependency")
    return order


# ── Preflight gate matrix ────────────────────────────────────────────────────

PREFLIGHT_CHECKS = (
    "docker_daemon_identity",
    "image_digest_cached",
    "postgres_health",
    "database_connectivity",
    "server_identity",
    "environment_marker",
    "reader_acl",
    "read_only_transaction",
    "source_kind",
    "http_endpoint",
)


def run_preflight_gates(checks: dict) -> dict:
    """Run the preflight matrix from INJECTED check callables (no real env).

    ``checks`` maps each name in :data:`PREFLIGHT_CHECKS` to a zero-arg
    callable. A check signals failure by raising ``StartupGateError`` with
    a stable code; the matrix stops at the FIRST failure (fail-closed) and
    returns ``{"ok": False, "failed_check": ..., "error_code": ...}``.
    On success returns ``{"ok": True, "executed": [...], "results": {...}}``
    where ``results`` carries each check's return value.
    """
    missing = [c for c in PREFLIGHT_CHECKS if c not in checks]
    if missing:
        raise StartupGateError("CONFIG_INVALID",
                               "missing check(s): %s" % ",".join(missing))
    executed: list = []
    results: dict = {}
    for name in PREFLIGHT_CHECKS:
        try:
            results[name] = checks[name]()
        except StartupGateError as exc:
            return {
                "ok": False,
                "failed_check": name,
                "error_code": exc.code,
                "executed": executed,
                "results": results,
            }
        executed.append(name)
    return {"ok": True, "executed": executed, "results": results}


# ── Retryable cleanup (never swallows primary or cleanup errors) ────────────

def one_click_cleanup(secret_file: SecretFile | None = None,
                      stop_fn: Callable[[], None] | None = None) -> None:
    """Cleanup with stable codes; safe to re-run; never masks errors.

    Order: stop the stack (injected), delete the secret file (idempotent).
    Failures become stable codes carried on ``StartupCleanupError``.
    """
    codes: list = []
    if stop_fn is not None:
        try:
            stop_fn()
        except BaseException:
            codes.append("STACK_STOP_FAILED")
    if secret_file is not None:
        try:
            secret_file.delete()
        except BaseException:
            codes.append("SECRET_DELETE_FAILED")
        if secret_file.exists():
            codes.append("SECRET_FILE_STILL_PRESENT")
    if codes:
        raise StartupCleanupError("CLEANUP_RESIDUE", codes)


# ── Convenience: rendered YAML contract (structure-only) ─────────────────────

def compose_ports_binding(config: dict) -> dict:
    """Return {service: [bind spec]} for every published port (audit hook)."""
    out = {}
    for name, svc in config.get("services", {}).items():
        ports = svc.get("ports") or []
        if ports:
            out[name] = [str(p) for p in ports]
    return out


# ── Built-image identity registry (Phase 1-C) ────────────────────────────────
# Built services must record their IMMUTABLE identity (image ID / digest) at
# build time; a floating tag is never authoritative. The registry maps
# service -> recorded identity; it is populated by the (authorized) build
# round and checked by the orchestrator before start.

BUILT_SERVICES = ("policy-gateway", "controller", "demo-console",
                  "console-edge", "preflight", "gh-webhook")

_builtin_registry: dict = {}


def record_built_image_identity(service: str, image_id: str) -> None:
    """Record the immutable identity of a built service image.

    ``image_id`` must be ``sha256:<64-hex>``; a floating tag raises
    CONFIG_INVALID. Once recorded for a service, the identity is IMMUTABLE —
    a second, different value raises IMAGE_DIGEST_MISMATCH (fail-closed).
    """
    if service not in BUILT_SERVICES:
        raise StartupGateError("CONFIG_INVALID",
                               "unknown built service %r" % service)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id or ""):
        raise StartupGateError("CONFIG_INVALID",
                               "image identity must be sha256:<64-hex>")
    existing = _builtin_registry.get(service)
    if existing is not None and existing != image_id:
        raise StartupGateError("IMAGE_DIGEST_MISMATCH",
                               "recorded identity changed for %s" % service)
    _builtin_registry[service] = image_id


def get_built_image_identity(service: str) -> str:
    """Return the recorded immutable identity (or raise CONFIG_INVALID)."""
    if service not in _builtin_registry:
        raise StartupGateError("CONFIG_INVALID",
                               "no recorded identity for %s (build first)" % service)
    return _builtin_registry[service]


def built_identity_registry() -> dict:
    """Read-only snapshot of the recorded identities."""
    return dict(_builtin_registry)


# ── Docker-CLI orchestrator (Phase 1-C; no compose CLI required) ────────────

ORCHESTRATOR_NETWORK = "mergepilot-isolated-isolated"
# 1-G network design: the publication bridge is a NORMAL (non-internal)
# network so Docker actually wires the loopback publish; only the
# secretless console-edge attaches to it.
PUBLICATION_NETWORK = "mergepilot-isolated-publication"

_SERVICE_FLAGS = {
    # name: (aliases, publish-spec or None, healthcheck-cmd or None)
    "postgres": (["postgres"], None,
                 ["pg_isready", "-U", "mergepilot",
                  "-d", DB_NAME]),
    # Retry v3 Fix 3: REAL healthchecks for controller/gateway. Both probe
    # via the /app/healthcheck.py file shipped in their images (exec-form,
    # no shell quoting hazards). The gateway probe is a TCP connect to its
    # own listen port — uvicorn only binds after the upstream session is
    # up, so healthy == fully started. The controller probe is a TCP connect
    # to the configured PostgreSQL from inside the container. An exited
    # service never reports healthy (there is no standby state).
    "policy-gateway": (["policy-gateway"], None,
                       ["python", "/app/healthcheck.py"]),
    "controller": (["controller"], None,
                   ["python", "/app/healthcheck.py"]),
    # 1-G network design: the console is UNPUBLISHED (internal-only; the
    # loopback publish moved to the secretless console-edge).
    "demo-console": (["demo-console"], None,
                     ["python", "/app/console_healthcheck.py"]),
    "console-edge": (["console-edge"],
                     "%s:%d:8600" % (LOOPBACK_BIND, DEMO_CONSOLE_PORT),
                     ["python", "/app/console_edge_healthcheck.py"]),
    # M8-GH-3: gh-webhook publishes its receiver on loopback (like the
    # console-edge, it must be CREATED on the publication bridge or Docker
    # silently drops the publish on the internal network). The health-cmd
    # is shell-form (docker runs it via /bin/sh -c): the python -c body
    # MUST be single-quoted with double-quoted URL inside — a bare
    # `python -c import ...` fails with a sh syntax error (real-Docker
    # E2E finding).
    "gh-webhook": (["gh-webhook"],
                   "%s:%d:%d" % (LOOPBACK_BIND, GH_WEBHOOK_PORT,
                                 GH_WEBHOOK_PORT),
                   ["python", "-c",
                    "'import socket;s=socket.create_connection("
                    "(\"127.0.0.1\",%d),timeout=2);s.close()'"
                    % GH_WEBHOOK_PORT]),
    "preflight": (["preflight"], None, None),
}


def plan_network_create() -> list:
    """argv array creating the internal-only network."""
    return ["network", "create", "--internal", "--driver", "bridge",
            ORCHESTRATOR_NETWORK]


def plan_publication_network_create() -> list:
    """argv array creating the publication bridge (NORMAL network).

    1-G network design: Docker silently drops ``-p`` publishing on
    internal networks, so the loopback publish lives on this normal
    bridge. Only the secretless console-edge attaches to it."""
    return ["network", "create", "--driver", "bridge",
            PUBLICATION_NETWORK]


def plan_service_run(service: str, *, image_ref: str,
                     env_file: str | None = None,
                     declared_pg_image: str | None = None,
                     demo_console_env: dict | None = None,
                     controller_env: dict | None = None,
                     gateway_env: dict | None = None,
                     reader_dsn_env_file: str | None = None,
                     m4f_enabled: bool = False) -> list:
    """argv array (docker sub-args) to run one service per the contract.

    ``image_ref`` is the digest/image-ID (never a floating tag). The plan
    encodes: internal network + alias, pull never, restart no, no published
    ports except demo-console loopback, healthchecks (postgres, controller,
    policy-gateway), secret env-files (postgres, controller), and the
    in-network preflight environment.

    ``demo_console_env`` / ``controller_env`` / ``gateway_env`` are REQUIRED
    for their respective services (the extracted entrypoint contracts —
    fail-closed CONFIG_INVALID when missing). Non-secret values only;
    ``assert_argv_safe`` rejects a DSN or password smuggled into the values.
    """
    if service not in _SERVICE_FLAGS:
        raise StartupGateError("CONFIG_INVALID",
                               "unknown service %r" % service)
    if service == "console-edge":
        # 1-G network design: the edge MUST be created via its dedicated
        # plan (primary network = the NON-internal publication bridge, or
        # Docker silently drops the port publish — the retry-5 failure).
        # Creating it on the internal network first and connecting the
        # publication bridge afterwards reproduces Ports=null.
        raise StartupGateError(
            "CONFIG_INVALID",
            "console-edge must use plan_console_edge_run (creation on the "
            "internal network would silently drop the loopback publish)")
    if service == "gh-webhook":
        # M8-GH-3: same 1-G rule — the webhook receiver publishes on
        # loopback, so it must be created on the publication bridge.
        raise StartupGateError(
            "CONFIG_INVALID",
            "gh-webhook must use plan_gh_webhook_run (creation on the "
            "internal network would silently drop the loopback publish)")
    if not image_ref or not re.fullmatch(
            r"(sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9/.-]*@sha256:[0-9a-f]{64})",
            image_ref):
        raise StartupGateError("CONFIG_INVALID",
                               "image_ref must be sha256:<64-hex> digest/ID "
                               "(floating tags rejected)")
    if service == "demo-console" and not demo_console_env:
        raise StartupGateError(
            "CONFIG_INVALID",
            "demo_console_env is required for the demo-console service "
            "(entrypoint contract: mode/source-kind/run_id/role/bind "
            "context + five PG expected identity params); use "
            "_demo_console_environment() to build it")
    if service == "controller" and not controller_env:
        raise StartupGateError(
            "CONFIG_INVALID",
            "controller_env is required for the controller service "
            "(PG_HOST/PG_PORT/PG_DATABASE/PG_USER; secrets PG_PASS/ADMIN_PW "
            "travel via the controller secret env-file)")
    if service == "controller":
        # Review-gap Fix 1: the controller secret env-file is part of the
        # service contract, not an optional extra. Missing/None/blank/
        # non-string → CONFIG_INVALID. The message never echoes the path
        # value (a path leak narrows a secret's location).
        if not isinstance(env_file, str) or not env_file.strip():
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller requires the secret env-file path (a non-empty "
                "string carrying PG_PASS/ADMIN_PW via the SecretFile "
                "transport)")
    if service == "policy-gateway" and not gateway_env:
        raise StartupGateError(
            "CONFIG_INVALID",
            "gateway_env is required for the policy-gateway service "
            "(UPSTREAM_URL — non-secret; the gateway lifespan exits without "
            "a reachable upstream)")
    if service in ("demo-console", "preflight"):
        # 1-G stabilization sweep: BOTH reader-DSN consumers require the
        # secret env-file (MERGEPILOT_PG_DSN never rides -e argv). Missing/
        # None/blank/non-string → CONFIG_INVALID, message never echoes the
        # path (a path leak narrows a secret's location).
        if not isinstance(reader_dsn_env_file, str) \
                or not reader_dsn_env_file.strip():
            raise StartupGateError(
                "CONFIG_INVALID",
                "%s requires the reader-DSN secret env-file path "
                "(a non-empty string carrying MERGEPILOT_PG_DSN via the "
                "ReaderDsnSecretFile transport)" % service)
    for env in (demo_console_env, controller_env, gateway_env):
        if env:
            for key in env:
                if key in ("PG_PASS", "ADMIN_PW", "AUDIT_DSN", "L2_DSN",
                           "MERGEPILOT_PG_DSN"):
                    raise StartupGateError(
                        "CONFIG_INVALID",
                        "secret %s must travel via the secret env-file, "
                        "never -e argv" % key)
    aliases, publish, healthcheck = _SERVICE_FLAGS[service]
    argv = ["run", "-d",
            "--name", "mergepilot-isolated-%s-1" % service,
            "--network", ORCHESTRATOR_NETWORK,
            "--pull", "never",
            "--restart", "no"]
    for alias in aliases:
        argv += ["--network-alias", alias]
    if service == "postgres":
        argv += ["-e", "PGDATA=/tmp/pgdata"]
        if env_file:
            argv += ["--env-file", env_file]
    if service == "controller":
        # Validated above (Fix 1): env_file is a non-empty string here and
        # lands in the plan exactly once (asserted post-construction).
        argv += ["--env-file", env_file]
        if argv.count("--env-file") != 1:
            raise StartupGateError(
                "CONFIG_INVALID",
                "controller plan must carry exactly one --env-file")
    if service in ("demo-console", "preflight"):
        # Validated above: reader_dsn_env_file is a non-empty string here
        # and lands in the plan exactly once.
        argv += ["--env-file", reader_dsn_env_file]
        if argv.count("--env-file") != 1:
            raise StartupGateError(
                "CONFIG_INVALID",
                "%s plan must carry exactly one --env-file" % service)
    if healthcheck:
        argv += ["--health-cmd", " ".join(healthcheck),
                 "--health-interval", _HEALTHCHECK_INTERVAL,
                 "--health-timeout", _HEALTHCHECK_TIMEOUT,
                 "--health-retries", str(_HEALTHCHECK_RETRIES)]
    if service == "preflight":
        argv += ["-e", "MERGEPILOT_PG_HOST=postgres",
                 "-e", "MERGEPILOT_PG_PORT=5432",
                 "-e", "MERGEPILOT_DEMO_CONSOLE_URL=http://demo-console:8600"]
        if declared_pg_image:
            argv += ["-e", "MERGEPILOT_DECLARED_PG_IMAGE=%s" % declared_pg_image]
    if controller_env:
        for key in sorted(controller_env):
            argv += ["-e", "%s=%s" % (key, controller_env[key])]
    if service == "controller" and m4f_enabled:
        # M8-A1 opt-in: the ONLY non-secret transmission change. The
        # snapshot DSN itself rides the existing --env-file (never argv).
        argv += ["-e", "M4F_ENABLED=1"]
    if gateway_env:
        for key in sorted(gateway_env):
            argv += ["-e", "%s=%s" % (key, gateway_env[key])]
    if demo_console_env:
        for key in sorted(demo_console_env):
            argv += ["-e", "%s=%s" % (key, demo_console_env[key])]
    if publish:
        argv += ["-p", publish]
    argv.append(image_ref)
    assert_argv_safe(argv)
    return argv


def plan_console_edge_run(image_ref: str) -> list:
    """argv array running the console-edge on the PUBLICATION network.

    Order is the whole point (1-G network design): the edge is CREATED
    with the normal (non-internal) publication bridge as its primary
    network together with the loopback publish — only afterwards does
    plan_console_edge_connect_backend() attach the internal backend.
    No env-file, no -e values, fixed image, exactly-one loopback publish.
    """
    if not image_ref or not re.fullmatch(
            r"(sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9/.-]*@sha256:[0-9a-f]{64})",
            image_ref):
        raise StartupGateError("CONFIG_INVALID",
                               "image_ref must be sha256:<64-hex> digest/ID "
                               "(floating tags rejected)")
    _aliases, publish, healthcheck = _SERVICE_FLAGS["console-edge"]
    argv = ["run", "-d",
            "--name", "mergepilot-isolated-console-edge-1",
            "--network", PUBLICATION_NETWORK,      # PRIMARY: non-internal
            "--network-alias", "console-edge",
            "--pull", "never",
            "--restart", "no",
            "-p", publish]
    if healthcheck:
        argv += ["--health-cmd", " ".join(healthcheck),
                 "--health-interval", _HEALTHCHECK_INTERVAL,
                 "--health-timeout", _HEALTHCHECK_TIMEOUT,
                 "--health-retries", str(_HEALTHCHECK_RETRIES)]
    argv.append(image_ref)
    assert_argv_safe(argv)
    if argv.count("-p") != 1:
        raise StartupGateError("CONFIG_INVALID",
                               "console-edge plan must carry exactly one "
                               "loopback publish")
    if ORCHESTRATOR_NETWORK in argv:
        raise StartupGateError("CONFIG_INVALID",
                               "console-edge must not be created on the "
                               "internal network (publish would be dropped)")
    return argv


def plan_console_edge_connect_backend() -> list:
    """argv array attaching the backend-internal network to the edge.

    Runs AFTER the edge container exists (creation+publish already done
    on the publication bridge). This yields exactly two network
    memberships: publication (primary) + backend (secondary)."""
    return ["network", "connect", ORCHESTRATOR_NETWORK,
            "mergepilot-isolated-console-edge-1"]


def plan_gh_webhook_run(image_ref: str, *, env_file: str) -> list:
    """argv array running gh-webhook on the PUBLICATION network (M8-GH-3).

    Same 1-G ordering rule as console-edge: CREATED on the non-internal
    publication bridge together with the loopback publish (host 8090 ->
    container 8090), internal backend attached afterwards by
    plan_gh_webhook_connect_backend(). Requires the secret env-file
    (GITHUB_INGRESS_DSN + GITHUB_WEBHOOK_SECRET); no -e values."""
    if not image_ref or not re.fullmatch(
            r"(sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9/.-]*@sha256:[0-9a-f]{64})",
            image_ref):
        raise StartupGateError("CONFIG_INVALID",
                               "image_ref must be sha256:<64-hex> digest/ID "
                               "(floating tags rejected)")
    if not isinstance(env_file, str) or not env_file.strip():
        raise StartupGateError(
            "CONFIG_INVALID",
            "gh-webhook requires the secret env-file path (GITHUB_INGRESS_DSN"
            " + GITHUB_WEBHOOK_SECRET via the SecretFile transport)")
    _aliases, publish, healthcheck = _SERVICE_FLAGS["gh-webhook"]
    argv = ["run", "-d",
            "--name", "mergepilot-isolated-gh-webhook-1",
            "--network", PUBLICATION_NETWORK,      # PRIMARY: non-internal
            "--network-alias", "gh-webhook",
            "--pull", "never",
            "--restart", "no",
            "--env-file", env_file,
            "-p", publish]
    if healthcheck:
        argv += ["--health-cmd", " ".join(healthcheck),
                 "--health-interval", _HEALTHCHECK_INTERVAL,
                 "--health-timeout", _HEALTHCHECK_TIMEOUT,
                 "--health-retries", str(_HEALTHCHECK_RETRIES)]
    argv.append(image_ref)
    assert_argv_safe(argv)
    if argv.count("-p") != 1:
        raise StartupGateError("CONFIG_INVALID",
                               "gh-webhook plan must carry exactly one "
                               "loopback publish")
    if argv.count("--env-file") != 1:
        raise StartupGateError("CONFIG_INVALID",
                               "gh-webhook plan must carry exactly one "
                               "env-file")
    if ORCHESTRATOR_NETWORK in argv:
        raise StartupGateError("CONFIG_INVALID",
                               "gh-webhook must not be created on the "
                               "internal network (publish would be dropped)")
    return argv


def plan_gh_webhook_connect_backend() -> list:
    """argv array attaching the internal backend to gh-webhook (after its
    creation on the publication bridge)."""
    return ["network", "connect", ORCHESTRATOR_NETWORK,
            "mergepilot-isolated-gh-webhook-1"]


def plan_build(service: str) -> list:
    """argv array building a service image from its root Dockerfile."""
    dockerfile = "Dockerfile.%s" % service
    root = Path(__file__).resolve().parent.parent.parent  # tools/demo_console -> repo root
    if not (root / dockerfile).is_file():
        raise StartupGateError("CONFIG_INVALID",
                               "missing %s" % dockerfile)
    return ["build", "-f", dockerfile,
            "-t", "mergepilot-isolated-%s:local" % service, "."]


def plan_orchestrated_start(env_file: str | None = None, *,
                            controller_env_file: str,
                            reader_dsn_env_file: str,
                            gh_webhook_env_file: str = "",
                            demo_console_run_id: str = "",
                            demo_console_pg_server_addresses: str = "",
                            m4f_event_machinery: bool = False
                            ) -> list:
    """Full start plan: [network create, run postgres, run gateway, run
    controller, run gh-webhook, run demo-console, run preflight] as argv
    arrays in strict dependency order. The caller executes them
    sequentially and waits for each healthcheck before the next dependent
    step (mirroring depends_on: postgres healthy → gateway healthy →
    controller healthy → gh-webhook healthy → demo-console healthy →
    preflight).

    ``controller_env_file`` (the controller secret env-file carrying
    PG_PASS/ADMIN_PW via the SecretFile transport) is REQUIRED. Missing,
    None, blank or non-string → StartupGateError(CONFIG_INVALID) raised
    BEFORE any plan (not even the network-create plan) is generated. The
    error message never echoes the path value.

    ``reader_dsn_env_file`` (the reader-DSN secret env-file carrying
    MERGEPILOT_PG_DSN via the ReaderDsnSecretFile transport, consumed by
    BOTH demo-console and preflight) is likewise REQUIRED — the 1-G
    stabilization sweep closed the gap where neither DSN consumer could
    start under this orchestration.

    ``gh_webhook_env_file`` (M8-GH-3, the gh-webhook secret env-file
    carrying GITHUB_INGRESS_DSN + GITHUB_WEBHOOK_SECRET) is REQUIRED —
    the receiver refuses to start without it and the DSN may never ride
    -e argv.

    ``demo_console_run_id`` (the seeded run_id) and
    ``demo_console_pg_server_addresses`` (the postgres bridge IP MEASURED
    after the healthcheck passed) are REQUIRED — no defaults, no inference.
    ``env_file`` is the postgres secret env-file.
    """
    # Review-gap Fix 1: validate the controller secret env-file FIRST —
    # fail-closed before _demo_console_environment() or any plan is built.
    if not isinstance(controller_env_file, str) or \
            not controller_env_file.strip():
        raise StartupGateError(
            "CONFIG_INVALID",
            "controller_env_file is required (a non-empty string path to "
            "the SecretFile-transport env-file carrying the controller "
            "secrets); refusing to plan any start without it")
    # M8-A1: the controller secret env-file contract is validated
    # UNCONDITIONALLY (both feature states), BEFORE the reader-DSN gate
    # and before ANY plan (network/run/connect/health/preflight) is
    # generated. The key set is flag-dependent; values are never echoed.
    _validate_controller_env_file_contract(
        controller_env_file, m4f_event_machinery=m4f_event_machinery)
    # 1-G stabilization sweep: same fail-closed rule for the reader-DSN
    # secret env-file (demo-console + preflight both refuse to start
    # without MERGEPILOT_PG_DSN, and it may never ride -e argv).
    if not isinstance(reader_dsn_env_file, str) or \
            not reader_dsn_env_file.strip():
        raise StartupGateError(
            "CONFIG_INVALID",
            "reader_dsn_env_file is required (a non-empty string path to "
            "the ReaderDsnSecretFile-transport env-file carrying "
            "MERGEPILOT_PG_DSN for demo-console and preflight); refusing "
            "to plan any start without it")
    # M8-GH-3: same fail-closed rule for the gh-webhook secret env-file.
    if not isinstance(gh_webhook_env_file, str) or \
            not gh_webhook_env_file.strip():
        raise StartupGateError(
            "CONFIG_INVALID",
            "gh_webhook_env_file is required (a non-empty string path to "
            "the secret env-file carrying GITHUB_INGRESS_DSN + "
            "GITHUB_WEBHOOK_SECRET for the gh-webhook receiver); refusing "
            "to plan any start without it")
    demo_env = _demo_console_environment(
        demo_console_run_id, demo_console_pg_server_addresses)
    plans = [plan_network_create(), plan_publication_network_create()]
    plans.append(plan_service_run(
        "postgres", image_ref=PGVECTOR_IMAGE_DIGEST, env_file=env_file))
    plans.append(plan_service_run(
        "policy-gateway", image_ref=get_built_image_identity("policy-gateway"),
        gateway_env=_gateway_environment()))
    plans.append(plan_service_run(
        "controller", image_ref=get_built_image_identity("controller"),
        controller_env=_controller_environment(),
        env_file=controller_env_file,
        m4f_enabled=m4f_event_machinery))
    plans.append(plan_gh_webhook_run(
        get_built_image_identity("gh-webhook"),
        env_file=gh_webhook_env_file))
    plans.append(plan_gh_webhook_connect_backend())
    plans.append(plan_service_run(
        "demo-console", image_ref=get_built_image_identity("demo-console"),
        demo_console_env=demo_env,
        reader_dsn_env_file=reader_dsn_env_file))
    plans.append(plan_console_edge_run(
        get_built_image_identity("console-edge")))
    plans.append(plan_console_edge_connect_backend())
    plans.append(plan_service_run(
        "preflight", image_ref=get_built_image_identity("preflight"),
        declared_pg_image=PGVECTOR_IMAGE_DIGEST,
        reader_dsn_env_file=reader_dsn_env_file))
    return plans


def plan_orchestrated_cleanup() -> list:
    """argv arrays to stop+remove every service container and the network.

    Order: services in REVERSE dependency order, then the network. Each
    command is an argv array; the executor must run them with check=False
    and surface non-zero exits as stable cleanup codes (never as "absent").
    """
    plans = []
    for service in reversed(SERVICE_ORDER):
        plans.append(["rm", "-fv", "mergepilot-isolated-%s-1" % service])
    plans.append(["network", "rm", ORCHESTRATOR_NETWORK])
    plans.append(["network", "rm", PUBLICATION_NETWORK])
    return plans


__all__ = [
    "APP_NAME",
    "BUILT_SERVICE_BASE_IMAGE",
    "BUILT_SERVICE_BASE_IMAGE_ID",
    "BUILT_SERVICES",
    "CONTROLLER_PG_USER",
    "ControllerSecretFile",
    "DB_NAME",
    "DEMO_CONSOLE_PORT",
    "ENVIRONMENT_MARKER",
    "GATEWAY_ISOLATED_UPSTREAM_URL",
    "GATEWAY_LISTEN_PORT",
    "GH_WEBHOOK_PORT",
    "LOOPBACK_BIND",
    "ORCHESTRATOR_NETWORK",
    "PGVECTOR_IMAGE_DIGEST",
    "PREFLIGHT_CHECKS",
    "READER_ROLE",
    "ReaderDsnSecretFile",
    "SERVICE_ORDER",
    "SOURCE_KIND_ISOLATED",
    "SecretFile",
    "StartupCleanupError",
    "StartupGateError",
    "assert_argv_safe",
    "build_compose_config",
    "built_identity_registry",
    "canonicalize_server_address",
    "canonicalize_server_address_list",
    "compose_dependency_order",
    "compose_ports_binding",
    "get_built_image_identity",
    "one_click_cleanup",
    "plan_build",
    "plan_network_create",
    "plan_gh_webhook_connect_backend",
    "plan_gh_webhook_run",
    "plan_orchestrated_cleanup",
    "plan_orchestrated_start",
    "plan_service_run",
    "record_built_image_identity",
    "redact",
    "run_preflight_gates",
    "validate_compose_config",
]
