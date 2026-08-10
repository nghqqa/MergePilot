#!/usr/bin/env python3
"""Host + guest disk threshold guard for production startup.

Runs BEFORE any docker build/run/rm/restart. Checks BOTH:

  GUEST (inside WSL): free space on the filesystem holding the Docker root
    (MP_DOCKER_ROOT, default /var/lib/docker), via ``df -P -k``.

  HOST (Windows): free space on the volume holding the WSL VHDX
    (MP_WSL_VHDX_PATH), via powershell.exe ``Get-Volume``.

Fail-closed (exit 2) if ANY of:
  * guest free < MP_DISK_MIN_GUEST_GIB (default 100)
  * host free < MP_DISK_MIN_HOST_GIB (default 150)
  * MP_WSL_VHDX_PATH is unset/empty
  * the host query fails (powershell missing, error, non-numeric output)

The host VHDX path is NEVER hardcoded; it MUST be supplied via
MP_WSL_VHDX_PATH (e.g. ``E:\\WSL\\Ubuntu-22.04\\ext4.vhdx``). Unset/empty
or a failed query is fail-closed -- production startup MUST NOT proceed
when the host free space cannot be established.

Probes are injectable (runner callbacks) so unit tests need no WSL/Docker.
Thresholds are overridable via MP_DISK_MIN_GUEST_GIB / MP_DISK_MIN_HOST_GIB.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

DEFAULT_MIN_GUEST_GIB = 100
DEFAULT_MIN_HOST_GIB = 150
DEFAULT_DOCKER_ROOT = "/var/lib/docker"
DEFAULT_PS_PATH = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
)

_KIB = 1024
_GIB = 1024 ** 3


def _ps_escape_single(value):
    """Escape a value for a PowerShell single-quoted string literal."""
    return str(value).replace("'", "''")


def _default_runner(argv):
    """Run argv and return stdout text; raise on non-zero rc."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(
            "probe rc=%d stderr=%s" % (proc.returncode, proc.stderr[:200])
        )
    return proc.stdout


def probe_guest_free_kib(docker_root, runner=None):
    """Return free space (KiB) of the filesystem holding ``docker_root``.

    Returns None on any failure (caller treats as fail-closed).
    """
    runner = runner or _default_runner
    try:
        out = runner(["df", "-P", "-k", "--", str(docker_root)])
    except Exception:
        return None
    data_lines = [
        line
        for line in out.splitlines()
        if line.strip() and not line.lstrip().startswith("Filesystem")
    ]
    if not data_lines:
        return None
    fields = data_lines[-1].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3])  # Available column, 1K-blocks == KiB
    except ValueError:
        return None


def probe_host_free_bytes(vhdx_path, ps_path=None, runner=None):
    """Return free bytes of the Windows volume holding ``vhdx_path``.

    Uses powershell.exe Get-Volume on the drive of the VHDX file. Returns
    None on any failure (missing powershell, query error, non-numeric
    output) so the caller can fail-closed. Never raises.
    """
    runner = runner or _default_runner
    ps_path = ps_path or os.environ.get("MP_POWERSHELL_PATH", DEFAULT_PS_PATH)
    if not vhdx_path:
        return None
    if not ps_path:
        return None
    escaped = _ps_escape_single(vhdx_path)
    ps_cmd = (
        "$ErrorActionPreference='Stop';"
        "try{"
        "$f=Get-Item -LiteralPath '" + escaped + "';"
        "$drv=$f.PSDrive.Name;"
        "[long](Get-Volume -DriveLetter $drv).SizeRemaining"
        "}catch{ Write-Error $_; exit 86 }"
    )
    try:
        out = runner([ps_path, "-NoProfile", "-NonInteractive",
                      "-Command", ps_cmd])
    except Exception:
        return None
    stripped = out.strip()
    if not stripped:
        return None
    last = stripped.splitlines()[-1].strip()
    if not re.fullmatch(r"[0-9]+", last):
        return None
    return int(last)


def _kib_to_gib(kib):
    return kib // _KIB // _KIB


def _bytes_to_gib(b):
    return b // _GIB


def check(min_guest_gib=None, min_host_gib=None, vhdx_path=None,
          docker_root=None, guest_runner=None, host_runner=None,
          ps_path=None):
    """Run both probes. Returns (ok: bool, detail: dict)."""
    min_guest = int(
        min_guest_gib
        if min_guest_gib is not None
        else os.environ.get("MP_DISK_MIN_GUEST_GIB", DEFAULT_MIN_GUEST_GIB)
    )
    min_host = int(
        min_host_gib
        if min_host_gib is not None
        else os.environ.get("MP_DISK_MIN_HOST_GIB", DEFAULT_MIN_HOST_GIB)
    )
    vhdx = vhdx_path if vhdx_path is not None else os.environ.get(
        "MP_WSL_VHDX_PATH", ""
    )
    root = docker_root or os.environ.get("MP_DOCKER_ROOT", DEFAULT_DOCKER_ROOT)

    detail = {
        "min_guest_gib": min_guest,
        "min_host_gib": min_host,
        "vhdx_path": vhdx,
        "docker_root": root,
        "ok": False,
    }

    guest_kib = probe_guest_free_kib(root, guest_runner)
    if guest_kib is None:
        detail["error"] = "guest probe failed for %s" % root
        return (False, detail)
    guest_gib = _kib_to_gib(guest_kib)
    detail["guest_free_gib"] = guest_gib
    if guest_gib < min_guest:
        detail["error"] = "guest free %dGiB < %dGiB" % (guest_gib, min_guest)
        return (False, detail)

    host_bytes = probe_host_free_bytes(vhdx, ps_path, host_runner)
    if host_bytes is None:
        detail["error"] = "host probe failed (fail-closed)"
        return (False, detail)
    host_gib = _bytes_to_gib(host_bytes)
    detail["host_free_gib"] = host_gib
    if host_gib < min_host:
        detail["error"] = "host free %dGiB < %dGiB" % (host_gib, min_host)
        return (False, detail)

    detail["ok"] = True
    return (True, detail)


def main():
    ok, detail = check()
    if ok:
        sys.stdout.write(
            "disk_guard OK guest=%dGiB(>=%d) host=%dGiB(>=%d) vhdx=%s\n"
            % (detail["guest_free_gib"], detail["min_guest_gib"],
               detail["host_free_gib"], detail["min_host_gib"],
               detail["vhdx_path"])
        )
        return 0
    sys.stderr.write("disk_guard FAIL %s\n" % detail.get("error", "unknown"))
    sys.stderr.write("detail=%s\n" % detail)
    return 2


if __name__ == "__main__":
    sys.exit(main())
