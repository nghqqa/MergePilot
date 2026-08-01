"""Round-3 hardening tests: rigorous cleanup correctness, Job Object transaction
safety, POSIX process-group reaping, precise output-cap boundaries, and the
finalize decision order.

The Windows cleanup tests mock the ctypes helper functions to simulate each
Win32 failure mode (TerminateProcess fails, TerminateJobObject fails,
WaitForSingleObject times out, OpenProcess access-denied) and assert the
cleanup never reports ``all_dead=True``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skills.case_retrieval import core
from skills.case_retrieval.embedding import fastembed_provider as fp
from skills.common.runtime.cli import Deadline


WIN = os.name == "nt"
_FIXTURES = Path(__file__).parent / "fixtures"


class _FakeProc:
    def __init__(self, pid, returncode=0):
        self.pid = pid
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _modes(monkeypatch):
    monkeypatch.setattr(fp, "_WORKER_PATH", str(_FIXTURES / "_stub_modes_worker.py"))
    return fp.FastEmbedProvider("stub-model", "1.0.0")


def _count_worker_procs() -> int:
    if not WIN:
        return 0
    out = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
        capture_output=True, text=True,
    ).stdout
    return sum(1 for line in out.splitlines() if "_fastembed_worker.py" in line)


def _assert_no_net_workers(before: int, timeout: float = 5.0) -> None:
    """The wmic process snapshot lags real termination, and on the
    job-creation-failure path a racing grandchild only self-exits once it reads
    stdin EOF (Python startup can be slow under load).  Poll until the count
    returns to ``<= before`` (the test must add no net worker).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _count_worker_procs() <= before:
            return
        time.sleep(0.1)
    assert _count_worker_procs() <= before, "worker residue after abort"


# --------------------------------------------------------------------------- #
# 1. Windows cleanup false-positive rejection (mocked ctypes helpers)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not WIN, reason="Windows cleanup primitives")
class TestWindowsCleanupFalsePositives:
    def _patch_ok(self, monkeypatch, *, pid_alive=False, terminate=True,
                  job=True, wait=True, descendants=None):
        calls = {"n": 0}
        def_desc = descendants if descendants is not None else []

        def fake_desc(root):
            calls["n"] += 1
            return def_desc if calls["n"] == 1 else []

        monkeypatch.setattr(fp, "_windows_descendant_pids", fake_desc)
        monkeypatch.setattr(fp, "_windows_terminate_pid", lambda pid: terminate)
        monkeypatch.setattr(fp, "_windows_terminate_job", lambda job_h: job)
        monkeypatch.setattr(fp, "_windows_wait_pid_gone", lambda pid, ms: wait)
        monkeypatch.setattr(fp, "_windows_pid_alive", lambda pid: pid_alive)

    def test_captured_descendant_survives_root_exit_returns_not_dead(self, monkeypatch):
        # Initial capture includes PID 200; after root exits the re-scan is
        # empty, but PID 200 is still alive -> must NOT report all_dead.
        calls = {"n": 0}

        def fake_desc(root):
            calls["n"] += 1
            return [200] if calls["n"] == 1 else []

        monkeypatch.setattr(fp, "_windows_descendant_pids", fake_desc)
        monkeypatch.setattr(fp, "_windows_terminate_pid", lambda pid: True)
        monkeypatch.setattr(fp, "_windows_terminate_job", lambda job_h: True)
        monkeypatch.setattr(fp, "_windows_wait_pid_gone", lambda pid, ms: True)
        monkeypatch.setattr(fp, "_windows_pid_alive", lambda pid: pid == 200)
        captured, all_dead = fp._cleanup_tree(_FakeProc(1234), job=object())
        assert 200 in captured
        assert all_dead is False

    def test_terminate_process_failure(self, monkeypatch):
        self._patch_ok(monkeypatch, terminate=False)
        _, all_dead = fp._cleanup_tree(_FakeProc(1234), job=object())
        assert all_dead is False

    def test_terminate_job_failure(self, monkeypatch):
        self._patch_ok(monkeypatch, job=False)
        _, all_dead = fp._cleanup_tree(_FakeProc(1234), job=object())
        assert all_dead is False

    def test_wait_timeout_failure(self, monkeypatch):
        self._patch_ok(monkeypatch, wait=False)
        _, all_dead = fp._cleanup_tree(_FakeProc(1234), job=object())
        assert all_dead is False

    def test_pid_query_says_alive(self, monkeypatch):
        # PID query returns alive (or access-denied/unknown) -> cannot prove gone
        self._patch_ok(monkeypatch, pid_alive=True, descendants=[55])
        _, all_dead = fp._cleanup_tree(_FakeProc(1234), job=object())
        assert all_dead is False

    def test_clean_success_returns_all_dead(self, monkeypatch):
        self._patch_ok(monkeypatch, pid_alive=False)
        captured, all_dead = fp._cleanup_tree(_FakeProc(1234), job=object())
        assert all_dead is True
        assert captured == [1234]


