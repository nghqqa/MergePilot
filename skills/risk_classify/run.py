"""risk-classify Skill entry point.

Reuses the M4-A common runtime (envelope/errors/cli) -- it does NOT reimplement
the envelope, error codes, redactor or CLI. See ``skills/diff_parse/run.py`` for
the dual invocation contract (direct entry vs. ``--skill`` via common CLI).
"""
from __future__ import annotations

import json
import os
import sys

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.common.runtime.cli import run_request

from . import core

SKILL_NAME = "risk-classify"
SKILL_VERSION = "1.0.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_INPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "input.schema.json")
_OUTPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "output.schema.json")
_INPUT_VALIDATOR = None
_OUTPUT_VALIDATOR = None


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

    change_context = inp["change_context"]
    risk_floor = inp.get("risk_floor", "L0")
    expected = inp.get("expected_rules_version")

    try:
        ruleset = core.load_rules(core.DEFAULT_RULES_PATH)
        result = core.classify(
            change_context, risk_floor=risk_floor,
            ruleset=ruleset, expected_rules_version=expected,
        )
    except core.RiskClassifyError as exc:
        raise errors.SkillError(exc.message, code=exc.code)

    # production-side validation of the business output against this Skill's own
    # output schema (the common envelope only checks the response shape).
    out_errors = sorted(_output_validator().iter_errors(result), key=lambda e: list(e.absolute_path))
    if out_errors:
        raise errors.SkillError(
            "risk-classify produced schema-invalid output",
            code=errors.INTERNAL_ERROR,
        )

    return {"status": "OK", "output": result}


def _safe_id(req, key, default):
    val = req.get(key) if isinstance(req, dict) else None
    return val if isinstance(val, str) and val else default


def _safe_finalize(env):
    """Apply credential redaction + 1 MiB size limit before serialization.

    Mirrors the common CLI's ``_finalize`` safety for the pre-execution error
    path built here in ``main()`` (which otherwise bypasses ``run_request``).
    Never raises: a finalize failure falls back to a minimal internal error.
    """
    try:
        from skills.common.runtime import redact
        env = redact.redact_envelope(env)
        env, _ = E.enforce_limits(env)
    except Exception:  # noqa: BLE001
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

    rid = _safe_id(req, "request_id", "req-unknown")
    tid = _safe_id(req, "trace_id", "trace-unknown")

    try:
        E.validate_request(req)
        env, code = run_request(
            req, handle, name=SKILL_NAME, version=SKILL_VERSION,
            timeout_ms=req.get("timeout_ms") if isinstance(req, dict) else None,
        )
    except errors.SkillError as exc:
        env = E.build_response(
            SKILL_NAME, SKILL_VERSION, rid, tid, "ERROR",
            error_code=exc.code, message=exc.message,
        )
        env = _safe_finalize(env)
        code = errors.cli_exit_code(env)

    sys.stdout.write(E.serialize(env) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
