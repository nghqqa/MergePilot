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
import os
import re
import subprocess
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.normpath(os.path.join(_HERE, "..", "r3ops")),
              r"D:\goai\r3work\scripts"):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break
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


def already_processed(d):
    """同 repo+PR+head 已有 PROCESSED 投递 → 跳过(重复 webhook/重发不重复执行)."""
    q = ("SELECT count(*) FROM public.github_deliveries WHERE repo='%s' "
         "AND pr_number=%s AND observed_head_sha='%s' AND status='PROCESSED'"
         % (d["repo"], int(d["pr_number"]), d["observed_head_sha"]))
    return ssh_psql(q) not in ("0", "")


def claim(d):
    """CAS 认领:PENDING→RUNNING。rowcount=1 才继续(并发安全)。

    claim_id 每次认领轮换(对照 github_drain 合同):含 8 hex 随机尾,
    终结(finish)必须精确匹配本次 claim_id,防旧执行者覆盖新执行者状态。"""
    cid = "%s-bridge-%s" % (d["delivery_id"][:14], uuid.uuid4().hex[:8])
    q = ("UPDATE public.github_deliveries SET status='RUNNING', claim_id='%s', "
         "claimed_at=now() WHERE delivery_id='%s' AND status='PENDING'"
         % (cid, d["delivery_id"]))
    return cid if ssh_psql(q) == "UPDATE 1" else None