# --------------------------------------------------------------------------- #
# 2. Job Object create/bind transaction safety
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not WIN, reason="Job Object is Windows-only")
class TestJobObjectTransaction:
    def test_job_creation_failure_aborts_and_reaps(self, monkeypatch):
        monkeypatch.setattr(fp, "_create_kill_on_close_job", lambda: None)
        provider = fp.FastEmbedProvider("stub-model", "1.0.0")
        with pytest.raises(core.CaseRetrievalError) as raised:
            provider.embed("hello", deadline=Deadline(5000))
        assert raised.value.subcode == core.MODEL_UNAVAILABLE
        assert provider._proc is not None
        # direct worker reaped (descendant self-exits via closed stdin; the
        # full tree reap is verified by the marker tree-stub tests).
        assert not fp._pid_alive(provider._proc.pid)

    def test_job_bind_failure_aborts_and_reaps(self, monkeypatch):
        monkeypatch.setattr(fp, "_assign_job", lambda job, handle: False)
        provider = fp.FastEmbedProvider("stub-model", "1.0.0")
        with pytest.raises(core.CaseRetrievalError) as raised:
            provider.embed("hello", deadline=Deadline(5000))
        assert raised.value.subcode == core.MODEL_UNAVAILABLE
        assert provider._proc is not None
        assert not fp._pid_alive(provider._proc.pid)

    def test_job_config_stage_exception_aborts_and_reaps(self, monkeypatch):
        def boom():
            raise RuntimeError("config stage failure")
        monkeypatch.setattr(fp, "_create_kill_on_close_job", boom)
        provider = fp.FastEmbedProvider("stub-model", "1.0.0")
        with pytest.raises(core.CaseRetrievalError) as raised:
            provider.embed("hello", deadline=Deadline(5000))
        assert raised.value.subcode == core.MODEL_UNAVAILABLE
        assert provider._proc is not None
        assert not fp._pid_alive(provider._proc.pid)


# --------------------------------------------------------------------------- #
# 3. POSIX process-group cleanup (mocked os.killpg; runs on any platform)
# --------------------------------------------------------------------------- #
class TestPosixGroupCleanup:
    def test_sigterm_clears_group(self, monkeypatch):
        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)

        def fake_killpg(pgid, sig):
            raise ProcessLookupError()  # any signal -> group gone

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, _FakeProc(99)) is True

    def test_sigterm_survives_then_sigkill_clears(self, monkeypatch):
        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)
        sigkilled = {"v": False}

        def fake_killpg(pgid, sig):
            if sig == 0:
                if sigkilled["v"]:
                    raise ProcessLookupError()
                return  # alive before SIGKILL
            if sig == fp._SIGKILL:
                sigkilled["v"] = True
            # SIGTERM is a no-op (group survives)

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, _FakeProc(99)) is True
        assert sigkilled["v"] is True

    def test_group_survives_sigkill_is_fail_closed(self, monkeypatch):
        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)

        def fake_killpg(pgid, sig):
            if sig == 0:
                return  # always alive
            return  # signals no-op

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, _FakeProc(99)) is False

    def test_permission_error_is_fail_closed(self, monkeypatch):
        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)

        def fake_killpg(pgid, sig):
            raise PermissionError()

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, _FakeProc(99)) is False


