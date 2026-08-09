#!/usr/bin/env python3
"""Pure unit tests for the D2B-2 OTel/SLS evidence capture module."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import builtins
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("capture_otel_sls", HERE / "capture_otel_sls.py")
assert SPEC and SPEC.loader
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)

HEAD = "a" * 40


def _spans():
    return [
        {"trace_id": "trace-1", "span_id": "span-1", "name": "controller.process_event", "status": "OK", "run_id": "run-1", "attributes": {}},
        {"trace_id": "trace-1", "span_id": "span-2", "name": "skill.pr_lifecycle", "status": "OK", "run_id": "run-1", "attributes": {}},
        {"trace_id": "trace-1", "span_id": "span-3", "name": "gateway.call_tool", "status": "OK", "run_id": "run-1", "attributes": {}},
    ]


def _sls():
    return {"name": "mergepilot-sls", "version": "1", "sha256": "b" * 64, "validated_records": 3}


def _raw():
    return {"spans": _spans(), "sls_schema": _sls()}


def _expect(condition, message):
    if not condition:
        raise AssertionError(message)


def test_validate_success():
    ok, errors = C.validate_otel(_spans(), _sls())
    _expect(ok and not errors, "valid raw capture should pass")


def test_required_span_missing():
    spans = [s for s in _spans() if s["name"] != "gateway.call_tool"]
    ok, errors = C.validate_otel(spans, _sls())
    _expect(not ok and any("required" in e for e in errors), "missing required span must fail")


def test_non_ok_status():
    spans = _spans()
    spans[0]["status"] = "ERROR"
    ok, errors = C.validate_otel(spans, _sls())
    _expect(not ok and any("status" in e for e in errors), "non-OK span must fail")


def test_bad_sls_digest():
    sls = _sls()
    sls["sha256"] = "not-a-digest"
    ok, errors = C.validate_otel(_spans(), sls)
    _expect(not ok and any("sha256" in e for e in errors), "bad SLS digest must fail")


def test_zero_records():
    sls = _sls()
    sls["validated_records"] = 0
    ok, errors = C.validate_otel(_spans(), sls)
    _expect(not ok and any("validated_records" in e for e in errors), "zero records must fail")


def test_secret_scan():
    _expect(C.secret_scan("access_token=redacted"), "secret marker must be detected")
    _expect(not C.secret_scan(_raw()), "clean raw capture must pass scan")


def test_raw_keys_strict():
    raw = _raw()
    raw["source_commit"] = HEAD
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "raw.json")
        Path(path).write_text(json.dumps(raw), encoding="utf-8")
        try:
            C.load_raw_capture(path)
        except ValueError as exc:
            _expect("exactly" in str(exc), "unexpected raw keys must fail")
        else:
            raise AssertionError("unexpected raw keys accepted")


def test_raw_must_be_external():
    path = HERE / "raw-not-allowed.json"
    try:
        try:
            C.load_raw_capture(str(path))
        except ValueError as exc:
            _expect("outside" in str(exc), "repo-local input must fail")
        else:
            raise AssertionError("repo-local input accepted")
    finally:
        if path.exists():
            path.unlink()


def test_publish_schema_fail_closed():
    old = C._schema_validate
    C._schema_validate = lambda payload: (False, "schema unavailable")
    try:
        with tempfile.TemporaryDirectory() as temp:
            ok, error = C.publish_otel(_spans(), _sls(), HEAD, os.path.join(temp, "evidence.json"))
            _expect(not ok and "schema" in (error or ""), "schema failure must block publish")
            _expect(not os.path.exists(os.path.join(temp, "evidence.json")), "failed publish must not leave evidence")
    finally:
        C._schema_validate = old


def test_schema_validator_unavailable():
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("blocked for test")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        ok, error = C._schema_validate(C.build_payload(_spans(), _sls(), HEAD))
        _expect(not ok and "unavailable" in (error or ""), "missing jsonschema must fail closed")
    finally:
        builtins.__import__ = original_import


def test_publish_secret_rejected():
    sls = _sls()
    sls["name"] = "clean"
    spans = _spans()
    spans[0]["attributes"] = {"authorization": "access_token"}
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "secret.json")
        ok, error = C.publish_otel(spans, sls, HEAD, path)
        _expect(not ok and "secret" in (error or ""), "secret evidence must be rejected")
        _expect(not os.path.exists(path), "secret evidence must not be published")


def test_publish_success():
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "evidence.json")
        ok, error = C.publish_otel(_spans(), _sls(), HEAD, path)
        _expect(ok and error is None and os.path.exists(path), "success must publish evidence")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        _expect(data["source_commit"] == HEAD, "source commit must be derived into payload")
        if os.name == "posix":
            _expect((os.stat(path).st_mode & 0o777) == 0o644, "evidence mode must be 100644")


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print("ALL UNIT TESTS PASSED (%d)" % len(tests))


if __name__ == "__main__":
    main()
