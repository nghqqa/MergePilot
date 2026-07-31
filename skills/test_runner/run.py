"""test-runner Skill entry point (reuses the M4-A common runtime).

verdict FAIL is a business failure (status=OK, exit 10). TIMEOUT and ERROR are
runtime errors (status=ERROR) mapped to GENERIC codes; the structured result is
still carried in ``output`` (incl. timeout summary). Skill subcodes ride in
``message``; no change to skills/common is required.
"""
from __future__ import annotations

import json
import os
import sys

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.common.runtime.cli import run_request

from . import core

SKILL_NAME = "test-runner"
SKILL_VERSION = "1.0.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_INPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "input.schema.json")
_OUTPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "output.schema.json")
_INPUT_VALIDATOR = None
_OUTPUT_VALIDATOR = None

# pre-run TestRunnerError subcode -> GENERIC error_code
_GENERIC = {
    core.INPUT_INVALID: errors.INVALID_INPUT,
    core.INVALID_COMMAND: errors.INVALID_INPUT,
    core.PATH_ESCAPE: errors.INVALID_INPUT,
    core.NO_TRUSTED_EXECUTOR: errors.DENIED,
    core.NETWORK_DENIED: errors.DENIED,
    core.TRUSTED_CONFIG_MISSING: errors.DENIED,
    core.EXEC_UNAVAILABLE: errors.DEPENDENCY_UNAVAILABLE,
    core.CONTAINER_UNAVAILABLE: errors.DEPENDENCY_UNAVAILABLE,
    core.TIMEOUT_SUB: errors.TIMEOUT,
    core.INTERNAL: errors.INTERNAL_ERROR,
}

_DEP_SUBCODES = {core.EXEC_UNAVAILABLE, core.CONTAINER_UNAVAILABLE}


def _input_validator():
    global _INPUT_VALIDATOR
    if _INPUT_VALIDATOR is None:
        import jsonschema
        with open(_INPUT_SCHEMA_PATH, encoding="utf-8") as fh:
            _INPUT_VALIDATOR = jsonschema.Draft202012Validator(json.load(fh))
    return _INPUT_VALIDATOR


def _output_validator():
    global _OUTPUT_VALIDATOR
    if _OUTPUT_VALIDATOR is None:
        import jsonschema
        with open(_OUTPUT_SCHEMA_PATH, encoding="utf-8") as fh:
            _OUTPUT_VALIDATOR = jsonschema.Draft202012Validator(json.load(fh))
    return _OUTPUT_VALIDATOR


def handle(ctx):
    inp = (ctx.get("input") if isinstance(ctx, dict) else None) or {}
    if not isinstance(inp, dict):
        raise errors.InvalidInput("input must be an object")

    errs = sorted(_input_validator().iter_errors(inp), key=lambda e: list(e.absolute_path))
    if errs:
        path = "/".join(str(p) for p in errs[0].absolute_path) or "<root>"
        raise errors.InvalidInput("%s: %s" % (path, errs[0].message))

    try:
        output = core.run(inp, expected_profiles_version=inp.get("expected_profiles_version"),
                          deadline=ctx.get("deadline"))
    except core.TestRunnerError as exc:
        code = _GENERIC.get(exc.subcode, errors.INTERNAL_ERROR)
        raise errors.SkillError(exc.subcode + (": " + exc.detail if exc.detail else ""), code=code)

    runtime_error = output.pop("_runtime_error", None)
    executed = output.pop("_executed", False)
    out_errs = sorted(_output_validator().iter_errors(output), key=lambda e: list(e.absolute_path))
    if out_errs:
        raise errors.SkillError("test-runner produced schema-invalid output", code=errors.INTERNAL_ERROR)

    # accurate side_effects: sandbox copy (fs_tmp) always on the run path;
    # process_exec only when the executor actually launched.
    side_effects = [{"type": "fs_tmp", "target": "sandbox copy", "declared": True}]
    if executed:
        side_effects.append({"type": "process_exec", "target": "test runner subprocess/container", "declared": True})

    verdict = output["verdict"]
    if verdict in (core.PASS, core.FAIL):
        return {"status": "OK", "output": output, "side_effects": side_effects}
    if verdict == core.TIMEOUT:
        return {"status": "ERROR", "error_code": errors.TIMEOUT, "output": output,
                "message": core.TIMEOUT_SUB, "side_effects": side_effects}
    # verdict ERROR
    code = errors.DEPENDENCY_UNAVAILABLE if runtime_error in _DEP_SUBCODES else errors.INTERNAL_ERROR
    return {"status": "ERROR", "error_code": code, "output": output,
            "message": runtime_error or core.INTERNAL, "side_effects": side_effects}


def _safe_id(req, key, default):
    val = req.get(key) if isinstance(req, dict) else None
    return val if isinstance(val, str) and val else default


def _safe_finalize(env_):
    try:
        from skills.common.runtime import redact
        env_ = redact.redact_envelope(env_)
        env_, _ = E.enforce_limits(env_)
    except Exception:  # noqa: BLE001
        env_ = E.build_response(SKILL_NAME, SKILL_VERSION, "req-unknown", "trace-unknown", "ERROR",
                                error_code=errors.INTERNAL_ERROR, message="finalize failed")
    return env_


def main(argv=None):
    raw = sys.stdin.read()
    try:
        req = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        req = {}
    rid = _safe_id(req, "request_id", "req-unknown")
    tid = _safe_id(req, "trace_id", "trace-unknown")
    try:
        E.validate_request(req)
        env_, code = run_request(req, handle, name=SKILL_NAME, version=SKILL_VERSION,
                                 timeout_ms=req.get("timeout_ms") if isinstance(req, dict) else None)
    except errors.SkillError as exc:
        env_ = E.build_response(SKILL_NAME, SKILL_VERSION, rid, tid, "ERROR",
                                error_code=exc.code, message=exc.message)
        env_ = _safe_finalize(env_)
        code = errors.cli_exit_code(env_)
    sys.stdout.write(E.serialize(env_) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
