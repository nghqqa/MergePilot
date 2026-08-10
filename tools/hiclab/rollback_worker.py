#!/usr/bin/env python3
"""Rollback a hardened worker to its pre-hardening state.

Reads a saved full-inspect artifact (from ``save_rollback_artifact`` in
worker_argv.py, written to /dev/shm BEFORE the original container was
removed) and rebuilds the ``docker run`` argv that faithfully restores the
original container -- preserving the original RestartPolicy (no forced
``--restart=no``) and the full contract.

The original env is re-materialized into a fresh /dev/shm env-file (0600,
deleted by the caller). The argv never contains ``-e KEY=VALUE``.

This module is the testable core; ``rollback_worker.sh`` is the shell
orchestrator. No Docker calls here -- only argv construction.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker_argv  # noqa: E402


def build_rollback_argv(saved_inspect, env_file_path):
    """Build a docker run argv restoring the original container exactly.

    Uses force_restart_no=False so the original RestartPolicy is preserved.
    Empty hardening (no tmpfs/storage-opt/label additions).
    """
    if not isinstance(saved_inspect, dict):
        raise ValueError("saved_inspect must be a dict")
    name = (saved_inspect.get("Name") or "").lstrip("/")
    if not name:
        raise ValueError("saved_inspect has no Name")
    empty_hardening = {
        "kind": "rollback",
        "tmpfs_mounts": [],
        "storage_opt": None,
        "extra_labels": {},
        "env_additions": [],
    }
    return worker_argv.build_run_argv_from_inspect(
        name, saved_inspect, env_file_path, empty_hardening,
        force_restart_no=False)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.stderr.write("usage: rollback_worker.py <rollback_json_path>\n")
        return 2
    path = argv[0]
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("rollback_worker: cannot read %s: %s\n" % (path, exc))
        return 2
    env_file = os.environ.get("MP_ENV_FILE", "")
    out = build_rollback_argv(saved, env_file)
    sys.stdout.buffer.write(("\0".join(out) + "\0").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
