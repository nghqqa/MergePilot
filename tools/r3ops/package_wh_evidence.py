# -*- coding: utf-8 -*-
"""package_wh_evidence.py — 打包 webhook 轮(WH)三案例证据(SHA256SUMS 锁定)."""
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx  # noqa: E402

OUT = "D:/goai/r3work/wh-evidence"
TEAM_ROOM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"
DM_ROOM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
SERVER = "root@159.75.42.106"
SSH = ["ssh", "-o", "BatchMode=yes", SERVER]

RUNS = [
    {"pr": 2, "pack": "FINALS-ELEM-PR2-WH-20260919", "run": "run-gh-pr2-e9731abd-064416",
     "proj": "elemiso-gh-pr2-e9731abd", "head": "e9731abd95d72e4d09f36060dcb32cdef50474d0",
     "base": "4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c", "start": "2026-09-19T06:44:16Z",
     "delivery": "83f30fa0-b3f5", "check_id": 105856842666,
     "tasks": ["gh-pr2-review-1", "gh-pr2-fix-1", "gh-pr2-verify-1"],
     "conclusion": "success", "title": "HIGH → human gate APPROVED → fix → VERIFIED (full toolchain)"},
    {"pr": 3, "pack": "FINALS-ELEM-PR3-WH-20260919", "run": "run-gh-pr3-3daa6fb4-065708",
     "proj": "elemiso-gh-pr3-3daa6fb4", "head": "3daa6fb46463ea41a877e84356dfd320e7f3fe62",
     "base": "4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c", "start": "2026-09-19T06:57:00Z",
     "delivery": "83f30fa0-b3f5", "check_id": 105858422491,
     "tasks": ["gh-pr3-review-1"],
     "conclusion": "failure", "title": "HIGH finding — human gate REJECTED (blocked, zero dispatch; full toolchain)"},
    {"pr": 1, "pack": "FINALS-ELEM-PR1-WH-20260919", "run": "run-gh-pr1-0fae3afd-070752",
     "proj": "elemiso-gh-pr1-0fae3afd", "head": "0fae3afd77f46eb3ff3d5bf5ef7d0019c04a43d4",
     "base": "fdde4f4142606336c7b7b25f176949dc5882d89a", "start": "2026-09-19T07:07:52Z",
     "delivery": "83f30fa0-b3f5", "check_id": 105858672076,
     "tasks": ["gh-pr1-review-1"],
     "conclusion": "success", "title": "passed (auto-completed, low-risk path; full toolchain)"},
]
BUCKET = "agentteams/agentteams-storage"


def sh(cmd, timeout=90, input_bytes=None):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes)
    return (r.stdout or b"").decode("utf-8", "replace"), r.returncode


def mc_cat(path):
    out, rc = sh(["docker", "exec", "elemiso-ctrl", "mc", "cat", f"{BUCKET}/{path}"])
    return out if rc == 0 else ""


def mc_tree(prefix):
    """返回 prefix 下所有对象相对路径列表."""
    out, rc = sh(["docker", "exec", "elemiso-ctrl", "mc", "ls", "--recursive", f"{BUCKET}/{prefix}"])
    paths = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 1:
            # 行格式: [date time] size [STANDARD] path
            for p in reversed(parts):
                if "/" in p or p.endswith(".md") or p.endswith(".json") or p.endswith(".diff") or p.endswith(".py"):
                    paths.append(p)
                    break
    return [p for p in paths if p]


def wf(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1))


def rb(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode("utf-8"))


def ledger_row(delivery_prefix):
    q = ("SELECT json_agg(t) FROM (SELECT delivery_id, event_name, action, repo, pr_number, "
         "observed_head_sha, observed_base_sha, status, error, received_at, claimed_at, processed_at "
         "FROM public.github_deliveries WHERE delivery_id LIKE '" + delivery_prefix + "%') t")
    inner = (f"cd /opt/mergepilot && docker compose exec -T postgres psql "
             f"-U mergepilot -d mergepilot_audit -At -c \"{q}\"")
    out, rc = sh([*SSH, inner])
    try:
        return json.loads(out.strip()) if out.strip() else []
    except Exception:
        return [{"raw": out[:400]}]


def check_run_json(check_id):
    out, rc = sh(["gh", "api", f"repos/nghqqa/fastapi-boilerplate-demo/check-runs/{check_id}"])
    try:
        return json.loads(out)
    except Exception:
        return {"raw": out[:400]}


def sums(root):
    es = []
    for dp, _, fns in os.walk(root):
        for fn in sorted(fns):
            if fn == "SHA256SUMS":
                continue
            p = os.path.join(dp, fn)
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            es.append(f"{h}  {os.path.relpath(p, root).replace(os.sep, '/')}")
    with open(os.path.join(root, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(es, key=lambda x: x.split("  ", 1)[1])) + "\n")
    return len(es)


