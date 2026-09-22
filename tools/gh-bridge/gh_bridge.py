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
  受控案例定向认领: MERGEPILOT_TARGET_PR=9 MERGEPILOT_TARGET_HEAD=<40hex>     python gh_bridge.py run --once   # 只认领该 PR+head;其他 PENDING 行保持不动
"""
import argparse
import base64
import hashlib
import importlib.util
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


TARGET_REPO_ENV = "MERGEPILOT_TARGET_REPO"  # 且只认领该仓库(owner/name)
TARGET_PR_ENV = "MERGEPILOT_TARGET_PR"      # 且只认领该 PR
TARGET_HEAD_ENV = "MERGEPILOT_TARGET_HEAD"  # 且只认领该 head(空提交产生的新 SHA)


def target_filter_sql():
    """定向认领过滤(受控案例用,FAIL-CLOSED):三项全设=过滤;三项全 unset=普通模式;
    部分设置/空值/非法值 → ValueError 拒绝启动(绝不退回全队列处理)。"""
    repo = os.environ.get(TARGET_REPO_ENV, "").strip()
    pr = os.environ.get(TARGET_PR_ENV, "").strip()
    head = os.environ.get(TARGET_HEAD_ENV, "").strip().lower()
    set_count = sum(1 for v in (repo, pr, head) if v)
    if set_count == 0:
        return "", None
    if set_count != 3:
        raise ValueError(
            "定向认领配置不完整(MERGEPILOT_TARGET_REPO/PR/HEAD 需三项同时设置,"
            "收到 %d/3)——拒绝退回全队列处理" % set_count)
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        raise ValueError("MERGEPILOT_TARGET_REPO 格式非法: %r" % repo)
    if not re.fullmatch(r"\d+", pr):
        raise ValueError("MERGEPILOT_TARGET_PR 格式非法: %r" % pr)
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("MERGEPILOT_TARGET_HEAD 格式非法: %r" % head)
    return (" AND repo='%s' AND pr_number=%s AND observed_head_sha='%s'"
            % (repo, pr, head)), (repo, pr, head)


def pending_deliveries():
    extra, _pair = target_filter_sql()  # repo+PR+head 三元组过滤(见 target_filter_sql)
    return ssh_psql(
        "SELECT json_agg(t) FROM (SELECT delivery_id, event_name, action, repo, "
        "pr_number, observed_head_sha, observed_base_sha, received_at "
        "FROM public.github_deliveries "
        "WHERE status='PENDING' AND event_name='pull_request' "
        "AND action IN ('opened','synchronize','reopened') "
        "AND repo IS NOT NULL%s ORDER BY received_at) t" % extra, as_json=True)


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
    """终结投递。带 cid 时精确匹配(租约正确性);不带时仅限认领前拒绝路径——
    按 status='PENDING' AND claim_id IS NULL 终结(F1 修复:原 LIKE '%-bridge%'
    对 PENDING 行(claim_id NULL)永不匹配,导致拒绝行静默滞留被反复轮询)。"""
    status = "PROCESSED" if ok else "ERROR"
    where = "delivery_id='%s'" % d["delivery_id"]
    if cid:
        where += " AND claim_id='%s'" % cid
    else:
        where += " AND status='PENDING' AND claim_id IS NULL"
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
try:
    r = urllib.request.urlopen(req, timeout=20)
    d = json.load(r)
    print(json.dumps({'http': r.status, 'matches': [
        {'check_run_id': c['id'], 'conclusion': c.get('conclusion'), 'url': c.get('html_url')}
        for c in d.get('check_runs', []) if c.get('name') == 'mergepilot/review']}))
except urllib.error.HTTPError as e:
    print(json.dumps({'http': e.code, 'error': str(e.reason), 'matches': None}))
except Exception as e:
    print(json.dumps({'http': None, 'error': type(e).__name__ + ': ' + str(e)[:120], 'matches': None}))
'''


def parse_publish_out(out):
    """reporter 输出 → 结构化结果;仅 HTTP 200/201 且带 check_run_id 视为成功.
    失败保留 http 状态码供分类(unknown/permanent/retryable/auth)."""
    try:
        j = json.loads(out)
        if isinstance(j, dict) and j.get("http") in (200, 201) and j.get("check_run_id"):
            return {"ok": True, "check_run_id": j["check_run_id"],
                    "url": j.get("url", ""), "http": j["http"], "adopted": False}
        if isinstance(j, dict) and "http" in j:
            return {"ok": False, "http": j.get("http"),
                    "raw": (out or "")[:200]}
    except Exception:
        pass
    return {"ok": False, "http": None, "raw": (out or "")[:200]}


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
    last_unknown = False   # 最后一次尝试是否"结果未知"(传输层失败,非明确拒绝)
    for i in range(1, PUBLISH_ATTEMPTS + 1):
        rec_out, rec_ok = _reporter_exec(r64, q64)
        rec = parse_reconcile_out(rec_out) if rec_ok else {"ok": False, "raw": rec_out[:200]}
        if rec.get("ok") and rec.get("adopted"):
            write_receipt(proj, rec, d, run, verdict)
            return rec
        out, ok = _reporter_exec(s64, b64)
        last_unknown = not ok          # 传输层失败=POST 结果未知;有结构化输出=可分类
        res = parse_publish_out(out) if ok else {"ok": False, "raw": out[:200]}
        if res["ok"]:
            if not write_receipt(proj, res, d, run, verdict):
                log("receipt write failed (non-fatal; reconcile covers recovery)")
            return res
        last = res
        log("publish attempt %d/%d failed: %s" % (i, PUBLISH_ATTEMPTS, res.get("raw", "")[:120]))
        if i < PUBLISH_ATTEMPTS:
            time.sleep(PUBLISH_BACKOFF_S[min(i - 1, len(PUBLISH_BACKOFF_S) - 1)])
    # 结果分类(2026-09-22 执行保护复核):
    # unknown      传输层失败/超时——可能已产生 check-run,禁止盲目重发,先对账;
    # auth         401——installation token 由 provider 按次刷新,有界重试后仍 401 视为配置问题;
    # retryable    5xx/429/403 rate limit——有界重试;
    # permanent    403(非限频)/404/422——参数或权限问题,重试无意义。
    http = last.get("http")
    if last_unknown:
        last["outcome"] = "unknown"
    elif http == 401:
        last["outcome"] = "auth"
    elif http == 429 or http in (500, 502, 503, 504) or (
            http == 403 and "rate limit" in str(last.get("raw", "")).lower()):
        last["outcome"] = "retryable"
    elif http in (403, 404, 422):
        last["outcome"] = "permanent"
    else:
        last["outcome"] = "unknown"   # 无法归类的一律按未知处理(保守)
    return last


# ── run 级版本清单(产品化备忘九.2):派发前持久化,派发引用其内容摘要 ────────
# 原则:只记录桥在派发时能真实取得的值;取不到的写 null 并列入 missing[],
# 不伪造、不把事后版本当成执行时版本。write-once:已存在且哈希一致→采纳,
# 不一致→拒绝覆盖(版本绑定 run,恢复路径只读不重写)。
MANIFEST_PATH = "teams/elemiso-team/shared/projects/%s/run-manifest.json"
SKILLS_IN_SPEC = ("skill_diff_parse", "skill_risk_classify", "skill_sast_scan",
                  "skill_case_retrieval")


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha_text(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def manifest_sha(m):
    return _sha_text(_canon(m))


def _bridge_source_sha():
    try:
        with open(os.path.join(_HERE, "gh_bridge.py"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def _git_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_HERE,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _worker_image_id(container):
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", container],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


_WORKER_CFG = "/root/.copaw-worker/%s/openclaw.json"
_SKILL_DIR = "/opt/mergepilot/skills"
# MCP 工具名(skill_*)与镜像内目录名的映射(R5 探查核实,2026-09-22)
_SKILL_DIRS = {"skill_diff_parse": "diff_parse", "skill_risk_classify": "risk_classify",
               "skill_sast_scan": "sast_scan", "skill_case_retrieval": "case_retrieval"}

# ── RAG 快照绑定与派发门(RAG-AUDIT 缺口修复,RAG-4/6) ─────────────────────
# 语料事实源在 repo(tools/rag/corpus/);运行副本按布局回退查找,env 可覆盖。
# required 模式:快照不可读或服务不可达即拒绝派发(fail-closed,不静默降级)。
# 默认 advisory(当前产品语义):RAG 不可用照常派发,但状态写入 manifest 可见。
RAG_CORPUS_ENV = "MERGEPILOT_RAG_CORPUS"
RAG_HEALTH_ENV = "MERGEPILOT_RAG_HEALTH_URL"
RAG_REQUIRED_ENV = "MERGEPILOT_RAG_REQUIRED"


def _rag_corpus_path():
    env = os.environ.get(RAG_CORPUS_ENV)
    if env:
        return env
    for cand in (os.path.normpath(os.path.join(_HERE, "..", "rag-live",
                                               "rag-live-corpus.json")),   # r3work 运行布局
                 os.path.normpath(os.path.join(_HERE, "..", "rag", "corpus",
                                               "org-security-knowledge-v1.json"))):  # repo 事实源
        if os.path.isfile(cand):
            return cand
    return None


def _rag_snapshot_info():
    """派发时语料快照(内容寻址)。读不到 → None(调用方决定 required 行为)。"""
    path = _rag_corpus_path()
    if not path:
        return None
    try:
        _rt = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.normpath(os.path.join(_rt, "..", "rag")),
                  os.path.normpath(os.path.join(_rt, "rag"))):
            if p not in sys.path:
                sys.path.insert(0, p)
        import corpus_tool
        doc = corpus_tool.load(path)
        info = corpus_tool.describe(doc)
        info["source"] = os.path.basename(path)
        return info
    except Exception:
        return None


def _rag_service_state(url=None, timeout_s=2.0):
    """只读 /health 探测(dispatch 时服务状态)。任何失败 = unreachable。"""
    url = url or os.environ.get(RAG_HEALTH_ENV, "http://host.docker.internal:4184/health")
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return "reachable" if r.status == 200 else "unreachable"
    except Exception:
        return "unreachable"


def _worker_model_id(container, role):
    """只读取 worker 配置中的主模型标识;仅提取 model 字段,不搬运其余配置。"""
    try:
        r = subprocess.run(["docker", "exec", container, "cat", _WORKER_CFG % role],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        cfg = json.loads(r.stdout)
        return (((cfg.get("agents") or {}).get("defaults") or {})
                .get("model") or {}).get("primary")
    except Exception:
        return None


def _skills_content_hashes(container, skills=SKILLS_IN_SPEC):
    """对 worker 镜像内 skill 目录做内容哈希(sha256 of 排序后逐文件 sha256)。

    工具名→目录名经 _SKILL_DIRS 映射;目录不存在或无文件时不输出该 skill
    (宁可缺失进 missing[],也不产出空串哈希冒充)。"""
    try:
        pairs = ["%s %s" % (s, _SKILL_DIRS[s]) for s in skills if s in _SKILL_DIRS]
        r = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             "cd %s || exit 1; while read -r s d; do "
             "cnt=$(find \"$d\" -type f ! -path '*__pycache__*' 2>/dev/null | wc -l); "
             "[ \"$cnt\" -gt 0 ] || continue; "
             "printf '%%s ' \"$s\"; "
             "find \"$d\" -type f ! -path '*__pycache__*' -exec sha256sum {} \\; "
             "| awk '{print $1}' | sort | sha256sum | cut -d' ' -f1; done <<'EOF'\n%s\nEOF"
             % (_SKILL_DIR, "\n".join(pairs))],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        out = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
                out[parts[0]] = parts[1]
        return out or None
    except Exception:
        return None


def build_manifest(d, run, proj, task, kickoff_base, timeout_min):
    """派发时的版本事实快照。缺失即标注(备忘九.2:可追溯≠可复算)。"""
    workers = {n: _worker_image_id("elemiso-worker-" + n)
               for n in ("leader", "reviewer", "fixer", "verifier")}
    bridge_sha = _bridge_source_sha()
    git = _git_commit()
    # R5 探查(2026-09-22):模型标识与 Skill 内容哈希可从 reviewer 只读取得;
    # RAG 版本仅剩 :4184 endpoint(rag-live 未运行),无从取版本 → 保持 missing。
    model_id = _worker_model_id("elemiso-worker-reviewer", "reviewer")
    skill_hashes = _skills_content_hashes("elemiso-worker-reviewer")
    rag_snap = _rag_snapshot_info()
    rag_state = _rag_service_state()
    rag_required = os.environ.get(RAG_REQUIRED_ENV, "") == "1"
    cfg = {"allow_repos": sorted(ALLOW_REPOS), "repo_url": REPO_URL,
           "leader": LEADER, "team_room": TEAM_ROOM, "dm_room": DM_ROOM,
           "watch_poll_s": WATCH_POLL_S, "timeout_min": timeout_min,
           "publish_attempts": PUBLISH_ATTEMPTS, "stale_minutes": STALE_MINUTES,
           "requeue_max": REQUEUE_MAX, "server": SERVER}
    m = {
        "manifest_version": 1,
        "run_id": run,
        "project_id": proj,
        "delivery_id": d["delivery_id"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "code": {"repo": d["repo"], "head_sha": d["observed_head_sha"],
                 "base_sha": d["observed_base_sha"]},
        "prompt": {"task_id": task,
                   "kickoff_base_sha256": _sha_text(kickoff_base)},
        "orchestrator": {"bridge_source_sha256": bridge_sha, "git_commit": git},
        "workers": {"images": workers},
        "skills": {"names": list(SKILLS_IN_SPEC), "content_sha256": skill_hashes},
        "model": {"primary": model_id,
                  "generation_params": None,   # agentloop 不外露;不伪造
                  "note": "primary from reviewer openclaw.json; fixer/verifier assumed同模型池"},
        "rag": {"snapshot_id": rag_snap["snapshot_id"] if rag_snap else None,
                "chunks": rag_snap["chunks"] if rag_snap else None,
                "data_mode": rag_snap["data_mode"] if rag_snap else None,
                "retrieval_mode": rag_snap["retrieval_mode"] if rag_snap else None,
                "strategy_id": rag_snap["strategy_id"] if rag_snap else None,
                "corpus": rag_snap["source"] if rag_snap else None,
                "service_state_at_dispatch": rag_state,
                "policy": "required" if rag_required else "optional"},
        "config": {"canonical": cfg, "sha256": _sha_text(_canon(cfg))},
    }
    missing = ["model.generation_params"]
    if rag_snap is None:
        missing.append("rag.snapshot_id")   # 语料不可读:知识版本不可追溯
    if model_id is None:
        missing.append("model.primary")
    if skill_hashes is None:
        missing.append("skills.content_sha256")
    if any(v is None for v in workers.values()):
        missing.append("workers.images")
    if bridge_sha is None:
        missing.append("orchestrator.bridge_source_sha256")
    if git is None:
        missing.append("orchestrator.git_commit")
    m["missing"] = missing
    return m


def rag_dispatch_gate():
    """RAG_REQUIRED=1 时的派发前置门( fail-closed,不静默降级)。

    返回 (ok, detail)。required 模式下语料快照不可读或服务不可达 → 拒派发;
    advisory 模式(默认)恒放行,状态由 manifest 如实记录。"""
    required = os.environ.get(RAG_REQUIRED_ENV, "") == "1"
    snap = _rag_snapshot_info()
    state = _rag_service_state()
    if not required:
        return True, {"policy": "optional", "snapshot": bool(snap),
                      "service": state}
    if snap is None:
        return False, "RAG_REQUIRED_UNAVAILABLE: corpus snapshot unreadable"
    if state != "reachable":
        return False, "RAG_REQUIRED_UNAVAILABLE: rag service %s" % state
    return True, {"policy": "required", "snapshot": snap["snapshot_id"][:12],
                  "service": state}


def read_run_manifest(proj):
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/{MANIFEST_PATH % proj}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def write_run_manifest(proj, m):
    """write-once:无则写;有且哈希一致→adopted;有但不一致→拒绝(不覆盖)。"""
    old = read_run_manifest(proj)
    if old is not None:
        if manifest_sha(old) == manifest_sha(m):
            return {"ok": True, "adopted": True}
        return {"ok": False, "reason": "run-manifest conflict (write-once per run)"}
    return {"ok": minio_put(MANIFEST_PATH % proj,
                            json.dumps(m, ensure_ascii=False, indent=2))}


def prepare_run_manifest(d, run, proj, task, kickoff_base, timeout_min):
    """派发前置:构建+持久化清单,返回带引用行的 kickoff 或 None(fail-closed)。"""
    man = build_manifest(d, run, proj, task, kickoff_base, timeout_min)
    mw = write_run_manifest(proj, man)
    if not mw.get("ok"):
        return None, mw.get("reason", "write failed")
    ref = ("\n\nrun-manifest: sha256 %s (projects/%s/run-manifest.json)\n"
           % (manifest_sha(man), proj))
    return kickoff_base + ref, None


# ── v3 adapter 钩子(M3.5):off=零开销;shadow=只读证据;失败不影响旧链路 ───
def _load_v3_adapter():
    """桥侧加载 v3 adapter(repo 布局);运行副本无此文件 → None(诚实降级)。"""
    name = "mp_v3_adapter"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.normpath(os.path.join(_HERE, "..", "orchestrator", "adapter.py"))
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass/相对导入的宿主注册
    spec.loader.exec_module(mod)
    return mod


def v3_shadow_hook(d, cid, log):
    """MERGEPILOT_REVIEW_V3=shadow 时在派发边界产出 v3 只读证据。
    任何失败只记日志,绝不改变旧链路行为;off 时零开销。"""
    try:
        adapter = _load_v3_adapter()
        if adapter is None:
            return  # 运行副本未同步 adapter:旧链路照常
        mode = adapter.review_v3_mode()
        if mode == "off":
            return
        if mode == "on":
            log("v3 mode=on requires R1/R2 authorization; running shadow-only")
        rag = _rag_snapshot_info()
        store = adapter.open_run_store()
        try:
            ev = adapter.run_v3_shadow(
                d, run_store=store,
                rag_snapshot=(rag or {}).get("snapshot_id"))
            log("v3 shadow evidence: %s manifest=%s coverage=%s"
                % (ev["run_id"], ev["manifest_hash"][:12],
                   ev["outcome"]["coverage_missing"] or "none"))
        finally:
            store.close()
    except Exception as e:
        log("v3 shadow hook error (legacy continues): %s %s"
            % (type(e).__name__, str(e)[:120]))
        try:  # 持久痕迹:fail-soft 不等于不可观测(复核整改,2026-09-22)
            if adapter is not None:
                estore = adapter.open_run_store()
                try:
                    estore.record_hook_error(
                        str(d.get("delivery_id", "?"))[:24],
                        "%s %s" % (type(e).__name__, str(e)[:200]),
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                finally:
                    estore.close()
        except Exception:
            pass  # 连错误记录都失败时,只能依赖上面的 stdout 日志


# ── 主流程 ──────────────────────────────────────────────────────────────────
def conclude(d, st, report, run, proj, cid, log):
    """终态→结论→发布→终结。process 与 resume 共用(场景7 语义)。"""
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
        if pub.get("outcome") == "unknown":
            # 结果未知:停止自动重试,先 reconcile 人工/受控确认,防重复 check-run。
            finish(d, False, "PUBLISH_UNKNOWN(manual-reconcile) %s/%s; last=%s"
                   % (st, verdict, pub.get("raw", "")[:100]), cid)
            log("PUBLISH UNKNOWN — delivery ERROR (manual reconcile before any retry)")
        else:
            finish(d, False, "PUBLISH_FAILED(retryable) %s/%s; last=%s"
                   % (st, verdict, pub.get("raw", "")[:100]), cid)
            log("PUBLISH FAILED after %d attempts — delivery ERROR (recoverable)" % PUBLISH_ATTEMPTS)
    else:
        finish(d, False, "TIMEOUT(manual) %s; publish=%s"
               % (st, "ok" if pub.get("ok") else "failed"), cid)
        log("review TIMEOUT — delivery ERROR (manual attention)")


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
    v3_shadow_hook(d, cid, log)   # M3.5:off=零开销;shadow=只读证据,不改旧链路
    rag_ok, rag_detail = rag_dispatch_gate()
    if not rag_ok:
        finish(d, False, str(rag_detail)[:160], cid)
        log("RAG gate REFUSED — no dispatch:", rag_detail)
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
    log("workers awake; preparing run-manifest (pre-dispatch, fail-closed)")
    kickoff2, merr = prepare_run_manifest(d, run, proj, task, kickoff, timeout_min)
    if kickoff2 is None:
        finish(d, False, "run-manifest failed: " + str(merr)[:120], cid)
        log("run-manifest FAILED — no dispatch (fail-closed):", merr)
        return
    kickoff = kickoff2
    log("run-manifest persisted; sending kickoff to leader DM")
    r = mx.send(DM_ROOM, LEADER, kickoff, txn_prefix="ghbridge")
    if not r.get("event_id"):
        finish(d, False, "kickoff send failed: " + str(r.get("error"))[:120], cid)
        return
    log("kickoff sent:", r["event_id"])
    st, report = watch_run(run, proj, time.time() + timeout_min * 60)
    conclude(d, st, report, run, proj, cid, log)


# ── 崩溃恢复(场景1/2/8):接管过期桥租约,按项目权威状态续接 ──────────────────
STALE_MINUTES = 45   # 一轮含人工门可达 ~30min;超过 45min 视为孤儿租约
REQUEUE_MAX = 2      # 无项目回队上限(跨崩溃有界,场景4)


def _new_cid(d):
    return "%s-bridge-%s" % (d["delivery_id"][:14], uuid.uuid4().hex[:8])


def take_over_stale(mins=STALE_MINUTES):
    """接管过期 RUNNING 桥租约:CAS 换新 claim_id(旧执行者此后 rowcount=0)。

    只认本桥新格式 claim_id(含 '-bridge-' 尾段)——为 Controller 留互斥边界:
    其他编排器的认领格式不同,不会被本桥接管(场景9 契约的一半)。"""
    rows = ssh_psql(
        "SELECT json_agg(t) FROM (SELECT delivery_id, event_name, action, repo, "
        "pr_number, observed_head_sha, observed_base_sha, received_at, claim_id, error "
        "FROM public.github_deliveries WHERE status='RUNNING' "
        "AND claim_id LIKE '%%-bridge-%%' "
        "AND claimed_at < now() - interval '%d minutes' ORDER BY claimed_at) t" % mins,
        as_json=True) or []
    taken = []
    for r in rows:
        new = _new_cid(r)
        q = ("UPDATE public.github_deliveries SET claim_id='%s', claimed_at=now() "
             "WHERE delivery_id='%s' AND claim_id='%s' AND status='RUNNING'"
             % (new, r["delivery_id"], r["claim_id"]))
        if ssh_psql(q) == "UPDATE 1":
            d = dict(r)
            d["_cid"] = new
            taken.append(d)
    return taken


def _requeue_count(d):
    m = re.match(r"RQ(\d+)", d.get("error") or "")
    return int(m.group(1)) if m else 0


def resume(d, timeout_min, log):
    """按项目权威状态续接(场景2:不重发 kickoff,无重复业务副作用)。

    分流:receipt→直接终结;项目终态→续发布;项目非终态→只续观察;
    无项目→有界回队 PENDING(计数 RQn,超限转 MANUAL)。"""
    cid = d["_cid"]
    proj = "elemiso-gh-pr%d-%s" % (d["pr_number"], d["observed_head_sha"][:8])
    run = "resume-%s" % d["delivery_id"][:8]
    try:
        man = read_run_manifest(proj)
    except Exception:
        man = None
    if man:
        log("resumed %s: run-manifest %s (bound at dispatch; read-only)"
            % (d["delivery_id"][:12], manifest_sha(man)[:12]))
    else:
        # 前期 run 无清单属已知缺失,不阻断恢复(恢复语义以项目权威状态为准)。
        log("resumed %s: no run-manifest (pre-manifest run or lost); continuing"
            % d["delivery_id"][:12])
    rcpt = read_receipt(proj)
    if rcpt and rcpt.get("check_run_id"):
        finish(d, True, "recovered via receipt; check_run=%s (orig run %s)"
               % (rcpt.get("check_run_id"), rcpt.get("run_id", "?")), cid)
        log("resumed %s: receipt present -> PROCESSED" % d["delivery_id"][:12])
        return
    st = project_status(proj)
    if st is None:
        n = _requeue_count(d)
        if n >= REQUEUE_MAX:
            finish(d, False, "MANUAL: requeued %d times, project never appeared" % n, cid)
            log("resumed %s: requeue budget exhausted -> MANUAL" % d["delivery_id"][:12])
        else:
            q = ("UPDATE public.github_deliveries SET status='PENDING', claim_id=NULL, "
                 "claimed_at=NULL, error='RQ%d' WHERE delivery_id='%s' AND claim_id='%s' "
                 "AND status='RUNNING'" % (n + 1, d["delivery_id"], cid))
            ssh_psql(q)
            log("resumed %s: no project -> requeued (RQ%d)" % (d["delivery_id"][:12], n + 1))
        return
    if st in ("completed", "blocked"):
        conclude(d, st, None, run, proj, cid, log)
        return
    st2, report = watch_run(run, proj, time.time() + timeout_min * 60)
    conclude(d, st2, report, run, proj, cid, log)


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
    log = lambda *m: print(time.strftime("[%H:%M:%S]"), *m, flush=True)
    try:
        target_filter_sql()   # FAIL-CLOSED:部分定向配置在启动时即拒绝
    except ValueError as e:
        print("TARGET CONFIG ERROR:", e, flush=True)
        sys.exit(2)
    # 启动即接管崩溃残留的孤儿租约(场景1:认领后崩溃可恢复)
    try:
        stale = take_over_stale()
        for d in stale:
            try:
                log("recovering stale", d["delivery_id"][:12])
                resume(d, a.timeout_min, log)
            except Exception as e:
                print("resume error:", type(e).__name__, str(e)[:150], flush=True)
    except Exception as e:
        print("take_over_stale error (will continue):",
              type(e).__name__, str(e)[:150], flush=True)
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
