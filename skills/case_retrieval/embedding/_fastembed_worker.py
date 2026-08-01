"""FastEmbed inference worker -- versioned subprocess boundary.

Invoked ONLY by :class:`FastEmbedProvider` via ``subprocess.Popen`` with a
minimal environment.  It reads one bounded JSON request from stdin and writes
one bounded JSON response to stdout.

Trust contract:

* The parent passes ``model`` and ``text`` over stdin; nothing sensitive is
  needed and none is read.
* The worker NEVER emits exception text, environment contents, file system
  paths, or stack traces.  Every failure path produces a short, stable
  ``error`` response carrying only a fixed ``subcode``.
* Exit code is 0 whenever a structured response could be written, so the parent
  treats any non-JSON / empty stdout as ``MODEL_UNAVAILABLE`` and never inspects
  stderr (which must stay opaque).
"""
from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = 1
EMBEDDING_DIM = 384
MAX_INPUT_BYTES = 65536
MAX_OUTPUT_BYTES = 262144


def _emit(payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _error(subcode: str) -> None:
    _emit({"version": PROTOCOL_VERSION, "status": "error", "subcode": subcode})


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except Exception:
        _error("INPUT_INVALID")
        return 0
    if not raw or len(raw) > MAX_INPUT_BYTES:
        _error("INPUT_INVALID")
        return 0
    try:
        request = json.loads(raw.decode("utf-8"))
        model = request["model"]
        text = request["text"]
        if not isinstance(model, str) or not model:
            raise ValueError("model")
        if not isinstance(text, str) or not text:
            raise ValueError("text")
    except Exception:
        _error("INPUT_INVALID")
        return 0

    try:
        from fastembed import TextEmbedding

        vector = list(TextEmbedding(model_name=model).embed([text]))[0].tolist()
    except Exception:
        _error("MODEL_UNAVAILABLE")
        return 0

    if not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
        _error("MODEL_UNAVAILABLE")
        return 0
    try:
        encoded = [float(value) for value in vector]
    except Exception:
        _error("MODEL_UNAVAILABLE")
        return 0

    payload = {
        "version": PROTOCOL_VERSION,
        "status": "ok",
        "dim": EMBEDDING_DIM,
        "vector": encoded,
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_OUTPUT_BYTES:
        _error("MODEL_UNAVAILABLE")
        return 0
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
