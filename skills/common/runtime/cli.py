"""Common Skill CLI runner.

Reads a request envelope from stdin (or ``--request-file``), runs a Skill
callable, wraps the result in a response envelope, applies credential
redaction and output-size limits, and prints EXACTLY one JSON envelope to
stdout.

Output isolation guarantees (fd-level + Python-stream-level):
  * While a Skill (or its module import) runs, real fds 1 and 2 are redirected
    to temp files (covers Python ``print``, ``os.write``, subprocess inherit,
    and import-time output). Captured stdout/stderr are redacted and routed to
    the real stderr; they never reach real stdout.
  * The Python ``sys.stdout``/``sys.stderr`` objects are saved and restored, so
    a Skill that reassigns or closes them cannot corrupt envelope emission.
  * The envelope is emitted via a stable duplicated real-stdout fd captured at
    module load, so emission succeeds even if the Skill closed fd 1 / sys.stdout.

Process-exit guarantees:
  * A Skill (or its import) that raises ``SystemExit`` is converted to an
    INTERNAL_ERROR envelope rather than terminating the process without output.
  * argparse errors become INVALID_INPUT envelopes; only ``--help`` exits normally.

Correlation-ID safety: the request is validated before the Skill is resolved,
so every pre-execution failure yields a schema-valid error envelope.
Serialization: ``_finalize`` performs a real ``json.dumps``; non-serializable
Skill output becomes INTERNAL_ERROR (no ``repr`` masking).

CLI exit codes: 0 OK/PARTIAL | 10 verdict=FAIL | 2 INVALID_INPUT/SCHEMA_VERSION_UNSUPPORTED
| 3 TIMEOUT | 4 DENIED | 5 DEPENDENCY_UNAVAILABLE | 1 other ERROR.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import tempfile
import time

from . import envelope as E
from . import errors
from .redact import redact_envelope, redact_value

SKILL_NAME = "common-runner"
SKILL_VERSION = "1.0.0"
DEFAULT_TIMEOUT_MS = 60000
_UNKNOWN_RID = "req-unknown"
_UNKNOWN_TID = "trace-unknown"
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

# Stable references to the original process stdout/stderr, captured at module
# load (before any redirection). Emission uses these so a Skill that closes or
# reassigns sys.stdout / fd 1 cannot suppress the envelope.
try:
    _REAL_STDOUT_FD = os.dup(1)
except Exception:  # noqa: BLE001
    _REAL_STDOUT_FD = None
try:
    _REAL_STDERR_FD = os.dup(2)
except Exception:  # noqa: BLE001
    _REAL_STDERR_FD = None


def _write_all(fd, data):
    """Write all bytes to fd (looping over partial writes). Return True on success."""
    if fd is None:
        return False
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except OSError:
            return False
        if n <= 0:
            return False
        view = view[n:]
    return True


class Deadline:
    """Cooperative deadline. The runner checks it before/after the Skill."""

    def __init__(self, timeout_ms):
        seconds = max(0, int(timeout_ms)) / 1000.0
        self._deadline = time.monotonic() + seconds

    def remaining_ms(self):
        return max(0, int((self._deadline - time.monotonic()) * 1000))

    def expired(self):
        return time.monotonic() >= self._deadline

    def check(self):
        if self.expired():
            raise errors.SkillTimeout("deadline exceeded")


class _FdCapture:
    """Redirect real fds 1/2 to temp files AND save/restore sys.stdout/stderr.

    Covers Python writes, ``os.write``, subprocess inheritance and import-time
    output. On exit, fds 1/2 are restored, Python stream objects are restored,
    and captured text is stored on ``.stdout``/``.stderr``.
    """

    def __init__(self):
        self.stdout = ""
        self.stderr = ""
        self._real = ()
        self._tmp_out = None
        self._tmp_err = None
        self._saved_py_stdout = None
        self._saved_py_stderr = None

    def __enter__(self):
        self._saved_py_stdout = sys.stdout
        self._saved_py_stderr = sys.stderr
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass
        self._real = (os.dup(1), os.dup(2))
        self._tmp_out = tempfile.TemporaryFile()
        self._tmp_err = tempfile.TemporaryFile()
        os.dup2(self._tmp_out.fileno(), 1)
        os.dup2(self._tmp_err.fileno(), 2)
        return self

    def __exit__(self, *exc):
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass
        # restore real fds 1/2
        if len(self._real) == 2:
            try:
                os.dup2(self._real[0], 1)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.dup2(self._real[1], 2)
            except Exception:  # noqa: BLE001
                pass
            for fd in self._real:
                try:
                    os.close(fd)
                except Exception:  # noqa: BLE001
                    pass
        # restore Python stream objects (handles Skill reassignment/closure)
        sys.stdout = self._saved_py_stdout
        sys.stderr = self._saved_py_stderr
        # read captured text
        for tmp, attr in ((self._tmp_out, "stdout"), (self._tmp_err, "stderr")):
            try:
                tmp.seek(0)
                setattr(self, attr, tmp.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                setattr(self, attr, "")
            try:
                tmp.close()
            except Exception:  # noqa: BLE001
                pass
        return False


# --------------------------------------------------------------------------- #
# Built-in fixture skills (tests only). Real Skills use ``--skill module.func``.
# --------------------------------------------------------------------------- #
def _b_echo(ctx):
    return {"status": "OK", "output": dict(ctx.get("input") or {})}


def _b_ok(ctx):
    return {"status": "OK", "output": {"verdict": "PASS"}}


def _b_verdict_fail(ctx):
    return {"status": "OK", "output": {"verdict": "FAIL", "reason": "forced business failure"}}


def _b_partial(ctx):
    return {
        "status": "PARTIAL",
        "warning_codes": ["SUB_ENGINE_TIMEOUT"],
        "degradations": [{"what": "sub_engine", "reason": "timeout", "fallback": "regex"}],
        "output": {"done": 1, "total": 2},
    }


def _b_timeout(ctx):
    raise errors.SkillTimeout("forced whole-execution timeout")


def _b_denied(ctx):
    raise errors.SkillDenied("forced policy denial")


def _b_dep(ctx):
    raise errors.DependencyUnavailable("forced dependency unavailable")


def _b_invalid(ctx):
    raise errors.InvalidInput("forced invalid input")


def _b_boom(ctx):
    raise RuntimeError("forced uncaught exception")


def _b_slow(ctx):
    ms = int((ctx.get("input") or {}).get("sleep_ms", 50))
    time.sleep(ms / 1000.0)
    return {"status": "OK", "output": {"slept_ms": ms}}


def _b_fdwrite(ctx):
    os.write(1, b"RAW_FD_STDOUT_LEAK\n")
    return {"status": "OK", "output": {}}


def _b_stderrleak(ctx):
    probe = "ghp_" + "a" * 36
    sys.stderr.write("token " + probe + "\n")
    return {"status": "OK", "output": {}}


def _b_reassign_stdout(ctx):
    import io as _io
    sys.stdout = _io.StringIO()
    print("LEAK_VIA_REASSIGNED_STDOUT")
    return {"status": "OK", "output": {}}


def _b_close_stdout(ctx):
    sys.stdout.close()
    return {"status": "OK", "output": {}}


def _b_subprocess(ctx):
    import subprocess as _sp
    _sp.run([sys.executable, "-c",
             "import sys; sys.stdout.write('SUBPROC_OUT'); sys.stderr.write('SUBPROC_ERR')"],
            check=False)
    return {"status": "OK", "output": {}}


_BUILTINS = {
    "echo": _b_echo, "ok": _b_ok, "verdict_fail": _b_verdict_fail, "partial": _b_partial,
    "timeout": _b_timeout, "denied": _b_denied, "dep": _b_dep, "invalid": _b_invalid,
    "boom": _b_boom, "slow": _b_slow, "fdwrite": _b_fdwrite, "stderrleak": _b_stderrleak,
    "reassign_stdout": _b_reassign_stdout, "close_stdout": _b_close_stdout, "subprocess": _b_subprocess,
}


def _resolve_skill(args):
    if args.builtin:
        fn = _BUILTINS.get(args.builtin)
        if fn is None:
            raise errors.InvalidInput("unknown builtin: %s" % args.builtin)
        return fn
    if args.skill:
        module_path, _, func = args.skill.rpartition(".")
        if not module_path or not func:
            raise errors.InvalidInput("--skill expects 'module.func'")
        import importlib
        try:
            module = importlib.import_module(module_path)
            obj = getattr(module, func)
        except (ImportError, AttributeError) as exc:
            raise errors.InvalidInput("cannot resolve skill %r: %s" % (args.skill, exc))
        if not callable(obj):
            raise errors.InvalidInput("skill target not callable: %s" % args.skill)
        return obj
    return _b_echo


def _read_request(args):
    if args.request_file:
        with open(args.request_file, encoding="utf-8") as fh:
            return json.loads(fh.read())
    raw = sys.stdin.read()
    if not raw.strip():
        raise errors.InvalidInput("empty request (no stdin and no --request-file)")
    return json.loads(raw)


def _resolve_timeout(timeout_ms, req):
    if timeout_ms is not None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise errors.InvalidInput("timeout_ms override must be an integer")
        if timeout_ms < 1:
            raise errors.InvalidInput("timeout_ms must be >= 1")
        return timeout_ms
    rt = req.get("timeout_ms") if isinstance(req, dict) else None
    return rt or DEFAULT_TIMEOUT_MS


def _safe_str(value, default):
    return value if isinstance(value, str) and value else default


def _safe_version(value):
    return value if isinstance(value, str) and _SEMVER_RE.match(value) else SKILL_VERSION


def _rebuild_internal(env, reason):
    """Minimal, schema-valid INTERNAL_ERROR envelope (sanitizes all metadata)."""
    return E.build_response(
        _safe_str(env.get("name"), SKILL_NAME), _safe_version(env.get("version")),
        _safe_str(env.get("request_id"), _UNKNOWN_RID),
        _safe_str(env.get("trace_id"), _UNKNOWN_TID),
        "ERROR", error_code=errors.INTERNAL_ERROR, message=_safe_str(reason, "internal error"),
    )


def _result_to_envelope(result, name, version, request_id, trace_id, started_iso, started_monotonic):
    result = result or {}
    status = result.get("status", "OK")
    return E.build_response(
        name, version, request_id, trace_id, status,
        output=result.get("output"),
        error_code=result.get("error_code"),
        warning_codes=result.get("warning_codes"),
        degradations=result.get("degradations"),
        message=result.get("message", ""),
        evidence=result.get("evidence"),
        artifacts=result.get("artifacts"),
        retryable=result.get("retryable", False),
        side_effects=result.get("side_effects"),
        started_at=started_iso,
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
    )


def _truncate_text(text, limit=2000):
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def _write_stream(target_fd, fallback_stream, text):
    data = text.encode("utf-8", "replace")
    if not _write_all(target_fd, data):
        try:
            fallback_stream.write(text)
            fallback_stream.flush()
        except Exception:  # noqa: BLE001
            pass


def _route_captured(stdout_text, stderr_text):
    """Redact captured Skill stdout/stderr and write to the REAL stderr."""
    parts = []
    if stdout_text:
        cleaned, _ = redact_value(stdout_text)
        parts.append("[skill_stdout_isolated] " + _truncate_text(cleaned))
    if stderr_text:
        cleaned, _ = redact_value(stderr_text)
        parts.append("[skill_stderr_isolated] " + _truncate_text(cleaned))
    if parts:
        _write_stream(_REAL_STDERR_FD, sys.stderr, "\n".join(parts) + "\n")


def _finalize(env):
    """Conditions -> redact -> limits -> schema-validate -> REAL serialize."""
    try:
        E.check_conditions(env)
    except errors.SkillError as exc:
        env = _rebuild_internal(env, "envelope condition violated: %s" % exc.message)
    try:
        env = redact_envelope(env)
        env, _truncated = E.enforce_limits(env)
        E.validate_response(env)
        E.serialize(env)  # explicit JSON-serializability check (no repr masking)
    except errors.SkillError:
        env = _rebuild_internal(env, "response schema validation failed")
    except Exception:  # noqa: BLE001
        env = _rebuild_internal(env, "response not JSON-serializable")
    return env


def run_request(req, skill_fn, *, name=SKILL_NAME, version=SKILL_VERSION, timeout_ms=None):
    """Run a validated request through ``skill_fn``. Returns ``(envelope, exit_code)``."""
    started_monotonic = time.monotonic()
    started_iso = E._now_iso()
    dur = lambda: int((time.monotonic() - started_monotonic) * 1000)

    try:
        E.validate_request(req)
    except errors.SkillError as exc:
        env = E.build_response(name, version, _UNKNOWN_RID, _UNKNOWN_TID, "ERROR",
                               error_code=exc.code, message=exc.message,
                               started_at=started_iso, duration_ms=dur())
        env = _finalize(env)
        return env, errors.cli_exit_code(env)

    request_id = req["request_id"]
    trace_id = req["trace_id"]

    # M6-A: OTel skill span (fail-closed if otel module not available)
    _otel_ctx = None
    _otel_span_obj = None
    try:
        import sys as _otel_sys
        _otel_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "tools", "otel")
        if _otel_dir not in _otel_sys.path:
            _otel_sys.path.insert(0, _otel_dir)
        import otel_spans as _otel_mod
        # PoC(Phase7.1): optional worker-provided parent context via env
        # (fail-closed; malformed value -> None, span stays hop-root).
        _mp_parent_span_id = None
        _tp = os.environ.get("MP_TRACEPARENT", "")
        if _tp:
            _pc = _otel_mod.from_traceparent(_tp)
            if _pc is not None:
                _mp_parent_span_id = _pc.span_id
        _otel_ctx = _otel_mod.skill_span(
            run_id="", trace_id=trace_id,
            skill_name=name, skill_version=version,
            request_id=request_id, agent_role="skill",
            parent_span_id=_mp_parent_span_id)
        _otel_span_obj = _otel_ctx.__enter__()
    except Exception:
        _otel_ctx = None  # ensure no double-enter

    # M6-A: outer try/finally ensures OTel span is exited on ALL return paths
    try:
        try:
            effective_timeout = _resolve_timeout(timeout_ms, req)
        except errors.SkillError as exc:
            env = E.build_response(name, version, request_id, trace_id, "ERROR",
                                   error_code=exc.code, message=exc.message,
                                   started_at=started_iso, duration_ms=dur())
            env = _finalize(env)
            return env, errors.cli_exit_code(env)

        deadline = Deadline(effective_timeout)
        ctx = {"request_id": request_id, "trace_id": trace_id, "deadline": deadline,
               "input": req.get("input") or {}}

        cap = _FdCapture()
        try:
            with cap:
                deadline.check()
                result = skill_fn(ctx)
                deadline.check()
            env = _result_to_envelope(result, name, version, request_id, trace_id, started_iso, started_monotonic)
        except errors.SkillError as exc:
            env = E.build_response(name, version, request_id, trace_id, "ERROR",
                                   error_code=exc.code, message=exc.message,
                                   started_at=started_iso, duration_ms=dur())
        except SystemExit:
            env = E.build_response(name, version, request_id, trace_id, "ERROR",
                                   error_code=errors.INTERNAL_ERROR, message="skill raised SystemExit",
                                   started_at=started_iso, duration_ms=dur())
        except Exception as exc:  # noqa: BLE001
            env = E.build_response(name, version, request_id, trace_id, "ERROR",
                                   error_code=errors.INTERNAL_ERROR,
                                   message="internal error: %s" % type(exc).__name__,
                                   started_at=started_iso, duration_ms=dur())

        if cap.stdout or cap.stderr:
            _route_captured(cap.stdout, cap.stderr)
            ev = list(env.get("evidence") or [])
            if cap.stdout:
                ev.append({"kind": "skill_stdout_digest", "ref": "sha256:" + E.sha256_hex(cap.stdout.encode("utf-8", "replace"))})
            if cap.stderr:
                ev.append({"kind": "skill_stderr_digest", "ref": "sha256:" + E.sha256_hex(cap.stderr.encode("utf-8", "replace"))})
            env["evidence"] = ev

        env = _finalize(env)
        return env, errors.cli_exit_code(env)
    finally:
        # M6-A: exit OTel skill span on ALL return paths (fail-closed)
        if _otel_ctx is not None:
            try:
                # If the skill produced an ERROR response, mark the span ERROR
                if 'env' in dir() and isinstance(env, dict) and env.get('status') == 'ERROR':
                    if _otel_span_obj is not None:
                        _otel_span_obj.set_status('ERROR')
                _otel_ctx.__exit__(None, None, None)
            except Exception:
                pass


def _emit(env):
    """Emit the envelope via the stable real-stdout fd (immune to sys.stdout corruption)."""
    data = (E.serialize(env) + "\n").encode("utf-8", "replace")
    if not _write_all(_REAL_STDOUT_FD, data):
        try:
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass


def _emit_finalized(env):
    """Route an envelope through ``_finalize`` (redact + 1 MiB limit + schema
    check) and then emit it. Every pre-execution / error emission MUST go
    through this so credential-shaped content in a malformed request envelope
    can never reach stdout unredacted. Returns the CLI exit code."""
    env = _finalize(env)
    _emit(env)
    return errors.cli_exit_code(env)


def _emit_error(error_code, message):
    env = E.build_response(SKILL_NAME, SKILL_VERSION, _UNKNOWN_RID, _UNKNOWN_TID, "ERROR",
                           error_code=error_code, message=message)
    return _emit_finalized(env)


def _build_parser():
    parser = argparse.ArgumentParser(prog="mergepilot-skill", description="MergePilot common Skill runner")
    parser.add_argument("--request-file", help="read request envelope from this JSON file (else stdin)")
    parser.add_argument("--builtin", help="run a built-in fixture skill (tests only)")
    parser.add_argument("--skill", help="dotted path 'pkg.mod.func' to a real Skill run callable")
    parser.add_argument("--timeout-ms", type=int, default=None, help="override whole-execution timeout (ms, >=1)")
    return parser


def _read_and_validate(args):
    """Read+validate request; return (req, None, (rid, tid)) or (None, exit_code, None)."""
    try:
        req = _read_request(args)
    except errors.SkillError as exc:
        env = E.build_response(SKILL_NAME, SKILL_VERSION, _UNKNOWN_RID, _UNKNOWN_TID, "ERROR",
                               error_code=exc.code, message=exc.message)
        return None, _emit_finalized(env), None
    except Exception as exc:  # noqa: BLE001
        return None, _emit_error(errors.INVALID_INPUT, "cannot read request: %s" % type(exc).__name__), None
    try:
        E.validate_request(req)
    except errors.SkillError as exc:
        env = E.build_response(SKILL_NAME, SKILL_VERSION, _UNKNOWN_RID, _UNKNOWN_TID, "ERROR",
                               error_code=exc.code, message=exc.message)
        return None, _emit_finalized(env), None
    return req, None, (req["request_id"], req["trace_id"])


def _resolve_isolated(args, request_id, trace_id):
    """Resolve the Skill under fd isolation. Returns (skill_fn, exit_code_on_error)."""
    cap = _FdCapture()
    try:
        with cap:
            skill_fn = _resolve_skill(args)
    except errors.SkillError as exc:
        err = exc
    except SystemExit:
        err = errors.SkillError("skill module raised SystemExit", errors.INTERNAL_ERROR)
    else:
        err = None
    if cap.stdout or cap.stderr:
        _route_captured(cap.stdout, cap.stderr)
    if err is not None:
        env = E.build_response(SKILL_NAME, SKILL_VERSION, request_id, trace_id, "ERROR",
                               error_code=err.code, message=err.message)
        return None, _emit_finalized(env)
    return skill_fn, None


def _main_run(args):
    if args.timeout_ms is not None and args.timeout_ms < 1:
        return _emit_error(errors.INVALID_INPUT, "--timeout-ms must be >= 1")

    req, early_code, ids = _read_and_validate(args)
    if req is None:
        return early_code
    request_id, trace_id = ids

    skill_fn, resolve_code = _resolve_isolated(args, request_id, trace_id)
    if skill_fn is None:
        return resolve_code

    env, code = run_request(req, skill_fn, timeout_ms=args.timeout_ms)
    _emit(env)
    return code


def main(argv=None):
    parser = _build_parser()
    try:
        try:
            args = parser.parse_args(argv)
        except SystemExit as se:
            if se.code == 0:
                raise  # --help
            return _emit_error(errors.INVALID_INPUT, "invalid CLI arguments")
        return _main_run(args)
    except SystemExit as se:
        if se.code == 0:
            raise
        return _emit_error(errors.INTERNAL_ERROR, "unexpected SystemExit")
    except Exception as exc:  # noqa: BLE001
        try:
            return _emit_error(errors.INTERNAL_ERROR, "fatal: %s" % type(exc).__name__)
        except Exception:  # noqa: BLE001
            return 1


if __name__ == "__main__":
    sys.exit(main())
