"""Bounded-capture / exit-code stub. ``text`` selects the mode:

  stdout_overrun -- write well over WORKER_MAX_OUTPUT_BYTES to stdout (forces
           the provider's bounded reader to cap and tree-kill).
  exact:<n>      -- write exactly ``n`` bytes to stdout (precise cap boundary).
  stderr_flood   -- write a large volume to stderr (DEVNULL) then return a
           valid ``ok`` response; proves stderr never deadlocks or loads memory.
  badexit        -- write a valid ``ok`` response then exit nonzero; proves
           valid-JSON + nonzero-exit maps to MODEL_UNAVAILABLE.
"""
from __future__ import annotations

import json
import sys

DIM = 384


def main() -> int:
    raw = sys.stdin.buffer.read(65537)
    try:
        request = json.loads(raw.decode("utf-8"))
        mode = request["text"]
    except Exception:
        sys.stdout.buffer.write(
            json.dumps({"version": 1, "status": "error", "subcode": "INPUT_INVALID"})
            .encode("utf-8")
        )
        sys.stdout.buffer.flush()
        return 0

    ok_payload = json.dumps(
        {"version": 1, "status": "ok", "dim": DIM, "vector": [0.0] * DIM}
    ).encode("utf-8")

    if mode == "stdout_overrun":
        sys.stdout.buffer.write(b"x" * (1024 * 1024))  # 1 MiB >> 256 KiB cap
        sys.stdout.buffer.flush()
        return 0
    if mode.startswith("exact:"):
        n = int(mode.split(":", 1)[1])
        sys.stdout.buffer.write(b"x" * n)
        sys.stdout.buffer.flush()
        return 0
    if mode == "stderr_flood":
        sys.stderr.buffer.write(b"noise-" * 100000)  # ~600 KiB to stderr (DEVNULL)
        sys.stderr.buffer.flush()
        sys.stdout.buffer.write(ok_payload)
        sys.stdout.buffer.flush()
        return 0
    if mode == "badexit":
        sys.stdout.buffer.write(ok_payload)
        sys.stdout.buffer.flush()
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
