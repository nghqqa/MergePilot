# -*- coding: utf-8 -*-
"""gh_bridge.py — Phase 3 lite bridge: github_deliveries(PENDING) → AgentTeams kickoff → check-run 回写.

链路(每段均已单独验证):
  服务器 PG PENDING 行 --SSH--> 本桥 --Matrix kickoff--> 本机 elemiso 栈 Leader
  → Reviewer 独立审查(真实执行) → 桥监听团队房/Leader DM 终态
  → SSH 到服务器 mp-checks-reporter 容器以 App 身份 POST check-run → delivery=PROCESSED.

设计约束:
  * 不跑 P14 controller,不改服务器任何 schema/配置;交付行只用 CAS 认领
    (status PENDING→RUNNING→PROCESSED/ERROR,复用 github_drain 的 claim 语义).
  * SPEC 自包含(不依赖预播种的 PR-METADATA.md);kickoff 格式沿用决赛九次验证的
    send_kickoff_pr1sk5 模板,项目由控制面从 kickoff 消息自动创建.
  * HIGH → action_required(人工门停等,门决策仍属操作员);NOT_CONFIRMED/LOW → success
    (低风险自动路径,PR1SK5 同构);超时 → neutral.

用法:
  python gh_bridge.py status            # 查看待处理交付
  python gh_bridge.py run [--once] [--timeout-min 20] [--dry-run]
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, r"D:\goai\r3work\scripts")
import matrix as mx  # noqa: E402

SERVER = "root@159.75.42.106"
COMPOSE_DIR = "/opt/mergepilot"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

LEADER = "@leader:elemiso-matrix:6167"
DM_ROOM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"      # Leader DM(admin↔leader)
TEAM_ROOM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"     # Team room
ALLOW_REPOS = {"nghqqa/fastapi-boilerplate-demo"}
REPO_URL = "https://github.com/nghqqa/fastapi-boilerplate-demo.git"

WATCH_POLL_S = 10


# ── 服务器 PG 通道(SSH + docker compose exec psql;零新增攻击面) ────────────
def ssh_psql(sql, as_json=False):
    """在服务器 postgres 容器执行 SQL;-At 输出;as_json 时解析为 JSON 对象列表."""
    q = sql.replace("'", "'\\''")
    inner = (f"cd {COMPOSE_DIR} && docker compose exec -T postgres "
             f"psql -U mergepilot -d mergepilot_audit -At -c '{q}'")
    r = subprocess.run(["ssh", *SSH_OPTS, SERVER, inner],
                       capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        raise RuntimeError("ssh/psql failed: " + (r.stderr or r.stdout)[:300])
    out = r.stdout.strip()
    if not as_json:
        return out
    if not out:
        return []
    return json.loads(out)


def pending_deliveries():
    return ssh_psql(
        "SELECT json_agg(t) FROM (SELECT delivery_id, event_name, action, repo, "
        "pr_number, observed_head_sha, observed_base_sha, received_at "
        "FROM public.github_deliveries "
        "WHERE status='PENDING' AND event_name='pull_request' "
        "AND action IN ('opened','synchronize','reopened') "
        "AND repo IS NOT NULL ORDER BY received_at) t", as_json=True)


def claim(d):
    """CAS 认领:PENDING→RUNNING。rowcount=1 才继续(并发安全)."""
    q = ("UPDATE public.github_deliveries SET status='RUNNING', claim_id='%s', "
         "claimed_at=now() WHERE delivery_id='%s' AND status='PENDING'"
         % (d["delivery_id"][:24] + "-bridge", d["delivery_id"]))
    return ssh_psql(q) == "UPDATE 1"


def finish(d, ok, note=""):
    status = "PROCESSED" if ok else "ERROR"
    q = ("UPDATE public.github_deliveries SET status='%s', processed_at=now(), "
         "error='%s' WHERE delivery_id='%s' AND claim_id LIKE '%%-bridge'"
         % (status, note.replace("'", " ")[:180], d["delivery_id"]))
    return ssh_psql(q)


# ── 本机 AgentTeams 栈:播种项目 + 拉起 worker + 发 kickoff + 监听终态 ──────
BUCKET = "agentteams/agentteams-storage"


def minio_put(path, content):
    """经 ctrl 容器内 mc 客户端写共享存储(与决赛 send_gate_decision 同通道)."""
    env = dict(__import__("os").environ, MSYS_NO_PATHCONV="1")
    p = subprocess.run(["docker", "exec", "-i", "elemiso-ctrl", "mc", "pipe",
                        f"{BUCKET}/{path}"],
                       input=content.encode("utf-8"), env=env, capture_output=True)
    return p.returncode == 0


def seed_project(proj, task, d, run):
    """播种项目两件套(meta.json + plan.md);任务目录由 Leader 的 delegate_task 创建."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {"project_id": proj, "title": f"PR #{d['pr_number']} webhook review ({run})",
            "status": "pending", "source": "matrix", "requester": "admin"}
    plan = (
        f"# Team Project: PR #{d['pr_number']} webhook review ({run})\n\n"
        f"**ID**: {proj}\n**Created**: {ts}\n\n"
        f"## DAG Task Plan\n\n**Plan Type**: dag\n\n"
        f"- [ ] {task} — Independent security review of PR #{d['pr_number']} "
        f"(assigned: @reviewer:elemiso-matrix:6167)\n")
    base = f"teams/elemiso-team/shared/projects/{proj}"
    return (minio_put(f"{base}/meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
            and minio_put(f"{base}/plan.md", plan))


def wake_workers(names=("leader", "reviewer", "fixer", "verifier")):
    for n in names:
        # agt worker wake 只认 --name(status 才认 --team,CLI 不一致)
        subprocess.run(["docker", "exec", "elemiso-ctrl", "agt", "worker", "wake",
                        "--name", n],
                       capture_output=True, text=True, timeout=120)
    # 等待容器落位
    for _ in range(24):
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
        if all(any(n in line for line in r.stdout.splitlines()) for n in names):
            return True
        time.sleep(5)
    return False


def build_kickoff(d):
    run = "run-gh-pr%d-%s-%s" % (d["pr_number"], d["observed_head_sha"][:8],
                                 time.strftime("%H%M%S", time.gmtime()))
    proj = "elemiso-gh-pr%d-%s" % (d["pr_number"], d["observed_head_sha"][:8])
    task = "gh-pr%d-review-1" % d["pr_number"]
    ws = "ghwork-" + run
    spec = (
        f"{task} / {run} - Independent security review of PR #{d['pr_number']} "
        f"({d['repo']}, webhook-triggered).\n"
        f"1) taskflow(ack_task) taskId \"{task}\".\n"
        f"2) Workspace setup (self-contained; no pre-seeded metadata):\n"
        f"   cd ~ && git clone --quiet {REPO_URL} {ws}\n"
        f"   cd ~/{ws} && git checkout --quiet {d['observed_head_sha']} && git rev-parse HEAD "
        f"(MUST equal {d['observed_head_sha']}; else stop and report BLOCKED)\n"
        f"   git diff --stat {d['observed_base_sha']}..{d['observed_head_sha']}  "
        f"# this IS the PR change set; review the changed files only\n"
        f"3) Independent review: read changed code; if you suspect a vulnerability, "
        f"write and run your own PoC against the checked-out tree; run the PR's own "
        f"tests if present. Deterministic skills available via MCP "
        f"(skill_diff_parse / skill_risk_classify / skill_sast_scan / skill_case_retrieval "
        f"- advisory only, never replace your own judgment). rag_retrieve provides org "
        f"standards (references only).\n"
        f"4) taskflow(submit_task) with YOUR independent conclusion, including exactly: "
        f"STATUS: FINDING_CONFIRMED|NOT_CONFIRMED; SEVERITY: HIGH|MEDIUM|LOW; "
        f"HUMAN_VERIFICATION_REQUIRED: YES|NO; plus evidence (PoC outputs / file:line).\n"
        f"5) Reply in team room: TASK_COMPLETED: {run}-review\n"
        f"Constraints: no repo modification; zero GitHub writes; your own workspace only.")
    kickoff = (
        f"[kickoff {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {run} - project {proj}.\n\n"
        f"You are the Team Leader. The DAG has ONE review task. Do NOT create projects/tasks beyond it.\n\n"
        f"1. projectflow(ready_nodes) projectId \"{proj}\" -> expect {task}.\n"
        f"2. taskflow(delegate_task) projectId \"{proj}\" taskId \"{task}\" "
        f"roomId \"room:{TEAM_ROOM}\" spec = SPEC below.\n"
        f"3. Wait; check_task. If reviewer reports SEVERITY HIGH with "
        f"HUMAN_VERIFICATION_REQUIRED YES: STOP at the human security gate "
        f"(do NOT delegate any fix), message me the final report and wait. "
        f"If NOT_CONFIRMED or LOW (gate not required): mark the node completed, "
        f"message me the final report.\n\n"
        f"=== SPEC ({task} -> @reviewer) ===\n{spec}")
    return run, proj, task, kickoff


def project_status(proj):
    """轮询项目 meta.json 的权威生命周期状态(completed/blocked/...)."""
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/teams/elemiso-team/shared/projects/{proj}/meta.json"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout).get("status")
    except Exception:
        return None


def project_result(proj):
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/teams/elemiso-team/shared/projects/{proj}/result.md"],
                       capture_output=True, text=True, timeout=30)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def gate_record(proj, kind):
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/teams/elemiso-team/shared/projects/{proj}/human-gate-{kind}.md"],
                       capture_output=True, text=True, timeout=30)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def watch_run(run, proj, deadline_ts):
    """终态以项目 meta.json 权威状态为准(completed/blocked);房间消息仅作进度线索."""
    t0_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    seen, report = set(), None
    while time.time() < deadline_ts:
        st = project_status(proj)
        if st in ("completed", "blocked", "cancelled"):
            # 终态后再宽限数秒,让 result.md/gate 记录发布完整
            for _ in range(6):
                if project_result(proj):
                    break
                time.sleep(5)
            return st, report or ""
        for rid in (TEAM_ROOM, DM_ROOM):
            try:
                evs = mx.since(rid, t0_iso)
            except Exception:
                continue
            for ev in evs:
                if ev["event_id"] in seen:
                    continue
                seen.add(ev["event_id"])
                body = ev["body"]
                if ((run in body or proj in body) and ev["sender"].startswith("@leader")
                        and len(body) > 300
                        and any(k in body for k in ("已完成", "状态报告", "最终报告"))):
                    report = body
        time.sleep(WATCH_POLL_S)
    return "timeout", report or ""


