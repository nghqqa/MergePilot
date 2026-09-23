"""真实案例运行前门禁(CASE1 GATE):只读核查,不启动任何服务或模型。

对照 CASE1-RUNBOOK §7 四项确认 + 技术前置,逐项输出 PASS/BLOCKED。
全 PASS 才允许进入真实执行;任何 BLOCKED 输出缺口描述。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools" / "orchestrator"))
sys.path.insert(0, str(_ROOT / "tools" / "approval" / "pg"))

from prerun_gate import run_gate, gate_passed, format_report  # noqa: E402


def _git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True,
                       cwd=str(_ROOT), timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def _sha(p):
    import hashlib
    return hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None


def check_all(dsn: str):
    checks = []

    # ── 1. Git 基线 ────────────────────────────────────────────────────────
    head = _git("rev-parse", "HEAD")
    clean = not _git("status", "--porcelain")
    checks.append(("git_baseline", bool(head) and clean,
                   "HEAD=%s clean=%s" % (head[:12], clean)))

    # ── 2. 运行副本同步 ────────────────────────────────────────────────────
    repo_bridge = _ROOT / "tools" / "gh-bridge" / "gh_bridge.py"
    run_bridge = Path("D:/goai/r3work/scripts/gh_bridge.py")
    repo_sha = _sha(str(repo_bridge))
    run_sha = _sha(str(run_bridge))
    checks.append(("run_copy_synced", repo_sha is not None and repo_sha == run_sha,
                   "repo=%s run=%s" % ((repo_sha or "?")[:12], (run_sha or "?")[:12])))

    # ── 3. RAG 语料快照 ────────────────────────────────────────────────────
    sys.path.insert(0, str(_ROOT / "tools" / "rag"))
    spec = importlib.util.spec_from_file_location("ct", _ROOT / "tools" / "rag" / "corpus_tool.py")
    ct = importlib.util.module_from_spec(spec)
    sys.modules["ct"] = ct
    spec.loader.exec_module(ct)
    corpus = _ROOT / "tools" / "rag" / "corpus" / "org-security-knowledge-v1.json"
    run_corpus = Path("D:/goai/r3work/rag-live/rag-live-corpus.json")
    repo_snap = ct.snapshot_id(ct.load(str(corpus))) if corpus.is_file() else None
    run_snap = ct.snapshot_id(ct.load(str(run_corpus))) if run_corpus.is_file() else None
    checks.append(("rag_snapshot", repo_snap is not None and repo_snap == run_snap,
                   "repo=%s run=%s" % ((repo_snap or "?")[:12], (run_snap or "?")[:12])))

    # ── 4. 隔离 PG 就绪 ────────────────────────────────────────────────────
    pg_ok = False
    pg_detail = "未检查"
    try:
        import psycopg2
        conn = psycopg2.connect(_DSN, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        pg_ok = cur.fetchone() is not None
        pg_detail = "reachable"
        conn.close()
    except Exception as e:
        pg_detail = str(e)[:100]
    checks.append(("isolated_pg", pg_ok, pg_detail))

    # ── 5. 本地栈容器 ──────────────────────────────────────────────────────
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=10)
        names = set(r.stdout.splitlines())
        needed = ["elemiso-ctrl", "elemiso-worker-leader", "elemiso-worker-reviewer",
                  "elemiso-worker-fixer", "elemiso-worker-verifier",
                  "elemiso-element-web", "elemiso-proxy", "elemiso-case-pg"]
        missing = [n for n in needed if n not in names]
        checks.append(("stack_containers", not missing,
                       "全部在位" if not missing else "缺失: %s" % ", ".join(missing)))
    except Exception as e:
        checks.append(("stack_containers", False, str(e)[:100]))

    # ── 6. GitHub PR 可达(匿名只读) ────────────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.github.com/repos/nghqqa/fastapi-boilerplate-demo/pulls/9",
            headers={"User-Agent": "mergepilot-prerun"})
        with urllib.request.urlopen(req, timeout=10) as r:
            pr = json.loads(r.read())
        checks.append(("github_pr_reachable", pr.get("state") == "open",
                       "PR#%d state=%s head=%s" % (
                           pr.get("number"), pr.get("state"),
                           (pr.get("head") or {}).get("sha", "?")[:12])))
    except Exception as e:
        checks.append(("github_pr_reachable", False, str(e)[:100]))

    # ── 7. 预算硬上限 ──────────────────────────────────────────────────────
    budget = os.environ.get("MERGEPILOT_RUN_BUDGET_TOKENS", "")
    provider_cap = os.environ.get("MERGEPILOT_PROVIDER_CAP_CONFIRMED", "")
    if budget and provider_cap == "1":
        checks.append(("budget_hard_cap", True,
                       "tokens=%s provider_cap=confirmed" % budget))
    elif budget and not provider_cap:
        checks.append(("budget_hard_cap", False,
                       "MERGEPILOT_RUN_BUDGET_TOKENS 已设但 provider 侧硬上限未确认"))
    else:
        checks.append(("budget_hard_cap", False,
                       "未配置预算硬上限(需 D-4 拍板 + provider 侧确认)"))

    # ── 8. 凭证状态 ────────────────────────────────────────────────────────
    cred_rot = os.environ.get("MERGEPILOT_CRED_ROTATION_DONE", "")
    if cred_rot == "1":
        checks.append(("credentials", True, "轮换已完成"))
    elif cred_rot == "skip":
        checks.append(("credentials", False,
                       "用户明确跳过轮换——风险已知,需书面确认"))
    else:
        checks.append(("credentials", False,
                       "轮换状态未确认(挂起中)——需用户决定"))

    # ── 9. rag-live 服务 ───────────────────────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:4184/health")
        with urllib.request.urlopen(req, timeout=3) as r:
            h = json.loads(r.read())
        checks.append(("rag_live_running", h.get("ok") is True,
                       "chunks=%s" % h.get("chunks")))
    except Exception:
        checks.append(("rag_live_running", False,
                       "rag-live :4184 不可达(需启动,属 R7 授权范围)"))

    return checks


def main():
    checks = check_all(_DSN := "")
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print("== CASE1 PRE-RUN GATE ==")
    print("通过: %d/%d\n" % (passed, total))
    for name, ok, detail in checks:
        print("  [%s] %s: %s" % ("PASS" if ok else "BLOCKED", name, detail))
    if passed < total:
        print("\n结论: %d 项 BLOCKED——不可启动真实案例" % (total - passed))
        print("缺口需用户逐项确认(见 CASE1-RUNBOOK §7 和 AUTH-DECISION-PACKAGE)")
        return 1
    print("\n结论: 全部通过——可进入真实执行(仍需用户最终批准)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
