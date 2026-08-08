#!/usr/bin/env python3
"""Unit tests for M5-0D D1 capture_c3_evidence pure helpers.
Covers: truncated log, missing marker, multiple summaries, 9/10, duplicate
run_key, resource drift, secret hit, residue, wrong SHA, success path.
Pure (no c3_runner, no MergePilot-Test, no network)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture_c3_evidence as C

HEAD = "c" * 40  # synthetic 40-hex-ish commit
ENTRY = Path(HERE) / "run_c3_evidence_capture.sh"


def _run(i):
    return {
        "run": i, "final_rc": 0, "c2_exit_rc": 0,
        "run_key": "c2-178600000%d-abc%02d" % (i, i), "run_id": "c2-178600000%d-abc%02d" % (i, i),
        "positives": "22/22", "negatives": "15/15",
        "src_pr": 380 + 2 * i, "fix_pr": 381 + 2 * i, "head_sha": "a" * 40,
        "m4f_event_id": "$evt%d:m5c2-hs" % i,
        "branch_residue": [], "openpr_residue": [],
        "direct_gh": {"m5c2-cand-%d" % i: 0, "m5c2-gw-%d" % i: 0},
        "llm": 0, "secret_hits_all0": True,
        "docker_residue": {"containers": 0, "networks": 0}, "duration_s": 120 + i,
    }


def _valid_summary():
    return {
        "gate": "m5-0c-c3", "n_runs": 10, "n_pass": 10, "all_pass": True,
        "final_rc": 0, "total_duration_s": 1263, "state_stable": True,
        "docker_state_pre": {"volume_count": 167, "volume_hash": "h", "image_hash": "i", "c2_containers": "0", "c2_networks": "0"},
        "docker_state_post": {"volume_count": 167, "volume_hash": "h", "image_hash": "i", "c2_containers": "0", "c2_networks": "0"},
        "runs": [_run(i) for i in range(1, 11)],
    }


def _stdout(s):
    return "=== C3 PREFLIGHT ===\n  ok\n--- C3 RUN 1/10 ---\n  run 1: ...\n=== C3 SUMMARY ===\n" + json.dumps(s, indent=2)


def _expect(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)


def test_extract_and_validate():
    # 1. success path
    s = _valid_summary()
    obj, err = C.extract_summary_json(_stdout(s))
    _expect(err is None, "success: extract ok")
    ok, fail = C.validate_summary(obj, HEAD, HEAD)
    _expect(ok, "success: validate ok (got %s)" % fail)
    # publish to temp
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    ok, perr = C.publish_evidence(s, HEAD, p)
    _expect(ok and os.path.exists(p), "success: publish ok")
    reparsed = json.load(open(p))
    _expect(reparsed["source_commit"] == HEAD and reparsed["n_pass"] == 10, "success: reparsed ok")
    # mode 100644 is only meaningful on POSIX (MergePilot-Test Linux); the host
    # Windows FS has no Unix mode bits, so assert only there.
    if os.name == "posix":
        _expect(oct(os.stat(p).st_mode & 0o777) == "0o644", "success: mode 100644")
    else:
        print("  PASS: success: mode 100644 (skipped on non-POSIX host; enforced at publish on MergePilot-Test Linux)")
    os.remove(p)
    print("ALL CAPTURE UNIT TESTS BELOW MUST FAIL VALIDATION AS SHOWN")


def test_truncated_log():
    obj, err = C.extract_summary_json("=== C3 PREFLIGHT ===\n--- RUN 1/10 ---\n  run 1: (trunc")
    _expect(obj is None and "marker" in err, "truncated log: no marker -> err")


def test_missing_marker():
    obj, err = C.extract_summary_json("some output without the summary marker at all")
    _expect(obj is None and "marker" in err, "missing marker -> err")


def test_multiple_markers():
    s = _valid_summary()
    obj, err = C.extract_summary_json(_stdout(s) + "\n=== C3 SUMMARY ===\n" + json.dumps(s))
    _expect(obj is None and "multiple" in err, "multiple markers -> err")


def test_9_of_10():
    s = _valid_summary()
    s["n_pass"] = 9
    s["runs"][-1]["final_rc"] = 1
    obj, err = C.extract_summary_json(_stdout(s))
    _expect(err is None, "9/10: extract ok")
    ok, fail = C.validate_summary(obj, HEAD, HEAD)
    _expect(not ok and any("n_pass" in f or "final_rc" in f for f in fail), "9/10 -> validate fail (%s)" % fail)


def test_duplicate_run_key():
    s = _valid_summary()
    s["runs"][1]["run_key"] = s["runs"][0]["run_key"]
    obj, _ = C.extract_summary_json(_stdout(s))
    ok, fail = C.validate_summary(obj, HEAD, HEAD)
    _expect(not ok and any("run_key not unique" in f for f in fail), "duplicate run_key -> fail (%s)" % fail)


def test_resource_drift():
    s = _valid_summary()
    s["docker_state_post"] = dict(s["docker_state_pre"], volume_count=168)
    s["state_stable"] = False
    obj, _ = C.extract_summary_json(_stdout(s))
    ok, fail = C.validate_summary(obj, HEAD, HEAD)
    _expect(not ok and any("docker_state_pre!=post" in f or "state_stable" in f for f in fail), "resource drift -> fail (%s)" % fail)


def test_secret_hit():
    s = _valid_summary()
    s["runs"][0]["head_sha"] = "ghp_" + "a" * 36  # PAT-shaped secret in a field
    ok, perr = C.publish_evidence(s, HEAD, tempfile.mkstemp()[1] + "x.json")
    _expect(not ok and "secret" in (perr or ""), "secret hit -> publish refused (%s)" % perr)


def test_residue():
    s = _valid_summary()
    s["runs"][2]["branch_residue"] = ["fix/leftover"]
    obj, _ = C.extract_summary_json(_stdout(s))
    ok, fail = C.validate_summary(obj, HEAD, HEAD)
    _expect(not ok and any("branch_residue" in f for f in fail), "residue -> fail (%s)" % fail)


def test_wrong_sha():
    s = _valid_summary()
    obj, _ = C.extract_summary_json(_stdout(s))
    ok, fail = C.validate_summary(obj, "deadbeef" + "0" * 32, HEAD)  # mismatched
    _expect(not ok and any("source_commit" in f for f in fail), "wrong SHA -> fail (%s)" % fail)


def test_entry_requires_isolated_stopped_production():
    text = ENTRY.read_text(encoding="utf-8")
    required = [
        'TEST_DISTRO="MergePilot-Test"',
        'PROD_DISTRO="Ubuntu-22.04"',
        'if [ "$prod_before" != "Stopped" ]',
        'if [ "$test_before" != "Running" ]',
        'test "${WSL_DISTRO_NAME:-}" = "MergePilot-Test"',
        'test "$(stat -c %a /dev/shm/m5c-c3/fixture-pat)" = "600"',
    ]
    _expect(all(item in text for item in required), "entry fail-closes on distro, production state, and PAT mode")


def test_entry_cleanup_contract():
    text = ENTRY.read_text(encoding="utf-8")
    required = [
        "trap cleanup EXIT INT TERM",
        'rm -f -- "$PAT_FILE"',
        'wsl.exe --terminate "$TEST_DISTRO"',
        '[ "$test_after" = "Stopped" ]',
        '[ "$prod_after" = "Stopped" ]',
        'exit "$capture_rc"',
    ]
    _expect(all(item in text for item in required), "entry always removes PAT, stops test WSL, and preserves capture rc")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("=== %s ===" % name)
            fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
