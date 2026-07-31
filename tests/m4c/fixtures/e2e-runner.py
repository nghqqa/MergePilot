#!/usr/bin/env python3
"""E2E runner: executes 4 real production-chain Docker scenarios via the
actual Skill CLI entry and writes structured container-e2e.json.

Scenarios:
1. PASS: a test that passes (artifacts=[]; tmpfs ephemeral by design)
2. TIMEOUT: a test that sleeps past the per-run timeout
3. ERROR: runner exit code 2 (pytest internal error)
4. TMPFS_QUOTA: a test that writes >8 MiB to /artifacts, requires ENOSPC, and
   passes only after observing that exact errno

Each scenario records: status, verdict, exit_code, executor, isolation,
network_policy, side_effects, duration_ms, artifacts (name/size/digest),
residual_containers, and raw_envelope_digest. TMPFS_QUOTA also records errno.

The output JSON is validated by run_all.sh against required markers.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_VENV = r"D:\goai\m4a-venv\Scripts\python.exe"
_DIGEST = "localhost:5000/mergepilot/test-runner-py@sha256:41c6ab6e8dd9a8dcacfad34650df2aa12079ddb6fd844fdaa778d6c5ba7376b0"
_PROFILE = os.path.join(_REPO, "tests", "m4c", "fixtures", "e2e-profile.json")


def _docker_residual(label):
    """Count containers matching label (via wsl.exe)."""
    try:
        r = subprocess.run(
            ["wsl.exe", "-d", "Ubuntu-22.04", "--", "docker", "ps", "-aq",
             "--filter", "label=mp-run=" + label],
            capture_output=True, timeout=10)
        lines = [l for l in r.stdout.decode("utf-8", "replace").split() if l.strip()]
        return len(lines)
    except Exception:
        return -1


def _run_scenario(name, ws, art, test_file, timeout_ms, env_extra=None):
    """Run one CLI scenario via python -m skills.test_runner.run."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["MERGEPILOT_TR_WORKSPACE"] = ws
    env["MERGEPILOT_TR_EXECUTOR"] = "container"
    env["MERGEPILOT_TR_NETWORK_POLICY"] = "denied"
    env["MERGEPILOT_TR_DOCKER_TRANSPORT"] = "wsl"
    env["MERGEPILOT_TR_WSL_DISTRO"] = "Ubuntu-22.04"
    env["MERGEPILOT_TR_IMAGE"] = _DIGEST
    env["MERGEPILOT_TR_ARTIFACT_ROOT"] = art
    env["MERGEPILOT_TR_CPUS"] = "1.0"
    env["MERGEPILOT_TR_MEMORY"] = "512m"
    env["MERGEPILOT_TR_PIDS"] = "64"
    env["MERGEPILOT_TR_UID"] = "1000"
    env["MERGEPILOT_TR_GID"] = "1000"
    # Use E2E fixture profile that matches the localhost:5000 repository
    # The Skill's run.py uses DEFAULT_PROFILES_PATH; we pass via a temp profiles
    # file that the core.run call can use. But CLI doesn't accept profiles_path.
    # Instead, temporarily replace the bundled profile for the E2E run.
    bundled = os.path.join(_REPO, "skills", "test_runner", "config", "runner-profiles.v1.json")
    backup = bundled + ".e2e-backup"
    import shutil
    shutil.copy2(bundled, backup)
    shutil.copy2(_PROFILE, bundled)
    try:
        req = json.dumps({
            "contract_version": "1", "request_id": "e2e-" + name, "trace_id": "t",
            "input": {"runner_key": "pytest", "test_paths": [test_file],
                      "timeout_ms": timeout_ms}})
        proc = subprocess.run(
            [_VENV, "-m", "skills.test_runner.run"],
            input=req, capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=120)
    finally:
        shutil.copy2(backup, bundled)
        os.remove(backup)

    result = {"scenario": name}
    try:
        env_out = json.loads(proc.stdout)
        result["status"] = env_out.get("status")
        result["error_code"] = env_out.get("error_code")
        o = env_out.get("output") or {}
        result["verdict"] = o.get("verdict")
        result["exit_code"] = o.get("exit_code")
        result["executor"] = o.get("executor")
        result["isolation"] = o.get("isolation")
        result["network_policy"] = o.get("network_policy")
        result["duration_ms"] = o.get("duration_ms")
        result["side_effects"] = [s["type"] for s in env_out.get("side_effects", [])]
        result["stdout_tail"] = o.get("stdout_tail", "")
        result["stderr_tail"] = o.get("stderr_tail", "")
        if name == "tmpfs_quota":
            match = re.search(r"TMPFS_QUOTA_ERRNO=([0-9]+)", result["stdout_tail"])
            result["quota_errno"] = int(match.group(1)) if match else None
        arts = o.get("artifacts") or []
        result["artifacts"] = [{"name": a["name"], "size": a["size"], "digest": a["digest"]} for a in arts]
        result["envelope_sha256"] = hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()
    except Exception as exc:
        result["parse_error"] = str(exc)
        result["stdout_head"] = proc.stdout[:200] if proc.stdout else ""
    result["rc"] = proc.returncode
    result["residual"] = _docker_residual("e2e-" + name)
    return result


