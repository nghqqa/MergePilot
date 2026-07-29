"""Common envelope construction, schema validation, status-condition checks,
SHA-256 digests, output size control (truncation) and serialization.

Schema validation uses JSON Schema Draft 2020-12 via ``jsonschema`` (the only
runtime third-party dependency). Status-condition rules are enforced both by the
schema (``allOf`` if/then) and by :func:`check_conditions` at runtime.
"""
from __future__ import annotations
import copy
import datetime as _dt
import hashlib
import json
import os

from jsonschema import Draft202012Validator

from . import errors

CONTRACT_VERSION = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCHEMA_DIR = os.path.normpath(os.path.join(_HERE, "..", "schema"))

_DEFAULT_FIELD_LIMIT = 64 * 1024        # 64 KiB per string field
_DEFAULT_TOTAL_LIMIT = 1024 * 1024      # 1 MiB whole envelope
_TRUNC_KEEP_ROOM = 80                   # room for the truncation marker suffix
_ARRAY_FIELDS = ("evidence", "artifacts", "degradations", "warning_codes", "side_effects", "redactions")

_VALIDATORS = {}


def _validator(filename):
    if filename not in _VALIDATORS:
        with open(os.path.join(_SCHEMA_DIR, filename), encoding="utf-8") as fh:
            schema = json.load(fh)
        _VALIDATORS[filename] = Draft202012Validator(schema)
    return _VALIDATORS[filename]


def _err_path(err):
    loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
    return "%s: %s" % (loc, err.message)


def validate_request(req):
    """Validate a request envelope. Raise on schema/contract violations."""
    if isinstance(req, dict):
        cv = req.get("contract_version")
        if cv is not None and cv != CONTRACT_VERSION:
            raise errors.SchemaVersionUnsupported(
                "contract_version %r not supported (only %r)" % (cv, CONTRACT_VERSION)
            )
    errs = sorted(_validator("request.envelope.schema.json").iter_errors(req), key=_err_path)
    if errs:
        raise errors.InvalidInput("; ".join(_err_path(e) for e in errs))


def validate_response(resp):
    """Validate a response envelope against the contract schema (incl. allOf)."""
    errs = sorted(_validator("response.envelope.schema.json").iter_errors(resp), key=_err_path)
    if errs:
        raise errors.InvalidInput("; ".join(_err_path(e) for e in errs))


def check_conditions(resp):
    """Enforce status/error_code/warning/degradation consistency (runtime guard
    mirroring the schema's ``allOf``)."""
    status = resp.get("status")
    err = resp.get("error_code")
    warn = resp.get("warning_codes") or []
    degr = resp.get("degradations") or []
    if status == "ERROR":
        if not (isinstance(err, str) and err):
            raise errors.SkillError("ERROR requires a non-empty error_code", errors.INTERNAL_ERROR)
    elif status == "OK":
        if err is not None:
            raise errors.SkillError("OK must have null error_code", errors.INTERNAL_ERROR)
    elif status == "PARTIAL":
        if err is not None:
            raise errors.SkillError("PARTIAL must have null error_code", errors.INTERNAL_ERROR)
        if not (warn or degr):
            raise errors.SkillError(
                "PARTIAL requires non-empty warning_codes or degradations", errors.INTERNAL_ERROR
            )
    else:
        raise errors.SkillError("unknown status %r" % (status,), errors.INTERNAL_ERROR)


def build_response(name, version, request_id, trace_id, status, *, output=None,
                   error_code=None, warning_codes=None, degradations=None,
                   message="", evidence=None, artifacts=None, retryable=False,
                   side_effects=None, started_at=None, duration_ms=0, truncated=False):
    """Construct a response envelope dict with contract defaults applied."""
    return {
        "name": name,
        "version": version,
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "trace_id": trace_id,
        "status": status,
        "error_code": error_code,
        "warning_codes": list(warning_codes or []),
        "degradations": list(degradations or []),
        "message": message,
        "output": output if output is not None else {},
        "truncated": bool(truncated),
        "evidence": list(evidence or []),
        "artifacts": list(artifacts or []),
        "started_at": started_at or _now_iso(),
        "duration_ms": int(duration_ms or 0),
        "retryable": bool(retryable),
        "side_effects": list(side_effects or []),
        "redactions": [],
    }


