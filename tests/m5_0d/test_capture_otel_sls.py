#!/usr/bin/env python3
"""Pure tests for the D2B-2 deploy-owned OTel/SLS collector."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("capture_otel_sls", HERE / "capture_otel_sls.py")
assert SPEC and SPEC.loader
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)

HEAD = "a" * 40
RUN = "m5live-review-1"
START = "2026-08-09T01:00:00Z"
END = "2026-08-09T01:10:00Z"


def _raw():
    return {
        "spans": [
            {"trace_id": "t1", "span_id": "s1", "name": "controller.process_event", "status": "OK", "run_id": RUN, "attributes": {}},
            {"trace_id": "t1", "span_id": "s2", "name": "skill.pr_lifecycle", "status": "OK", "run_id": RUN, "attributes": {}},
            {"trace_id": "t1", "span_id": "s3", "name": "gateway.call_tool", "status": "OK", "run_id": RUN, "attributes": {}},
        ],
        "sls_schema": {"name": "mergepilot-sls", "version": "1", "sha256": "b" * 64, "validated_records": 3},
    }


def _provenance():
    # collector_script_sha256 + collector_command_digest are recomputable (verified
    # by validate_otel); collector_endpoint_digest + raw_capture_sha256 are
    # trust-boundary (raw OTel sink bytes / tmpfs endpoint) — format-checked only.
    command = ["capture_otel_sls.py", "--run-id", RUN, "--window-start", START, "--window-end", END]
    with open(str(HERE / "capture_otel_sls.py"), "rb") as stream:
        script_sha = hashlib.sha256(stream.read()).hexdigest()
    return {
        "collector_kind": "deploy-owned-otel-sls",
        "collector_script_sha256": script_sha,
        "collector_command_digest": hashlib.sha256(C.canonical_bytes(command)).hexdigest(),
        "collector_endpoint_digest": "3" * 64,
        "capture_window": {"started_at": START, "ended_at": END},
        "captured_at": END,
        "raw_capture_sha256": "4" * 64,
        "trace_run_binding": {"expected_run_id": RUN, "observed_run_ids": [RUN]},
    }


def _expect(condition, message):
    if not condition:
        raise AssertionError(message)


def test_validate_success():
    raw = _raw()
    ok, errors = C.validate_otel(raw["spans"], raw["sls_schema"], _provenance())
    _expect(ok and not errors, "complete provenance should pass")


def test_required_span_missing():
    raw = _raw()
    raw["spans"] = raw["spans"][:-1]
    ok, errors = C.validate_otel(raw["spans"], raw["sls_schema"], _provenance())
    _expect(not ok and any("required" in e for e in errors), "missing required span must fail")


def test_trace_run_binding_mismatch():
    raw = _raw()
    raw["spans"][0]["run_id"] = "m5live-other"
    ok, errors = C.validate_otel(raw["spans"], raw["sls_schema"], _provenance())
    _expect(not ok and any("binding" in e for e in errors), "trace/run mismatch must fail")


def test_non_ok_status():
    raw = _raw()
    raw["spans"][0]["status"] = "ERROR"
    ok, errors = C.validate_otel(raw["spans"], raw["sls_schema"], _provenance())
    _expect(not ok and any("status" in e for e in errors), "non-OK span must fail")


def test_bad_window():
    provenance = _provenance()
    provenance["capture_window"]["started_at"] = END
    ok, errors = C.validate_otel(_raw()["spans"], _raw()["sls_schema"], provenance)
    _expect(not ok and any("window" in e for e in errors), "invalid capture window must fail")


def test_bad_provenance_digest():
    provenance = _provenance()
    provenance["collector_endpoint_digest"] = "fake"
    ok, errors = C.validate_otel(_raw()["spans"], _raw()["sls_schema"], provenance)
    _expect(not ok and any("endpoint" in e for e in errors), "bad endpoint digest must fail")


def test_tmpfs_symlink_rejected():
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp) / "target"
        link = Path(temp) / "link"
        target.write_text("https://otel.invalid", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError:
            return
        try:
            C.read_tmpfs_file(str(link))
        except ValueError as exc:
            _expect("non-symlink" in str(exc), "symlink must be rejected")
        else:
            raise AssertionError("symlink input accepted")


def test_schema_validator_unavailable():
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("blocked")
        return original(name, *args, **kwargs)

    builtins.__import__ = blocked
    try:
        ok, error = C._schema_validate(C.build_payload(_raw(), _provenance(), HEAD))
        _expect(not ok and "unavailable" in (error or ""), "jsonschema absence must fail closed")
    finally:
        builtins.__import__ = original


def test_publish_success():
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "otel-sls.json")
        ok, error = C.publish_otel(_raw(), _provenance(), HEAD, path)
        _expect(ok and error is None, "valid evidence should publish")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        _expect(data["source_commit"] == HEAD, "source commit must be bound")
        _expect(data["provenance"]["trace_run_binding"]["expected_run_id"] == RUN, "run binding must persist")
        if os.name == "posix":
            _expect((os.stat(path).st_mode & 0o777) == 0o644, "mode must be 100644")


def test_publish_failure_leaves_no_file():
    raw = _raw()
    raw["spans"][0]["status"] = "ERROR"
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "otel-sls.json")
        ok, _ = C.publish_otel(raw, _provenance(), HEAD, path)
        _expect(not ok and not os.path.exists(path), "failed capture must not publish")


# ── Fix 4: recomputable OTel digests (real comparison) ──

def test_collector_command_digest_recompute():
    """collector_command_digest is recomputed from evidence; tampering → fail."""
    provenance = _provenance()
    provenance["collector_command_digest"] = "0" * 64
    ok, errors = C.validate_otel(_raw()["spans"], _raw()["sls_schema"], provenance)
    _expect(not ok and any("collector_command_digest" in e for e in errors),
            "wrong command digest must fail")


def test_collector_script_digest_recompute():
    """collector_script_sha256 is recomputed from the collector source file."""
    provenance = _provenance()
    provenance["collector_script_sha256"] = "0" * 64
    ok, errors = C.validate_otel(_raw()["spans"], _raw()["sls_schema"], provenance)
    _expect(not ok and any("collector_script_sha256" in e for e in errors),
            "wrong script digest must fail")


def test_otel_trust_boundary_documented():
    """raw_capture_sha256 (raw sink bytes) + collector_endpoint_digest (tmpfs)
    are trust-boundary — named in the validator, only format-checked."""
    src = (HERE / "capture_otel_sls.py").read_text(encoding="utf-8")
    _expect("Trust-boundary" in src, "validator documents OTel trust-boundary digests")
    _expect("raw_capture_sha256" in src and "collector_endpoint_digest" in src,
            "trust-boundary digest names present")


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print("ALL UNIT TESTS PASSED (%d)" % len(tests))


if __name__ == "__main__":
    main()
