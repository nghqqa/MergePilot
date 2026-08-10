#!/usr/bin/env python3
"""Orchestrate hardened worker/manager recreation from a ``docker inspect``.

Reads a full inspect object, and in one pass:
  1. saves a rollback artifact (full inspect -> /dev/shm, 0600)
  2. prepares the env-file (Config.Env + non-secret additions -> /dev/shm, 0600)
  3. builds the hardened ``docker run`` argv from the FULL inspect contract

The argv NEVER contains ``-e KEY=VALUE`` -- env travels only via
``--env-file <shm_path>``. Secret values never touch the argv or regular
disk.

This is the testable core; ``create_hardened_worker.sh`` calls ``main()``
via stdin pipe. No Docker calls here.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker_argv  # noqa: E402


def _normalize_inspect(obj):
    """docker inspect returns a list; accept list or single dict."""
    if isinstance(obj, list):
        if not obj:
            raise ValueError("empty inspect list")
        return obj[0]
    return obj


def orchestrate(inspect_obj, kind, agent_name, run_id,
                storage_opt_gib=None, shm_dir=worker_argv.DEFAULT_SHM_DIR,
                rollback_writer=None, env_writer=None, rng_fn=None):
    """Run the three-step orchestration. Returns dict:
      {argv, rollback_path, envfile_path, env_pairs}
    """
    inspect = _normalize_inspect(inspect_obj)
    cfg = inspect.get("Config", {}) or {}
    image = cfg.get("Image")
    if not image:
        raise ValueError("inspect Config.Image missing")

    # 1. rollback artifact (full inspect, saved BEFORE original removal)
    container_name = (inspect.get("Name") or "").lstrip("/")
    rollback_path = worker_argv.save_rollback_artifact(
        container_name or agent_name, inspect, shm_dir=shm_dir,
        rng_fn=rng_fn, writer=rollback_writer)

    # 2. env-file: authoritative Config.Env + non-secret additions
    hardening = worker_argv.make_hardening(
        kind, agent_name, run_id, storage_opt_gib=storage_opt_gib)
    env_pairs = list(cfg.get("Env") or []) + hardening["env_additions"]
    envfile_path = worker_argv.prepare_env_file(
        env_pairs, shm_dir=shm_dir, rng_fn=rng_fn, writer=env_writer)

    # 3. build argv from FULL inspect contract + env-file + hardening
    argv = worker_argv.build_run_argv_from_inspect(
        container_name or ("hiclaw-%s-%s" % (kind, agent_name)),
        inspect, envfile_path, hardening)
    return {
        "argv": argv,
        "rollback_path": rollback_path,
        "envfile_path": envfile_path,
        "env_pairs": env_pairs,
    }


def _extract_secret_values(env_pairs):
    """Extract the VALUE portion of KEY=VALUE pairs for the no-leak test."""
    vals = []
    for pair in env_pairs:
        if "=" in pair:
            vals.append(pair.split("=", 1)[1])
    return vals


def verify_no_secret_in_argv(result):
    """Verify no env KEY=VALUE pair and no ``-e`` flag appears in argv.

    A legitimate path value (e.g. WorkingDir ``/root``) may coincide with an
    env value (``HOME=/root``); that is NOT a leak. A leak is the full
    ``KEY=VALUE`` assignment form or a ``-e`` flag in argv -- both prove env
    traveled inline instead of via ``--env-file``.
    """
    argv = result["argv"]
    if "-e" in argv:
        return False
    for pair in result["env_pairs"]:
        if "=" in pair and pair in argv:
            return False
    return True


def main():
    raw = sys.stdin.buffer.read()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        sys.stderr.write("harden_orchestrate: invalid inspect JSON: %s\n" % exc)
        return 2
    kind = os.environ.get("MP_CONTAINER_KIND", "worker")
    agent = os.environ.get("MP_AGENT_NAME", "")
    run_id = os.environ.get("MP_RUN_ID", "")
    storage_gib = os.environ.get("MP_STORAGE_OPT_GIB", "")
    storage_gib = int(storage_gib) if storage_gib else None

    result = orchestrate(obj, kind, agent, run_id, storage_opt_gib=storage_gib)
    if not verify_no_secret_in_argv(result):
        sys.stderr.write("harden_orchestrate: SECRET LEAK DETECTED in argv -- abort\n")
        return 3
    # stdout: NUL-delimited argv; stderr: paths for shell capture
    sys.stdout.buffer.write(("\0".join(result["argv"]) + "\0").encode("utf-8"))
    sys.stderr.write("ROLLBACK=%s\n" % result["rollback_path"])
    sys.stderr.write("ENVFILE=%s\n" % result["envfile_path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