# --------------------------------------------------------------------------- #
# 4. Precise output-cap boundaries and overrun (real subprocess)
# --------------------------------------------------------------------------- #
class TestBoundedOutputBoundaries:
    def test_cap_minus_one_is_not_over_limit(self, monkeypatch):
        provider = _modes(monkeypatch)
        n = fp.WORKER_MAX_OUTPUT_BYTES - 1
        with pytest.raises(core.CaseRetrievalError):
            provider.embed("exact:%d" % n, deadline=Deadline(10000))
        assert provider._over_limit is False  # read fully; not over cap

    def test_cap_is_not_over_limit(self, monkeypatch):
        provider = _modes(monkeypatch)
        n = fp.WORKER_MAX_OUTPUT_BYTES
        with pytest.raises(core.CaseRetrievalError):
            provider.embed("exact:%d" % n, deadline=Deadline(10000))
        assert provider._over_limit is False

    def test_cap_plus_one_is_over_limit(self, monkeypatch):
        provider = _modes(monkeypatch)
        n = fp.WORKER_MAX_OUTPUT_BYTES + 1
        with pytest.raises(core.CaseRetrievalError):
            provider.embed("exact:%d" % n, deadline=Deadline(10000))
        assert provider._over_limit is True

    def test_stdout_overrun_reaps_tree_and_threads(self, monkeypatch):
        provider = _modes(monkeypatch)
        with pytest.raises(core.CaseRetrievalError) as raised:
            provider.embed("stdout_overrun", deadline=Deadline(10000))
        assert raised.value.subcode == core.MODEL_UNAVAILABLE
        assert provider._proc is not None and provider._proc.poll() is not None


# --------------------------------------------------------------------------- #
# 5. Finalize decision order (pure unit tests)
# --------------------------------------------------------------------------- #
class TestFinalizeDecision:
    def _ok_out(self):
        return json.dumps(
            {"version": 1, "status": "ok", "dim": 384, "vector": [0.0] * 384}
        ).encode("utf-8")

    def test_cleanup_failure_outranks_timeout(self):
        with pytest.raises(core.CaseRetrievalError) as raised:
            fp._finalize_decision(
                all_dead=False, reader_alive=False, writer_alive=False,
                timed_out=True, over_limit=False, returncode=0, out=b"",
            )
        assert raised.value.subcode == core.MODEL_UNAVAILABLE  # not TIMEOUT

    def test_reader_alive_after_join_is_model_unavailable(self):
        with pytest.raises(core.CaseRetrievalError) as raised:
            fp._finalize_decision(
                all_dead=True, reader_alive=True, writer_alive=False,
                timed_out=True, over_limit=False, returncode=0, out=self._ok_out(),
            )
        assert raised.value.subcode == core.MODEL_UNAVAILABLE  # not TIMEOUT

    def test_writer_alive_after_join_is_model_unavailable(self):
        with pytest.raises(core.CaseRetrievalError) as raised:
            fp._finalize_decision(
                all_dead=True, reader_alive=False, writer_alive=True,
                timed_out=False, over_limit=False, returncode=0, out=self._ok_out(),
            )
        assert raised.value.subcode == core.MODEL_UNAVAILABLE

    def test_over_limit_returncode_nonzero_priorities(self):
        # over_limit outranks a nonzero returncode; both are MODEL_UNAVAILABLE
        with pytest.raises(core.CaseRetrievalError) as raised:
            fp._finalize_decision(
                all_dead=True, reader_alive=False, writer_alive=False,
                timed_out=False, over_limit=True, returncode=3, out=b"",
            )
        assert raised.value.subcode == core.MODEL_UNAVAILABLE

    def test_success_returns_validated_vector(self):
        vec = fp._finalize_decision(
            all_dead=True, reader_alive=False, writer_alive=False,
            timed_out=False, over_limit=False, returncode=0, out=self._ok_out(),
        )
        assert isinstance(vec, list) and len(vec) == 384


