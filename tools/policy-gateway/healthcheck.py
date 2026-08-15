"""Policy-gateway container healthcheck (Phase 1-D retry v3 Fix 3).

TCP connect to the gateway's own listen port on container loopback.
uvicorn only binds AFTER the lifespan completes (upstream MCP session
established), so a passing probe means the gateway is FULLY started —
not retrying, not exited. A gateway whose upstream never came up never
binds and never reports healthy; there is no standby state.

Read-only: opens and closes one TCP connection; sends nothing.
"""

from __future__ import annotations

import os
import socket
import sys

HOST = "127.0.0.1"
PORT = int(os.environ.get("LISTEN_PORT", "8083"))


def main() -> int:
    try:
        sock = socket.create_connection((HOST, PORT), timeout=3)
    except OSError as exc:
        print("gateway probe failed: %s" % type(exc).__name__,
              file=sys.stderr)
        return 1
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
