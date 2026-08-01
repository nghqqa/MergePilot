"""FastEmbed provider with a killable, credential-isolated subprocess boundary.

When a cooperative deadline is supplied the inference runs in a child process
launched via :func:`subprocess.Popen` (``shell=False``) with an explicit
MINIMAL environment built by allowlist.  The child never receives deploy
credentials (``MERGEPILOT_CR_*`` or any DSN/TOKEN/SECRET/PASSWORD/... named
variable): nothing of the sort is on the allowlist, so it cannot be inherited.

The child communicates over stdin/stdout only, using a small versioned JSON
protocol; model and text travel over stdin and never appear on the command
line.  ``stderr`` is ``DEVNULL`` (never captured).  ``stdout`` is drained by a
reader thread that keeps at most ``WORKER_MAX_OUTPUT_BYTES`` in memory and
treats strictly more than that as over-limit.

Reaping discipline (one routine for success / failure / timeout / protocol
error): the full descendant set is captured BEFORE anything is terminated;
descendants are terminated leaves-first then root; the Job Object is also
terminated; every PID is waited on and then re-queried; new descendants are
re-scanned and merged.  EVERY Win32 call's return value is checked and
``GetLastError`` distinguishes "process gone" (ERROR_INVALID_PARAMETER) from
"access denied / unknown" -- only the former counts as dead.  On POSIX the
child runs in its own session and the whole process group is signalled and
loop-verified.  If cleanup cannot PROVE the tree and the IO threads are gone we
fail closed with ``MODEL_UNAVAILABLE`` -- this outranks TIMEOUT, the protocol
result, and any pass.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time

from ..core import (
    CaseRetrievalError,
    DIMENSION_MISMATCH,
    INVALID_INPUT,
    MODEL_UNAVAILABLE,
    TIMEOUT_SUB,
)


_SIGTERM = signal.SIGTERM
_SIGKILL = getattr(signal, "SIGKILL", _SIGTERM)  # POSIX-only; tests mock killpg

EMBEDDING_DIM = 384
PROTOCOL_VERSION = 1

WORKER_MAX_INPUT_BYTES = 65536
WORKER_MAX_OUTPUT_BYTES = 262144
WORKER_IO_CHUNK = 8192

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER_PATH = os.path.join(_HERE, "_fastembed_worker.py")

_PLATFORM_ALLOW = {"PATH"}
_WINDOWS_ALLOW = {
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "PATHEXT",
    "COMSPEC",
}
_POSIX_ALLOW = {"TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE"}
_CACHE_ALLOW = {
    "FASTEMBED_CACHE_PATH",
    "HF_HOME",
    "HF_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "XDG_CACHE_HOME",
}

_CREDENTIAL_NAME_RE = re.compile(
    r"(?:^MERGEPILOT_CR_|DSN|TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL|AUTH|COOKIE)",
    re.IGNORECASE,
)


def _is_sensitive_name(name: str) -> bool:
    return bool(_CREDENTIAL_NAME_RE.search(name))


def _minimal_env() -> dict:
    allow = set(_PLATFORM_ALLOW)
    if os.name == "nt":
        allow |= _WINDOWS_ALLOW
    else:
        allow |= _POSIX_ALLOW
    allow |= _CACHE_ALLOW
    env = {}
    for key in allow:
        value = os.environ.get(key)
        if isinstance(value, str) and value and not _is_sensitive_name(key):
            env[key] = value
    for key in list(env):
        if _is_sensitive_name(key):
            env.pop(key, None)
    return env


def _parse_response(out: bytes):
    if not out or len(out) > WORKER_MAX_OUTPUT_BYTES:
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker response invalid")
    try:
        payload = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker response invalid")
    if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker response invalid")
    if payload.get("status") != "ok":
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "model unavailable")
    return payload.get("vector")


def _validate_vector(vector):
    if not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
        raise CaseRetrievalError(DIMENSION_MISMATCH, "dimension mismatch")
    if any(not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in vector):
        raise CaseRetrievalError(DIMENSION_MISMATCH, "vector invalid")
    return [float(value) for value in vector]


# --------------------------------------------------------------------------- #
# Windows process-tree primitives
# --------------------------------------------------------------------------- #
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32
    # OpenProcess needs a reliable last-error read: use_last_error captures
    # GetLastError immediately after the native call, before Python/ctypes
    # bookkeeping can clobber it.  A plain GetLastError() afterwards is
    # unreliable and mis-classifies a gone PID as not-gone.
    _kernel32_le = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32_le.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32_le.OpenProcess.restype = wintypes.HANDLE
    _kernel32_le.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32_le.TerminateProcess.restype = wintypes.BOOL

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 0x00000102
    _ERROR_INVALID_PARAMETER = 87
    _ERROR_ACCESS_DENIED = 5
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetLastError.argtypes = []
    _kernel32.GetLastError.restype = wintypes.DWORD
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL

    def _windows_open_process(access, pid):
        """Open a process by PID.  Return ``(handle, is_gone)``.

        ``is_gone`` is True ONLY when OpenProcess failed specifically because
        the PID no longer exists (ERROR_INVALID_PARAMETER).  Any other failure
        (access denied, etc.) leaves ``is_gone`` False so the caller cannot
        prove the PID is gone and must treat it as still alive (fail-closed).
        The last error is read via ``use_last_error`` so Python/ctypes
        bookkeeping cannot clobber it between the call and the read.
        """
        handle = _kernel32_le.OpenProcess(access, False, pid)
        err = ctypes.get_last_error()
        if not handle or handle == _INVALID_HANDLE_VALUE:
            return None, (err == _ERROR_INVALID_PARAMETER)
        return handle, False

    def _windows_descendant_pids(root_pid: int):
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            return []
        parents = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parents.setdefault(entry.th32ParentProcessID, []).append(
                        entry.th32ProcessID
                    )
                    if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            _kernel32.CloseHandle(snapshot)
        found, stack = [], [root_pid]
        while stack:
            parent = stack.pop()
            for child in parents.get(parent, []):
                found.append(child)
                stack.append(child)
        return found

    def _windows_pid_alive(pid: int) -> bool:
        """True if the PID is alive OR we cannot prove it is gone.  False ONLY
        on a confirmed 'process does not exist' result."""
        handle, is_gone = _windows_open_process(_PROCESS_QUERY_LIMITED_INFORMATION, pid)
        if handle is None:
            return not is_gone  # gone -> False; access-denied/unknown -> True
        try:
            code = wintypes.DWORD(0)
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # query failed -> cannot prove gone -> alive
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_terminate_pid(pid: int) -> bool:
        """True if termination succeeded OR the PID was already gone / dying.

        False only on a genuine unknown failure.  TerminateProcess on a process
        in its final exit phase returns ERROR_ACCESS_DENIED and on an exited
        process ERROR_INVALID_PARAMETER; both mean the PID is going/gone (the
        subsequent wait + pid_alive confirm), so they are not cleanup failures.
        """
        handle, is_gone = _windows_open_process(_PROCESS_TERMINATE, pid)
        if handle is None:
            return is_gone  # gone -> True; else False
        try:
            if _kernel32_le.TerminateProcess(handle, 1):
                return True
            err = ctypes.get_last_error()
            return err in (_ERROR_ACCESS_DENIED, _ERROR_INVALID_PARAMETER)
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_wait_pid_gone(pid: int, timeout_ms: int) -> bool:
        """True if the PID is confirmed gone within the timeout.  False on
        timeout, access denied, or unknown."""
        handle, is_gone = _windows_open_process(_SYNCHRONIZE, pid)
        if handle is None:
            return is_gone
        try:
            return _kernel32.WaitForSingleObject(handle, timeout_ms) == _WAIT_OBJECT_0
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_terminate_job(job) -> bool:
        if not job:
            return True
        return bool(_kernel32.TerminateJobObject(job, 1))

    def _create_kill_on_close_job():
        job = _kernel32.CreateJobObjectW(None, None)
        if not job or job == _INVALID_HANDLE_VALUE:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(job)
            return None
        return job

    def _assign_job(job, proc_handle) -> bool:
        if not job or not proc_handle:
            return False
        return bool(_kernel32.AssignProcessToJobObject(job, proc_handle))

else:  # POSIX
    def _windows_descendant_pids(root_pid: int):  # pragma: no cover - POSIX
        return []

    def _windows_pid_alive(pid: int):  # pragma: no cover - POSIX
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _windows_terminate_pid(pid: int):  # pragma: no cover - POSIX
        return True

    def _windows_wait_pid_gone(pid: int, timeout_ms: int):  # pragma: no cover - POSIX
        return True

    def _windows_terminate_job(job):  # pragma: no cover - POSIX
        return True

    def _create_kill_on_close_job():  # pragma: no cover - POSIX
        return None

    def _assign_job(job, proc_handle):  # pragma: no cover - POSIX
        return False


def _pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' (conservative: unknown=alive)."""
    return _windows_pid_alive(pid)


