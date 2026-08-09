#!/usr/bin/env python3
"""M5-0D D1 — capture C3 10/10 machine evidence durably.

Runs the COMMITTED tests/m5_0c/c3_runner.py exactly once, captures its full
stdout to a repo-EXTERNAL temp file, extracts the unique `=== C3 SUMMARY ===`
JSON, strictly validates it, and atomically publishes
evidence/m5/0c/c3-10x.json (temp + os.replace, mode 100644).

Any validation failure → final_rc != 0, NO evidence written, hiclaw_live stays
false. source_commit is read from `git rev-parse HEAD` (not caller-supplied).

The pure helpers (extract_summary_json / validate_summary / publish_evidence /
secret_scan) take explicit args and are unit-testable without running c3_runner.
Only capture() touches the live environment (c3_runner subprocess + git + paths).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = "/mnt/d/goai/mergepilot-os"
C3_RUNNER = ROOT + "/tests/m5_0c/c3_runner.py"
EVIDENCE = ROOT + "/evidence/m5/0c/c3-10x.json"
MARKER = "=== C3 SUMMARY ==="
C3_TIMEOUT = 2400  # 10 runs ~21min + cleanup_gh propagation/retry margin; allow 40min

_SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82,}"
    r"|access_token|sync_token|registration_token|HICLAW_REGISTRATION_TOKEN"
    r"|password|private_key|client_secret",
    re.IGNORECASE,
)


def secret_scan(text: str) -> bool:
    return bool(_SECRET_RE.search(text or ""))


def extract_summary_json(stdout: str):
    """Return (obj, error). Exactly one marker; one JSON object after it."""
    if not stdout:
        return None, "empty stdout"
    cnt = stdout.count(MARKER)
    if cnt == 0:
        return None, "marker not found"
    if cnt > 1:
        return None, "multiple markers (%d)" % cnt
    idx = stdout.rfind(MARKER) + len(MARKER)
    tail = stdout[idx:]
    br = tail.find("{")
    if br < 0:
        return None, "no JSON object after marker"
    try:
        obj, _end = json.JSONDecoder().raw_decode(tail[br:])
    except Exception as e:
        return None, "JSON parse failed: %s" % str(e)[:80]
    if not isinstance(obj, dict):
        return None, "summary is not a JSON object"
    return obj, None


def _pass_str(s) -> bool:
    """'X/Y' with X==Y and X>0."""
    if not isinstance(s, str) or "/" not in s:
        return False
    a, b = s.split("/", 1)
    try:
        a, b = int(a), int(b)
    except Exception:
        return False
    return a == b and a > 0


def validate_summary(s: dict, source_commit: str, expected_commit: str):
    """Strict validation per D1 req 3. Returns (ok, [failed_reasons])."""
    fail = []

    def chk(cond, msg):
        if not cond:
            fail.append(msg)

    chk(s.get("gate") == "m5-0c-c3", "gate!=m5-0c-c3")
    chk(s.get("n_runs") == 10, "n_runs!=10")
    chk(s.get("n_pass") == 10, "n_pass!=10")
    chk(s.get("all_pass") is True, "all_pass!=true")
    chk(s.get("final_rc") == 0, "final_rc!=0")
    chk(s.get("state_stable") is True, "state_stable!=true")
    chk(s.get("docker_state_pre") == s.get("docker_state_post"),
        "docker_state_pre!=post")
    chk(isinstance(source_commit, str) and len(source_commit) == 40
        and source_commit == expected_commit,
        "source_commit mismatch/invalid")
    runs = s.get("runs") or []
    chk(isinstance(runs, list) and len(runs) == 10, "runs count!=10")
    if isinstance(runs, list) and len(runs) == 10:
        rks = [r.get("run_key") for r in runs]
        chk(all(isinstance(k, str) and k for k in rks), "run_key empty/non-str")
        chk(len(set(rks)) == 10, "run_key not unique")
        src_prs = [r.get("src_pr") for r in runs]
        fix_prs = [r.get("fix_pr") for r in runs]
        chk(all(src_prs) and len(set(src_prs)) == 10, "src_pr not unique/non-empty")
        chk(all(fix_prs) and len(set(fix_prs)) == 10, "fix_pr not unique/non-empty")
        for i, r in enumerate(runs):
            tag = "run%d" % (i + 1)
            chk(r.get("final_rc") == 0, "%s final_rc!=0" % tag)
            chk(r.get("c2_exit_rc") == 0, "%s c2_exit_rc!=0" % tag)
            chk(_pass_str(r.get("positives")), "%s positives not pass (%s)" % (tag, r.get("positives")))
            chk(_pass_str(r.get("negatives")), "%s negatives not pass (%s)" % (tag, r.get("negatives")))
            chk(r.get("branch_residue") == [], "%s branch_residue!=[]" % tag)
            chk(r.get("openpr_residue") == [], "%s openpr_residue!=[]" % tag)
            dg = r.get("direct_gh")
            chk(isinstance(dg, dict) and all(v == 0 for v in dg.values()),
                "%s direct_gh!=0 (%s)" % (tag, dg))
            chk(r.get("llm") == 0, "%s llm!=0" % tag)
            chk(r.get("secret_hits_all0") is True, "%s secret_hits_all0!=true" % tag)
            dr = r.get("docker_residue") or {}
            chk(dr.get("containers") == 0 and dr.get("networks") == 0,
                "%s docker_residue!=0/0 (%s)" % (tag, dr))
    return (len(fail) == 0), fail


def publish_evidence(s: dict, source_commit: str, path: str):
    """Atomically write evidence JSON (temp + os.replace, mode 100644).
    Returns (ok, error). Aborts if any secret pattern present."""
    payload = dict(s)
    payload["source_commit"] = source_commit
    payload["evidence_kind"] = "m5-0c-c3-10x-stability"
    blob = json.dumps(payload, indent=2, sort_keys=True)
    if secret_scan(blob):
        return False, "secret pattern detected in evidence — refuse to publish"
    d = os.path.dirname(path)
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", prefix=".c3-10x-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False, "publish failed: %s" % str(e)[:120]
    return True, None


def git_head() -> str:
    # -c safe.directory bypasses git's dubious-ownership check when the repo
    # (Windows-owned on /mnt/d) is accessed as root inside MergePilot-Test.
    # Per-command (no global config mutation); read-only rev-parse only.
    r = subprocess.run(
        ["git", "-c", "safe.directory=" + ROOT, "-C", ROOT, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""


def capture(expected_commit: str = None):
    """Run c3_runner once, capture stdout to repo-external temp, validate,
    publish. expected_commit defaults to git HEAD at capture start."""
    source_commit = expected_commit or git_head()
    if not (isinstance(source_commit, str) and len(source_commit) == 40):
        print("FATAL: cannot resolve 40-char git HEAD (got %r)" % source_commit)
        return 2
    # repo-EXTERNAL temp log (MergePilot-Test /tmp, NOT under ROOT)
    fd, log_path = tempfile.mkstemp(prefix="c3-capture-", suffix=".log", dir="/tmp")
    os.close(fd)
    print("=== D1 capture: source_commit=%s ===" % source_commit)
    print("=== D1 capture: running committed c3_runner (full stdout -> %s) ===" % log_path)
    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            r = subprocess.run(["python3", C3_RUNNER], stdout=logf,
                               stderr=subprocess.STDOUT, timeout=C3_TIMEOUT,
                               env=dict(os.environ))
        c3_rc = r.returncode
    except subprocess.TimeoutExpired:
        print("FAIL: c3_runner timed out after %ds" % C3_TIMEOUT)
        return 2
    except Exception as e:
        print("FAIL: c3_runner invocation error: %s" % str(e)[:160])
        return 2
    print("=== D1 capture: c3_runner exit rc=%d ===" % c3_rc)
    try:
        stdout = open(log_path, encoding="utf-8", errors="replace").read()
    finally:
        try:
            os.remove(log_path)
        except Exception:
            pass
    obj, err = extract_summary_json(stdout)
    if err:
        print("FAIL: extract — %s" % err)
        print("  (stdout tail): %s" % stdout[-400:].replace("\n", " | "))
        return 1
    ok, failed = validate_summary(obj, source_commit, source_commit)
    if not ok:
        print("FAIL: validation — %s" % "; ".join(failed))
        return 1
    ok, perr = publish_evidence(obj, source_commit, EVIDENCE)
    if not ok:
        print("FAIL: publish — %s" % perr)
        return 1
    print("PASS: evidence published %s (source_commit=%s, 10/10, state_stable)" % (EVIDENCE, source_commit))
    return 0


if __name__ == "__main__":
    sys.exit(capture())