def main():
    import shutil as _sh
    base = tempfile.mkdtemp(prefix="mp-e2e-prod-")
    ws = os.path.join(base, "ws")
    art = os.path.join(base, "art")
    os.makedirs(os.path.join(ws, "tests"))
    os.makedirs(art)

    # Scenario 1: PASS (structured output is authoritative; tmpfs /artifacts is ephemeral)
    with open(os.path.join(ws, "tests", "test_pass.py"), "w") as f:
        f.write(
            "def test_pass():\n"
            "    assert 1 + 1 == 2\n"
            "    import os\n"
            "    with open('/artifacts/result.txt', 'w') as f:\n"
            "        f.write('PASS\\n')\n"
        )
    s1 = _run_scenario("pass", ws, art, "tests/test_pass.py", 120000)

    # Scenario 2: TIMEOUT
    with open(os.path.join(ws, "tests", "test_timeout.py"), "w") as f:
        f.write("import time\ndef test_slow():\n    time.sleep(30)\n")
    s2 = _run_scenario("timeout", ws, art, "tests/test_timeout.py", 5000)

    # Scenario 3: ERROR (pytest exit 2 = internal error via bad conftest)
    os.makedirs(os.path.join(ws, "bad"))
    with open(os.path.join(ws, "bad", "test_err.py"), "w") as f:
        f.write("def test_err():\n    pass\n")
    with open(os.path.join(ws, "bad", "conftest.py"), "w") as f:
        f.write("raise ImportError('forced internal error')\n")
    s3 = _run_scenario("error", ws, art, "bad/test_err.py", 120000)

    # Scenario 4: TMPFS_QUOTA — write >8 MiB to /artifacts, expect ENOSPC (errno 28)
    with open(os.path.join(ws, "tests", "test_quota.py"), "w") as f:
        f.write(
            "import errno\n"
            "def test_quota():\n"
            "    try:\n"
            "        with open('/artifacts/big.bin', 'wb') as f:\n"
            "            f.write(b'x' * (9 * 1024 * 1024))  # 9 MiB > 8 MiB limit\n"
            "        assert False, 'write should have failed'\n"
            "    except OSError as e:\n"
            "        assert e.errno == errno.ENOSPC, f'expected ENOSPC({errno.ENOSPC}), got errno={e.errno}'\n"
            "        print(f'TMPFS_QUOTA_ERRNO={e.errno}')\n"
        )
    s4 = _run_scenario("tmpfs_quota", ws, art, "tests/test_quota.py", 120000)

    output = {
        "date": "2026-07-31",
        "image_digest": _DIGEST,
        "scenarios": [s1, s2, s3, s4],
        "all_passed": (
            s1.get("status") == "OK" and s1.get("verdict") == "PASS"
            and s1.get("executor") == "container"
            and s1.get("residual") == 0
            and s2.get("verdict") == "TIMEOUT"
            and len(s2.get("artifacts", [])) == 0
            and s2.get("residual") == 0
            and s3.get("verdict") == "ERROR"
            and len(s3.get("artifacts", [])) == 0
            and s3.get("residual") == 0
            and s4.get("verdict") == "PASS"
            and s4.get("status") == "OK"
            and s4.get("quota_errno") == errno.ENOSPC
            and len(s4.get("artifacts", [])) == 0
            and s4.get("residual") == 0
        )
    }
    out_path = os.path.join(_REPO, "evidence", "m4", "m4c", "container-e2e.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    # Don't print to stdout (GBK console can't handle WSL stderr residue)
    sys.stdout.write("E2E_DONE\n")
    _sh.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