# ── 回写:经服务器 reporter 容器以 App 身份 POST check-run ───────────────────
CHECK_PAYLOAD_SCRIPT = r'''
import base64, json, sys, urllib.request
from token_provider import GitHubAppTokenProvider, TokenProviderConfig
p = GitHubAppTokenProvider(TokenProviderConfig.from_env())
tok = p.get_token()
spec = json.loads(base64.b64decode(sys.argv[1]).decode())
req = urllib.request.Request('https://api.github.com/repos/%s/check-runs' % spec['repo'],
    data=json.dumps(spec['body']).encode(), method='POST',
    headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json',
             'Content-Type': 'application/json'})
r = urllib.request.urlopen(req, timeout=20)
d = json.load(r)
print(json.dumps({'http': r.status, 'check_run_id': d['id'], 'url': d['html_url']}))
'''


def post_check(d, verdict, report, run):
    concl, title = {
        "pass": ("success", "MergePilot review: passed (auto-completed)"),
        "pass_verified": ("success",
                          "MergePilot review: HIGH → human gate APPROVED → fix → VERIFIED"),
        "high": ("action_required", "MergePilot review: HIGH finding — human gate required"),
        "rejected": ("failure",
                     "MergePilot review: HIGH finding — human gate REJECTED (blocked, zero fix/verify dispatch)"),
        "gate": ("action_required", "MergePilot review: stopped at human gate"),
        "timeout": ("neutral", "MergePilot review: bridge timeout (manual check needed)"),
    }[verdict]
    excerpt = (report or "").strip().replace("`", "'").replace("\r", " ")
    excerpt = re.sub(r"\n+", " | ", excerpt)[:600]
    summary = (f"run_id: {run}\nverdict: {verdict}\n"
               f"triggered by: pull_request {d['action']} #{d['pr_number']} @ "
               f"{d['observed_head_sha'][:12]}\n\nleader report (excerpt):\n{excerpt}")
    payload = {"repo": d["repo"], "body": {
        "name": "mergepilot/review",
        "head_sha": d["observed_head_sha"],
        "status": "completed", "conclusion": concl,
        "output": {"title": title, "summary": summary[:60000]}}}
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    s64 = base64.b64encode(CHECK_PAYLOAD_SCRIPT.encode()).decode()
    # 脚本与载荷全部 base64,单行无引号——免疫 ssh/bash 转义
    cmd = (f"cd {COMPOSE_DIR} && docker exec mp-checks-reporter python -c "
           f"\"import base64;exec(base64.b64decode('{s64}').decode())\" {b64}")
    r = subprocess.run(["ssh", *SSH_OPTS, SERVER, cmd],
                       capture_output=True, text=True, timeout=90)
    out = (r.stdout or "").strip() or (r.stderr or "").strip()[:200]
    return out


