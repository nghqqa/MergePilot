"""Demo-console container entrypoint (Phase 1-D retry v2).

Bridges environment variables to the serve.py CLI for ISOLATED_LIVE mode.
REFUSES to run in REPLAY mode — a missing/misconfigured environment is a
CONFIG_INVALID failure, never a silent fallback to static file serving.

Required environment:
  MERGEPILOT_MODE          must be exactly "isolated_live" (case-insensitive;
                           REPLAY is REJECTED)
  MERGEPILOT_SOURCE_KIND   must be exactly "postgres" (case-insensitive)
  MERGEPILOT_RUN_ID        non-empty; ^[a-zA-Z0-9_-]+$; caller-provided
  MERGEPILOT_EXPECTED_ROLE must be exactly "mergepilot_reader"
  MERGEPILOT_BIND_CONTEXT  "host" or "container" (validated; NOT inferred
                           from the host value). In container mode,
                           MERGEPILOT_HOST=0.0.0.0 is allowed (Docker
                           bridge). In host mode, only 127.0.0.1/localhost.
  MERGEPILOT_HOST          bind address; validated against the context
  MERGEPILOT_PORT          valid TCP port (default 8600)

PostgreSQL expected identity (Fix 2 — all REQUIRED, no defaults):
  MERGEPILOT_PG_EXPECTED_DATABASE          must be "mergepilot_audit"
  MERGEPILOT_PG_ENVIRONMENT_ID            must match the isolated seed marker
  MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES  caller-measured bridge IP (not
                                           hardcoded; comma-separated)
  MERGEPILOT_PG_EXPECTED_SERVER_PORT       must be a valid port int
  MERGEPILOT_PG_EXPECTED_APPLICATION_NAME must be "mergepilot_isolated_live_reader"

The serve.py argv is built as a plain list[str] (never shell) and is passed
through assert_argv_safe. The 5 PG expected fields are forwarded via their
existing serve.py env-var contract (NOT placed in argv).

Exit codes: 0 = launched (exec); 1 = CONFIG_INVALID.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from one_click_startup import (  # noqa: E402
    assert_argv_safe,
    canonicalize_server_address_list,
    redact,
)

REQUIRED_MODE = "isolated_live"
REQUIRED_SOURCE_KIND = "postgres"
REQUIRED_ROLE = "mergepilot_reader"
REQUIRED_DATABASE = "mergepilot_audit"
REQUIRED_APP_NAME = "mergepilot_isolated_live_reader"

DEFAULT_PORT = 8600

# Retry v3 Fix 2 + Phase 1-E protected-path fix: the image ships the dynamic
# console (from tools/demo_console/live_assets, a NON-protected path —
# samples/ is protected and stays REPLAY-frozen) at this FIXED allowlisted
# path. The entrypoint verifies dir + index.html BEFORE exec and passes
# --serve-dir explicitly; a missing bundle is a stable CONFIG_INVALID,
# never a fallback.
CONTAINER_SERVE_DIR = "/app/live-console"

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Bind context (Fix 1): explicit, NOT inferred from host value.
_VALID_BIND_CONTEXTS = frozenset({"host", "container"})
_HOST_MODE_HOSTS = frozenset({"127.0.0.1", "localhost"})
_CONTAINER_MODE_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost"})

# 5 PG expected identity env vars (Fix 2).
_PG_EXPECTED_ENV_KEYS = (
    "MERGEPILOT_PG_EXPECTED_DATABASE",
    "MERGEPILOT_PG_ENVIRONMENT_ID",
    "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES",
    "MERGEPILOT_PG_EXPECTED_SERVER_PORT",
    "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME",
)


class EntrypointConfigError(Exception):
    """Stable CONFIG_INVALID; message is redacted (never contains secrets)."""

    def __init__(self, detail: str):
        self.code = "CONFIG_INVALID"
        super().__init__(redact("CONFIG_INVALID: %s" % detail))


def _validate_env(environ=None) -> dict:
    """Read and strictly validate the entrypoint environment.

    Returns a dict of validated values. Raises EntrypointConfigError on any
    missing, malformed, or contradictory configuration. REPLAY mode is an
    explicit rejection, not a fallback.
    """
    env = environ if environ is not None else os.environ

    # Mode
    mode = env.get("MERGEPILOT_MODE", "").strip().lower()
    if not mode:
        raise EntrypointConfigError(
            "MERGEPILOT_MODE is not set; refusing to default "
            "(REPLAY fallback is forbidden)")
    if mode == "replay":
        raise EntrypointConfigError(
            "MERGEPILOT_MODE=replay is REJECTED in the isolated stack")
    if mode != REQUIRED_MODE:
        raise EntrypointConfigError(
            "MERGEPILOT_MODE must be %r (got %r)" % (REQUIRED_MODE, mode))

    # Source kind
    kind = env.get("MERGEPILOT_SOURCE_KIND", "").strip().lower()
    if not kind:
        raise EntrypointConfigError("MERGEPILOT_SOURCE_KIND is not set")
    if kind != REQUIRED_SOURCE_KIND:
        raise EntrypointConfigError(
            "MERGEPILOT_SOURCE_KIND must be %r (got %r)"
            % (REQUIRED_SOURCE_KIND, kind))

    # Run ID
    run_id = env.get("MERGEPILOT_RUN_ID", "").strip()
    if not run_id:
        raise EntrypointConfigError(
            "MERGEPILOT_RUN_ID is not set; a seeded run_id is required "
            "(hardcoding is forbidden)")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise EntrypointConfigError(
            "MERGEPILOT_RUN_ID must match ^[a-zA-Z0-9_-]+$ (got %r)"
            % run_id[:20])

    # Expected role
    role = env.get("MERGEPILOT_EXPECTED_ROLE", "").strip()
    if not role:
        raise EntrypointConfigError("MERGEPILOT_EXPECTED_ROLE is not set")
    if role != REQUIRED_ROLE:
        raise EntrypointConfigError(
            "MERGEPILOT_EXPECTED_ROLE must be %r (got %r)"
            % (REQUIRED_ROLE, role))

    # Bind context (Fix 1): explicit, validated, NOT inferred from host.
    bind_context = env.get("MERGEPILOT_BIND_CONTEXT", "").strip().lower()
    if not bind_context:
        raise EntrypointConfigError(
            "MERGEPILOT_BIND_CONTEXT is not set; must be 'host' or "
            "'container' (the context is explicit, never inferred from "
            "the host value)")
    if bind_context not in _VALID_BIND_CONTEXTS:
        raise EntrypointConfigError(
            "MERGEPILOT_BIND_CONTEXT must be 'host' or 'container' "
            "(got %r)" % bind_context)

    # Host: validated AGAINST the bind context.
    host = env.get("MERGEPILOT_HOST", "").strip()
    if not host:
        raise EntrypointConfigError("MERGEPILOT_HOST is not set")
    allowed = (_CONTAINER_MODE_HOSTS if bind_context == "container"
               else _HOST_MODE_HOSTS)
    if host not in allowed:
        if bind_context == "container":
            raise EntrypointConfigError(
                "MERGEPILOT_HOST must be 0.0.0.0/127.0.0.1/localhost in "
                "container mode (got %r); LAN addresses are not valid "
                "container listen addresses" % host)
        raise EntrypointConfigError(
            "MERGEPILOT_HOST must be 127.0.0.1/localhost in host mode "
            "(got %r); set MERGEPILOT_BIND_CONTEXT=container for Docker "
            "bridge 0.0.0.0" % host)

    # Port
    port_s = env.get("MERGEPILOT_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(port_s)
    except ValueError:
        raise EntrypointConfigError(
            "MERGEPILOT_PORT must be an integer (got %r)" % port_s[:10]
        ) from None
    if not (0 < port < 65536):
        raise EntrypointConfigError("MERGEPILOT_PORT out of range: %d" % port)

    # ── Fix 2: 5 PG expected identity params (all REQUIRED) ─────────────
    pg_database = env.get("MERGEPILOT_PG_EXPECTED_DATABASE", "").strip()
    if not pg_database:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_DATABASE is not set")
    if pg_database != REQUIRED_DATABASE:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_DATABASE must be %r (got %r)"
            % (REQUIRED_DATABASE, pg_database))

    pg_env_id = env.get("MERGEPILOT_PG_ENVIRONMENT_ID", "").strip()
    if not pg_env_id:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_ENVIRONMENT_ID is not set (the environment "
            "marker is mandatory; never guessed)")

    pg_server_addrs = env.get(
        "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES", "").strip()
    if not pg_server_addrs:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES is not set (the "
            "orchestrator must measure the postgres container's bridge IP "
            "and inject it; hardcoding is forbidden)")
    # Retry v3 Fix 1: validate via the ONE shared canonicalizer — a bare
    # 172.18.0.2 and a single-host 172.18.0.2/32 are the same address;
    # hostnames/aliases, IPv6, non-/32 CIDR and malformed values rejected.
    try:
        pg_server_addrs_canonical = ",".join(
            canonicalize_server_address_list(pg_server_addrs))
    except ValueError as exc:
        raise EntrypointConfigError(str(exc)) from None

    pg_server_port_s = env.get(
        "MERGEPILOT_PG_EXPECTED_SERVER_PORT", "").strip()
    if not pg_server_port_s:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_SERVER_PORT is not set")
    try:
        pg_server_port = int(pg_server_port_s)
    except ValueError:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_SERVER_PORT must be an integer"
        ) from None
    if not (0 < pg_server_port < 65536):
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_SERVER_PORT out of range: %d"
            % pg_server_port)

    pg_app_name = env.get(
        "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME", "").strip()
    if not pg_app_name:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME is not set")
    if pg_app_name != REQUIRED_APP_NAME:
        raise EntrypointConfigError(
            "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME must be %r (got %r)"
            % (REQUIRED_APP_NAME, pg_app_name))

    return {
        "mode": REQUIRED_MODE,
        "source_kind": REQUIRED_SOURCE_KIND,
        "run_id": run_id,
        "expected_role": role,
        "bind_context": bind_context,
        "host": host,
        "port": port,
        "pg_expected_database": pg_database,
        "pg_environment_id": pg_env_id,
        "pg_expected_server_addresses": pg_server_addrs_canonical,
        "pg_expected_server_port": pg_server_port,
        "pg_expected_application_name": pg_app_name,
    }


def _verify_container_serve_dir(serve_dir: str = CONTAINER_SERVE_DIR) -> str:
    """Fail-closed existence check for the container static bundle (Fix 2).

    Returns the verified path. Raises EntrypointConfigError (stable
    CONFIG_INVALID) when the directory or its index.html is missing — the
    entrypoint refuses to exec serve.py rather than letting it start a DB
    poller against a bundle that can never be served. No fallback path and
    no REPLAY mode exists.
    """
    path = Path(serve_dir)
    if not path.is_dir():
        raise EntrypointConfigError(
            "container serve dir %s is missing (the image must COPY "
            "tools/demo_console/live_assets there)" % serve_dir)
    if not (path / "index.html").is_file():
        raise EntrypointConfigError(
            "container serve dir %s has no index.html" % serve_dir)
    return serve_dir


def build_serve_argv(config: dict) -> list:
    """Build the serve.py CLI argv from validated config.

    Only the core flags go into argv. The 5 PG expected identity fields
    are forwarded via their existing serve.py env-var contract (NOT argv,
    keeping argv minimal and secret-safe). In container mode the FIXED
    allowlisted --serve-dir is passed explicitly (retry v3 Fix 2); host
    mode keeps serve.py's repo-layout default.
    """
    argv = [
        sys.executable, "-u", "/app/serve.py",
        "--mode", config["mode"],
        "--source-kind", config["source_kind"],
        "--run-id", config["run_id"],
        "--expected-role", config["expected_role"],
        "--host", config["host"],
        "--port", str(config["port"]),
    ]
    if config.get("bind_context") == "container":
        argv += ["--serve-dir", CONTAINER_SERVE_DIR]
    assert_argv_safe(argv)
    return argv


def main() -> int:
    try:
        config = _validate_env()
        if config["bind_context"] == "container":
            _verify_container_serve_dir()
    except EntrypointConfigError as exc:
        print(redact(str(exc)), file=sys.stderr, flush=True)
        return 1
    argv = build_serve_argv(config)
    # MERGEPILOT_BIND_CONTEXT and the 5 PG expected vars are already in
    # the process environment; serve.py reads them from env. The entrypoint
    # does NOT duplicate them into argv.
    os.execv(argv[0], argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
