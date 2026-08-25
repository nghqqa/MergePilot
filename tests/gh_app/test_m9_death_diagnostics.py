# -*- coding: utf-8 -*-
"""M9 finding D: demo-console exit 1 must carry the REAL error.

The external round saw demo-console exit 1 with diagnostics holding
only the preflight banner; the startup probe's real error never made
it into the failure detail. These tests pin the fix: the
container-not-running failure for demo-console embeds the FIRST
STABLE ERROR extracted from the full container logs (and the full
stderr tail rides along in diagnostics).
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "cli"))

import mergepilot as mp  # noqa: E402


class _FakeProc:
    def __init__(self, rc=0, stdout=b"", stderr=b""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class _DeathWorld:
    """Minimal docker-double: a demo-console container that has exited
    with the real error on stderr (after the preflight banner)."""

    LOGS = (
        "Config preflight passed: mode=ISOLATED_LIVE\n"
        "  loopback_only=True, read_only=True\n"
        "STARTUP PROBE FAILED: state=INIT, error=RUN_NOT_FOUND\n"
        "Traceback (most recent call last):\n"
        '  File "/app/serve.py", line 88, in main\n'
        "RuntimeError: seeded showcase case not found\n"
    )

    def __init__(self):
        self.calls = []
        self._planner = None

    def _run(self, argv, **kw):
        self.calls.append(tuple(argv))
        tail = argv[argv.index("--") + 1:] if "--" in argv else argv
        if tail[:1] == ["docker"]:
            args = tail[1:]
            if args[:1] == ["inspect"]:
                if "{{.Id}}@@{{json .State}}" in args:
                    state = {
                        "Dead": False, "Error": "",
                        "ExitCode": 1,
                        "FinishedAt": "2026-08-25T00:00:00Z",
                        "Health": {"Status": "unhealthy"},
                        "Status": "exited",
                    }
                    blob = ("sha256:" + "0" * 64 + "@@" +
                            json.dumps(state))
                    return _FakeProc(0, stdout=blob.encode())
            if args[:1] == ["logs"]:
                return _FakeProc(0, stdout=self.LOGS.encode())
        return _FakeProc(0, stdout=b"")

    # surface used by WslDocker
    class _P:
        def assert_argv_safe(self, argv):
            return True

    def docker(self, args, **kw):
        argv = ["wsl.exe", "-u", "root", "-d", "X", "--", "docker"] + list(args)
        cp = self._run(argv, **kw)
        return cp


class DemoConsoleDeathDetail(unittest.TestCase):
    def _wait(self):
        import subprocess
        world = _DeathWorld()
        docker = mp.WslDocker.__new__(mp.WslDocker)
        docker._planner = _DeathWorld._P()
        docker._project_dir = Path(".")
        docker._distro_states = {"MergePilot-Test": "Running"}
        docker._allow_wake = False
        docker._wake_attempted = True
        docker._run_wsl = world._run
        docker.docker = world.docker
        try:
            docker.wait_healthy("mergepilot-isolated-demo-console-1", 4)
            return None, None
        except mp.Failure as failure:
            return failure, world

    def test_failure_carries_first_stable_error(self):
        failure, world = self._wait()
        self.assertIsNotNone(failure, "exited container must raise")
        detail = getattr(failure, "detail", "") or ""
        self.assertIn("RUN_NOT_FOUND", detail,
                      "the startup probe's stable error must surface")
        self.assertIn("demo-console", detail)

    def test_failure_carries_stderr_tail_not_just_banner(self):
        failure, world = self._wait()
        detail = getattr(failure, "detail", "") or ""
        self.assertIn("RuntimeError", detail,
                      "the traceback's real error must ride along")

    def test_logs_were_fetched_during_death_path(self):
        failure, world = self._wait()
        logged = any("logs" in " ".join(c) for c in world.calls)
        self.assertTrue(logged, "container logs must be fetched on death")


if __name__ == "__main__":
    unittest.main()