# ── 主流程 ──────────────────────────────────────────────────────────────────
def process(d, timeout_min, dry):
    if d["repo"] not in ALLOW_REPOS:
        finish(d, False, "repo not in bridge allowlist")
        return
    if not claim(d):
        return  # 被并发认领
    run, proj, task, kickoff = build_kickoff(d)
    log = lambda *a: print(time.strftime("[%H:%M:%S]"), *a, flush=True)
    log(f"claimed {d['delivery_id'][:18]} PR#{d['pr_number']} {d['action']} -> {run}")
    if dry:
        log("DRY-RUN kickoff:\n" + kickoff)
        finish(d, False, "dry-run (no dispatch)")
        return
    if not seed_project(proj, task, d, run):
        finish(d, False, "project seeding failed (mc pipe)")
        log("project seeding FAILED — delivery marked ERROR")
        return
    log(f"seeded project {proj} (meta.json + plan.md)")
    if not wake_workers():
        finish(d, False, "worker wake failed")
        log("worker wake FAILED — delivery marked ERROR")
        return
    log("workers awake; sending kickoff to leader DM")
    r = mx.send(DM_ROOM, LEADER, kickoff, txn_prefix="ghbridge")
    if not r.get("event_id"):
        finish(d, False, "kickoff send failed: " + str(r.get("error"))[:120])
        return
    log("kickoff sent:", r["event_id"])
    st, report = watch_run(run, proj, time.time() + timeout_min * 60)
    # 终态→结论,以权威产物(meta.json 状态 + result.md + gate 记录)裁决
    if st == "completed":
        verdict = "pass_verified" if gate_record(proj, "approval") else "pass"
    elif st == "blocked":
        verdict = "rejected"
    else:
        verdict = "timeout"
    evidence = "\n\n".join(x for x in [
        project_result(proj),
        "## gate record\n" + (gate_record(proj, "approval") or gate_record(proj, "rejection")),
        "## leader report\n" + (report or ""),
    ] if x.strip())
    log("terminal:", st, "-> verdict:", verdict)
    cr = post_check(d, verdict, evidence, run)
    log("check-run:", cr)
    finish(d, st in ("completed", "blocked"), f"{st}/{verdict}; {cr[:150]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "run"])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--timeout-min", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        for d in pending_deliveries():
            print(json.dumps(d, ensure_ascii=False))
        return
    while True:
        for d in pending_deliveries():
            try:
                process(d, a.timeout_min, a.dry_run)
            except Exception as e:
                print("ERROR processing delivery:", type(e).__name__, str(e)[:200], flush=True)
                try:
                    finish(d, False, "bridge exception: " + str(e)[:150])
                except Exception:
                    pass
        if a.once:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
