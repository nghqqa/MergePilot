#!/usr/bin/env python3
"""Pure tests for the D2B-3 production-live raw evidence importer."""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("capture_production_live", HERE / "capture_production_live.py")
assert SPEC and SPEC.loader
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)

HEAD = "a" * 40
HS = "matrix-local.hiclaw.io"


def _agent(role, uid):
    return {"role": role, "container_id": "cid-" + role, "image_id": "img-" + role, "matrix_user_id": uid,
            "started_at": "2026-01-01T00:00:00Z", "command_digest": "d" * 64, "log_digest": "e" * 64}


def _raw():
    return {
        "matrix_server_name": HS,
        "sync_events": [{"sync_batch_id": "b1", "event_id": "$evt", "room_id": "!room", "sender": "@manager:" + HS,
                          "event_type": "M4F_RUN", "body_sha256": "a" * 64, "received_at": "t1", "consumer_name": "candidate"}],
        "stage_events": [{"stage_event_id": "se1", "matrix_event_id": "$evt", "room_id": "!room", "sender": "@manager:" + HS,
                           "event_type": "M4F_RUN", "stage": "m4f_snapshot", "status": "PROCESSED", "parsed_run_id": "run-1",
                           "processed_by": "candidate", "error_code": ""}],
        "agent_processes": {r: _agent(r, "@" + r + ":" + HS) for r in ("manager", "reviewer", "fixer", "verifier")},
        "task_run": {"run_id": "run-1", "room_id": "!room", "status": "HOLD", "current_stage": "m4f_await_review", "verdict": "PASS",
                      "consumer_name": "candidate", "revision_binding_id": "bnd-1", "base_sha": "a" * 40, "head_sha": "b" * 40,
                      "review_stage_event_id": "se1", "fix_stage_event_id": "se1", "verify_stage_event_id": "se1"},
        "skill_jobs": [{"skill_name": "skill-%d" % i, "job_id": "j%d" % i, "invocation_id": "i%d" % i,
                         "status": "SUCCEEDED", "revision_binding_id": "bnd-1", "output_schema_validated": True} for i in range(6)],
        "mcp_calls": [],
        "dispatch_rows": [],
        "watcher_config": {"excluded_prefixes": ["m5live-"]},
        "injection_scan": {"scanned_sources": ["runner", "send_as", "inject", "logs"], "forbidden_invocations": []},
        "secret_scan": {"scanned_targets": ["candidate", "gateway", "agents"], "matches": []},
        "residue": {k: [] for k in ("containers", "networks", "volumes", "temp_dirs", "open_prs", "branches")},
    }


def _expect(condition, message):
    if not condition:
        raise AssertionError(message)


def test_validate_success():
    ok, errors = C.validate_production(_raw(), HEAD)
    _expect(ok and not errors, "valid raw production record should pass")


def test_source_metadata_rejected():
    raw = _raw()
    raw["source_commit"] = HEAD
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "raw.json")
        Path(path).write_text(json.dumps(raw), encoding="utf-8")
        try:
            C.load_raw_capture(path)
        except ValueError as exc:
            _expect("source metadata" in str(exc), "caller source metadata must be rejected")
        else:
            raise AssertionError("caller source metadata accepted")


def test_extra_key_rejected():
    raw = _raw()
    raw["precomputed_boolean"] = True
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "raw.json")
        Path(path).write_text(json.dumps(raw), encoding="utf-8")
        try:
            C.load_raw_capture(path)
        except ValueError as exc:
            _expect("extra" in str(exc), "extra raw field must be rejected")
        else:
            raise AssertionError("extra raw field accepted")


def test_secret_matches_rejected():
    raw = _raw()
    raw["secret_scan"]["matches"] = ["credential-pattern"]
    ok, errors = C.validate_production(raw, HEAD)
    _expect(not ok and any("secret_scan" in e for e in errors), "secret matches must fail closed")


def test_residue_rejected():
    raw = _raw()
    raw["residue"]["branches"] = ["feature/m5live-run"]
    ok, errors = C.validate_production(raw, HEAD)
    _expect(not ok and any("residue.branches" in e for e in errors), "residue must fail closed")


def test_schema_validator_unavailable():
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("blocked for test")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        ok, errors = C.validate_production(_raw(), HEAD)
        _expect(not ok and any("unavailable" in e for e in errors), "missing jsonschema must fail closed")
    finally:
        builtins.__import__ = original_import


def test_publish_success():
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "production-live.json")
        ok, error = C.publish_production(_raw(), HEAD, path)
        _expect(ok and error is None, "valid production evidence should publish")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        _expect(data["source_commit"] == HEAD, "source commit must be injected from trusted value")
        if os.name == "posix":
            _expect((os.stat(path).st_mode & 0o777) == 0o644, "evidence mode must be 100644")


def test_publish_failure_leaves_no_new_file():
    raw = _raw()
    raw["residue"]["containers"] = ["leak"]
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "production-live.json")
        ok, _ = C.publish_production(raw, HEAD, path)
        _expect(not ok and not os.path.exists(path), "failed evidence must not publish")


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print("ALL UNIT TESTS PASSED (%d)" % len(tests))


if __name__ == "__main__":
    main()
