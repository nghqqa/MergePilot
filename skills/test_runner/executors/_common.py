"""Shared executor helpers: bounded streaming capture (no unbounded temp files),
process-tree kill, and the WSL/native Docker transport (argv-array only -- never
a shell string). Framework-neutral (stdlib only).
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import threading
import time


class _Drain:
    """Continuously drain a pipe into a rolling sha256 + bounded tail.

    Drains in a thread so the child never blocks on a full pipe (no deadlock),
    keeps only the last ``cap`` bytes in memory (no unbounded temp file), and
    computes the digest over ALL bytes seen. ``total`` lets the caller mark
    truncation when output exceeded ``cap``.
    """

    def __init__(self, pipe, cap):
        self.pipe = pipe
        self.cap = max(0, int(cap))  # 0 = drain fully, keep empty tail (still digest all)
        self.digest = None
        self.tail = ""
        self.total = 0
        self.truncated = False

    def run(self):
        h = hashlib.sha256()
        tail = bytearray()
        total = 0
        try:
            while True:
                chunk = self.pipe.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
                tail.extend(chunk)
                if self.cap > 0 and len(tail) > self.cap:
                    del tail[: len(tail) - self.cap]
                elif self.cap == 0:
                    # keep no tail bytes, but keep draining + digesting
                    tail = bytearray()
        finally:
            try:
                self.pipe.close()
            except Exception:  # noqa: BLE001
                pass
        self.digest = h.hexdigest()
        text = tail.decode("utf-8", "replace")
        # the RETURNED text re-encoded to UTF-8 must not exceed the stream budget
        # (a multibyte char split at the boundary could expand via U+FFFD on re-encode)
        while self.cap > 0 and len(text.encode("utf-8", "replace")) > self.cap and text:
            text = text[:-1]
        self.tail = text
        self.total = total
        self.truncated = total > self.cap


def _kill_tree(proc):
    try:
        pid = proc.pid
    except Exception:  # noqa: BLE001
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def run_captured(argv, cwd, env, timeout_ms, per_stream_cap, executor, isolation,
                 kill_group=True, cleanup_hook=None):
    """Run an argv list with streaming capture + bounded output + tree kill.

    ``per_stream_cap`` is the per-stream byte budget (stdout and stderr each).
    Returns an executor-result dict. ``started=False`` means the executable
    could not be launched (caller maps to DEPENDENCY_UNAVAILABLE).
    """
    started = False
    exit_code = None
    timed_out = False
    cleanup_ok = True
    t0 = time.monotonic()
    try:
        kwargs = dict(cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      close_fds=True)
        if kill_group:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        started = True
        out_drain = _Drain(proc.stdout, per_stream_cap)
        err_drain = _Drain(proc.stderr, per_stream_cap)
        to = threading.Thread(target=out_drain.run)
        te = threading.Thread(target=err_drain.run)
        to.start(); te.start()
        try:
            proc.wait(timeout=timeout_ms / 1000.0)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            exit_code = proc.returncode
        to.join(timeout=10); te.join(timeout=10)
        # fail-closed: a still-alive drain thread means we cannot safely return
        # (pipes not fully drained) -> mark cleanup failed
        if to.is_alive() or te.is_alive():
            cleanup_ok = False
    except FileNotFoundError:
        started = False
    except OSError:
        started = False
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        if cleanup_hook is not None:
            try:
                cleanup_hook()
            except Exception:  # noqa: BLE001
                cleanup_ok = False

    if started:
        so_digest, so_tail = out_drain.digest or hashlib.sha256(b"").hexdigest(), out_drain.tail
        se_digest, se_tail = err_drain.digest or hashlib.sha256(b"").hexdigest(), err_drain.tail
        truncated = out_drain.truncated or err_drain.truncated
    else:
        z = hashlib.sha256(b"").hexdigest()
        so_digest = se_digest = z
        so_tail = se_tail = ""
        truncated = False

    return {
        "started": started,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout_text": so_tail,
        "stderr_text": se_tail,
        "stdout_digest": so_digest,
        "stderr_digest": se_digest,
        "truncated": truncated,
        "cleanup_ok": cleanup_ok,
        "executor": executor,
        "isolation": isolation,
        "artifacts": [],
    }


def to_wsl_path(win_path):
    """Translate a Windows path to its WSL view (D:\\goai\\x -> /mnt/d/goai/x)."""
    p = win_path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2))
    return p


def docker_prefix(transport, wsl_distro):
    if transport == "wsl":
        return ["wsl.exe", "-d", wsl_distro, "--", "docker"]
    return ["docker"]


def build_docker_run_argv(transport, wsl_distro, image, work_mount,
                          run_argv, run_id, container_name, memory, cpus, pids_limit,
                          host_uid, host_gid, env_pairs):
    """Construct the hardened ``docker run`` argv (no shell, no --rm).

    No ``--rm``: cleanup is explicit and idempotent (see container_executor).
    Hardening: non-root verified UID:GID, --network=none, resource limits,
    cap-drop, no-new-privileges, read-only root, tmpfs /tmp + /artifacts.
    """
    prefix = docker_prefix(transport, wsl_distro)
    args = [
        "run",
        "--name", container_name,
        "--label=mp-run=" + run_id,
        "--user=" + str(host_uid) + ":" + str(host_gid),
        "--network=none",
        "--memory=" + memory,
        "--cpus=" + cpus,
        "--pids-limit=" + str(pids_limit),
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m,mode=1777",
        # /artifacts uses tmpfs with a hard 8 MiB execution-time size limit.
        # This is the ONLY Docker mechanism that provides execution-time quota.
        # tmpfs is ephemeral (data lost on container stop) — by design, file
        # artifacts cannot be extracted post-run from a container executor.
        # The structured test output (stdout/stderr, captured by drain threads)
        # is the authoritative result. The subprocess executor (trusted-dev)
        # uses a real host directory for artifacts; container artifacts are [].
        "--tmpfs", "/artifacts:rw,size=8388608,uid=" + str(host_uid) + ",gid=" + str(host_gid) + ",mode=1777",
    ]
    for pair in env_pairs:
        args += ["-e", pair]
    args += [
        "-v", work_mount + ":/work:ro",
        image,
    ]
    return prefix + args + list(run_argv)