# --------------------------------------------------------------------------- #
# 6. Round-4: POSIX session leader / zombie reap order
# --------------------------------------------------------------------------- #
class TestPosixReapOrder:
    """A zombie leader keeps the process group visible until reaped, so the
    direct child must be reaped BEFORE the final ``killpg(pgid, 0)`` verdict.
    These mock ``os.killpg`` + a stateful proc so they run on any platform."""

    def _patch_sleep(self, monkeypatch):
        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)

    def test_killpg_alive_before_wait_gone_after_returns_true(self, monkeypatch):
        # Group is alive until proc.wait() reaps the leader, then gone.
        self._patch_sleep(monkeypatch)
        state = {"waited": False}

        class Proc:
            def wait(self, timeout=None):
                state["waited"] = True
                return 0

            def poll(self):
                return 0 if state["waited"] else None

        def fake_killpg(pgid, sig):
            if sig == 0:
                if state["waited"]:
                    raise ProcessLookupError()
                return
            return

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is True

    def test_normal_exit_leader_reaped_passes(self, monkeypatch):
        self._patch_sleep(monkeypatch)

        class Proc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def fake_killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError()
            return

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is True

    def test_leader_exited_grandchild_cleaned_by_sigkill(self, monkeypatch):
        # Leader already exited (poll=0) but a grandchild survives SIGTERM; the
        # saved pgid must still be SIGKILLed so the grandchild is reaped.
        self._patch_sleep(monkeypatch)
        state = {"killed": False}

        class Proc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0  # leader gone

        def fake_killpg(pgid, sig):
            if sig == 0:
                if state["killed"]:
                    raise ProcessLookupError()
                return  # grandchild alive
            if sig == fp._SIGKILL:
                state["killed"] = True
            # SIGTERM is a no-op (grandchild survives)

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is True
        assert state["killed"] is True

    def test_sigterm_ineffective_sigkill_reaps_leader(self, monkeypatch):
        self._patch_sleep(monkeypatch)
        state = {"killed": False}

        class Proc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return None if not state["killed"] else 0

        def fake_killpg(pgid, sig):
            if sig == 0:
                if state["killed"]:
                    raise ProcessLookupError()
                return
            if sig == fp._SIGKILL:
                state["killed"] = True

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is True
        assert state["killed"] is True

    def test_wait_failure_is_fail_closed(self, monkeypatch):
        self._patch_sleep(monkeypatch)

        class Proc:
            def wait(self, timeout=None):
                raise OSError("wait failed")

            def poll(self):
                return None

        def fake_killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError()

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is False

    def test_group_persists_is_fail_closed(self, monkeypatch):
        self._patch_sleep(monkeypatch)

        class Proc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def fake_killpg(pgid, sig):
            if sig == 0:
                return  # always alive
            return

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX real process group/session")
class TestPosixRealTreeStub:
    """Real _stub_tree_worker on POSIX: success / error / timeout, verifying the
    whole session is reaped (not pure mocks)."""

    def _run(self, monkeypatch, tmp_path, mode):
        monkeypatch.setattr(
            fp, "_WORKER_PATH", str(_FIXTURES / "_stub_tree_worker.py")
        )
        marker = tmp_path / ("posix_tree_%s.txt" % mode)
        provider = fp.FastEmbedProvider("stub-model", "1.0.0")
        subcode = None
        try:
            provider.embed("%s|%s" % (mode, marker), deadline=Deadline(2000))
        except core.CaseRetrievalError as exc:
            subcode = exc.subcode
        worker_pid, grandchild_pid = (int(x) for x in marker.read_text().split())
        return subcode, worker_pid, grandchild_pid

    def test_success(self, monkeypatch, tmp_path):
        subcode, worker_pid, grandchild_pid = self._run(monkeypatch, tmp_path, "exit")
        assert subcode is None
        assert not fp._pid_alive(worker_pid)
        assert not fp._pid_alive(grandchild_pid)

    def test_error(self, monkeypatch, tmp_path):
        subcode, worker_pid, grandchild_pid = self._run(monkeypatch, tmp_path, "error")
        assert subcode == core.MODEL_UNAVAILABLE
        assert not fp._pid_alive(worker_pid)
        assert not fp._pid_alive(grandchild_pid)

    def test_timeout(self, monkeypatch, tmp_path):
        subcode, worker_pid, grandchild_pid = self._run(monkeypatch, tmp_path, "sleep")
        assert subcode == core.TIMEOUT_SUB
        assert not fp._pid_alive(worker_pid)
        assert not fp._pid_alive(grandchild_pid)


