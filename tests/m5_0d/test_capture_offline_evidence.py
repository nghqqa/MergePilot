#!/usr/bin/env python3
"""D2B-1 unit tests: offline evidence capture pure parsers + validators.
Covers: success path, wrong gate count, duplicate gate, missing gate,
rc≠0, wrong legacy count, legacy mismatch, schema validation, source_commit
mismatch, secret scan, publish atomicity, parse edge cases."""
from __future__ import annotations

import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture_offline_evidence as C

HEAD = "f" * 40


def _gates(ok=True):
    g = [{"gate_id": n, "rc": 0 if ok else (1 if i == 0 else 0), "status": "PASS" if ok else ("FAIL" if i == 0 else "PASS")}
         for i, n in enumerate(C.EXPECTED_GATES)]
    return g


def _legacy(ok=True):
    base = [("M4-E-posix/posix (container --init)", 120, 120),
            ("M4-E-windows/m4a-venv", 88, 88),
            ("M4-D-posix/posix (container --init)", 100, 100),
            ("M4-D-windows/m4a-venv", 82, 82),
            ("M4-C-posix/posix (container --init)", 110, 110),
            ("M4-C-windows/m4a-venv", 90, 90)]
    return [{"platform_id": b[0], "rc": 0 if ok else 1, "match": ok,
             "expected_count": b[1], "actual_count": b[2] if ok else b[1] - 1} for b in base]


def _gate_log_text(ok=True):
    lines = []
    for n in C.EXPECTED_GATES:
        lines.append("%s\t%s" % ("0" if ok else ("1" if n == C.EXPECTED_GATES[0] else "0"), n))
    return "\n".join(lines)


def _legacy_text(ok=True):
    rows = [
        "M4-E-posix\ttests/m4e\tposix (container --init)\tpytest\t120\t0\t0\t0\t0\t120\t0\tMATCH",
        "M4-E-windows\ttests/m4e\tm4a-venv\tpytest\t88\t0\t0\t0\t0\t88\t0\tMATCH",
        "M4-D-posix\ttests/m4d\tposix (container --init)\tpytest\t100\t0\t0\t0\t0\t100\t0\tMATCH",
        "M4-D-windows\ttests/m4d\tm4a-venv\tpytest\t82\t0\t0\t0\t0\t82\t0\tMATCH",
        "M4-C-posix\ttests/m4c\tposix (container --init)\tpytest\t110\t0\t0\t0\t0\t110\t0\tMATCH",
        "M4-C-windows\ttests/m4c\tm4a-venv\tpytest\t90\t0\t0\t0\t0\t90\t0\tMATCH",
    ]
    if not ok:
        rows[0] = rows[0].replace("120\t0\t0\t0\t0\t120", "119\t0\t0\t0\t0\t120").replace("MATCH", "MISMATCH")
    header = "label\tdir\tplatform\tpassed\tskipped\tfailed\terrors\trc\texpected_pass\texpected_skip\tstatus"
    return "[suites]\n" + header + "\n" + "\n".join(rows) + "\n\nsuites_total: 6\nsuites_matched: %d\n" % (6 if ok else 5)


def _expect(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)


def test_parse_gates_success():
    gates = C.parse_gates_from_log(_gate_log_text())
    _expect(len(gates) == 17, "17 gates parsed")
    _expect(all(g["rc"] == 0 for g in gates), "all rc=0")
    _expect(all(g["status"] == "PASS" for g in gates), "all PASS")
    _expect(gates[0]["gate_id"] == C.EXPECTED_GATES[0], "first gate name matches")


def test_parse_gates_count_wrong():
    text = _gate_log_text() + "\n0\textra gate"
    try:
        C.parse_gates_from_log(text)
        _expect(False, "should raise on 18 gates")
    except ValueError as e:
        _expect("18" in str(e), "error mentions count=18")


def test_parse_gates_count_16():
    text = "\n".join(_gate_log_text().splitlines()[:16])
    try:
        C.parse_gates_from_log(text)
        _expect(False, "should raise on 16 gates")
    except ValueError as e:
        _expect("16" in str(e), "error mentions count=16")


def test_parse_gates_dup():
    text = _gate_log_text()
    lines = text.splitlines()
    lines[-1] = lines[0]  # duplicate first gate
    try:
        C.parse_gates_from_log("\n".join(lines))
        _expect(False, "should raise on duplicate")
    except ValueError as e:
        _expect("duplicate" in str(e).lower(), "error mentions duplicate")


def test_parse_gates_missing():
    text = _gate_log_text().replace(C.EXPECTED_GATES[5], "WRONG GATE NAME")
    try:
        C.parse_gates_from_log(text)
        _expect(False, "should raise on missing gate")
    except ValueError as e:
        _expect("missing" in str(e).lower(), "error mentions missing")


def test_parse_gates_rc_nonzero():
    gates = C.parse_gates_from_log(_gate_log_text(ok=False))
    _expect(gates[0]["rc"] == 1, "gate 0 rc=1")
    _expect(gates[0]["status"] == "FAIL", "gate 0 status=FAIL")


def test_parse_legacy_success():
    legacy = C.parse_legacy_from_output(_legacy_text())
    _expect(len(legacy) == 6, "6 legacy rows")
    _expect(all(l["rc"] == 0 for l in legacy), "all rc=0")
    _expect(all(l["match"] is True for l in legacy), "all match=True")


