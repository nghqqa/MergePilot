"""Controller readiness sentinel contract (Phase 1-D retry v3 review-gap Fix 2).

The Docker healthcheck for the controller must prove MORE than "PostgreSQL
TCP is reachable": it must prove the controller completed its startup
assertions (``startup_assert_l2`` + candidate validation) and entered the
run loop. This module implements that contract:

- ``CONTROLLER_READY_SENTINEL`` (env) names the FIXED sentinel path. The
  isolated stack injects it; unset → the contract is disabled (standalone
  repo runs keep their historical behavior).
- ``clear_stale_sentinel(path)`` at PROCESS BOOT, before any assertion: a
  restarted container never inherits the previous boot's readiness.
- ``mark_ready(path)`` ONLY after every startup assertion passed and the
  controller is about to enter ``run_forever()``: atomic exclusive create
  (``O_CREAT | O_EXCL``). The content is a single non-secret ISO-8601
  timestamp line — never a password, DSN or SQL fragment.
- ``is_ready(path)`` used by healthcheck.py: the sentinel must exist at the
  EXACT configured path, be a REGULAR file (symlinks and other types are
  rejected), and contain a single well-formed non-secret line. Missing,
  invalid, or symlinked → NOT ready (unhealthy).

No shell, no subprocess; stdlib only. Secrets never appear in argv, logs,
exceptions or the sentinel itself.
"""

from __future__ import annotations

import os
import datetime
import sys

ENV_NAME = "CONTROLLER_READY_SENTINEL"

# Non-secret single line: ISO-8601 UTC timestamp (no other content, ever).
_LINE_RE = None  # validated structurally below; no regex dependency needed


def readiness_path(environ=None) -> str:
    """Return the configured sentinel path, or '' when the contract is off.

    A configured value must be an ABSOLUTE path (container contract: a
    leading '/'; on a Windows host any drive-absolute path is also
    accepted so the unit tests exercise the same code). A relative value
    or one containing '..' is rejected by raising ValueError (the caller
    fails closed — an ambiguous readiness path must never be guessed). A
    blank value means the contract is disabled ('' is returned).
    """
    env = os.environ if environ is None else environ
    value = env.get(ENV_NAME, "").strip()
    if not value:
        return ""
    if not (value.startswith("/") or os.path.isabs(value)) or \
            ".." in value.replace("\\", "/").split("/"):
        raise ValueError(
            "CONFIG_INVALID: %s must be an absolute container path without "
            "'..' (got an invalid value)" % ENV_NAME)
    return value


def clear_stale_sentinel(path: str) -> bool:
    """Remove any pre-existing sentinel at PROCESS BOOT.

    Returns True if a stale sentinel was removed. Called BEFORE the startup
    assertions so a container restart never inherits old readiness. Missing
    file → no-op (returns False). Removal errors are surfaced (fail-closed:
    a sentinel we cannot clear must not be trusted later).
    """
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def mark_ready(path: str) -> None:
    """Atomically create the sentinel (exclusive create, no overwrite).

    Called ONLY after all startup assertions passed. ``O_CREAT | O_EXCL``
    makes the creation atomic: a second/parallel mark fails loudly instead
    of silently refreshing an old file. Content: one ISO-8601 UTC timestamp
    line — non-secret by construction.
    """
    line = datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n"
    # O_BINARY (Windows) prevents CRT newline translation — the sentinel
    # must be byte-identical on every platform (is_ready rejects CR).
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)


def is_ready(path: str) -> bool:
    """True iff the sentinel is present and well-formed at the exact path.

    Regular file only (``os.path.islink`` and non-file types are rejected —
    a symlinked or device sentinel is an invalid readiness signal). The
    content must be exactly one non-empty ASCII line (the timestamp).
    """
    if not path:
        return False
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        with open(path, "rb") as fh:
            data = fh.read(512)
    except OSError:
        return False
    if not data or b"\x00" in data or b"\r" in data:
        return False
    lines = data.split(b"\n")
    # Exactly one content line (+ trailing newline).
    if len(lines) != 2 or not lines[0] or lines[1] != b"":
        return False
    return lines[0].isascii() if hasattr(bytes, "isascii") else True


if __name__ == "__main__":
    # Standalone introspection helper (prints READY/NOT_READY only).
    print("READY" if is_ready(readiness_path()) else "NOT_READY")
    sys.exit(0)
