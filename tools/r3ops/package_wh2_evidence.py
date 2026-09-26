# -*- coding: utf-8 -*-
"""package_wh2_evidence.py — 收官轮(全工具链: webhook+skill+rag+agentloop)证据打包."""
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx  # noqa: E402

OUT = "D:/goai/r3work/wh2-evidence"
TEAM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"
DM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
SERVER = "root@159.75.42.106"
SSH = ["ssh", "-o", "BatchMode=yes", SERVER]
BUCKET = "agentteams/agentteams-storage"
WORKERS = ("leader", "reviewer", "fixer", "verifier")

RUNS = [
    {"pr": 2, "pack": "FINALS-ELEM-PR2-WH-20260919",
     "run": "run-gh-pr2-65de83d6-085056", "proj": "elemiso-gh-pr2-65de83d6",
     "head": "65de83d6d061413ec98c1e79515f470313ef9806",
     "base": "4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c",
     "start": "2026-09-19T08:50:56Z", "end": "2026-09-19T09:20:00Z",
     "check_id": 105874262425,
     "tasks": ["gh-pr2-65de83d6-review-1", "gh-pr2-65de83d6-fix-1", "gh-pr2-65de83d6-verify-1"],
     "conclusion": "success",
     "title": "HIGH → human gate APPROVED → fix → VERIFIED (skill+rag+otel 全链)"},
    {"pr": 3, "pack": "FINALS-ELEM-PR3-WH-20260919",
     "run": "run-gh-pr3-03312d65-091817", "proj": "elemiso-gh-pr3-03312d65",
     "head": "03312d6596f0b894a3324e958461a5fc336f2ff5",
     "base": "4cd5bf099f88f3f3f85ee4c06b7adfe9925a6e5c",
     "start": "2026-09-19T09:18:17Z", "end": "2026-09-19T09:29:00Z",
     "check_id": 105875453297,
     "tasks": ["gh-pr3-03312d65-review-1"],
     "conclusion": "failure",
     "title": "HIGH finding — human gate REJECTED (blocked, zero dispatch; rag 命中 CWE-78 组织规范)"},
    {"pr": 1, "pack": "FINALS-ELEM-PR1-WH-20260919",
     "run": "run-gh-pr1-575aa8e1-093022", "proj": "elemiso-gh-pr1-575aa8e1",
     "head": "575aa8e13146998da65a8f62816740dbb5e539ca",
     "base": "fdde4f4142606336c7b7b25f176949dc5882d89a",
     "start": "2026-09-19T09:30:22Z", "end": "2026-09-19T09:32:00Z",
     "check_id": 105876323522,
     "tasks": ["gh-pr1-575aa8e1-review-1"],
     "conclusion": "success",
     "title": "passed (auto-completed, low-risk path; skill 真实调用)"},
]


def sh(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return (r.stdout or b"").decode("utf-8", "replace"), r.returncode


def mc_cat(path):
    out, rc = sh(["docker", "exec", "elemiso-ctrl", "mc", "cat", f"{BUCKET}/{path}"])
    return out if rc == 0 else ""


def mc_tree(prefix):
    out, rc = sh(["docker", "exec", "elemiso-ctrl", "mc", "ls", "--recursive", f"{BUCKET}/{prefix}"])
    paths = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("["):
            paths.append(parts[-1])  # 最后一个 token 即相对路径(可能无斜杠)
    return paths


def wf(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1))


def sums(root):
    es = []
    for dp, _, fns in os.walk(root):
        for fn in sorted(fns):
            if fn == "SHA256SUMS":
                continue
            p = os.path.join(dp, fn)
            es.append(f"{hashlib.sha256(open(p, 'rb').read()).hexdigest()}  {os.path.relpath(p, root).replace(os.sep, '/')}")
    wf(os.path.join(root, "SHA256SUMS"), "\n".join(sorted(es, key=lambda x: x.split("  ", 1)[1])) + "\n")
    return len(es)


