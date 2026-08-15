"""Workflow-controller container entrypoint (Phase 1-D retry v3 Fix 3).

Validates the controller's runtime contract — variable names extracted from
the ACTUAL tools/workflow-controller/controller.py (never guessed) — then
execs it. controller.py's own __main__ gate requires ADMIN_PW + PG_PASS and
exits 1 without a stable code; this entrypoint front-runs it with explicit,
stable CONFIG_INVALID diagnostics so a missing config is diagnosable.

Required/validated environment:
  PG_HOST       non-empty. In this stack: the in-network alias 'postgres'
                (the code default 'audit-pg' does not exist here and must
                be overridden explicitly, never silently).
  PG_PORT       integer port (default 5432).
  PG_DATABASE   non-empty (canonical: mergepilot_audit).
  PG_USER       non-empty (canonical admin user: mergepilot).
  PG_PASS       non-empty SECRET — travels via the controller secret
                env-file, never argv; value never printed.
  ADMIN_PW      non-empty SECRET — same transport. (Used only by the
                unreachable-by-design Matrix domain in the isolated stack;
                a random per-session value.)

Exit codes: 0 = launched (exec); 1 = CONFIG_INVALID.
Secrets never appear in argv, logs or exceptions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from one_click_startup import redact  # noqa: E402

CONTROLLER_SCRIPT = "/app/controller.py"


class EntrypointConfigError(Exception):
    """Stable CONFIG_INVALID; message is redacted (never contains secrets)."""

    def __init__(self, detail: str):
        self.code = "CONFIG_INVALID"
        super().__init__(redact("CONFIG_INVALID: %s" % detail))


def _validate_env(environ=None) -> dict:
    env = environ if environ is not None else os.environ

    def _require(key: str) -> str:
        value = env.get(key, "").strip()
        if not value:
            raise EntrypointConfigError(
                "%s is not set; controller.py refuses to start without the "
                "full database contract" % key)
        return value

    pg_host = _require("PG_HOST")
    pg_database = _require("PG_DATABASE")
    pg_user = _require("PG_USER")
    _require("PG_PASS")     # secret — presence only, value never returned
    _require("ADMIN_PW")    # secret — presence only, value never returned

    pg_port_s = env.get("PG_PORT", "5432").strip()
    try:
        pg_port = int(pg_port_s)
    except ValueError:
        raise EntrypointConfigError(
            "PG_PORT must be an integer (got %r)" % pg_port_s[:10]
        ) from None
    if not (0 < pg_port < 65536):
        raise EntrypointConfigError("PG_PORT out of range: %d" % pg_port)

    return {
        "pg_host": pg_host,
        "pg_port": pg_port,
        "pg_database": pg_database,
        "pg_user": pg_user,
    }


def main() -> int:
    try:
        _validate_env()
    except EntrypointConfigError as exc:
        print(redact(str(exc)), file=sys.stderr, flush=True)
        return 1
    os.execv(sys.executable,
             [sys.executable, "-u", CONTROLLER_SCRIPT])
    return 0


if __name__ == "__main__":
    sys.exit(main())
