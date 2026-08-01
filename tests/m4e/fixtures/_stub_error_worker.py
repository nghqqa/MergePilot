"""Offline stub of the FastEmbed worker protocol that emits an error payload.

Used to prove that a misbehaving worker's extra fields never leak across the
trust boundary: the parent must raise only the stable ``MODEL_UNAVAILABLE``
subcode, and neither the exception message nor its detail may contain the fake
internal trace emitted here.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdin.buffer.read(65537)  # drain request
    payload = {
        "version": 1,
        "status": "error",
        "subcode": "MODEL_UNAVAILABLE",
        "trace": "/internal/worker/blob/value",
    }
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
