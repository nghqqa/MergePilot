"""CaseRetrieval Skill entry point (M4-A common runtime)."""
from __future__ import annotations

import json
import os
import sys

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.common.runtime.cli import run_request

from . import core

SKILL_NAME = "case-retrieval"
SKILL_VERSION = "1.0.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_INPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "input.schema.json")
_OUTPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "output.schema.json")
_INPUT_VALIDATOR = None
_OUTPUT_VALIDATOR = None

_GENERIC = {
    core.INVALID_INPUT: errors.INVALID_INPUT,
    core.SCOPE_MISSING: errors.DENIED,
    core.SCHEMA_UNSUPPORTED: errors.INTERNAL_ERROR,
    core.DB_UNAVAILABLE: errors.DEPENDENCY_UNAVAILABLE,
    core.MODEL_UNAVAILABLE: errors.DEPENDENCY_UNAVAILABLE,
    core.TIMEOUT_SUB: errors.TIMEOUT,
    core.DIMENSION_MISMATCH: errors.INTERNAL_ERROR,
    core.VERSION_MISMATCH: errors.INVALID_INPUT,
    core.CITATION_INVALID: errors.INTERNAL_ERROR,
    core.INTERNAL: errors.INTERNAL_ERROR,
}


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
        result = core.run(inp, deadline=ctx.get("deadline"))
    except core.CaseRetrievalError as exc:
        code = _GENERIC.get(exc.subcode, errors.INTERNAL_ERROR)
        # Only the stable subcode is public. Internal detail may contain
        # dependency/configuration data and must never enter the envelope.
        raise errors.SkillError(exc.subcode, code=code)
    out_errs = sorted(_output_validator().iter_errors(result), key=lambda e: list(e.absolute_path))
    if out_errs:
        raise errors.SkillError("case-retrieval produced schema-invalid output", code=errors.INTERNAL_ERROR)
    return {"status": "OK", "output": result}


def _safe_id(req, key, default):
    val = req.get(key) if isinstance(req, dict) else None
    return val if isinstance(val, str) and val else default


def _safe_finalize(env):
    try:
        from skills.common.runtime import redact
        env = redact.redact_envelope(env)
        env, _ = E.enforce_limits(env)
    except Exception:
        env = E.build_response(SKILL_NAME, SKILL_VERSION, "req-unknown", "trace-unknown", "ERROR",
                               error_code=errors.INTERNAL_ERROR, message="finalize failed")
    return env


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
        env, code = run_request(req, handle, name=SKILL_NAME, version=SKILL_VERSION,
                                timeout_ms=req.get("timeout_ms") if isinstance(req, dict) else None)
    except errors.SkillError as exc:
        env = E.build_response(SKILL_NAME, SKILL_VERSION, rid, tid, "ERROR",
                               error_code=exc.code, message=exc.message)
        env = _safe_finalize(env)
        code = errors.cli_exit_code(env)
    sys.stdout.write(E.serialize(env) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
