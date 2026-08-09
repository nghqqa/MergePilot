#!/usr/bin/env python3
"""Unit tests for finalize_m5_0d.py TOCTOU-safe reading + semantic validation."""
from __future__ import annotations
import json, os, sys, tempfile, stat

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import finalize_m5_0d as F

HEAD = "d" * 40
H64 = "f" * 64


def _x(cond, msg):
    if not cond: raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)


def test_sha256_file():
    """SHA-256 computed inside read_evidence_toctou_safe must be exact."""
    import hashlib
    blob = b'{"a":1}'
    fd, p = tempfile.mkstemp(); os.write(fd, blob); os.close(fd)
    _content, _data, digest = F.read_evidence_toctou_safe(p)
    _x(digest == hashlib.sha256(blob).hexdigest(), "sha256 value exact")
    os.remove(p)


def test_read_toctou_regular():
    d = tempfile.mkdtemp(); p = os.path.join(d, "test.json")
    open(p, "w").write('{"a":1}')
    content, data, digest = F.read_evidence_toctou_safe(p)
    _x(data == {"a": 1}, "JSON parsed")
    _x(len(digest) == 64, "sha256 computed")
    os.remove(p); os.rmdir(d)


def test_read_toctou_symlink_rejected():
    """Symlink is rejected by lstat S_ISLNK on every platform that can create one."""
    d = tempfile.mkdtemp(); real = os.path.join(d, "r"); link = os.path.join(d, "l")
    open(real, "w").write("{}")
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError, AttributeError):
        print("  PASS: skipped (symlink creation unavailable on this OS)")
        os.remove(real); os.rmdir(d); return
    try:
        F.read_evidence_toctou_safe(link); _x(False, "symlink must be rejected")
    except ValueError:
        print("  PASS: symlink rejected (lstat S_ISLNK)")
    finally:
        os.remove(link); os.remove(real); os.rmdir(d)


def test_read_toctou_replaced_during_read_fails():
    """TOCTOU: identity mismatch between lstat and fstat (file swapped between the
    two) must fail-closed. Deterministic via a divergent fstat; works on Windows
    where O_NOFOLLOW is unavailable — the identity check is the primary defense."""
    d = tempfile.mkdtemp(); p = os.path.join(d, "ev.json")
    open(p, "w").write('{"valid":true}')
    real_fstat = os.fstat

    class _SwappedStat:
        def __init__(self, base):
            for a in ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
                      "st_atime", "st_mtime", "st_ctime",
                      "st_atime_ns", "st_ctime_ns"):
                setattr(self, a, getattr(base, a))
            self.st_size = base.st_size + 4096        # diverge → mismatch
            self.st_mtime_ns = base.st_mtime_ns + 1000

    def fake_fstat(fd):
        return _SwappedStat(real_fstat(fd))

    os.fstat = fake_fstat
    try:
        F.read_evidence_toctou_safe(p); _x(False, "identity mismatch must fail-closed")
    except ValueError:
        print("  PASS: identity mismatch (replaced) rejected")
    finally:
        os.fstat = real_fstat
        os.remove(p); os.rmdir(d)


def test_toctou_identity_check_present():
    """Source must compare lstat vs fstat identity (not rely solely on O_NOFOLLOW)."""
    src = open(os.path.join(HERE, "finalize_m5_0d.py"), encoding="utf-8").read()
    for attr in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
        _x(attr in src, "compares %s (lstat vs fstat)" % attr)
    _x("O_NOFOLLOW" in src, "uses O_NOFOLLOW where available")
    _x("getattr(os, \"O_NOFOLLOW\"" in src, "guards O_NOFOLLOW availability")


def test_validate_schema_success():
    data = {"schema_version": "1", "source_commit": HEAD,
            "m4f_gates": [{"gate_id": "g%02d" % i, "rc": 0, "status": "PASS", "output_sha256": H64} for i in range(1, 18)],
            "legacy_runs": [{"platform_id": "p%d" % i, "rc": 0, "match": True, "expected_count": 10, "actual_count": 10, "output_sha256": H64} for i in range(1, 7)]}
    ok, err = F._validate_schema(data, "offline-regression.schema.json")
    _x(ok, "schema ok (err=%s)" % err)


def test_validate_schema_fail():
    ok, err = F._validate_schema({"bad": True}, "offline-regression.schema.json")
    _x(not ok, "invalid → schema reject")


def test_schema_validator_unavailable():
    import builtins
    orig = builtins.__import__
    def blocked(name, *a, **kw):
        if name == "jsonschema": raise ImportError("blocked")
        return orig(name, *a, **kw)
    builtins.__import__ = blocked
    try:
        ok, err = F._validate_schema({"schema_version": "1"}, "offline-regression.schema.json")
        _x(not ok and "unavailable" in (err or ""), "jsonschema unavailable → fail-closed")
    finally:
        builtins.__import__ = orig


def test_git_head():
    h = F._git_head()
    _x(len(h) == 0 or len(h) == 40, "git_head 40 chars or empty")


def test_code_facts_not_hardcoded():
    """Finalizer must use _load_code_facts(), not hardcoded booleans."""
    src = open(os.path.join(HERE, "finalize_m5_0d.py"), encoding="utf-8").read()
    _x("c2_smoke_has_audit_dsn\":True" not in src and "'c2_smoke_has_audit_dsn':True" not in src,
       "no hardcoded code facts boolean")
    _x("_load_code_facts" in src, "uses _load_code_facts()")


def test_semantic_validation_imports():
    """Finalizer must import + call validate_offline/validate_otel/validate_production."""
    src = open(os.path.join(HERE, "finalize_m5_0d.py"), encoding="utf-8").read()
    _x("CO.validate_offline" in src, "calls validate_offline")
    _x("CT.validate_otel" in src, "calls validate_otel")
    _x("CP.validate_production" in src, "calls validate_production")


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n); fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
