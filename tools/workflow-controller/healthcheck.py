"""Controller container healthcheck (Phase 1-D retry v3, review-gap Fix 2).

REAL readiness = BOTH conditions hold:

  1. The readiness sentinel exists at the exact configured path and is
     well-formed (see readiness.py): the controller cleared any stale
     sentinel at boot and created this one atomically ONLY after every
     startup assertion (startup_assert_l2 + candidate validation) passed
     and it entered the run loop. Missing/invalid/symlinked sentinel →
     the boot did not complete → unhealthy. A restarted container never
     inherits the old sentinel (cleared at boot before assertions).
  2. The configured PostgreSQL answers TCP from inside the container.

An exited container never runs this script at all — exit 0 is healthy,
anything else is unhealthy; there is no standby state.

Read-only: one file stat/read + one TCP connect; sends nothing. No
passwords, DSNs or SQL literals in argv, output or exceptions.
"""

from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import readiness  # noqa: E402  (same image directory)


def main() -> int:
    path = ""
    try:
        path = readiness.readiness_path()
    except ValueError:
        print("readiness: invalid sentinel configuration", file=sys.stderr)
        return 1
    if not path:
        print("readiness: CONTROLLER_READY_SENTINEL not configured",
              file=sys.stderr)
        return 1
    if not readiness.is_ready(path):
        print("readiness: sentinel missing/invalid (startup assertions "
              "not completed)", file=sys.stderr)
        return 1

    host = os.environ.get("PG_HOST", "")
    port = int(os.environ.get("PG_PORT", "5432"))
    if not host:
        print("pg probe: PG_HOST not set", file=sys.stderr)
        return 1
    try:
        sock = socket.create_connection((host, port), timeout=3)
    except OSError as exc:
        print("pg probe failed: %s" % type(exc).__name__, file=sys.stderr)
        return 1
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