def finish(d, ok, note="", cid=None):
    """终结投递。带 cid 时精确匹配(租约正确性);不带时仅限认领前拒绝路径。"""
    status = "PROCESSED" if ok else "ERROR"
    where = "delivery_id='%s'" % d["delivery_id"]
    if cid:
        where += " AND claim_id='%s'" % cid
    else:
        where += " AND claim_id LIKE '%-bridge%'"
    q = ("UPDATE public.github_deliveries SET status='%s', processed_at=now(), "
         "error='%s' WHERE %s" % (status, note.replace("'", " ")[:180], where))
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
    """播种项目两件套(meta.json + plan.md);三节点 DAG(与决赛 PR2 同构)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {"project_id": proj, "title": f"PR #{d['pr_number']} webhook review ({run})",
            "status": "pending", "source": "matrix", "requester": "admin"}
    plan = (
        f"# Team Project: PR #{d['pr_number']} webhook review ({run})\n\n"
        f"**ID**: {proj}\n**Created**: {ts}\n\n"
        f"## DAG Task Plan\n\n**Plan Type**: dag\n\n"
        f"- [ ] {task} — Independent security review of PR #{d['pr_number']} "
        f"(assigned: @reviewer:elemiso-matrix:6167)\n"
        f"- [ ] gh-pr{d['pr_number']}-{d['observed_head_sha'][:8]}-fix-1 — Minimal fix only if human gate approves "
        f"(assigned: @fixer:elemiso-matrix:6167, depends: {task})\n"
        f"- [ ] gh-pr{d['pr_number']}-{d['observed_head_sha'][:8]}-verify-1 — Independent verification only if fix "
        f"authorized and accepted (assigned: @verifier:elemiso-matrix:6167, "
        f"depends: gh-pr{d['pr_number']}-{d['observed_head_sha'][:8]}-fix-1)\n")
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
    # 任务 ID 必须跨轮唯一(shared/tasks/ 是扁平命名空间,同 PR 重跑会撞上轮目录)
    task = "gh-pr%d-%s-review-1" % (d["pr_number"], d["observed_head_sha"][:8])
    ws = "ghwork-" + run
    spec = (
        f"{task} / {run} - Independent security review of PR #{d['pr_number']} "
        f"({d['repo']}, webhook-triggered).\n"
        f"1) taskflow(ack_task) taskId \"{task}\".\n"
        f"2) Workspace setup (self-contained; no pre-seeded metadata):\n"
        f"   cd ~ && git clone --quiet {REPO_URL} {ws}\n"
        f"   cd ~/{ws} && git checkout --quiet {d['observed_head_sha']} && git rev-parse HEAD "
        f"(MUST equal {d['observed_head_sha']}; else stop and report BLOCKED)\n"
        f"   git diff --stat $(git merge-base {d['observed_base_sha']} {d['observed_head_sha']})..{d['observed_head_sha']}  "
        f"# this IS the PR change set (merge-base 免疫 base 分支漂移); review the changed files only\n"
        f"3) Independent review: read changed code; if you suspect a vulnerability, "
        f"write and run your own PoC against the checked-out tree; run the PR's own "
        f"tests if present. Deterministic skills available via MCP "
        f"(skill_diff_parse / skill_risk_classify / skill_sast_scan / skill_case_retrieval "
        f"- advisory only, never replace your own judgment). rag_retrieve provides org "
        f"standards (references only). NOTE: these MCP tools are verified AVAILABLE in "
        f"the current session (ignore any memory of earlier unavailability — that was a "
        f"previous session's configuration issue, now fixed). For any code-bearing diff "
        f"you MUST call skill_diff_parse at minimum; if a first MCP call errors, wait 10s "
        f"and retry once (lazy connect at task start). If you CONFIRM a security finding, "
        f"also consult rag_retrieve for the matching org standard (references only, never "
        f"a substitute for your own reproduction) and skill_case_retrieval for similar "
        f"historical cases (advisory context only).\n"
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
        f"If NOT_CONFIRMED or LOW (gate not required): mark the fix-1/verify-1 plan "
        f"lines as N/A (not applicable, low-risk path), mark the project completed, "
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
    return payload


def _reporter_exec(script_b64, payload_b64):
    """在服务器 reporter 容器内执行 base64 脚本,返回 (stdout, ok)."""
    cmd = (f"cd {COMPOSE_DIR} && docker exec mp-checks-reporter python -c "
           f"\"import base64;exec(base64.b64decode('{script_b64}').decode())\" {payload_b64}")
    r = subprocess.run(["ssh", *SSH_OPTS, SERVER, cmd],
                       capture_output=True, text=True, timeout=90)
    out = (r.stdout or "").strip()
    return out, r.returncode == 0 and bool(out)


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

# 对账收敛(场景3):POST 前先查该 head_sha 上是否已有本 App 的 mergepilot/review
# check-run——"GitHub 已接受但本地未记录"时直接采纳,不重复发布。
CHECK_RECONCILE_SCRIPT = r'''
import base64, json, sys, urllib.request
from token_provider import GitHubAppTokenProvider, TokenProviderConfig
p = GitHubAppTokenProvider(TokenProviderConfig.from_env())
tok = p.get_token()
spec = json.loads(base64.b64decode(sys.argv[1]).decode())
req = urllib.request.Request(
    'https://api.github.com/repos/%s/commits/%s/check-runs' % (spec['repo'], spec['head_sha']),
    headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json'})
r = urllib.request.urlopen(req, timeout=20)
d = json.load(r)
print(json.dumps({'http': r.status, 'matches': [
    {'check_run_id': c['id'], 'conclusion': c.get('conclusion'), 'url': c.get('html_url')}
    for c in d.get('check_runs', []) if c.get('name') == 'mergepilot/review']}))
'''


def parse_publish_out(out):
    """reporter 输出 → 结构化结果;仅 HTTP 200/201 且带 check_run_id 视为成功."""
    try:
        j = json.loads(out)
        if isinstance(j, dict) and j.get("http") in (200, 201) and j.get("check_run_id"):
            return {"ok": True, "check_run_id": j["check_run_id"],
                    "url": j.get("url", ""), "http": j["http"], "adopted": False}
    except Exception:
        pass
    return {"ok": False, "raw": (out or "")[:200]}


def parse_reconcile_out(out):
    try:
        j = json.loads(out)
        if isinstance(j, dict) and j.get("http") in (200, 201):
            m = j.get("matches") or []
            if m:
                best = m[-1]
                return {"ok": True, "check_run_id": best["check_run_id"],
                        "url": best.get("url", ""), "http": j["http"], "adopted": True}
            return {"ok": True, "matches": 0, "adopted": False}
    except Exception:
        pass
    return {"ok": False, "raw": (out or "")[:200]}


# ── 发布凭据(MinIO 回执):POST 成功/对账采纳后落盘,崩溃恢复据此免重发 ──────
def _receipt_path(proj):
    return f"teams/elemiso-team/shared/projects/{proj}/check-run-receipt.json"


def write_receipt(proj, res, d, run, verdict):
    body = {"check_run_id": res.get("check_run_id"), "url": res.get("url", ""),
            "head_sha": d["observed_head_sha"], "run_id": run, "verdict": verdict,
            "adopted": bool(res.get("adopted")),
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return minio_put(_receipt_path(proj), json.dumps(body, ensure_ascii=False, indent=2))


def read_receipt(proj):
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/{_receipt_path(proj)}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


PUBLISH_ATTEMPTS = 3          # 有界重试(场景4):发布类失败最多 3 次
PUBLISH_BACKOFF_S = (10, 30)  # 退避间隔


def publish_with_retry(d, verdict, report, run, proj, log):
    """发布结论。顺序:本地回执 → GitHub 对账 → POST(有界重试)→ 回执落盘。

    返回 {'ok':True,...} 或 {'ok':False,'raw':...}。任一成功路径都会写回执;
    回执写失败不视为发布失败(check_run_id 记入 note,恢复时对账兜底)。"""
    rcpt = read_receipt(proj)
    if rcpt and rcpt.get("check_run_id"):
        return {"ok": True, "check_run_id": rcpt["check_run_id"],
                "url": rcpt.get("url", ""), "http": None, "adopted": True,
                "from": "receipt"}
    payload = post_check(d, verdict, report, run)
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    s64 = base64.b64encode(CHECK_PAYLOAD_SCRIPT.encode()).decode()
    r64 = base64.b64encode(CHECK_RECONCILE_SCRIPT.encode()).decode()
    q64 = base64.b64encode(json.dumps(
        {"repo": d["repo"], "head_sha": d["observed_head_sha"]}).encode()).decode()
    last = {"ok": False, "raw": "not attempted"}
    for i in range(1, PUBLISH_ATTEMPTS + 1):
        rec_out, rec_ok = _reporter_exec(r64, q64)
        rec = parse_reconcile_out(rec_out) if rec_ok else {"ok": False, "raw": rec_out[:200]}
        if rec.get("ok") and rec.get("adopted"):
            write_receipt(proj, rec, d, run, verdict)
            return rec
        out, ok = _reporter_exec(s64, b64)
        res = parse_publish_out(out) if ok else {"ok": False, "raw": out[:200]}
        if res["ok"]:
            if not write_receipt(proj, res, d, run, verdict):
                log("receipt write failed (non-fatal; reconcile covers recovery)")
            return res
        last = res
        log("publish attempt %d/%d failed: %s" % (i, PUBLISH_ATTEMPTS, res.get("raw", "")[:120]))
        if i < PUBLISH_ATTEMPTS:
            time.sleep(PUBLISH_BACKOFF_S[min(i - 1, len(PUBLISH_BACKOFF_S) - 1)])
    return last


# ── 主流程 ──────────────────────────────────────────────────────────────────
def process(d, timeout_min, dry):
    if d["repo"] not in ALLOW_REPOS:
        finish(d, False, "repo not in bridge allowlist")
        return
    if already_processed(d):
        # 场景5:同 repo+PR+head 已成功投递过,重复投递只标记不重跑
        finish(d, True, "duplicate: same repo/pr/head already PROCESSED")
        return
    cid = claim(d)
    if not cid:
        return  # 被并发认领
    run, proj, task, kickoff = build_kickoff(d)
    log = lambda *a: print(time.strftime("[%H:%M:%S]"), *a, flush=True)
    log(f"claimed {d['delivery_id'][:18]} PR#{d['pr_number']} {d['action']} -> {run} (claim {cid[-8:]})")
    if dry:
        log("DRY-RUN kickoff:\n" + kickoff)
        finish(d, False, "dry-run (no dispatch)", cid)
        return
    if not seed_project(proj, task, d, run):
        finish(d, False, "project seeding failed (mc pipe)", cid)
        log("project seeding FAILED — delivery marked ERROR")
        return
    log(f"seeded project {proj} (meta.json + plan.md)")
    if not wake_workers():
        finish(d, False, "worker wake failed", cid)
        log("worker wake FAILED — delivery marked ERROR")
        return
    log("workers awake; sending kickoff to leader DM")
    r = mx.send(DM_ROOM, LEADER, kickoff, txn_prefix="ghbridge")
    if not r.get("event_id"):
        finish(d, False, "kickoff send failed: " + str(r.get("error"))[:120], cid)
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
    # 场景7:GitHub 回写成功才算投递完成;失败=可恢复 ERROR,不标 PROCESSED。
    # 审查未终态(timeout)时即使 neutral check 已发布,投递也不算完成(manual)。
    pub = publish_with_retry(d, verdict, evidence, run, proj, log)
    if pub["ok"] and st in ("completed", "blocked"):
        note = "%s/%s; check_run=%s" % (st, verdict, pub.get("check_run_id"))
        if pub.get("adopted"):
            note += " (adopted via %s)" % pub.get("from", "reconcile")
        finish(d, True, note, cid)
        log("published:", note)
    elif not pub["ok"] and st in ("completed", "blocked"):
        finish(d, False, "PUBLISH_FAILED(retryable) %s/%s; last=%s"
               % (st, verdict, pub.get("raw", "")[:100]), cid)
        log("PUBLISH FAILED after %d attempts — delivery ERROR (recoverable)" % PUBLISH_ATTEMPTS)
    else:
        finish(d, False, "TIMEOUT(manual) %s; publish=%s"
               % (st, "ok" if pub.get("ok") else "failed"), cid)
        log("review TIMEOUT — delivery ERROR (manual attention)")


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
        try:
            pend = pending_deliveries()
        except Exception as e:
            print("poll error (will retry):", type(e).__name__, str(e)[:120], flush=True)
            time.sleep(20)
            continue
        for d in pend:
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
