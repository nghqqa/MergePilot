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
    """Finalizer must import + call validate_offline/validate_otel/validate_production/C3."""
    src = open(os.path.join(HERE, "finalize_m5_0d.py"), encoding="utf-8").read()
    _x("CO.validate_offline" in src, "calls validate_offline")
    _x("CT.validate_otel" in src, "calls validate_otel")
    _x("CP.validate_production" in src, "calls validate_production")
    _x("CC.validate_summary" in src, "calls C3 validate_summary")


# ── C3 tracking policy + clean-tree whitelist (Fix: C3/finalizer conflict) ──

def test_evidence_artifacts_whitelist_exact():
    """Whitelist must be exactly the 4 evidence artifacts (no more, no less)."""
    _x(len(F.EVIDENCE_ARTIFACTS) == 4, "exactly 4 evidence artifacts")
    for p in ("evidence/m5/0c/c3-10x.json", "evidence/m5/0d/offline-regression.json",
              "evidence/m5/0d/production-live.json", "evidence/m5/0d/otel-sls.json"):
        _x(p in F.EVIDENCE_ARTIFACTS, "whitelist contains %s" % p)


def test_classify_tree_accepts_tracked_c3():
    """Tracked-modified C3 (D1 history, re-captured at HEAD) + untracked 0d trio
    is the expected working tree and must be accepted (the conflict resolution)."""
    ok, err = F._classify_tree([
        " M evidence/m5/0c/c3-10x.json",
        "?? evidence/m5/0d/offline-regression.json",
        "?? evidence/m5/0d/production-live.json",
        "?? evidence/m5/0d/otel-sls.json",
    ])
    _x(ok, "tracked C3 + untracked 0d trio accepted (err=%s)" % err)


def test_classify_tree_accepts_untracked_c3():
    """Post-migration (C3 untracked, repo-external) is also accepted."""
    ok, err = F._classify_tree(["?? evidence/m5/0c/c3-10x.json"])
    _x(ok, "untracked C3 accepted (err=%s)" % err)


def test_classify_tree_accepts_clean():
    ok, err = F._classify_tree([])
    _x(ok, "clean tree accepted (err=%s)" % err)


def test_classify_tree_rejects_staged_c3():
    """Staged C3 (would enter the release commit → source_commit self-reference)
    must be rejected."""
    ok, err = F._classify_tree(["M  evidence/m5/0c/c3-10x.json"])
    _x(not ok and "staged" in err, "staged C3 rejected (err=%s)" % err)


def test_classify_tree_rejects_staged_0d():
    """Staged 0d evidence is also rejected (no evidence in the release commit)."""
    ok, err = F._classify_tree(["A  evidence/m5/0d/offline-regression.json"])
    _x(not ok and "staged" in err, "staged 0d evidence rejected (err=%s)" % err)


def test_classify_tree_rejects_dirty_unrelated():
    """A non-evidence tracked modification must be rejected (never silently ignored)."""
    ok, err = F._classify_tree([" M tools/workflow-controller/controller.py"])
    _x(not ok and "non-evidence" in err, "dirty unrelated file rejected (err=%s)" % err)


def test_classify_tree_rejects_untracked_non_evidence():
    """An untracked file outside the whitelist (even under evidence/m5/0d/) is rejected."""
    ok, err = F._classify_tree(["?? evidence/m5/0d/stray.json"])
    _x(not ok, "untracked non-whitelisted path rejected (err=%s)" % err)


def test_c3_source_commit_mismatch_rejected():
    """C3 with an old source_commit (!= HEAD) fails semantic validation."""
    import capture_c3_evidence as CC
    stale = {"gate": "m5-0c-c3", "n_runs": 10, "n_pass": 10, "all_pass": True,
             "final_rc": 0, "state_stable": True, "docker_state_pre": {}, "docker_state_post": {},
             "runs": []}
    ok, errs = CC.validate_summary(stale, "c2f503b20ce6bbb2cd4ee48a4220923fac6628bb",
                                   "ab77888aeb289a408a9420a505d489c0628e284d")
    _x(not ok and any("source_commit" in e for e in errs), "old C3 source_commit rejected")


def test_missing_evidence_raises():
    """A missing evidence file must raise (fail-closed), not be silently skipped."""
    missing = os.path.join(HERE, "__definitely_absent_evidence__.json")
    _x(not os.path.exists(missing), "precondition: file absent")
    try:
        F.read_evidence_toctou_safe(missing); _x(False, "missing evidence must raise")
    except (OSError, ValueError):
        print("  PASS: missing evidence raises (fail-closed)")


def test_finalizer_binds_c3_in_attestation():
    """Attestation must record C3 source_commit + digest (explicit binding, not ignored)."""
    src = open(os.path.join(HERE, "finalize_m5_0d.py"), encoding="utf-8").read()
    _x("evidence_source_commits" in src, "attestation records per-evidence source_commits")
    _x('digests["c3"]' in src, "attestation/tag records C3 digest")


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n); fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
