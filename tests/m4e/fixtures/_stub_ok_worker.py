"""Offline stub of the FastEmbed worker protocol for unit tests.

Speaks the SAME versioned stdin/stdout JSON protocol as
``skills/case_retrieval/embedding/_fastembed_worker.py`` but returns a canned,
deterministic 384-dim vector derived from the input -- no model, no network,
no fastembed import. Used to exercise the subprocess boundary (Popen + minimal
environment + protocol parse) deterministically.
"""
from __future__ import annotations

import json
import sys

DIM = 384


def main() -> int:
    raw = sys.stdin.buffer.read(65537)
    try:
        request = json.loads(raw.decode("utf-8"))
        text = request["text"]
        if not isinstance(text, str) or not text:
            raise ValueError("text")
    except Exception:
        sys.stdout.buffer.write(
            json.dumps({"version": 1, "status": "error", "subcode": "INPUT_INVALID"}).encode("utf-8")
        )
        return 0
    base = len(text)
    vector = [((base + index) % 1000) / 1000.0 for index in range(DIM)]
    sys.stdout.buffer.write(
        json.dumps({"version": 1, "status": "ok", "dim": DIM, "vector": vector}).encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