# --------------------------------------------------------------------------- #
# 7. Round-4: Job Object config-exception transaction
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not WIN, reason="Job Object is Windows-only")
class TestJobConfigExceptionTransaction:
    def test_abort_spawn_closes_job_handle_exactly_once(self, monkeypatch):
        fake_job = 999999
        closed = []
        monkeypatch.setattr(fp, "_cleanup_tree", lambda proc, job=None: ([proc.pid], True))
        monkeypatch.setattr(fp._kernel32, "CloseHandle", lambda h: closed.append(h))
        fp._abort_spawn(_FakeProc(99), fake_job)
        assert closed.count(fake_job) == 1  # closed exactly once

    def test_spawn_config_exception_passes_real_job_to_abort(self, monkeypatch):
        fake_job = 8888
        monkeypatch.setattr(fp, "_create_kill_on_close_job", lambda: fake_job)

        def boom(job, handle):
            raise RuntimeError("bind stage failure")

        monkeypatch.setattr(fp, "_assign_job", boom)
        received = []
        real_abort = fp._abort_spawn

        def trace_abort(proc, job=None):
            received.append(job)
            real_abort(proc, job)  # actually reap the tree

        monkeypatch.setattr(fp, "_abort_spawn", trace_abort)
        provider = fp.FastEmbedProvider("stub-model", "1.0.0")
        with pytest.raises(core.CaseRetrievalError) as raised:
            provider.embed("hi", deadline=Deadline(5000))
        assert raised.value.subcode == core.MODEL_UNAVAILABLE
        assert fake_job in received  # real job handle, not None
        assert provider._proc is not None
        assert not fp._pid_alive(provider._proc.pid)  # direct worker reaped


# --------------------------------------------------------------------------- #
# 8. Round-5: POSIX recoverable first-wait timeout
# --------------------------------------------------------------------------- #
class TestPosixRecoverableWaitTimeout:
    def test_first_wait_timeout_recovers_via_sigkill(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)
        # On Windows signal.SIGKILL is absent so _SIGKILL aliases _SIGTERM;
        # count signals so the test is robust to that aliasing.
        state = {"signals": 0, "waits": 0}

        class Proc:
            def wait(self, timeout=None):
                state["waits"] += 1
                if state["waits"] == 1:
                    raise subprocess.TimeoutExpired(cmd="embed", timeout=2)
                return 0  # second wait succeeds after the group is killed

            def poll(self):
                return None if state["signals"] < 2 else 0

        def fake_killpg(pgid, sig):
            if sig == 0:
                if state["signals"] >= 2:
                    raise ProcessLookupError()
                return  # group still alive
            state["signals"] += 1  # SIGTERM then SIGKILL

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is True
        assert state["waits"] == 2  # first (timeout) then second (after SIGKILL)
        assert state["signals"] >= 2  # SIGTERM then SIGKILL both sent

    def test_second_wait_failure_after_sigkill_is_fail_closed(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(fp.time, "sleep", lambda *a: None)
        state = {"waits": 0}

        class Proc:
            def wait(self, timeout=None):
                state["waits"] += 1
                if state["waits"] == 1:
                    raise subprocess.TimeoutExpired(cmd="embed", timeout=2)
                raise OSError("second wait failed")

            def poll(self):
                return None

        def fake_killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError()

        monkeypatch.setattr(fp.os, "killpg", fake_killpg, raising=False)
        assert fp._cleanup_posix_group(99, Proc()) is False

    def test_finalize_returns_timeout_when_cleanup_succeeds_after_request_timeout(self):
        # Original request timed out but cleanup succeeded -> TIMEOUT (not
        # MODEL_UNAVAILABLE). returncode is None (process killed by timeout).
        with pytest.raises(core.CaseRetrievalError) as raised:
            fp._finalize_decision(
                all_dead=True, reader_alive=False, writer_alive=False,
                timed_out=True, over_limit=False, returncode=None, out=b"",
            )
        assert raised.value.subcode == core.TIMEOUT_SUB


# --------------------------------------------------------------------------- #
# 9. Round-5: platform-aware gate expectations
# --------------------------------------------------------------------------- #
class TestGateExpectations:
    def test_expected_counts_select_correctly_per_platform(self):
        import runpy

        cfg = runpy.run_path(os.path.join(os.path.dirname(__file__), "conftest.py"))
        nt = cfg["_expected_for"]("nt")
        px = cfg["_expected_for"]("posix")
        assert isinstance(nt[0], int) and isinstance(nt[1], int)
        assert isinstance(px[0], int) and isinstance(px[1], int)
        assert nt != px  # platforms skip different subsets
        # the current platform's exported numbers match its selection
        assert (cfg["EXPECTED_PASS"], cfg["EXPECTED_SKIP"]) == cfg["_expected_for"](os.name)
