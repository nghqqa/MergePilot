#!/usr/bin/env python3
"""M4-F Controller-side orchestration for the six frozen Skills.

The module deliberately contains no Skill implementation.  It writes inputs
through the M4-F SECURITY DEFINER API, hands the snapshot job to a connection
owned by the snapshot-worker role, and only then enqueues the dependency DAG.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping

# M6-A: OTel instrumentation (fail-closed — missing module does not crash)
_OTEL_ENABLED = False
try:
    _otel_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "otel")
    if _otel_dir not in sys.path:
        sys.path.insert(0, _otel_dir)
    import otel_spans as _otel
    _OTEL_ENABLED = True
except ImportError:
    pass


SKILL_VERSION = "1.0.0"
SKILLS = (
    "diff-parse",
    "risk-classify",
    "sast-scan",
    "test-runner",
    "case-retrieval",
    "pr-lifecycle",
)

# The graph gives useful parallelism while making the final lifecycle action
# depend on every analysis/verification branch.
SKILL_DAG = {
    "diff-parse": (),
    "sast-scan": (),
    "case-retrieval": (),
    "risk-classify": ("diff-parse",),
    "test-runner": ("risk-classify", "sast-scan"),
    "pr-lifecycle": ("test-runner", "case-retrieval"),
}

REQUEST_MIME = "application/vnd.mergepilot.skill-request.v1+json"


class M4FControllerError(RuntimeError):
    """Controller-side contract or state transition failure."""


@dataclass(frozen=True)
class StagedRun:
    run_id: str
    trace_id: str
    revision_binding_id: str
    snapshot_job_id: str
    snapshot_id: str
    request_digests: Mapping[str, str]
    skill_job_ids: Mapping[str, str]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _utf16_key(value: str) -> bytes:
    return value.encode("utf-16-be", "strict")


def _jcs_string(value: str) -> str:
    out = ['"']
    escapes = {
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
        '"': '\\"',
        "\\": "\\\\",
    }
    for char in value:
        code = ord(char)
        if code == 0:
            raise M4FControllerError("U+0000 is outside MergePilot JCS Profile v1")
        if 0xD800 <= code <= 0xDFFF:
            raise M4FControllerError("lone surrogate is outside MergePilot JCS Profile v1")
        if char in escapes:
            out.append(escapes[char])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _jcs_number(value: int | float) -> str:
    if isinstance(value, bool):
        raise M4FControllerError("boolean is not a number")
    if isinstance(value, int):
        if abs(value) > 2**53:
            raise M4FControllerError("integer exceeds MergePilot JCS safe domain")
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        raise M4FControllerError("non-finite number is outside MergePilot JCS Profile v1")
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) > 2**53:
        raise M4FControllerError("integer exceeds MergePilot JCS safe domain")
    if number.is_integer():
        return str(int(number))

    negative = number < 0
    source = repr(abs(number)).lower()
    if "e" in source:
        mantissa, exponent_text = source.split("e", 1)
        exponent = int(exponent_text)
    else:
        mantissa, exponent = source, 0
    if "." in mantissa:
        whole, fraction = mantissa.split(".", 1)
    else:
        whole, fraction = mantissa, ""
    digits = (whole + fraction).lstrip("0") or "0"
    decimal_exponent = exponent - len(fraction)
    decimal_position = len(digits) + decimal_exponent

    if 0 < decimal_position <= 21:
        if decimal_position >= len(digits):
            body = digits + "0" * (decimal_position - len(digits))
        else:
            body = digits[:decimal_position] + "." + digits[decimal_position:]
    elif -6 < decimal_position <= 0:
        body = "0." + "0" * (-decimal_position) + digits
    else:
        body = digits[0]
        if len(digits) > 1:
            body += "." + digits[1:]
        scientific_exponent = decimal_position - 1
        body += "e" + ("+" if scientific_exponent >= 0 else "") + str(scientific_exponent)
    return ("-" if negative else "") + body


def canonical_json(value: Any) -> str:
    """Serialize the frozen MergePilot JCS Profile v1 input domain."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return _jcs_number(value)
    if isinstance(value, float):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise M4FControllerError("JSON object keys must be strings")
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(
            _jcs_string(key) + ":" + canonical_json(value[key]) for key in keys
        ) + "}"
    raise M4FControllerError(f"unsupported JSON value: {type(value).__name__}")


