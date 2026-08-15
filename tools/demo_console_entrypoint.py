"""Demo-console container entrypoint (Phase 1-D retry fix).

Bridges environment variables to the serve.py CLI for ISOLATED_LIVE mode.
REFUSES to run in REPLAY mode — a missing/misconfigured environment is a
CONFIG_INVALID failure, never a silent fallback to static file serving.

Required environment:
  MERGEPILOT_MODE          must be exactly "isolated_live" (case-insensitive
                           input; compared lowercased; REPLAY is REJECTED)
  MERGEPILOT_SOURCE_KIND   must be exactly "postgres" (case-insensitive)
  MERGEPILOT_RUN_ID        non-empty; validated against ^[a-zA-Z0-9_-]+$;
                           MUST be provided by the caller (no default,
                           no inference, no hardcoded value)
  MERGEPILOT_EXPECTED_ROLE must be exactly "mergepilot_reader" (the canonical
                           viewer role; any other value is CONFIG_INVALID)
  MERGEPILOT_HOST          optional; default "0.0.0.0" (container-internal
                           listen address). Allowed values: 127.0.0.1,
                           localhost, or 0.0.0.0 (the container must accept
                           connections from the Docker bridge network; the
                           HOST-side port publish remains 127.0.0.1-only,
                           enforced by the compose/orchestrator layer).
  MERGEPILOT_PORT          optional; default 8600

The serve.py argv is built as a plain list[str] (never shell) and is passed
through assert_argv_safe to guarantee no DSN/password/SQL PASSWORD literal
or token leaks into the process arguments.

Exit codes: 0 = launched (exec); 1 = CONFIG_INVALID (message to stderr,
already redacted).
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

# Import the argv safety check from one_click_startup (already in /app).
# The Dockerfile copies one_click_startup.py alongside this file.
from one_click_startup import assert_argv_safe, redact  # noqa: E402

REQUIRED_MODE = "isolated_live"
REQUIRED_SOURCE_KIND = "postgres"
REQUIRED_ROLE = "mergepilot_reader"
# Container-internal listen address: 0.0.0.0 is the normal container bind
# (the container must accept connections from the Docker bridge). The
# HOST-side port publish is separately enforced as 127.0.0.1-only by the
# compose config and the orchestrator — these are two DIFFERENT addresses.
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8600

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_ALLOWED_LISTEN_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost"})


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

    # Mode: must be isolated_live. REPLAY or anything else is rejected.
    mode = env.get("MERGEPILOT_MODE", "").strip().lower()
    if not mode:
        raise EntrypointConfigError(
            "MERGEPILOT_MODE is not set; refusing to default "
            "(REPLAY fallback is forbidden)")
    if mode == "replay":
        raise EntrypointConfigError(
            "MERGEPILOT_MODE=replay is REJECTED in the isolated stack; "
            "this container must run isolated_live")
    if mode != REQUIRED_MODE:
        raise EntrypointConfigError(
            "MERGEPILOT_MODE must be %r (got %r)" % (REQUIRED_MODE, mode))

    # Source kind: must be postgres.
    kind = env.get("MERGEPILOT_SOURCE_KIND", "").strip().lower()
    if not kind:
        raise EntrypointConfigError(
            "MERGEPILOT_SOURCE_KIND is not set; a postgres source is "
            "required for isolated_live")
    if kind != REQUIRED_SOURCE_KIND:
        raise EntrypointConfigError(
            "MERGEPILOT_SOURCE_KIND must be %r (got %r); no fallback "
            "to file-based sources is permitted"
            % (REQUIRED_SOURCE_KIND, kind))

    # Run ID: non-empty, strict charset.
    run_id = env.get("MERGEPILOT_RUN_ID", "").strip()
    if not run_id:
        raise EntrypointConfigError(
            "MERGEPILOT_RUN_ID is not set; a seeded run_id is required "
            "(hardcoding a run_id is forbidden; it must come from the "
            "caller)")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise EntrypointConfigError(
            "MERGEPILOT_RUN_ID must match ^[a-zA-Z0-9_-]+$ (got %r)"
            % run_id[:20])

    # Expected role: must be the canonical viewer role exactly.
    role = env.get("MERGEPILOT_EXPECTED_ROLE", "").strip()
    if not role:
        raise EntrypointConfigError(
            "MERGEPILOT_EXPECTED_ROLE is not set; the canonical viewer "
            "role is required")
    if role != REQUIRED_ROLE:
        raise EntrypointConfigError(
            "MERGEPILOT_EXPECTED_ROLE must be %r (got %r)"
            % (REQUIRED_ROLE, role))

    # Host: container-internal listen address. 0.0.0.0 is the normal
    # container bind (Docker bridge routing); 127.0.0.1/localhost are for
    # loopback-only edge cases. LAN-specific addresses are rejected (the
    # container does not know the host's LAN). The HOST-side publish is
    # a SEPARATE address enforced by the compose/orchestrator as
    # 127.0.0.1-only.
    host = env.get("MERGEPILOT_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    if host not in _ALLOWED_LISTEN_HOSTS:
        raise EntrypointConfigError(
            "MERGEPILOT_HOST must be 0.0.0.0 (container listen), "
            "127.0.0.1, or localhost (got %r); LAN-specific addresses "
            "are not valid container listen addresses" % host)

    # Port: valid TCP port.
    port_s = env.get("MERGEPILOT_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(port_s)
    except ValueError:
        raise EntrypointConfigError(
            "MERGEPILOT_PORT must be an integer (got %r)" % port_s[:10]
        ) from None
    if not (0 < port < 65536):
        raise EntrypointConfigError(
            "MERGEPILOT_PORT out of range: %d" % port)

    return {
        "mode": REQUIRED_MODE,
        "source_kind": REQUIRED_SOURCE_KIND,
        "run_id": run_id,
        "expected_role": role,
        "host": host,
        "port": port,
    }


def build_serve_argv(config: dict) -> list:
    """Build the serve.py CLI argv from validated config.

    Returns a list[str]; the caller must pass it to exec directly (never
    through a shell). The argv is checked by assert_argv_safe.
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
    assert_argv_safe(argv)
    return argv


def main() -> int:
    try:
        config = _validate_env()
    except EntrypointConfigError as exc:
        print(redact(str(exc)), file=sys.stderr, flush=True)
        return 1
    argv = build_serve_argv(config)
    # Replace this process with serve.py (no intermediate shell).
    os.execv(argv[0], argv)
    return 0  # unreachable when exec succeeds


if __name__ == "__main__":
    sys.exit(main())
