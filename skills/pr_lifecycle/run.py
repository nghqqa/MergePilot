"""PRLifecycle Skill entry point (M4-A common runtime)."""
from __future__ import annotations

import json
import os
import sys

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.common.runtime.cli import run_request

from . import core


SKILL_NAME = "pr-lifecycle"
SKILL_VERSION = "1.0.0"
_HERE = os.path.dirname(os.path.abspath(__file__))
_INPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "input.schema.json")
_OUTPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "output.schema.json")
_INPUT_VALIDATOR = None
_OUTPUT_VALIDATOR = None
_ADAPTER_FACTORY = None

_GENERIC = {
    core.INVALID_INPUT: errors.INVALID_INPUT,
    core.LIMIT_EXCEEDED: errors.INVALID_INPUT,
    core.TRUSTED_CONFIG_MISSING: errors.DENIED,
    core.ROLE_ACTION_DENIED: errors.DENIED,
    core.POLICY_DENIED: errors.DENIED,
    core.IDEMPOTENCY_CONFLICT: errors.DENIED,
    core.REVERT_DELETE_UNSUPPORTED: errors.DENIED,
    core.REVERT_STATE_MISMATCH: errors.DENIED,
    core.GATEWAY_UNAVAILABLE: errors.DEPENDENCY_UNAVAILABLE,
    core.EFFECT_UNKNOWN: errors.DEPENDENCY_UNAVAILABLE,
    core.DEADLINE_EXCEEDED: errors.TIMEOUT,
    core.INTERNAL: errors.INTERNAL_ERROR,
    core.OUTPUT_SCHEMA_INVALID: errors.INTERNAL_ERROR,
}


def _validator(path, which):
    global _INPUT_VALIDATOR, _OUTPUT_VALIDATOR
    current = _INPUT_VALIDATOR if which == "input" else _OUTPUT_VALIDATOR
    if current is None:
        import jsonschema
        with open(path, encoding="utf-8") as fh:
            current = jsonschema.Draft202012Validator(json.load(fh))
        if which == "input":
            _INPUT_VALIDATOR = current
        else:
            _OUTPUT_VALIDATOR = current
    return current


def _schema_error(validator, value):
    errs = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if not errs:
        return None
    path = "/".join(str(part) for part in errs[0].absolute_path) or "<root>"
    return "%s: %s" % (path, errs[0].message)


def handle(ctx):
    inp = (ctx.get("input") if isinstance(ctx, dict) else None) or {}
    if not isinstance(inp, dict):
        raise errors.InvalidInput("input must be an object")
    problem = _schema_error(_validator(_INPUT_SCHEMA_PATH, "input"), inp)
    if problem:
        raise errors.InvalidInput(problem)

    adapter = _ADAPTER_FACTORY() if _ADAPTER_FACTORY is not None else None
    try:
        output = core.run(inp, adapter=adapter, deadline=ctx.get("deadline"))
    except core.PRLifecycleError as exc:
        result = {
            "status": "ERROR",
            "error_code": _GENERIC.get(exc.subcode, errors.INTERNAL_ERROR),
            "message": exc.subcode,
            "retryable": exc.retryable,
            "side_effects": exc.effects,
        }
        if exc.output:
            problem = _schema_error(_validator(_OUTPUT_SCHEMA_PATH, "output"), exc.output)
            if problem:
                return {
                    "status": "ERROR",
                    "error_code": errors.INTERNAL_ERROR,
                    "message": core.OUTPUT_SCHEMA_INVALID,
                    "side_effects": exc.effects,
                }
            result["output"] = exc.output
        return result

    side_effects = output.pop("_side_effects", [])
    problem = _schema_error(_validator(_OUTPUT_SCHEMA_PATH, "output"), output)
    if problem:
        return {
            "status": "ERROR",
            "error_code": errors.INTERNAL_ERROR,
            "message": core.OUTPUT_SCHEMA_INVALID,
            "side_effects": side_effects,
        }
    return {"status": "OK", "output": output, "side_effects": side_effects}


def _safe_id(req, key, default):
    value = req.get(key) if isinstance(req, dict) else None
    return value if isinstance(value, str) and value else default


def _safe_finalize(env):
    try:
        from skills.common.runtime import redact
        env = redact.redact_envelope(env)
        env, _ = E.enforce_limits(env)
        E.validate_response(env)
        E.serialize(env)
    except Exception:
        env = E.build_response(
            SKILL_NAME, SKILL_VERSION, "req-unknown", "trace-unknown", "ERROR",
            error_code=errors.INTERNAL_ERROR, message="finalize failed",
        )
    return env


def main(argv=None):
    raw = sys.stdin.read()
    try:
        req = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        req = {}
    request_id = _safe_id(req, "request_id", "req-unknown")
    trace_id = _safe_id(req, "trace_id", "trace-unknown")
    try:
        E.validate_request(req)
        env, code = run_request(
            req,
            handle,
            name=SKILL_NAME,
            version=SKILL_VERSION,
            timeout_ms=req.get("timeout_ms") if isinstance(req, dict) else None,
        )
    except errors.SkillError as exc:
        env = E.build_response(
            SKILL_NAME, SKILL_VERSION, request_id, trace_id, "ERROR",
            error_code=exc.code, message=exc.message,
        )
        env = _safe_finalize(env)
        code = errors.cli_exit_code(env)
    sys.stdout.write(E.serialize(env) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