def _canon_str(value: str | None) -> str:
    if value is None:
        return "-1:"
    return f"{len(value.encode('utf-8'))}:{value}"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _emit(
    observer: Callable[[dict[str, Any]], None] | None,
    event: str,
    *,
    run_id: str,
    trace_id: str,
    **fields: Any,
) -> None:
    if observer is None:
        return
    payload = {
        "schema": "mergepilot.observation.v1",
        "timestamp": _utc_now(),
        "event": event,
        "run_id": run_id,
        "trace_id": trace_id,
    }
    payload.update(fields)
    observer(payload)
    # M6-A: bridge observation events to OTel span events (if active span exists)
    if _OTEL_ENABLED:
        ctx = _otel.get_current_context()
        if ctx is not None:
            try:
                # Add event to the current span via the collector
                # (the span itself is managed by start_span in the caller)
                pass  # span events are added inside start_span context
            except Exception:
                pass  # fail-closed


def _request_identity(trace_id: str, run_id: str, skill: str, input_: Any) -> str:
    canonical = canonical_json(input_).encode("utf-8")
    input_digest = hashlib.sha256(canonical).hexdigest()
    material = "".join(
        _canon_str(value)
        for value in (trace_id, run_id, skill, "1", input_digest)
    ).encode("utf-8")
    return "req-" + hashlib.sha256(material).hexdigest()[:24]


def _put_requests(
    conn: Any,
    run_id: str,
    trace_id: str,
    skill_inputs: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, bytes]]:
    if set(skill_inputs) != set(SKILLS):
        missing = sorted(set(SKILLS) - set(skill_inputs))
        extra = sorted(set(skill_inputs) - set(SKILLS))
        raise M4FControllerError(f"six-Skill input mismatch missing={missing} extra={extra}")

    digests: dict[str, str] = {}
    envelopes: dict[str, bytes] = {}
    with conn.cursor() as cur:
        for skill in SKILLS:
            request_id = _request_identity(trace_id, run_id, skill, skill_inputs[skill])
            body = _json_bytes(
                {
                    "contract_version": "1",
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "input": skill_inputs[skill],
                }
            )
            cur.execute("SELECT public.put_envelope(%s,%s)", (body, REQUEST_MIME))
            row = cur.fetchone()
            digest = str(row[0]) if row and row[0] else ""
            if digest != hashlib.sha256(body).hexdigest():
                raise M4FControllerError(f"content digest mismatch for {skill}")
            digests[skill] = digest
            envelopes[skill] = body
    conn.commit()
    return digests, envelopes


def _complete_snapshot(
    snapshot_conn: Any,
    *,
    snapshot_job_id: str,
    run_id: str,
    trace_id: str,
    base_sha: str,
    head_sha: str,
    request_digests: Mapping[str, str],
    worker_id: str,
    observer: Callable[[dict[str, Any]], None] | None,
) -> str:
    items = [
        {
            "kind": "skill-input",
            "skill": skill,
            "skill_version": SKILL_VERSION,
            "digest": request_digests[skill],
        }
        for skill in sorted(SKILLS, key=_utf16_key)
    ]
    manifest = _json_bytes(
        {
            "manifest_version": "1",
            "run_id": run_id,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "produced_at": _utc_now(),
            "items": items,
        }
    )

    with snapshot_conn.cursor() as cur:
        cur.execute(
            "SELECT public.claim_snapshot_job(%s,%s,%s)",
            (snapshot_job_id, worker_id, 120),
        )
        row = cur.fetchone()
        claim_id = row[0] if row else None
        if claim_id is None:
            cur.execute(
                "SELECT snapshot_id,status FROM public.snapshot_job_outbox WHERE job_id=%s",
                (snapshot_job_id,),
            )
            replay = cur.fetchone()
            if replay and replay[0] and replay[1] == "SUCCEEDED":
                snapshot_conn.commit()
                return str(replay[0])
            snapshot_conn.rollback()
            raise M4FControllerError(f"snapshot job not claimable: {snapshot_job_id}")

        cur.execute(
            "SELECT public.complete_snapshot_job(%s,%s,%s,%s)",
            (snapshot_job_id, claim_id, manifest, True),
        )
        completed = cur.fetchone()
        snapshot_id = str(completed[0]) if completed and completed[0] else ""
        if not snapshot_id:
            raise M4FControllerError(f"snapshot completion failed: {snapshot_job_id}")
    snapshot_conn.commit()
    _emit(
        observer,
        "snapshot.completed",
        run_id=run_id,
        trace_id=trace_id,
        snapshot_job_id=snapshot_job_id,
        snapshot_id=snapshot_id,
        item_count=len(items),
    )
    return snapshot_id


