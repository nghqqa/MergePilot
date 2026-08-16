"""Healthcheck for the console-edge publication plumbing (1-G).

Proves BOTH halves of the edge contract from inside its container:
  1. the edge itself is listening on its loopback port, and
  2. the FIXED upstream chain is live: /api/live/status must answer
     200 + JSON + source_kind=POSTGRES_ISOLATED + source_read_only=true.

Loopback-only, stdlib-only, explicit timeout, no external network.
Exit 0 = healthy; 1 = not healthy.
"""

from __future__ import annotations

import json
import sys
import urllib.request

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8600
TIMEOUT_SECONDS = 5.0


def main() -> int:
    url = "http://%s:%d/api/live/status" % (LISTEN_HOST, LISTEN_PORT)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as r:
            if r.status != 200:
                print("EDGE_HEALTHCHECK_FAIL http_status=%d" % r.status,
                      file=sys.stderr)
                return 1
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # fail-closed on ANY error
        print("EDGE_HEALTHCHECK_FAIL %s" % type(exc).__name__,
              file=sys.stderr)
        return 1
    if payload.get("source_kind") != "POSTGRES_ISOLATED":
        print("EDGE_HEALTHCHECK_FAIL source_kind", file=sys.stderr)
        return 1
    if payload.get("source_read_only") is not True:
        print("EDGE_HEALTHCHECK_FAIL source_read_only", file=sys.stderr)
        return 1
    print("EDGE_HEALTHCHECK_OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
