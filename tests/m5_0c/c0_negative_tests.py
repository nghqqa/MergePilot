#!/usr/bin/env python3
"""C0 negative + idempotency test runner for deploy_test_stack.sh.

Runs inside MergePilot-Test via wsl_test.sh. Writes JSON results to /tmp
(repo-external), parses with json.load (no nested bash -c quoting issues).

Each test outputs: test_id, expected_rc, actual_rc, passed.
Final output: JSON summary with all_passed + per-test results.
Exit 0 only if ALL tests pass.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

SCRIPT = "/mnt/d/goai/mergepilot-os/tests/m5_0c/deploy_test_stack.sh"
TMP = pathlib.Path("/tmp/c0_neg")
TMP.mkdir(parents=True, exist_ok=True)

results = []
test_num = 0


def run_deploy(rk: str | None, action: str, env_extra: dict | None = None) -> tuple[int, dict | None]:
    """Run deploy_test_stack.sh, return (rc, parsed_json_or_None)."""
    env = os.environ.copy()
    if rk is not None:
        env["M5C_RUN_KEY"] = rk
    if env_extra:
        env.update(env_extra)
    outfile = TMP / f"t{test_num}_{action}.json"
    proc = subprocess.run(
        ["bash", SCRIPT, action],
        capture_output=True, text=True, timeout=300, env=env,
    )
    outfile.write_text(proc.stdout)
    try:
        data = json.loads(proc.stdout.strip().split("\n")[-1] if proc.stdout.strip() else "{}")
    except Exception:
        # Try to find JSON block in output
        text = proc.stdout
        idx = text.rfind('{"gate"')
        if idx >= 0:
            try:
                data = json.loads(text[idx:])
            except Exception:
                data = None
        else:
            data = None
    return proc.returncode, data


def record(tid: str, expected: int, actual: int, detail: str = "") -> bool:
    passed = (actual == expected)
    results.append({
        "test_id": tid, "expected_rc": expected, "actual_rc": actual,
        "passed": passed, "detail": detail[:120],
    })
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {tid}: expected_rc={expected} actual_rc={actual} {detail}")
    return passed


def cleanup_rk(rk: str):
    subprocess.run(["bash", SCRIPT, "down"], capture_output=True, text=True,
                   timeout=60, env={**os.environ, "M5C_RUN_KEY": rk})


all_ok = True

# ── T1-T4: RUN_KEY validation (no deploy, fast) ──
print("=== RUN_KEY validation tests ===")

test_num = 1
rc, _ = run_deploy("", "up")
all_ok &= record("T1_empty_runkey", 4, rc)

test_num = 2
rc, _ = run_deploy("a/b", "up")
all_ok &= record("T2_slash_runkey", 4, rc)

test_num = 3
rc, _ = run_deploy("a..b", "up")
all_ok &= record("T3_dots_runkey", 4, rc)

test_num = 4
rc, _ = run_deploy("x" * 65, "up")
all_ok &= record("T4_toolong_runkey", 4, rc)

# ── T5-T8: idempotency cycle ──
print("\n=== Idempotency cycle (RUN_KEY=c0-idem) ===")

test_num = 5
rc, d = run_deploy("c0-idem", "up")
detail = f"all_passed={d.get('all_passed') if d else 'None'} matrix={d.get('embedded',{}).get('matrix_6167') if d else 'None'}"
all_ok &= record("T5_first_up", 0, rc, detail)

# Count resources after first up
c1 = subprocess.run(["docker", "ps", "-aq", "--filter", "label=com.mergepilot.run_key=c0-idem"],
                    capture_output=True, text=True).stdout.strip().split("\n")
c1_count = len([x for x in c1 if x.strip()])

test_num = 6
rc, d = run_deploy("c0-idem", "up")
detail = f"status={d.get('status') if d else 'None'} idempotent={d.get('idempotent') if d else 'None'}"
all_ok &= record("T6_second_up_idempotent", 0, rc, detail)

# Count resources after second up (must not increase)
c2 = subprocess.run(["docker", "ps", "-aq", "--filter", "label=com.mergepilot.run_key=c0-idem"],
                    capture_output=True, text=True).stdout.strip().split("\n")
c2_count = len([x for x in c2 if x.strip()])
test_num = 7
no_growth = (c2_count == c1_count)
results.append({"test_id": "T7_no_resource_growth", "expected_rc": 0, "actual_rc": 0 if no_growth else 1,
                "passed": no_growth, "detail": f"before={c1_count} after={c2_count}"})
print(f"  [{'PASS' if no_growth else 'FAIL'}] T7_no_resource_growth: before={c1_count} after={c2_count}")
all_ok &= no_growth

# Health
test_num = 8
rc, d = run_deploy("c0-idem", "health")
detail = f"all_passed={d.get('all_passed') if d else 'None'}"
all_ok &= record("T8_health_after_idempotent", 0, rc, detail)

# Status
test_num = 9
rc, _ = run_deploy("c0-idem", "status")
all_ok &= record("T9_status", 0, rc)

# Down
test_num = 10
rc, d = run_deploy("c0-idem", "down")
detail = f"residue={d.get('residue') if d else 'None'}"
all_ok &= record("T10_down", 0, rc, detail)

# ── T11-T14: collision / partial / isolation ──
print("\n=== Collision + isolation tests ===")

# T11: same name no labels → collision rc=5
subprocess.run(["docker", "network", "create", "m5c-net-c0-bare"], capture_output=True, text=True)
test_num = 11
rc, d = run_deploy("c0-bare", "up")
detail = f"error={d.get('error','')[:60] if d else 'None'}"
all_ok &= record("T11_bare_network_collision", 5, rc, detail)
subprocess.run(["docker", "network", "rm", "m5c-net-c0-bare"], capture_output=True, text=True)

# T12: different RUN_KEY isolation
test_num = 12
rc, d = run_deploy("c0-iso-a", "up")
all_ok &= record("T12a_iso_a_up", 0, rc)
rc, d = run_deploy("c0-iso-b", "up")
all_ok &= record("T12b_iso_b_up", 0, rc)

# T13: down A doesn't delete B
test_num = 13
cleanup_rk("c0-iso-a")
b_containers = subprocess.run(
    ["docker", "ps", "-aq", "--filter", "label=com.mergepilot.run_key=c0-iso-b"],
    capture_output=True, text=True).stdout.strip()
b_alive = len([x for x in b_containers.split("\n") if x.strip()]) > 0
results.append({"test_id": "T13_down_a_keeps_b", "expected_rc": 0, "actual_rc": 0 if b_alive else 1,
                "passed": b_alive, "detail": f"b_containers={len([x for x in b_containers.split(chr(10)) if x.strip()])}"})
print(f"  [{'PASS' if b_alive else 'FAIL'}] T13_down_a_keeps_b")
all_ok &= b_alive
cleanup_rk("c0-iso-b")

# T14: down doesn't delete unknown resources
subprocess.run(["docker", "network", "create", "m5c-unknown-net"], capture_output=True, text=True)
test_num = 14
cleanup_rk("c0-nonexist")
unknown_alive = subprocess.run(
    ["docker", "network", "inspect", "m5c-unknown-net"], capture_output=True).returncode == 0
results.append({"test_id": "T14_down_keeps_unknown", "expected_rc": 0, "actual_rc": 0 if unknown_alive else 1,
                "passed": unknown_alive, "detail": f"unknown_net_exists={unknown_alive}"})
print(f"  [{'PASS' if unknown_alive else 'FAIL'}] T14_down_keeps_unknown")
all_ok &= unknown_alive
subprocess.run(["docker", "network", "rm", "m5c-unknown-net"], capture_output=True, text=True)

# ── T15-T17: health fail conditions ──
print("\n=== Health fail conditions ===")

# T15: health on non-existent RUN_KEY → non-0
test_num = 15
rc, d = run_deploy("c0-noexist-health", "health")
all_ok &= record("T15_health_nonexist", 1, rc, f"rc={rc}")

# T16: secret scan (from any prior up JSON)
test_num = 16
secret_found = False
for f in TMP.glob("*.json"):
    text = f.read_text()
    if "ghp_" in text or "PRIVATE_KEY" in text:
        secret_found = True
        break
results.append({"test_id": "T16_secret_scan", "expected_rc": 0, "actual_rc": 0 if not secret_found else 1,
                "passed": not secret_found, "detail": "scanned all /tmp/c0_neg/*.json"})
print(f"  [{'PASS' if not secret_found else 'FAIL'}] T16_secret_scan")
all_ok &= (not secret_found)

# T17: JSON parseable — check that deploy script produces parseable JSON on success
# (error-rc tests may produce non-JSON or partial stdout; we only check successful up/down/health)
test_num = 17
all_parse = True
parse_count = 0
for f in TMP.glob("*.json"):
    text = f.read_text().strip()
    if not text:
        continue
    # Try to find a JSON object in the file
    idx = text.find('{"gate"')
    if idx < 0:
        continue
    try:
        json.loads(text[idx:])
        parse_count += 1
    except Exception:
        all_parse = False
results.append({"test_id": "T17_json_parseable", "expected_rc": 0, "actual_rc": 0 if all_parse else 1,
                "passed": all_parse, "detail": f"parsed {parse_count} gate JSONs"})
print(f"  [{'PASS' if all_parse else 'FAIL'}] T17_json_parseable ({parse_count} parsed)")
all_ok &= all_parse

# ── Final summary ──
print("\n=== SUMMARY ===")
passed = sum(1 for r in results if r["passed"])
failed = sum(1 for r in results if not r["passed"])
summary = {
    "gate": "m5-0c-c0-negatives",
    "all_passed": all_ok,
    "total": len(results),
    "passed_count": passed,
    "failed_count": failed,
    "results": results,
}
summary_file = TMP / "summary.json"
summary_file.write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))

# Cleanup temp files
for f in TMP.glob("*.json"):
    f.unlink()

sys.exit(0 if all_ok else 1)
