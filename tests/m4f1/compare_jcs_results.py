#!/usr/bin/env python3
"""Fail-closed comparison of PG JCS results against fixed oracle output."""

from __future__ import annotations

import re
import sys
from pathlib import Path


CANON_IDS = (
    "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8",
    "N1", "N2", "N3", "N4",
    "S_BS", "S_TAB", "S_LF", "S_FF", "S_CR", "S_QUOTE",
    "S_BSLS", "S_CTRL", "LIT_BS_U_CANON", "R1",
)

ERROR_CASES = {
    "DUP_ROOT": "duplicate object key",
    "DUP_NESTED": "duplicate object key",
    "DUP_ARRAY": "duplicate object key",
    "DUP_ESC_EQ": "duplicate object key",
    "U0000_ESCAPE": "U+0000 not allowed",
    "RAW_NUL": "U+0000 not allowed",
    "SURRO_H": "invalid Unicode scalar",
    "SURRO_L": "invalid Unicode scalar",
    "BAD_JSON": "invalid UTF-8 or JSON",
    "BAD_UTF8": "invalid UTF-8 or JSON",
}


def load_rows(path: Path) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw:
            continue
        parts = raw.split("|", 2)
        if len(parts) != 3 or not parts[0]:
            raise ValueError(f"{path}:{number}: malformed row")
        if parts[0] in rows:
            raise ValueError(f"{path}:{number}: duplicate id {parts[0]}")
        rows[parts[0]] = (parts[1], parts[2])
    return rows


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: compare_jcs_results.py EXPECTED PG")
    expected = load_rows(Path(sys.argv[1]))
    actual = load_rows(Path(sys.argv[2]))
    failures: list[str] = []
    passed = 0

    for test_id in CANON_IDS:
        if expected.get(test_id) != actual.get(test_id):
            failures.append(
                f"{test_id}: expected={expected.get(test_id)!r} actual={actual.get(test_id)!r}"
            )
        else:
            passed += 1

    v9 = actual.get("V9", ("", ""))[0]
    if not v9.startswith("ERR:P0001:") or not re.search(
        r"safe range|exceeds", v9, re.IGNORECASE
    ):
        failures.append(f"V9: unexpected result {v9!r}")
    else:
        passed += 1

    for test_id, fragment in ERROR_CASES.items():
        result = actual.get(test_id, ("", ""))[0]
        if not result.startswith("ERR:P0001:") or fragment not in result:
            failures.append(f"{test_id}: expected P0001/{fragment!r}, got {result!r}")
        elif "22P05" in result or "22P02" in result or "54000" in result:
            failures.append(f"{test_id}: leaked internal SQLSTATE in {result!r}")
        else:
            passed += 1

    literal = actual.get("LIT_BS_U", ("", ""))[0]
    if not re.fullmatch(r"OK:[0-9a-f]{64}", literal):
        failures.append(f"LIT_BS_U: expected real success, got {literal!r}")
    else:
        passed += 1

    fixed = {
        "REQID": "req-523b4899a7f81fd7ecb8e16c",
        "D_IN": "fcc1504e92491760b7ff43d07fe7d83ff26bce5b12762333019a14a8907afbdc",
    }
    for test_id, value in fixed.items():
        if expected.get(test_id, ("", ""))[0] != value:
            failures.append(f"{test_id}: fixed oracle drift")
        else:
            passed += 1

    allowed = set(CANON_IDS) | set(ERROR_CASES) | {"V9", "LIT_BS_U"}
    extra = sorted(set(actual) - allowed)
    missing = sorted(allowed - set(actual))
    if extra:
        failures.append(f"unexpected PG ids: {extra}")
    if missing:
        failures.append(f"missing PG ids: {missing}")

    print(f"JCS SUMMARY PASS={passed} FAIL={len(failures)}")
    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