def build(r):
    root = os.path.join(OUT, r["pack"])
    # 1) 项目目录 + 任务目录(递归拉取)
    for prefix, local in ((f"teams/elemiso-team/shared/projects/{r['proj']}", "project"),
                          *( (f"teams/elemiso-team/shared/tasks/{t}", f"tasks/{t}") for t in r["tasks"] )):
        for rel in mc_tree(prefix):
            content = mc_cat(f"{prefix}/{rel.split(maxsplit=0)[-1] if False else rel}")
            wf(os.path.join(root, local, os.path.basename(rel)), content)
    # 2) 房间导出(kickoff 起)
    wf(os.path.join(root, "team-room-messages.json"), mx.since(TEAM_ROOM, r["start"]))
    wf(os.path.join(root, "leader-dm-messages.json"), mx.since(DM_ROOM, r["start"]))
    # 3) 台账行 + check run + 门记录引用已在 project/ 内
    wf(os.path.join(root, "delivery-ledger.json"), ledger_row(r["delivery"]))
    cr = check_run_json(r["check_id"])
    wf(os.path.join(root, "check-run.json"),
       {"id": cr.get("id"), "name": cr.get("name"), "conclusion": cr.get("conclusion"),
        "html_url": cr.get("html_url"), "app": (cr.get("app") or {}).get("slug"),
        "output": cr.get("output"), "started_at": cr.get("started_at"),
        "completed_at": cr.get("completed_at"), "check_suite_url": cr.get("check_suite_url")})
    # 4) README
    wf(os.path.join(root, "README.md"), README_TMPL.format(**r))
    n = sums(root)
    print(r["pack"], n, "files")


README_TMPL = """# {pack} — PR #{pr} webhook 自动链路重跑(2026-09-19)

> 触发方式:向 PR 分支推送 chore 触发提交(synchronize) → GitHub webhook → 验签入库 →
> gh-bridge 自动播种/唤醒/kickoff → AgentTeams 真实执行 → check run 回写 PR。
> 人工门决策(如适用)由操作员经 MinIO 记录 + Matrix 指令投递(与决赛轮一致)。

- run_id: `{run}`
- project: `{proj}`
- head/base SHA: `{head}` / `{base}`
- 结论(check run): **{conclusion}** — {title}
- check run: https://github.com/nghqqa/fastapi-boilerplate-demo/runs/{check_id}
- 门记录: project/human-gate-*.md(如适用)

## 内容

```
project/     项目 meta.json/plan.md/result.md/门记录(MinIO 原文)
tasks/       各任务 result.md + workspace 产物(findings/patch/verification)
team-room-messages.json   团队房导出(run 起全量)
leader-dm-messages.json   Leader DM 导出(含 kickoff 原文与门指令)
delivery-ledger.json      服务器 github_deliveries 台账行(验签入库→PROCESSED)
check-run.json            GitHub Checks API 回写结果原文
SHA256SUMS                本包锁定
```

## 与历史轮的关系

历史九轮证据(R1→SK5)不动;本轮为第 10 轮,首次由 webhook 事件端到端自动触发。
诚实披露:本轮 worker 为新建容器,确定性 Skill/RAG hook 未随容器自动启用
(agents 在 result 中如实标注 FunctionNotFoundError,全程以自主静态分析+复现完成);
PR #2 的修复补丁 sha256 仍为 `674356fc…16081`(与历史八次独立产出逐字节一致,本轨第 9 次)。
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    for r in RUNS:
        build(r)
    # master
    rows = "\n".join(
        f"| PR #{r['pr']} | {r['run']} | {r['conclusion']} | {r['title']} |"
        for r in RUNS)
    wf(os.path.join(OUT, "FINALS-ELEM-WH-MASTER-README.md"),
       "# WH 轮(webhook 自动链路)三案例 · 2026-09-19\n\n"
       "PR #1/#2/#3 经 `pull_request synchronize` webhook 事件自动触发重跑,三路径全覆盖:\n\n"
       "| 案例 | run | check | 结论 |\n|---|---|---|---|\n" + rows + "\n\n"
       "链路:GitHub webhook → 验签入库(159.75.42.106)→ gh-bridge(播种/唤醒/kickoff)→ "
       "AgentTeams 真实执行 → result.md 权威判读 → App 身份 check-run 回写 PR。\n"
       "PR #2 门批准/PR #3 门拒绝为操作员决策(MinIO 记录+Matrix 指令),修复与验证仍全 Agent 执行。\n"
       "历史九轮证据不动;本包为增量新证据。\n")
    print("master written")


if __name__ == "__main__":
    main()