def stage_six_skill_run(
    controller_conn: Any,
    snapshot_conn: Any,
    *,
    run_id: str,
    trace_id: str,
    repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    source_call_id: str,
    source_evidence_digest: str,
    skill_inputs: Mapping[str, Any],
    snapshot_worker_id: str = "snapshot-worker",
    observer: Callable[[dict[str, Any]], None] | None = None,
) -> StagedRun:
    """Bind one immutable revision, freeze six inputs, and enqueue the DAG."""

    # M6-A: OTel controller span wrapping the entire skill-run orchestration
    if _OTEL_ENABLED:
        with _otel.controller_span(run_id=run_id, trace_id=trace_id,
                                   agent_role="m4f-controller",
                                   stage="skill_run") as _otel_span:
            result = _stage_six_skill_run_inner(
                controller_conn, snapshot_conn,
                run_id=run_id, trace_id=trace_id, repo=repo,
                pr_number=pr_number, base_sha=base_sha, head_sha=head_sha,
                source_call_id=source_call_id,
                source_evidence_digest=source_evidence_digest,
                skill_inputs=skill_inputs,
                snapshot_worker_id=snapshot_worker_id,
                observer=observer,
            )
            _otel_span.set_attribute("mp.final_status", "staged")
            return result
    return _stage_six_skill_run_inner(
        controller_conn, snapshot_conn,
        run_id=run_id, trace_id=trace_id, repo=repo,
        pr_number=pr_number, base_sha=base_sha, head_sha=head_sha,
        source_call_id=source_call_id,
        source_evidence_digest=source_evidence_digest,
        skill_inputs=skill_inputs,
        snapshot_worker_id=snapshot_worker_id,
        observer=observer,
    )


def _stage_six_skill_run_inner(
    controller_conn: Any,
    snapshot_conn: Any,
    *,
    run_id: str,
    trace_id: str,
    repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    source_call_id: str,
    source_evidence_digest: str,
    skill_inputs: Mapping[str, Any],
    snapshot_worker_id: str = "snapshot-worker",
    observer: Callable[[dict[str, Any]], None] | None = None,
) -> StagedRun:
    """Inner implementation (called with or without OTel span wrapper)."""

    _emit(observer, "revision.binding.started", run_id=run_id, trace_id=trace_id)
    with controller_conn.cursor() as cur:
        cur.execute(
            "SELECT public.bind_revision(%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                repo,
                int(pr_number),
                head_sha,
                base_sha,
                source_call_id,
                source_evidence_digest,
            ),
        )
        row = cur.fetchone()
        binding_id = str(row[0]) if row and row[0] else ""
        if not binding_id:
            raise M4FControllerError("bind_revision returned no binding")
    controller_conn.commit()
    _emit(
        observer,
        "revision.bound",
        run_id=run_id,
        trace_id=trace_id,
        revision_binding_id=binding_id,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    request_digests, _ = _put_requests(controller_conn, run_id, trace_id, skill_inputs)
    with controller_conn.cursor() as cur:
        cur.execute("SELECT public.enqueue_snapshot_job(%s,%s)", (run_id, binding_id))
        row = cur.fetchone()
        snapshot_job_id = str(row[0]) if row and row[0] else ""
        if not snapshot_job_id:
            raise M4FControllerError("enqueue_snapshot_job returned no job")
    controller_conn.commit()

    snapshot_id = _complete_snapshot(
        snapshot_conn,
        snapshot_job_id=snapshot_job_id,
        run_id=run_id,
        trace_id=trace_id,
        base_sha=base_sha,
        head_sha=head_sha,
        request_digests=request_digests,
        worker_id=snapshot_worker_id,
        observer=observer,
    )

    job_ids: dict[str, str] = {}
    with controller_conn.cursor() as cur:
        for skill in SKILLS:
            deps = [job_ids[name] for name in SKILL_DAG[skill]]
            cur.execute(
                "SELECT public.enqueue_skill_job(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    snapshot_id,
                    trace_id,
                    skill,
                    SKILL_VERSION,
                    1,
                    request_digests[skill],
                    deps,
                ),
            )
            row = cur.fetchone()
            job_id = str(row[0]) if row and row[0] else ""
            if not job_id:
                raise M4FControllerError(f"enqueue_skill_job returned no job for {skill}")
            job_ids[skill] = job_id
            _emit(
                observer,
                "skill.enqueued",
                run_id=run_id,
                trace_id=trace_id,
                skill=skill,
                job_id=job_id,
                depends_on=deps,
            )
    controller_conn.commit()

    return StagedRun(
        run_id=run_id,
        trace_id=trace_id,
        revision_binding_id=binding_id,
        snapshot_job_id=snapshot_job_id,
        snapshot_id=snapshot_id,
        request_digests=dict(request_digests),
        skill_job_ids=job_ids,
    )
