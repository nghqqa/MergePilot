"""Tree-spawning stub for process-tree reap verification.

Reads the protocol request from stdin; ``text`` encodes ``<mode>|<marker_path>``.
In every mode it first spawns a marked grandchild that sleeps 120s, and writes
``<worker_pid> <grandchild_pid>`` to the marker file so tests can independently
verify the grandchild is gone afterwards.

Modes:
  sleep -- produce no output, outlast the deadline (worker stays alive; the
           provider's snapshot captures worker + grandchild together).
  error -- emit an ``error`` protocol response and exit 0; the grandchild is
           reparented and must be reaped by the Job Object.
  exit  -- emit a valid ``ok`` protocol response and exit 0 immediately; the
           grandchild is reparented and must be reaped by the Job Object.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

DIM = 384


def main() -> int:
    raw = sys.stdin.buffer.read(65537)
    try:
        request = json.loads(raw.decode("utf-8"))
        mode, marker = request["text"].split("|", 1)
    except Exception:
        sys.stdout.buffer.write(
            json.dumps({"version": 1, "status": "error", "subcode": "INPUT_INVALID"})
            .encode("utf-8")
        )
        sys.stdout.buffer.flush()
        return 0

    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with open(marker, "w") as handle:
            handle.write("%d %d\n" % (os.getpid(), grandchild.pid))
    except Exception:
        pass

    ok_payload = json.dumps(
        {"version": 1, "status": "ok", "dim": DIM, "vector": [0.0] * DIM}
    ).encode("utf-8")
    error_payload = json.dumps(
        {"version": 1, "status": "error", "subcode": "MODEL_UNAVAILABLE"}
    ).encode("utf-8")

    if mode == "sleep":
        time.sleep(120)
    elif mode == "error":
        sys.stdout.buffer.write(error_payload)
        sys.stdout.buffer.flush()
    elif mode == "exit":
        sys.stdout.buffer.write(ok_payload)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