def _now_iso():
    # timezone-aware UTC, seconds precision (stable + parseable)
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_hex(data):
    """SHA-256 hex digest of ``str`` or ``bytes``."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def _json_bytes(obj):
    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")
    except (TypeError, ValueError):
        return repr(obj).encode("utf-8", "replace")


def _serialized_size(env):
    return len(_json_bytes(env))


def _truncation_suffix(original_text):
    return " ...[truncated; sha256:%s]" % sha256_hex(original_text)


def _collect_string_refs(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                acc.append((obj, k))
            else:
                _collect_string_refs(v, acc)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                acc.append((obj, i))
            else:
                _collect_string_refs(v, acc)


def enforce_limits(env, field_limit=_DEFAULT_FIELD_LIMIT, total_limit=_DEFAULT_TOTAL_LIMIT):
    """Truncate oversized content and guarantee the serialized envelope <= total_limit.

    Pass 1: any single string field longer than ``field_limit`` is truncated
    (original SHA-256 kept in ``evidence`` and in the truncated value).
    Pass 2: if still over ``total_limit``, repeatedly halve the largest string.
    Pass 3: if still over, replace the whole ``output`` with a digest summary
    whose ``sha256`` is of the ORIGINAL (pre-truncation) output.
    Pass 4: if still over (bulk in evidence/artifacts/degradations/side_effects/
    warning_codes/redactions), drop trailing items from the largest array,
    recording the original count + SHA-256 in ``message``.
    Final defense: if still over, clear all array contents.
    No content is dropped without a recorded digest/count. The guarantee holds
    for any distribution of strings / arrays / objects / scalars.
    Returns ``(env, truncated_bool)``. The caller's envelope is never mutated.
    """
    env = copy.deepcopy(env)  # full isolation: pass 1/2 truncate nested strings in place
    if not isinstance(env.get("evidence"), list):
        env["evidence"] = []
    truncated = False

    def record_digest(digest):
        ref = "sha256:%s" % digest
        ev = env["evidence"]
        if not any(isinstance(e, dict) and e.get("ref") == ref for e in ev):
            ev.append({"kind": "truncated_field", "ref": ref})

    # snapshot ORIGINAL output (before any truncation) for an accurate digest
    original_output = env.get("output")
    original_output_bytes = _json_bytes(original_output)
    original_output_digest = sha256_hex(original_output_bytes)

    # pass 1: per-field string limit
    refs = []
    _collect_string_refs(env, refs)
    for parent, key in refs:
        s = parent[key]
        if len(s) > field_limit:
            keep = max(0, field_limit - _TRUNC_KEEP_ROOM)
            digest = sha256_hex(s)
            parent[key] = s[:keep] + _truncation_suffix(s)
            record_digest(digest)
            truncated = True

    # pass 2: halve the largest string until under limit
    guard = 0
    while _serialized_size(env) > total_limit and guard < 10000:
        refs = []
        _collect_string_refs(env, refs)
        if not refs:
            break
        parent, key = max(refs, key=lambda rk: len(rk[0][rk[1]]))
        s = parent[key]
        if len(s) <= _TRUNC_KEEP_ROOM * 2:
            break
        digest = sha256_hex(s)
        keep = max(0, len(s) // 2 - _TRUNC_KEEP_ROOM)
        parent[key] = s[:keep] + _truncation_suffix(s)
        record_digest(digest)
        truncated = True
        guard += 1

    # pass 3: replace whole output with digest summary (uses ORIGINAL digest)
    if _serialized_size(env) > total_limit:
        env["output"] = {
            "_output_replaced": True,
            "original_bytes": len(original_output_bytes),
            "sha256": original_output_digest,
            "reason": "output exceeded total envelope limit; replaced with digest summary",
        }
        record_digest(original_output_digest)
        truncated = True

    # pass 4: truncate the largest array field (bulk outside output)
    array_truncation = {}  # field -> {count, digest} of the array as truncation began
    guard = 0
    while _serialized_size(env) > total_limit and guard < 100000:
        candidates = [(f, env[f]) for f in _ARRAY_FIELDS if isinstance(env.get(f), list) and env[f]]
        if not candidates:
            break
        field, arr = max(candidates, key=lambda kv: len(_json_bytes(kv[1])))
        if field not in array_truncation:
            array_truncation[field] = {"count": len(arr), "digest": sha256_hex(_json_bytes(arr))}
        drop_n = max(1, len(arr) // 10)
        env[field] = arr[:len(arr) - drop_n]
        truncated = True
        guard += 1
    if array_truncation:
        parts = ["%s:was=%d,sha256=%s" % (f, info["count"], info["digest"]) for f, info in array_truncation.items()]
        note = "arrays_truncated[" + ";".join(parts) + "]"
        msg = env.get("message", "")
        env["message"] = (msg + " | " + note) if msg else note

    # final defense: clear array contents if somehow still over
    if _serialized_size(env) > total_limit:
        for f in _ARRAY_FIELDS:
            if isinstance(env.get(f), list) and env[f]:
                env[f] = []
        truncated = True

    env["truncated"] = bool(truncated)
    return env, truncated


def serialize(env):
    """Serialize an envelope to a single-line JSON string (UTF-8, not ascii-escaped)."""
    return json.dumps(env, ensure_ascii=False)