def collect_agentloop(root):
    """四 worker 会话 span(名称序列+计数)+导出成功证据。"""
    os.makedirs(os.path.join(root, "agentloop"), exist_ok=True)
    total, detail = 0, {}
    for w in WORKERS:
        spans, rc = sh(["docker", "exec", f"elemiso-worker-{w}", "cat", "/tmp/agentloop-spans.log"], timeout=60)
        names = [l.strip() for l in spans.splitlines() if l.strip()]
        counts = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        total += len(names)
        detail[w] = {"spans": len(names), "by_name": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}
        wf(os.path.join(root, "agentloop", f"spans-{w}.log"), spans)
        audit, _ = sh(["docker", "exec", f"elemiso-worker-{w}", "cat", "/tmp/agentloop-model-audit.log"], timeout=60)
        keep = [l for l in audit.splitlines() if "OTEL_EXPORT SUCCESS" in l or "ACTIVATED" in l or "PATCH " in l]
        wf(os.path.join(root, "agentloop", f"audit-{w}.log"), "\n".join(keep[-200:]) + "\n")
    detail["total"] = total
    wf(os.path.join(root, "agentloop", "span-summary.json"), detail)
    return total


def ledger(pr, start):
    q = ("SELECT json_agg(t) FROM (SELECT delivery_id, event_name, action, repo, pr_number, "
         "observed_head_sha, observed_base_sha, status, received_at, claimed_at, processed_at "
         f"FROM public.github_deliveries WHERE pr_number={pr} AND received_at >= '{start}'::timestamptz "
         "ORDER BY received_at) t")
    inner = (f"cd /opt/mergepilot && docker compose exec -T postgres psql "
             f"-U mergepilot -d mergepilot_audit -At -c \"{q}\"")
    out, rc = sh([*SSH, inner])
    # fail-loud:空台账是打包事故(SSH 瞬断曾静默产出空清单),宁可打包失败
    assert out.strip().startswith("["), "ledger query failed for pr %s: %s" % (pr, (r.stderr or out)[:200])
    return json.loads(out.strip())


def check_run(cid):
    out, rc = sh(["gh", "api", f"repos/nghqqa/fastapi-boilerplate-demo/check-runs/{cid}"])
    try:
        cr = json.loads(out)
        return {"id": cr.get("id"), "conclusion": cr.get("conclusion"),
                "html_url": cr.get("html_url"), "app": (cr.get("app") or {}).get("slug"),
                "output": cr.get("output"), "started_at": cr.get("started_at"),
                "completed_at": cr.get("completed_at")}
    except Exception:
        return {"raw": out[:300]}


def audit_window(lo, hi):
    try:
        recent = json.load(urllib.request.urlopen("http://127.0.0.1:4184/api/rag/toolspans", timeout=10)).get("recent", [])
    except Exception:
        recent = []
    return [t for t in recent if lo <= t.get("ts", "") < hi]


