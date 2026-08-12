"""sast-scan Skill entry point (reuses the M4-A common runtime).

Error semantics reuse GENERIC error_codes so the common runtime maps exit codes
without any change to skills/common (skill subcodes ride in ``message``).
``trusted_workspace`` for the ``paths`` mode comes from the deploy-owned env
``MERGEPILOT_SAST_WORKSPACE`` -- never from the request envelope.
"""
from __future__ import annotations

import json
import os
import sys

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.common.runtime.cli import run_request

from . import core

SKILL_NAME = "sast-scan"
SKILL_VERSION = "1.0.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_INPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "input.schema.json")
_OUTPUT_SCHEMA_PATH = os.path.join(_HERE, "schema", "output.schema.json")
_INPUT_VALIDATOR = None
_OUTPUT_VALIDATOR = None

# subcode -> GENERIC error_code (common runtime maps the exit code)
_GENERIC = {
    core.INPUT_INVALID: errors.INVALID_INPUT,
    core.INPUT_TOO_LARGE: errors.INVALID_INPUT,
    core.PATH_ESCAPE: errors.INVALID_INPUT,
    core.RULESET_INVALID: errors.INTERNAL_ERROR,
    core.RULESET_VERSION_UNSUPPORTED: errors.INTERNAL_ERROR,
    core.ENGINE_FAILED: errors.INTERNAL_ERROR,
    core.TRUSTED_CONFIG_MISSING: errors.DENIED,
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

    trusted_workspace = os.environ.get("MERGEPILOT_SAST_WORKSPACE") or None

    # M6-RAG: Reviewer advisory retrieval (fail-closed, never blocks scan)
    # RAG provenance goes into the handler return's evidence[] list, which
    # _result_to_envelope passes through to the final response envelope's
    # evidence[] field (allowed by the common envelope schema).
    rag_status = "disabled"
    rag_fallback_reason = ""
    rag_hit_count = 0
    rag_case_items = []
    try:
        _rag_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "tools", "rag")
        if _rag_dir not in sys.path:
            sys.path.insert(0, _rag_dir)
        from rag_retrieval_service import query_for_reviewer, create_adapter_from_env
        trace_id = ctx.get("trace_id", "")
        run_id = os.environ.get("MERGEPILOT_RUN_ID", "")
        files = inp.get("files", [])
        query_text = " ".join(
            f.get("path", "").rsplit("/", 1)[-1] for f in files[:10]
        )[:200]
        if query_text:
            adapter = create_adapter_from_env()
            resp = query_for_reviewer(query_text, run_id, trace_id,
                                      adapter=adapter, timeout_ms=3000)
            rag_status = resp.status
            rag_fallback_reason = resp.fallback_reason
            rag_hit_count = resp.hit_count
            rag_case_items = [
                {"case_id": r.case_id, "similarity": r.similarity,
                 "citation_url": r.citation_url, "adopted": r.adopted,
                 "untrusted": r.untrusted}
                for r in resp.results
            ]
    except Exception:
        rag_status = "retrieval_unavailable"
        rag_fallback_reason = "exception"

    # Always produce at least one rag_advisory summary evidence item
    rag_evidence_items = [{
        "kind": "rag_advisory",
        "ref": json.dumps({
            "status": rag_status,
            "fallback_reason": rag_fallback_reason,
            "hit_count": rag_hit_count,
            "adopted": False,
            "untrusted": True,
            "cases": rag_case_items,
        })
    }]

    try:
        result = core.scan(
            inp,
            trusted_workspace=trusted_workspace,
            expected_rules_version=inp.get("expected_rules_version"),
            deadline=ctx.get("deadline"),
        )
    except core.SASTScanError as exc:
        code = _GENERIC.get(exc.subcode, errors.INTERNAL_ERROR)
        msg = exc.subcode + (": " + exc.detail if exc.detail else "")
        raise errors.SkillError(msg, code=code)

    out_errs = sorted(_output_validator().iter_errors(result), key=lambda e: list(e.absolute_path))
    if out_errs:
        raise errors.SkillError("sast-scan produced schema-invalid output", code=errors.INTERNAL_ERROR)

    if result["complete"]:
        return {"status": "OK", "output": result,
                "evidence": rag_evidence_items}

    degradations = [{"engine": d.get("engine", "core"), "reason": d.get("reason", "")} for d in result.get("degraded", [])]
    return {
        "status": "PARTIAL",
        "warning_codes": ["SAST_SCAN_PARTIAL"],
        "degradations": degradations,
        "output": result,
        "evidence": rag_evidence_items,
    }


def _safe_id(req, key, default):
    val = req.get(key) if isinstance(req, dict) else None
    return val if isinstance(val, str) and val else default


def _safe_finalize(env):
    try:
        from skills.common.runtime import redact
        env = redact.redact_envelope(env)
        env, _ = E.enforce_limits(env)
    except Exception:  # noqa: BLE001
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
