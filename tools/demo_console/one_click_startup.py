"""ISOLATED_LIVE productization Phase 1 — one-click startup (design-only code).

Builds a docker-compose configuration for the five-service isolated stack
(controller, policy-gateway, PostgreSQL/pgvector, demo-console, preflight),
provides a preflight-check matrix, a safe secret transport, redaction, and a
retryable cleanup entry point. This module NEVER starts WSL/Docker/
PostgreSQL and NEVER opens a real connection — it is pure configuration
generation and gate logic exercised by Mock/static tests.

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
import json
import re
from pathlib import Path
from typing import Any, Callable

# ── Constants ────────────────────────────────────────────────────────────────

# Digest-pinned pgvector image — the ONLY image this stack may use; a
# floating tag would defeat reproducibility. Same digest as the Phase-B
# ephemeral executor.
PGVECTOR_IMAGE_DIGEST = (
    "pgvector/pgvector@sha256:"
    "a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
)

# Only the demo-console port may be published, and ONLY on loopback.
LOOPBACK_BIND = "127.0.0.1"
DEMO_CONSOLE_PORT = 8600

DB_NAME = "mergepilot_audit"
READER_ROLE = "mergepilot_reader"
ENVIRONMENT_MARKER = "mergepilot-test-ephemeral"
APP_NAME = "mergepilot_isolated_live_reader"
SOURCE_KIND_ISOLATED = "POSTGRES_ISOLATED"

# Services and their dependency order (topological).
SERVICE_ORDER = ("postgres", "policy-gateway", "controller",
                 "demo-console", "preflight")

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


# ── docker-compose configuration ─────────────────────────────────────────────

def build_compose_config(*, demo_console_port: int = DEMO_CONSOLE_PORT,
                         project_name: str = "mergepilot-isolated",
                         admin_password_secret: str = "<secret-file>",
                         ) -> dict:
    """Build the five-service compose configuration as a pure dict.

    Guarantees:
      - the ONLY image reference is the digest-pinned pgvector
        (controller/policy-gateway/demo-console/preflight use build contexts
        with ``pull_policy: never`` — no implicit image pulls)
      - the ONLY published port is demo-console, bound to 127.0.0.1
        (postgres is NEVER published; inter-service traffic stays on the
        private compose network)
      - explicit service dependency order with healthcheck-gated conditions
      - passwords travel via the env-file (secret file), never argv
    """
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
        },
        "controller": {
            "build": {"context": ".", "dockerfile": "Dockerfile.controller"},
            "pull_policy": "never",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
                "policy-gateway": {"condition": "service_started"},
            },
            "networks": ["isolated"],
        },
        "demo-console": {
            "build": {"context": ".", "dockerfile": "Dockerfile.demo-console"},
            "pull_policy": "never",
            "depends_on": {
                "controller": {"condition": "service_started"},
            },
            "networks": ["isolated"],
            "ports": [
                "%s:%d:8600" % (LOOPBACK_BIND, demo_console_port),
            ],
        },
        "preflight": {
            "build": {"context": ".", "dockerfile": "Dockerfile.preflight"},
            "pull_policy": "never",
            "depends_on": {
                "demo-console": {"condition": "service_started"},
            },
            "networks": ["isolated"],
            # The preflight container runs the gate matrix and exits.
            "restart": "no",
        },
    }
    return {
        "name": project_name,
        "services": services,
        "networks": {
            "isolated": {"driver": "bridge", "internal": True},
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
    # Network binding: ONLY demo-console publishes, ONLY on loopback.
    for name, svc in services.items():
        ports = svc.get("ports") or []
        if ports and name != "demo-console":
            raise StartupGateError("BIND_NOT_LOOPBACK",
                                   "service %s publishes ports" % name)
        for p in ports:
            bind = str(p).split(":", 1)[0]
            if bind != LOOPBACK_BIND:
                raise StartupGateError("BIND_NOT_LOOPBACK",
                                       "port bind %r is not %s" % (bind, LOOPBACK_BIND))
    # Postgres must never be published.
    if "ports" in services["postgres"]:
        raise StartupGateError("BIND_NOT_LOOPBACK", "postgres publishes ports")
    # Healthcheck present on postgres.
    if "healthcheck" not in services["postgres"]:
        raise StartupGateError("COMPOSE_INVALID", "postgres lacks healthcheck")
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
        "preflight": {"demo-console"},
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


__all__ = [
    "APP_NAME",
    "DB_NAME",
    "DEMO_CONSOLE_PORT",
    "ENVIRONMENT_MARKER",
    "LOOPBACK_BIND",
    "PGVECTOR_IMAGE_DIGEST",
    "PREFLIGHT_CHECKS",
    "READER_ROLE",
    "SERVICE_ORDER",
    "SOURCE_KIND_ISOLATED",
    "SecretFile",
    "StartupCleanupError",
    "StartupGateError",
    "assert_argv_safe",
    "build_compose_config",
    "compose_dependency_order",
    "compose_ports_binding",
    "one_click_cleanup",
    "redact",
    "run_preflight_gates",
    "validate_compose_config",
]