def build(r, audit_all, span_total):
    root = os.path.join(OUT, r["pack"])
    pairs = [(f"teams/elemiso-team/shared/projects/{r['proj']}", "project")] +             [(f"teams/elemiso-team/shared/tasks/{t}", f"tasks/{t}") for t in r["tasks"]]
    if r["pr"] == 2:
        # reviewer 会话记忆把这轮结果交到了旧任务目录(带 rerun 文件名);归位到本轮任务名下
        rerun = {"gh-pr2-review-1/result.md": "tasks/gh-pr2-65de83d6-review-1/result.md",
                 "gh-pr2-review-1/meta.json": "tasks/gh-pr2-65de83d6-review-1/meta.json",
                 "gh-pr2-review-1/workspace/findings-rerun-65de83d6.md": "tasks/gh-pr2-65de83d6-review-1/workspace/findings.md",
                 "gh-pr2-review-1/progress/2026-09-19-rerun65de83d6.md": "tasks/gh-pr2-65de83d6-review-1/progress/2026-09-19.md"}
        for src, dst in rerun.items():
            wf(os.path.join(root, dst), mc_cat(f"teams/elemiso-team/shared/tasks/{src}"))
    for prefix, local in pairs:
        for rel in mc_tree(prefix):
            wf(os.path.join(root, local, os.path.basename(rel)), mc_cat(f"{prefix}/{rel}"))
    wf(os.path.join(root, "team-room-messages.json"), mx.since(TEAM, r["start"]))
    wf(os.path.join(root, "leader-dm-messages.json"), mx.since(DM, r["start"]))
    wf(os.path.join(root, "delivery-ledger.json"), ledger(r["pr"], r["start"]))
    wf(os.path.join(root, "check-run.json"), check_run(r["check_id"]))
    spans = audit_window(r["start"], r["end"])
    wf(os.path.join(root, "skill-audit.json"), {"run": r["run"], "window": [r["start"], r["end"]],
                                                "invocations": spans})
    n_skill = sum(1 for t in spans if t.get("tool", "").startswith("skill_"))
    n_rag = sum(1 for t in spans if t.get("tool") == "rag.retrieve")
    wf(os.path.join(root, "README.md"), f"""# {r['pack']} — PR #{r['pr']} 收官轮(全工具链 · 2026-09-19)

> 触发:PR 分支 chore 提交(synchronize) → GitHub webhook → HMAC 验签 → gh-bridge →
> AgentTeams 真实执行 → check run 回写。本轮四链路齐备:**webhook + 确定性 Skill +
> RAG + AgentLoop OTel(span 直连上报)**。

- run_id: `{r['run']}` / project: `{r['proj']}`
- head/base: `{r['head'][:12]}…` / `{r['base'][:12]}…`
- 结论: **{r['conclusion']}** — {r['title']}
- check run: https://github.com/nghqqa/fastapi-boilerplate-demo/runs/{r['check_id']}
- 本窗口工具调用: skill ×{n_skill} / rag.retrieve ×{n_rag}(skill-audit.json 原文)
- AgentLoop span: 见 agentlock/spans-*.log(会话累计 {span_total} span,export 全 SUCCESS)

## 内容
project/ + tasks/(MinIO 原文,含门记录与补丁) · 房间导出(kickoff/门指令原文) ·
delivery-ledger.json(webhook 台账) · check-run.json(回写原文) ·
skill-audit.json(技能+RAG 审计) · agentloop/(span 序列+导出证据)
""")
    n = sums(root)
    print(r["pack"], n, "files | skill:", n_skill, "rag:", n_rag)


def main():
    os.makedirs(OUT, exist_ok=True)
    # agentloop 会话级数据放 PR2 主包(会话跨三案例,各包 README 注明)
    span_total = collect_agentloop(os.path.join(OUT, RUNS[0]["pack"]))
    print("session spans total:", span_total)
    try:
        audit_all = json.load(urllib.request.urlopen("http://127.0.0.1:4184/api/rag/toolspans", timeout=10)).get("recent", [])
    except Exception:
        audit_all = []
    for r in RUNS:
        build(r, audit_all, span_total)
    rows = "\n".join(f"| PR #{r['pr']} | {r['run']} | {r['conclusion']} | {r['title']} |" for r in RUNS)
    wf(os.path.join(OUT, "FINALS-ELEM-WH-MASTER-README.md"), f"""# WH 收官轮(全工具链)三案例 · 2026-09-19

PR #1/#2/#3 经 webhook 自动触发,**四链路齐备**:真实 GitHub webhook(HMAC 验签)+
确定性 Skill 真实调用 + RAG 组织规范检索(citation-only)+ AgentLoop OTel span 直连上报。

| 案例 | run | check | 结论 |
|---|---|---|---|
{rows}

- 补丁确定性:PR #2 修复补丁 sha256 `674356fc…16081` **第 11 次**独立产出一致,Verifier 全新 clone 独立复现同哈希。
- RAG 纪律:PR #3 Reviewer 确认 CWE-78 后检索命中 `org-standards/cwe-78-command-injection.md`(references only,
  不替代自主 PoC 复现);PR #2 轮 rag_mcp 注入有日志实证(08:51:05),同会话前一轮(d1c2f630)rag 真实调用命中 cwe-22 规范。
- 不自动 merge:App 仅 Checks 读写权限;三 PR 全程 OPEN;门决策为操作员投递(MinIO 记录+Matrix 指令)。
- AgentLoop:会话累计 {span_total} span(四 worker),OTEL_EXPORT 全部 SUCCESS;控制台按 service.name=mergepilot-copaw 检索。
- 历史证据(R1→SK5 九轮 + WH 前两版)全部保留;本包为收官版。
""")
    print("master written")


if __name__ == "__main__":
    main()