def _cleanup_posix_group(pgid: int, proc) -> bool:
    """Reap a whole POSIX process group (``pgid`` captured at spawn time).

    Order: SIGTERM the saved group -> reap the direct child (a zombie leader
    would otherwise keep the group visible and mask a clean exit as a failure)
    -> SIGKILL the saved group if the direct child hasn't exited or members
    remain -> reap the direct child again -> loop-verify ``os.killpg(pgid, 0)``
    raises ProcessLookupError.  Even when the leader has already exited the
    SAVED pgid is signalled so descendants are still reached.
    """
    def _group_gone():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return None
        return False

    all_dead = True

    try:
        os.killpg(pgid, _SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        all_dead = False

    # Reap the direct child BEFORE the final group verdict (clears a zombie
    # leader that would otherwise keep killpg(pgid, 0) reporting the group).
    # A first-wait TimeoutExpired is RECOVERABLE: it just means we need to
    # escalate to SIGKILL, not a final cleanup failure.
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        all_dead = False

    # SIGKILL the saved group if the direct child hasn't exited OR members remain
    if proc.poll() is None or _group_gone() is False:
        try:
            os.killpg(pgid, _SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            all_dead = False
        # The second wait (after SIGKILL) must succeed; its failure is final.
        try:
            proc.wait(timeout=2.0)
        except Exception:
            all_dead = False

    # Final loop-verify: killpg(pgid, 0) must confirm the group is gone.
    for _ in range(50):  # ~1s
        if _group_gone() is True:
            break
        time.sleep(0.02)
    if _group_gone() is not True:
        all_dead = False

    return all_dead


def _cleanup_tree(proc, job=None):
    """Unified reaper.  Returns ``(captured_pids, all_dead)``.  Every Win32
    call's return value is checked; re-query after termination; new descendants
    merged.  ``all_dead`` is False if ANY PID is alive, ANY API failed, or ANY
    wait/query could not confirm the PID gone.

    On Windows the descendant snapshot is taken repeatedly and newly-seen PIDs
    are terminated each round: a child can spawn a grandchild AFTER our first
    snapshot, and on the job-creation-failure path there is no Job Object to
    catch it.  Looping until the tree is stable guarantees such a racing
    grandchild is reaped instead of orphaned.
    """
    root = proc.pid
    if os.name == "nt":
        all_dead = True
        captured = [root]
        # Repeatedly snapshot + terminate newly-seen descendants until stable.
        for _ in range(8):
            new = [p for p in _windows_descendant_pids(root) if p not in captured]
            if not new:
                break
            for pid in reversed(new):  # leaves first
                if not _windows_terminate_pid(pid):
                    all_dead = False
            for pid in new:
                captured.append(pid)
                if not _windows_wait_pid_gone(pid, 1000):
                    all_dead = False
        if not _windows_terminate_job(job):
            all_dead = False
        # terminate root last (idempotent for already-terminated descendants)
        # and CHECK every return value, including the root's.
        for pid in reversed(captured):
            if not _windows_terminate_pid(pid):
                all_dead = False
        for pid in captured:
            if not _windows_wait_pid_gone(pid, 2000):
                all_dead = False
            if _windows_pid_alive(pid):
                all_dead = False
        # final re-scan: any descendant still alive -> fail (and record it)
        for pid in _windows_descendant_pids(root):
            if pid not in captured:
                captured.append(pid)
            if _windows_pid_alive(pid):
                all_dead = False
        try:
            proc.wait(timeout=2.0)
        except Exception:
            all_dead = False
        return captured, all_dead
    # POSIX
    all_dead = _cleanup_posix_group(root, proc)
    return [root], all_dead


def _abort_spawn(proc, job=None):
    """Best-effort reap used when spawn/job setup fails; never raises.

    Closes the parent's stdin/stdout pipe ends first so a worker blocked on a
    stdin read receives EOF and can wind down on its own (the snapshot in
    ``_cleanup_tree`` then reaps it; this covers the race where the grandchild
    spawns after the first snapshot).
    """
    for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    try:
        _cleanup_tree(proc, job)
    except Exception:
        pass
    if job:
        try:
            _kernel32.CloseHandle(job)
        except Exception:
            pass


def _finalize_decision(all_dead, reader_alive, writer_alive, timed_out,
                       over_limit, returncode, out):
    """Decision order: cleanup failure > IO-thread liveness > timeout >
    over-limit > exit code > protocol.  cleanup failure always wins."""
    if not all_dead:
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker cleanup incomplete")
    if reader_alive or writer_alive:
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "io threads did not exit")
    if timed_out:
        raise CaseRetrievalError(TIMEOUT_SUB, "embedding timeout")
    if over_limit:
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker output exceeded limit")
    if returncode is None or returncode != 0:
        raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker exit code")
    return _validate_vector(_parse_response(out))


class FastEmbedProvider:
    def __init__(self, model, version):
        self.model = model
        self.version = version
        self._model = None
        self._proc = None
        self._job = None
        self._tree_pids = []
        self._over_limit = False

    def _embed_cached(self, text):
        if self._model is None:
            try:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=self.model)
            except Exception:
                raise CaseRetrievalError(MODEL_UNAVAILABLE, "model unavailable")
        try:
            vector = list(self._model.embed([text]))[0].tolist()
        except Exception:
            raise CaseRetrievalError(MODEL_UNAVAILABLE, "inference failed")
        return _validate_vector(vector)

    def _spawn(self):
        """Popen + transactional Job Object setup.  On ANY post-Popen failure
        the already-started process is reaped (no worker left blocking on stdin)
        and MODEL_UNAVAILABLE is raised.  self._proc is set first so tests can
        inspect it even on the failure path."""
        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_minimal_env(),
            shell=False,
            close_fds=True,
        )
        if os.name != "nt":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, _WORKER_PATH], **kwargs)
        self._proc = proc
        if os.name != "nt":
            return proc, None

        handle = getattr(proc, "_handle", None)
        if not handle:
            _abort_spawn(proc, None)
            raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker handle unavailable")
        job = None
        try:
            job = _create_kill_on_close_job()
            if not job:
                _abort_spawn(proc, None)
                raise CaseRetrievalError(MODEL_UNAVAILABLE, "job creation failed")
            if not _assign_job(job, int(handle)):
                _abort_spawn(proc, job)
                raise CaseRetrievalError(MODEL_UNAVAILABLE, "job bind failed")
        except CaseRetrievalError:
            raise
        except Exception:
            # job may already be a valid handle (e.g. _assign_job raised after a
            # successful create); pass it so _abort_spawn closes it exactly once.
            _abort_spawn(proc, job)
            raise CaseRetrievalError(MODEL_UNAVAILABLE, "job config failed")
        return proc, job

    def embed(self, text, deadline=None):
        if deadline is None:
            return self._embed_cached(text)

        deadline.check()
        timeout_s = max(0.001, deadline.remaining_ms() / 1000.0)
        try:
            request = json.dumps(
                {"version": PROTOCOL_VERSION, "model": self.model, "text": text},
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise CaseRetrievalError(INVALID_INPUT, "embedding input invalid")
        if len(request) > WORKER_MAX_INPUT_BYTES:
            raise CaseRetrievalError(INVALID_INPUT, "embedding input too large")

        try:
            proc, job = self._spawn()
        except CaseRetrievalError:
            raise
        except Exception:
            raise CaseRetrievalError(MODEL_UNAVAILABLE, "worker spawn failed")
        self._job = job

        chunks = []
        state = {"total": 0, "over_limit": False, "done": False, "timed_out": False}

        def _writer():
            try:
                proc.stdin.write(request)
                proc.stdin.close()
            except Exception:
                pass

        def _reader():
            try:
                while True:
                    chunk = proc.stdout.read(WORKER_IO_CHUNK)
                    if not chunk:
                        break
                    if state["over_limit"]:
                        break
                    chunks.append(chunk)
                    state["total"] += len(chunk)
                    if state["total"] > WORKER_MAX_OUTPUT_BYTES:
                        state["over_limit"] = True
                        break
            except Exception:
                pass
            finally:
                state["done"] = True

        writer = threading.Thread(target=_writer, daemon=True)
        reader = threading.Thread(target=_reader, daemon=True)
        writer.start()
        reader.start()

        deadline_at = time.monotonic() + timeout_s
        captured = []
        all_dead = True
        try:
            while True:
                if state["done"] or state["over_limit"]:
                    break
                if time.monotonic() >= deadline_at:
                    state["timed_out"] = True
                    break
                time.sleep(0.005)
        finally:
            captured, all_dead = _cleanup_tree(proc, job)
            reader.join(timeout=2.0)
            writer.join(timeout=2.0)
            reader_alive = reader.is_alive()
            writer_alive = writer.is_alive()
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            if job:
                try:
                    _kernel32.CloseHandle(job)
                except Exception:
                    pass
            self._tree_pids = captured
            self._over_limit = state["over_limit"]

        out = b"".join(chunks)[:WORKER_MAX_OUTPUT_BYTES]
        return _finalize_decision(
            all_dead, reader_alive, writer_alive, state["timed_out"],
            state["over_limit"], proc.returncode, out,
        )


class DeterministicFakeProvider:
    """Offline deterministic provider for unit/E2E fixtures."""

    def __init__(self, model="fake", version="1.0.0"):
        self.model = model
        self.version = version

    def embed(self, text, deadline=None):
        if deadline is not None:
            deadline.check()
        digest = hashlib.sha512(text.encode("utf-8")).digest()
        values = []
        for index in range(EMBEDDING_DIM):
            offset = (index * 4) % len(digest)
            chunk = (digest + digest)[offset : offset + 4]
            number = struct.unpack("!f", chunk)[0]
            values.append(max(-1.0, min(1.0, number if math.isfinite(number) else 0.0)))
        return values
