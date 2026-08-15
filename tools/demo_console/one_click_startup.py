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


# ── Built-image identity registry (Phase 1-C) ────────────────────────────────
# Built services must record their IMMUTABLE identity (image ID / digest) at
# build time; a floating tag is never authoritative. The registry maps
# service -> recorded identity; it is populated by the (authorized) build
# round and checked by the orchestrator before start.

BUILT_SERVICES = ("policy-gateway", "controller", "demo-console", "preflight")

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

_SERVICE_FLAGS = {
    # name: (aliases, publish-spec or None, healthcheck-cmd or None)
    "postgres": (["postgres"], None,
                 ["pg_isready", "-U", "mergepilot",
                  "-d", DB_NAME]),
    "policy-gateway": (["policy-gateway"], None, None),
    "controller": (["controller"], None, None),
    "demo-console": (["demo-console"],
                     "%s:%d:8600" % (LOOPBACK_BIND, DEMO_CONSOLE_PORT), None),
    "preflight": (["preflight"], None, None),
}


def plan_network_create() -> list:
    """argv array creating the internal-only network."""
    return ["network", "create", "--internal", "--driver", "bridge",
            ORCHESTRATOR_NETWORK]


def plan_service_run(service: str, *, image_ref: str,
                     env_file: str | None = None,
                     declared_pg_image: str | None = None) -> list:
    """argv array (docker sub-args) to run one service per the contract.

    ``image_ref`` is the digest/image-ID (never a floating tag). The plan
    encodes: internal network + alias, pull never, restart no, no published
    ports except demo-console loopback, healthcheck (postgres), env-file for
    postgres, and the in-network preflight environment.
    """
    if service not in _SERVICE_FLAGS:
        raise StartupGateError("CONFIG_INVALID",
                               "unknown service %r" % service)
    if not image_ref or not re.fullmatch(
            r"(sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9/.-]*@sha256:[0-9a-f]{64})",
            image_ref):
        raise StartupGateError("CONFIG_INVALID",
                               "image_ref must be sha256:<64-hex> digest/ID "
                               "(floating tags rejected)")
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
    if publish:
        argv += ["-p", publish]
    argv.append(image_ref)
    assert_argv_safe(argv)
    return argv


def plan_build(service: str) -> list:
    """argv array building a service image from its root Dockerfile."""
    dockerfile = "Dockerfile.%s" % service
    root = Path(__file__).resolve().parent.parent.parent  # tools/demo_console -> repo root
    if not (root / dockerfile).is_file():
        raise StartupGateError("CONFIG_INVALID",
                               "missing %s" % dockerfile)
    return ["build", "-f", dockerfile,
            "-t", "mergepilot-isolated-%s:local" % service, "."]


def plan_orchestrated_start(env_file: str | None = None) -> list:
    """Full start plan: [network create, run postgres, run gateway, run
    controller, run demo-console, run preflight] as argv arrays in strict
    dependency order. The caller executes them sequentially and waits for
    the postgres healthcheck between step 2 and 3 (mirroring depends_on)."""
    plans = [plan_network_create()]
    plans.append(plan_service_run(
        "postgres", image_ref=PGVECTOR_IMAGE_DIGEST, env_file=env_file))
    for service in ("policy-gateway", "controller", "demo-console"):
        plans.append(plan_service_run(
            service, image_ref=get_built_image_identity(service)))
    plans.append(plan_service_run(
        "preflight", image_ref=get_built_image_identity("preflight"),
        declared_pg_image=PGVECTOR_IMAGE_DIGEST))
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
    return plans


__all__ = [
    "APP_NAME",
    "BUILT_SERVICE_BASE_IMAGE",
    "BUILT_SERVICE_BASE_IMAGE_ID",
    "BUILT_SERVICES",
    "DB_NAME",
    "DEMO_CONSOLE_PORT",
    "ENVIRONMENT_MARKER",
    "LOOPBACK_BIND",
    "ORCHESTRATOR_NETWORK",
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
    "built_identity_registry",
    "compose_dependency_order",
    "compose_ports_binding",
    "get_built_image_identity",
    "one_click_cleanup",
    "plan_build",
    "plan_network_create",
    "plan_orchestrated_cleanup",
    "plan_orchestrated_start",
    "plan_service_run",
    "record_built_image_identity",
    "redact",
    "run_preflight_gates",
    "validate_compose_config",
]
