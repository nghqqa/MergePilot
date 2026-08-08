#!/usr/bin/env python3
"""M5-0C C3 — 10/10 consecutive real-chain stability validation (MergePilot-Test).

Runs the committed C2 smoke (c2_smoke.py) 10 times back-to-back, each as an
independent unique-RUN_KEY real chain (Matrix→Manager→Candidate→Reviewer→
Fixer→Policy Gateway→github-mcp→fixture repo→Verifier→HOLD/m5_verify_passed),
mapping the operator's C3 authorization to the C2 env vars c2_smoke reads.
The C2 runner is NOT modified (it is a committed C2 artifact); C3 reuses it.

Each run is fully isolated: c2_smoke brings up its own labelled stack, runs the
real chain, closes the 2 PRs + deletes the 2 RUN_KEY-bound branches via the
restricted c2_delete_test_branch, tears down, and leaves Docker/GitHub clean.
No manual fixes between runs; no cross-RUN_KEY deletion.

Security: PAT only in github-mcp bridge (secret-file). c3_runner/c2_smoke never
hold the PAT. No real LLM. github-mcp is the sole GitHub caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time

ROOT = "/mnt/d/goai/mergepilot-os"
C2_SMOKE = ROOT + "/tests/m5_0c/c2_smoke.py"
GHMCP_BRIDGE = "tests/m5_0c/c2_ghmcp_bridge.py"
GHMCP_CALL = "tests/m5_0c/c2_ghmcp_call.py"
RT_IMAGE = "mergepilot-m4f-runtime:demo"
GHMCP_IMAGE = "github-mcp-bridge:c2"
FIXTURE = "nghqqa/MergePilot-e2e-fixture"
OWNER, REPO = FIXTURE.split("/")
N_RUNS = 10
RUN_TIMEOUT = 760  # per-run seconds (each c2_smoke ~3-4 min)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120), **kw)


def preflight():
    print("=== C3 PREFLIGHT ===")
    allow = os.environ.get("M5C_C3_ALLOW_GITHUB_WRITES") == "1"
    pat = os.environ.get("M5C_C3_FIXTURE_GITHUB_PAT_FILE", "/dev/shm/m5c-c3/fixture-pat")
    print("  M5C_C3_ALLOW_GITHUB_WRITES=%s  PAT=%s" % ("1" if allow else "MISSING", pat))
    if not allow:
        return False, "M5C_C3_ALLOW_GITHUB_WRITES!=1"
    if not os.path.exists(pat):
        return False, "PAT file missing: %s" % pat
    st = os.stat(pat)
    if st.st_size == 0:
        return False, "PAT empty"
    if (st.st_mode & 0o777) != 0o600:
        return False, "PAT mode!=600"
    # daemon isolation
    ps = run(["docker", "ps", "-a", "--format", "{{.Names}}"]).stdout.split()
    prod = [n for n in ps if any(p in n for p in
             ["mergepilot-controller", "policy-gw", "audit-pg", "github-mcp", "hiclaw"])]
    if prod:
        return False, "production containers visible: %s" % prod
    # c2_smoke.py hardcodes the C2 mount path (PAT_FILE_HOST constant) for the
    # bridge -v mount; it is a committed C2 artifact (not modifiable here). Docker
    # does not follow a bind-mount symlink across the mount boundary, and the c2/c3
    # tmpfs dirs are separate mounts (no cross-device hardlink). So copy the C3 PAT
    # to the C2 path (transient tmpfs copy, mounted ONLY to the github-mcp bridge,
    # removed after all runs). PAT never enters env/logs/git/cmdline.
    import shutil
    c2_path = "/dev/shm/m5c-c2/fixture-pat"
    try:
        os.makedirs("/dev/shm/m5c-c2", exist_ok=True)
        os.chmod("/dev/shm/m5c-c2", 0o700)
        if os.path.lexists(c2_path):
            os.remove(c2_path)
        shutil.copy(pat, c2_path)
        os.chmod(c2_path, 0o600)
        if os.path.getsize(c2_path) != os.path.getsize(pat):
            return False, "C2 PAT copy size mismatch"
    except OSError as e:
        return False, "C2 PAT copy failed: %s" % e
    print("  preflight OK (allow=1, C3 PAT non-empty mode=600, C2 copy staged for c2_smoke, no prod visible)")
    return True, pat


def cleanup_c2_copy():
    """Remove the transient C2 PAT copy (operator's C3 PAT at c3 path remains)."""
    try:
        p = "/dev/shm/m5c-c2/fixture-pat"
        if os.path.lexists(p):
            os.remove(p)
    except OSError:
        pass


def docker_state():
    """Docker resources that must be unchanged across the 10 runs."""
    volumes = sorted(run(["docker", "volume", "ls", "-q"]).stdout.split())
    volume_hash = hashlib.sha256("\n".join(volumes).encode("utf-8")).hexdigest()
    imgs = run(["bash", "-c", "docker images -q | sort | sha256sum"]).stdout.strip().split()[0]
    cont = run(["bash", "-c", "docker ps -aq --filter label=com.mergepilot.c2 | wc -l"]).stdout.strip()
    nets = run(["bash", "-c", "docker network ls -q --filter label=com.mergepilot.c2 | wc -l"]).stdout.strip()
    return {"volume_count": len(volumes), "volume_hash": volume_hash,
            "image_hash": imgs[:16], "c2_containers": cont, "c2_networks": nets}


def parse_c2_summary(stdout):
    i = stdout.rfind('{\n  "gate": "m5-0c-c2"')
    if i < 0:
        return None
    try:
        return json.loads(stdout[i:])
    except Exception:
        return None


def run_once(pat, idx):
    env = dict(os.environ)
    env["M5C_C2_ALLOW_GITHUB_WRITES"] = "1"
    env["M5C_C2_FIXTURE_GITHUB_PAT_FILE"] = pat
    t0 = time.time()
    try:
        r = subprocess.run(["python3", C2_SMOKE], capture_output=True, text=True,
                           timeout=RUN_TIMEOUT, env=env)
        rc = r.returncode
        out = r.stdout
        err = r.stderr
    except subprocess.TimeoutExpired as e:
        return {"run": idx, "final_rc": 124, "timeout": True, "duration_s": int(time.time() - t0),
                "error": "run timed out"}
    dur = int(time.time() - t0)
    s = parse_c2_summary(out) or {}
    rec = {
        "run": idx, "final_rc": s.get("final_rc", 99) if s else rc,
        "c2_exit_rc": rc, "run_key": s.get("run_key"), "run_id": s.get("run_id"),
        "positives": "%s/%s" % (s.get("positives_passed"), s.get("positives_total")) if s else "?",
        "negatives": "%s/%s" % (s.get("negatives_passed"), s.get("negatives_total")) if s else "?",
        "src_pr": s.get("src_pr"), "fix_pr": s.get("pr_number"), "head_sha": s.get("head_sha"),
        "m4f_event_id": s.get("m4f_event_id"),
        "branch_residue": s.get("github_branch_residue"), "openpr_residue": s.get("github_openpr_residue"),
        "direct_gh": s.get("agent_direct_github_calls"), "llm": s.get("external_llm_calls"),
        "secret_hits_all0": all(v == 0 for v in (s.get("secret_hits") or {}).values()) if s else None,
        "docker_residue": s.get("docker_residue"), "duration_s": dur,
    }
    if not s:
        rec["error"] = (out + err)[-400:]
    return rec


def final_gh_check(pat):
    """Brief bridge: verify fixture repo is main-only + 0 open PR after all runs."""
    cid = "c3-finalcheck-%d" % int(time.time())
    run(["docker", "run", "-d", "--name", cid, "--network", "bridge",
         "-v", "%s:/secrets/pat:ro" % pat,
         "-v", "%s/%s:/app/bridge.py:ro" % (ROOT, GHMCP_BRIDGE), GHMCP_IMAGE])
    try:
        for _ in range(20):
            if "200" in run(["docker", "run", "--rm", "--network", "container:" + cid,
                             "--entrypoint", "python", RT_IMAGE, "-c",
                             "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8082/_health',timeout=3).status)"]).stdout:
                break
            time.sleep(2)
        r = run(["docker", "run", "--rm", "--network", "container:" + cid, "-v", "%s:/workspace:ro" % ROOT,
                 "-w", "/workspace", "-e", "C2_BRIDGE=http://127.0.0.1:8082",
                 "--entrypoint", "python", RT_IMAGE, "/workspace/" + GHMCP_CALL,
                 "list_branches", json.dumps({"owner": OWNER, "repo": REPO})], timeout=60)
        import json as _j
        d = _j.loads((r.get("content", "") if isinstance(r, dict) else _j.loads(r.stdout or "{}").get("content", "")) or "[]")
    except Exception as e:
        d = {"_err": str(e)[:120]}
    finally:
        run(["docker", "rm", "-f", cid])
    return d


def main():
    ok, info = preflight()
    if not ok:
        print("\n=== C3 PREFLIGHT FAIL-CLOSED: %s ===" % info)
        print(json.dumps({"gate": "m5-0c-c3", "final_rc": 2, "fail_closed": True, "reason": info}, indent=2))
        sys.exit(2)
    pat = info
    pre = docker_state()
    print("  pre-state: %s" % json.dumps(pre))
    runs = []
    overall_t0 = time.time()
    try:
        for i in range(1, N_RUNS + 1):
            print("\n--- C3 RUN %d/%d ---" % (i, N_RUNS))
            rec = run_once(pat, i)
            runs.append(rec)
            print("  run %d: final_rc=%s run_key=%s src_pr=%s fix_pr=%s dur=%ss" %
                  (i, rec.get("final_rc"), rec.get("run_key"), rec.get("src_pr"), rec.get("fix_pr"), rec.get("duration_s")))
    finally:
        cleanup_c2_copy()
    total_dur = int(time.time() - overall_t0)
    post = docker_state()
    print("\n  post-state: %s" % json.dumps(post))

    n_pass = sum(1 for r in runs if r.get("final_rc") == 0)
    runs_pass = n_pass == N_RUNS
    state_stable = pre == post
    all_pass = runs_pass and state_stable
    summary = {
        "gate": "m5-0c-c3", "n_runs": N_RUNS, "n_pass": n_pass, "all_pass": all_pass,
        "final_rc": 0 if all_pass else 1, "total_duration_s": total_dur,
        "docker_state_pre": pre, "docker_state_post": post,
        "state_stable": state_stable,
        "runs": runs,
    }
    print("\n=== C3 SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