def test_parse_legacy_count_wrong():
    text = _legacy_text().replace("M4-C-windows", "EXTRA")
    # add extra row to make 7
    rows = text.splitlines()
    try:
        C.parse_legacy_from_output(text)
    except ValueError as e:
        pass  # may fail on dup or count; both are valid rejections
    # test with too few
    short = _legacy_text().splitlines()
    # remove one data row
    for i, ln in enumerate(short):
        if "M4-C-windows" in ln:
            short.pop(i)
            break
    try:
        C.parse_legacy_from_output("\n".join(short))
        _expect(False, "should raise on 5 rows")
    except ValueError as e:
        _expect("5" in str(e), "error mentions 5")


def test_parse_legacy_mismatch():
    legacy = C.parse_legacy_from_output(_legacy_text(ok=False))
    _expect(legacy[0]["match"] is False, "row 0 match=False")
    _expect(legacy[0]["actual_count"] != legacy[0]["expected_count"], "counts differ")


def test_validate_success():
    ok, errs = C.validate_offline(_gates(), _legacy(), HEAD, HEAD)
    _expect(ok, "validate ok")


def test_validate_rc_fail():
    ok, errs = C.validate_offline(_gates(ok=False), _legacy(), HEAD, HEAD)
    _expect(not ok, "gate rc=1 → fail")
    _expect(any("gate" in e for e in errs), "error mentions gate")


def test_validate_legacy_mismatch():
    ok, errs = C.validate_offline(_gates(), _legacy(ok=False), HEAD, HEAD)
    _expect(not ok, "legacy mismatch → fail")


def test_validate_source_commit_mismatch():
    ok, errs = C.validate_offline(_gates(), _legacy(), "a" * 40, HEAD)
    _expect(not ok and any("source_commit" in e for e in errs), "SHA mismatch → fail")


def _gate_output(gate_names_with_rc):
    """Build realistic run_all.sh output: header + PASS/FAIL per gate."""
    lines = []
    for name, rc in gate_names_with_rc:
        lines.append("=== M4-F1 gate: %s ===" % name)
        lines.append("some output")
        if rc == 0:
            lines.append("=== M4-F1 gate PASS: %s ===" % name)
        else:
            lines.append("=== M4-F1 gate FAIL: %s (rc=%d) ===" % (name, rc))
    return "\n".join(lines)


def test_parse_gates_fail_stderr_only():
    """FAIL lines appear in stderr (run_gate prints FAIL >&2).
    When merged with stdout, parser must find all 17."""
    # Simulate: 15 PASS in stdout + 2 FAIL in stderr
    stdout_part = _gate_output([(n, 0) for n in C.EXPECTED_GATES[:15]])
    stderr_part = _gate_output([(n, 1) for n in C.EXPECTED_GATES[15:]])
    combined = stdout_part + "\n" + stderr_part
    gates = C.parse_gates_from_log(C._extract_gate_log_from_stdout(combined))
    _expect(len(gates) == 17, "merged stdout+stderr → 17 gates found")
    _expect(gates[15]["rc"] == 1, "gate 16 rc=1 (from stderr)")
    _expect(gates[15]["status"] == "FAIL", "gate 16 status=FAIL")


def test_merged_exactly_17():
    """Merged stdout+stderr with all 17 gates → exactly 17 parsed."""
    pairs = [(n, 0 if i < 16 else 1) for i, n in enumerate(C.EXPECTED_GATES)]
    combined = _gate_output(pairs)
    gates = C.parse_gates_from_log(C._extract_gate_log_from_stdout(combined))
    _expect(len(gates) == 17, "exactly 17 from merged")
    _expect(sum(1 for g in gates if g["status"] == "PASS") == 16, "16 PASS")
    _expect(sum(1 for g in gates if g["status"] == "FAIL") == 1, "1 FAIL")


def test_two_fail_gates_no_evidence():
    """2 FAIL gates → validate fails → no evidence published."""
    gates = [{"gate_id": n, "rc": 0 if i < 15 else 1,
              "status": "PASS" if i < 15 else "FAIL"}
             for i, n in enumerate(C.EXPECTED_GATES)]
    ok, errs = C.validate_offline(gates, _legacy(), HEAD, HEAD)
    _expect(not ok, "2 FAIL gates → validate fails")
    _expect(any("rc" in e for e in errs), "error mentions rc")


def test_secret_scan():
    _expect(C.secret_scan("ghp_" + "a" * 36), "PAT detected")
    _expect(not C.secret_scan("clean text"), "clean text ok")


def test_publish_atomic():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    ok, err = C.publish_offline(_gates(), _legacy(), HEAD, "a" * 64, "b" * 64, p)
    _expect(ok and os.path.exists(p), "publish ok")
    data = json.load(open(p))
    _expect(data["source_commit"] == HEAD, "source_commit in evidence")
    _expect(len(data["m4f_gates"]) == 17, "17 gates in evidence")
    _expect(len(data["legacy_runs"]) == 6, "6 legacy in evidence")
    if os.name == "posix":
        _expect(oct(os.stat(p).st_mode & 0o777) == "0o644", "mode 100644")
    os.remove(p)


def test_publish_secret_rejected():
    bad_gates = _gates()
    bad_gates[0]["gate_id"] = "ghp_" + "a" * 36
    ok, err = C.publish_offline(bad_gates, _legacy(), HEAD, "a" * 64, "b" * 64,
                                tempfile.mkstemp()[1] + "x.json")
    _expect(not ok and "secret" in (err or ""), "secret → refuse publish")


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n)
            fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
