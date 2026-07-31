"""Container executor -- PRODUCTION (isolation='container').

Runs the test argv inside a hardened container. /artifacts is an in-container
tmpfs with a hard 8 MiB execution-time size limit (the ONLY Docker mechanism
that provides execution-time quota). tmpfs is ephemeral — file artifacts
cannot be extracted post-run by design. The structured test output (stdout/
stderr, captured by drain threads) is the authoritative result.

Lifecycle safety: Phase 1 (run) and Phase 2 (verdict check) are wrapped in a
single ``try`` whose ``finally`` ALWAYS executes Phase 3 (container cleanup).
Missing or malformed ``cleanup_ok`` defaults to **False** (strict), never True.
No external init container or volume is needed.
"""
from __future__ import annotations

import os
import subprocess

from skills.test_runner.executors import _common


def _minimal_host_env():
    env = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _cleanup_container(transport, wsl_distro, run_id, container_name):
    """Idempotent fail-closed cleanup. Raises on rm failure or residue."""
    prefix = _common.docker_prefix(transport, wsl_distro)
    env = _minimal_host_env()
    rm = subprocess.run(prefix + ["rm", "-f", container_name],
                        capture_output=True, timeout=10, env=env)
    if rm.returncode != 0:
        err = (rm.stderr or b"").decode("utf-8", "replace").lower()
        if "no such container" not in err and "not found" not in err:
            raise RuntimeError("docker rm failed rc=%d: %s" % (rm.returncode, err.strip()))
    ps = subprocess.run(prefix + ["ps", "-aq", "--filter", "label=mp-run=" + run_id],
                        capture_output=True, timeout=10, env=env)
    if ps.returncode != 0:
        raise RuntimeError("docker ps query failed rc=%d" % ps.returncode)
    if ps.stdout.strip():
        raise RuntimeError("residual container after cleanup: %s" % ps.stdout.strip().decode("utf-8", "replace"))


def run(plan):
    transport = plan["transport"]
    wsl_distro = plan["wsl_distro"]
    work_mount = plan["cwd"]
    if transport == "wsl":
        work_mount = _common.to_wsl_path(work_mount)
    container_name = "mp-tr-" + plan["run_id"]
    env_pairs = ["%s=%s" % (k, v) for k, v in plan["env"].items()]
    docker_argv = _common.build_docker_run_argv(
        transport, wsl_distro, plan["image"], work_mount,
        plan["argv"], plan["run_id"], container_name,
        plan["memory"], plan["cpus"], plan["pids_limit"], plan["host_uid"], plan["host_gid"],
        env_pairs,
    )

    res = {}
    phase1_exception = None

    try:
        res = _common.run_captured(
            docker_argv, None, _minimal_host_env(),
            plan["timeout_ms"], plan["max_output_bytes"],
            executor="container", isolation="container",
            kill_group=True, cleanup_hook=None,
        )
        # No docker cp: tmpfs artifacts are ephemeral by design.

    except Exception as exc:  # noqa: BLE001
        phase1_exception = exc
        if not isinstance(res, dict):
            res = {}
        res["_phase_error"] = True
        res["_execution_attempted"] = True
        res["started"] = res.get("started", False)
        res["cleanup_ok"] = False

    finally:
        container_cleanup_ok = True
        try:
            _cleanup_container(transport, wsl_distro, plan["run_id"], container_name)
        except Exception:  # noqa: BLE001
            container_cleanup_ok = False

    if phase1_exception is not None:
        capture_ok = False
    else:
        capture_ok = res.get("cleanup_ok") is True
    res["cleanup_ok"] = capture_ok and container_cleanup_ok
    res["container_name"] = container_name
    return res
