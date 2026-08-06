#!/usr/bin/env python3
"""M5-0C real Policy Gateway runtime gate — OPT-IN, FAIL-CLOSED pytest driver.

Complements the STATIC contract tests in test_m5_0c_policy.py (YAML shape via a
self-made evaluator). This gate starts the REAL tools/policy-gateway/gateway.py
(isolated container) + counting fake GitHub MCP + isolated audit PG inside
MergePilot-Test, and drives the real MCP SSE authorization path for every
role/tool/repo/branch scenario. The two suites are complementary and NOT
substitutable.

Opt-in + fail-closed (requirement 2):
  * RUN_M5_0C_GATEWAY unset      -> SKIP (heavy Docker integration gate).
  * RUN_M5_0C_GATEWAY=1          -> NO skips. Every environment/parse/guard
                                    failure is a FAIL:
                                      - Git Bash missing             -> FAIL
                                      - wrapper rc 2/64/65/69 (guard)-> FAIL
                                      - final JSON missing/invalid   -> FAIL
                                      - scenarios != 17              -> FAIL
                                      - any audit/fake-count mismatch-> FAIL
The authoritative invocation is `bash tests/m5_0c/run_m5_0c_gateway_policy.sh`.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "tests" / "m5_0c" / "run_m5_0c_gateway_policy.sh"
OPTED_IN = os.environ.get("RUN_M5_0C_GATEWAY") == "1"


def _git_bash() -> str | None:
    """Locate Git Bash (sets $MSYSTEM). python subprocess `bash` may resolve to
    WSL bash, which breaks wsl_test.sh's /d/ path check."""
    candidates = []
    wb = shutil.which("bash")
    if wb:
        candidates.append(wb)
    for p in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if pathlib.Path(p).exists():
            candidates.append(p)
    for c in candidates:
        try:
            r = subprocess.run([c, "-lc", "echo ${MSYSTEM:-none}"], capture_output=True, text=True, timeout=5)
            if r.stdout.strip() and r.stdout.strip() != "none":
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


def _extract_gate_json(stdout: str) -> dict:
    text = stdout
    idx = text.rfind('"gate"')
    if idx == -1:
        pytest.fail(f"no gate JSON in stdout tail:\n{text[-1200:]}")
    start = text.rfind("{", 0, idx)
    depth = 0
    instr = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception as e:  # noqa: BLE001
                        pytest.fail(f"gate JSON did not parse: {e}\n{text[start:start+800]}")
    pytest.fail(f"unterminated gate JSON:\n{text[start:start+800]}")


@pytest.mark.skipif(not OPTED_IN, reason="opt-in Docker gate; set RUN_M5_0C_GATEWAY=1")
def test_m5_0c_gateway_policy_runtime():
    # FAIL-CLOSED: when opted in, no environment reason becomes a skip.
    gb = _git_bash()
    assert gb is not None, (
        "RUN_M5_0C_GATEWAY=1 but Git Bash not found (needed by wsl_test.sh); "
        "run via: bash tests/m5_0c/run_m5_0c_gateway_policy.sh")
    proc = subprocess.run([gb, str(WRAPPER)], capture_output=True, text=True, timeout=900)
    # opted-in: ANY non-zero wrapper rc (guard rc=2 / bad-path rc=3 / bad-RUN_KEY rc=4 /
    # collision rc=5 / gate-fail rc=1 / any other) is a FAIL, NEVER a skip.
    assert proc.returncode == 0, (
        f"opted-in gate rc={proc.returncode} (must be 0): {proc.stderr[-400:]}")
    result = _extract_gate_json(proc.stdout)
    assert result.get("gate") == "m5-0c-gateway-policy", result
    assert result.get("all_passed") is True, (
        f"runtime gate not all_passed: failed={result.get('failed')} "
        f"client_rc={result.get('client_rc')} error={result.get('error')}\n"
        + json.dumps([r for r in result.get("results", []) if not r.get("PASS")], indent=2)
    )
    assert result.get("scenarios") == 17, f"scenarios != 17: {result.get('scenarios')}"
    assert result.get("failed") == 0, result.get("failed")
    assert result.get("error") is None, result.get("error")
    res = result["residue"]
    assert res["containers"] == 0 and res["networks"] == 0, res
    for r in result["results"]:
        if r["expect"] == "DENY":
            assert r["fake_delta"] == 0, f"DENY reached upstream: {r}"
        else:
            assert r["fake_delta"] >= 1, f"ALLOW did not reach upstream: {r}"
        assert r["audit_ok"], f"audit mismatch: {r}"
