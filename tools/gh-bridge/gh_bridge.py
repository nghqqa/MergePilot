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
  * 2026-09-24 建票所有权冻结轮:正式票据由控制面依结构化 ReviewOutcome
    (review-outcome.v1)确定性创建(ensure_gate_ticket,幂等);leader marker
    仅作兼容信号(缺失不阻塞,冲突仅记录);模型文本/缺失不导致 HIGH 丢票。

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
import types
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-09-24:matrix.py 正典已收编本目录;优先本目录,旧运行布局作回退。
for _cand in (_HERE,
              os.path.normpath(os.path.join(_HERE, "..", "r3ops")),
              r"D:\goai\r3work\scripts"):
    if os.path.isfile(os.path.join(_cand, "matrix.py")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break
import matrix as mx  # noqa: E402

if not hasattr(mx, "preflight"):
    # fail-closed:运行副本的 matrix.py 缺预检 → 拒绝启动(逼出显式同步决策,
    # 不静默跳过凭证检查)。
    print("CREDENTIAL PREFLIGHT UNAVAILABLE: matrix.py lacks preflight(); "
          "sync the runtime copy before running the bridge", flush=True)
    sys.exit(2)

# R5 透传修复(2026-09-23):可信 run 上下文(纯逻辑模块;缺失=降级为旧行为)。
_rc_path = os.path.join(_HERE, "run_context.py")
if os.path.isfile(_rc_path):
    _rc_spec = importlib.util.spec_from_file_location("mp_gh_run_context", _rc_path)
    _rc = importlib.util.module_from_spec(_rc_spec)
    sys.modules["mp_gh_run_context"] = _rc
    _rc_spec.loader.exec_module(_rc)
else:
    _rc = None

# 人工门票据闭环(2026-09-24):gate marker → TicketStore 幂等建票。
# 失败=降级为纯标记语义(明确记日志),绝不阻断门流程。
# 加载方式:fake-package(adapter.py 同款,仓库验证过的模式)——tools/approval
# 以包身份注册,approval/gate_ticket/store_sqlite 的相对导入全部可用。
_APPROVAL_DIR = os.path.normpath(os.path.join(_HERE, "..", "approval"))
_gt = None
try:
    if os.path.isdir(_APPROVAL_DIR):
        _pkg_name = "mp_approval_pkg"
        _pkg = sys.modules.get(_pkg_name)
        if _pkg is None:
            _pkg = types.ModuleType(_pkg_name)
            _pkg.__path__ = [_APPROVAL_DIR]
            sys.modules[_pkg_name] = _pkg

        def _load_sub(name):
            full = _pkg_name + "." + name
            if full in sys.modules:
                return sys.modules[full]
            spec = importlib.util.spec_from_file_location(
                full, os.path.join(_APPROVAL_DIR, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = _pkg_name
            sys.modules[full] = mod
            spec.loader.exec_module(mod)
            return mod

        _load_sub("approval")
        _load_sub("store_sqlite")
        _gt = _load_sub("gate_ticket")
        sys.modules.setdefault("gate_ticket", sys.modules.get(
            "mp_approval_pkg.gate_ticket"))
except Exception:
    _gt = None

# 确定性建票控制面(2026-09-24 建票所有权冻结轮):结构化 ReviewOutcome →
# ensure_gate_ticket。缺失=回退纯 marker 语义(响亮记日志,不静默)。
_ro = None
try:
    _ro_path = os.path.join(_HERE, "review_outcome.py")
    if os.path.isfile(_ro_path):
        _ro_spec = importlib.util.spec_from_file_location("mp_review_outcome", _ro_path)
        _ro = importlib.util.module_from_spec(_ro_spec)
        sys.modules["mp_review_outcome"] = _ro
        _ro_spec.loader.exec_module(_ro)
except Exception:
    _ro = None
_orch = None
try:
    if os.path.isdir(_APPROVAL_DIR):
        _load_sub("policy")
        _orch = _load_sub("orchestration")
except Exception:
    _orch = None

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
        "pr_number, observed_head_sha, observed_base_sha, error, received_at "
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
        f"HUMAN_VERIFICATION_REQUIRED YES: FIRST write TWO structured files via your "
        f"file-sharing tool, then STOP at the human security gate (do NOT delegate any fix). "
        f"File A shared/projects/{proj}/review-outcome.json (preferred signal) with EXACTLY "
        f'these keys and no others: {{"schema_version": "review-outcome.v1", '
        f'"run_id": "{run}", "repo": "{d["repo"]}", "pr_number": {d["pr_number"]}, '
        f'"head_sha": "{d["observed_head_sha"]}", '
        f'"finding_validation": "CONFIRMED", "findings": [{{"finding_id": "<short-id>", '
        f'"severity": "HIGH", "cwe": "CWE-<n>"}}]}}. '
        f"File B shared/projects/{proj}/human-gate-required.json with EXACTLY "
        f'these keys: {{"version": 1, "run_id": "{run}", "task_id": "{task}", '
        f'"severity": "<HIGH|MEDIUM|LOW>", "requested_by": "leader", "requested_at": "<UTC ISO>"}}; '
        f"then STOP at the human security gate (do NOT delegate any fix), "
        f"message me the final report and wait. The bridge derives the formal approval "
        f"ticket from the structured review-outcome signal itself; your files and messages "
        f"cannot create, approve, or reject tickets by themselves. "
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


GATE_MARKER_VERSION = 1
GATE_MARKER_SEVERITIES = ("HIGH", "MEDIUM", "LOW")


def gate_marker(proj, run_id, task_id=None):
    """读取并校验人工门结构化标记(2026-09-24, CASE2 缺陷②)。

    契约:leader(受委托的编排角色)在停人工门**之前**写
      projects/<proj>/human-gate-required.json
      {"version":1, "run_id":<本 run>, "task_id":<任务>, "severity":"HIGH|MEDIUM|LOW",
       "requested_by":"leader", "requested_at":"<UTC ISO>"}
    桥只认**结构化且归属正确**的标记(run_id/task_id 精确匹配本次执行);
    模型自然语言报告不构成机器可执行的 gate 证据。任何不匹配 → 视为无标记,
    返回 (None, reason),流程照常走真实超时语义。

    信任层级说明:与既有 human-gate-approval.md 同级——项目目录命名空间 +
    leader 角色归属;不承担跨身份认证(该升级属审批票据域,TicketStore)。
    """
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/teams/elemiso-team/shared/projects/{proj}/human-gate-required.json"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None, "no marker"
    try:
        m = json.loads(p.stdout)
    except Exception:
        return None, "marker not json"
    if not isinstance(m, dict):
        return None, "marker not object"
    if m.get("version") != GATE_MARKER_VERSION:
        return None, "marker version mismatch"
    if m.get("run_id") != run_id:
        return None, "marker run_id mismatch (attribution refused)"
    if task_id is not None and m.get("task_id") != task_id:
        return None, "marker task_id mismatch"
    if m.get("severity") not in GATE_MARKER_SEVERITIES:
        return None, "marker severity invalid"
    if m.get("requested_by") != "leader":
        return None, "marker requested_by must be leader"
    if not isinstance(m.get("requested_at"), str) or not m.get("requested_at"):
        return None, "marker requested_at missing"
    return m, ""


def watch_run(run, proj, deadline_ts, task_id=None, outcome_box=None):
    """终态以项目 meta.json 权威状态为准(completed/blocked);房间消息仅作进度线索.

    2026-09-24:合法 gate 标记 → 提前返回 "gate"(审查阶段完成、等待人工),
    与真实超时(无标记、无终态)严格区分;终态优先于 gate 标记。
    2026-09-24 建票所有权冻结轮:outcome_box(dict,含 pr_number)非 None 时
    启用确定性审查结论通道——结构化 ReviewOutcome 优先于 marker/终态映射:
    CONFIRMED HIGH/CRITICAL → "gate"(marker 缺失照样成立);INCONCLUSIVE →
    "inconclusive";OUTCOME_ENFORCE=1 且 outcome 无效 → "attention"。
    outcome/err 回填 outcome_box 供 conclude 使用;outcome_box=None 保持旧语义。"""
    t0_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    seen, report = set(), None
    gate_logged = False

    def _outcome_state(enforce_on_error):
        """确定性裁决(仅 outcome_box 路径)。返回 watch 状态或 None(走旧映射)。

        enforce_on_error=False(轮询中):取数失败只继续轮询——产物尚未写出
        不等于无效 outcome;True(终态/标记):无效 outcome → attention。"""
        outcome, err = fetch_review_outcome(proj, outcome_box)
        outcome_box["outcome"], outcome_box["err"] = outcome, err
        if err is None:
            if _ro.gate_worthy(outcome):
                return "gate"
            if outcome.get("finding_validation") == "INCONCLUSIVE":
                return "inconclusive"
            return None    # NOT_CONFIRMED/LOW → 旧映射(终态/超时语义不变)
        if enforce_on_error and outcome_enforce():
            return "attention"
        return None

    while time.time() < deadline_ts:
        st = project_status(proj)
        if st in ("completed", "blocked", "cancelled"):
            # 终态后再宽限数秒,让 result.md/gate 记录发布完整
            for _ in range(6):
                if project_result(proj):
                    break
                time.sleep(5)
            if outcome_box is not None:
                decided = _outcome_state(enforce_on_error=True)
                if decided:
                    return decided, report or ""
            return st, report or ""
        marker, mreason = gate_marker(proj, run, task_id)
        if marker:
            if not gate_logged:
                gate_logged = True
                print(time.strftime("[%H:%M:%S]"),
                      "gate marker accepted (severity=%s) — review phase complete, awaiting human"
                      % marker.get("severity"), flush=True)
            # gate 后宽限:让 findings/result 发布完整
            for _ in range(6):
                if project_result(proj):
                    break
                time.sleep(5)
            if outcome_box is not None:
                decided = _outcome_state(enforce_on_error=True)
                if decided:
                    return decided, report or ""
            return "gate", report or ""
        if not gate_logged and mreason != "no marker":
            gate_logged = True
            print(time.strftime("[%H:%M:%S]"), "gate marker refused:", mreason, flush=True)
        if outcome_box is not None:
            # 无终态、无 marker:直接轮询结构化产物——leader 卡死不再导致
            # HIGH finding 丢票/超时掩盖(review_outcome.json/结果头/findings)。
            decided = _outcome_state(enforce_on_error=False)
            if decided:
                return decided, report or ""
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
def post_check(d, verdict, report, run, detail=None):
    """三事实分离(2026-09-24):审查结论(verdict)/票据事实(detail)/交付状态
    (delivery note)各自独立表达;neutral 只用于真正无法得出结论的场景。"""
    concl, title = {
        "pass": ("success", "MergePilot review: passed (auto-completed)"),
        "pass_verified": ("success",
                          "MergePilot review: HIGH → human gate APPROVED → fix → VERIFIED"),
        "high": ("action_required", "MergePilot review: HIGH finding — human gate required"),
        "rejected": ("failure",
                     "MergePilot review: HIGH finding — human gate REJECTED (blocked, zero fix/verify dispatch)"),
        "gate": ("action_required", "MergePilot review: HIGH finding — human gate required"),
        "timeout": ("neutral", "MergePilot review: bridge timeout (manual check needed)"),
        "inconclusive": ("neutral",
                         "MergePilot review: inconclusive — no conclusion, no ticket"),
        "attention": ("neutral",
                      "MergePilot review: manual attention — outcome/ticket control-plane failure"),
    }[verdict]
    if detail:
        title += " — " + detail
    excerpt = (report or "").strip().replace("`", "'").replace("\r", " ")
    excerpt = re.sub(r"\n+", " | ", excerpt)[:600]
    summary = (f"run_id: {run}\nverdict: {verdict}\n"
               f"triggered by: pull_request {d['action']} #{d['pr_number']} @ "
               f"{d['observed_head_sha'][:12]}\n\nleader report (excerpt):\n{excerpt}")
    if detail:
        summary = "control-plane: %s\n\n%s" % (detail, summary)
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
    'https://api.github.com/repos/%s/commits/%s/check-runs?per_page=100'
    % (spec['repo'], spec['head_sha']),
    headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json'})
try:
    r = urllib.request.urlopen(req, timeout=20)
    d = json.load(r)
    ms = [{'check_run_id': c['id'], 'conclusion': c.get('conclusion'),
           'url': c.get('html_url'), 'app': (c.get('app') or {}).get('slug'),
           'head_sha': c.get('head_sha')}
          for c in d.get('check_runs', []) if c.get('name') == 'mergepilot/review']
    print(json.dumps({'http': r.status, 'matches': ms,
                      'truncated': len(ms) >= 100}))
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


def decide_reconcile_adopt(matches, recorded_check_run_id=None,
                           expected_app="mergepilot", truncated=False):
    """对账采纳决策(纯函数;执行保护复核 v2 收紧)。

    新版 run 身份(cas6909 §3)下同 head 可并存多执行(legacy/v3 evidence/
    显式重跑),**head+名称+App+单条记录均不足以证明归属本次执行**——
    采纳必须有可信执行标识:数据库/凭据记录的 check_run_id(recorded)。

    决策:
      recorded 匹配某 match(且 App 归属相符) → 采纳该 id(RECORD_MATCH);
      recorded 不在任何 match / matches 歧义 / 无 recorded 且有 match
                                    → UNATTRIBUTED(人工对账,不猜测采纳);
      无任何 match                  → NO_MATCH(证明该 head 无本应用 check,
                                      允许安全 POST——这是"证明无副作用"的唯一途径)。
    matches 需含 app 字段(reconcile 脚本输出);列表分页不全时调用方须传入
    truncated 标记,截断列表一律 UNATTRIBUTED(不能断言不存在/唯一)。"""
    ms = list(matches or [])
    if recorded_check_run_id:
        for m in ms:
            if m.get("check_run_id") == recorded_check_run_id:
                if expected_app and m.get("app") and m["app"] != expected_app:
                    return None, "AMBIGUOUS:record-app-mismatch"
                return m.get("check_run_id"), "RECORD_MATCH"
        return None, "UNATTRIBUTED:record-not-found"
    if truncated:
        # 列表可能不完整:不能断言不存在/唯一,一律人工对账
        return None, "UNATTRIBUTED:truncated"
    if not ms:
        return None, "NO_MATCH"
    return None, "UNATTRIBUTED:no-trusted-execution-id"


def parse_reconcile_out(out):
    """对账输出 → 全量匹配列表;采纳决策交给 decide_reconcile_adopt(不盲选)。"""
    try:
        j = json.loads(out)
        if isinstance(j, dict) and j.get("http") in (200, 201):
            return {"ok": True, "http": j["http"],
                    "matches": j.get("matches") or []}
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


def publish_with_retry(d, verdict, report, run, proj, log, detail=None):
    """发布结论。顺序:本地回执 → GitHub 对账 → POST(有界重试)→ 回执落盘。

    返回 {'ok':True,...} 或 {'ok':False,'raw':...}。任一成功路径都会写回执;
    回执写失败不视为发布失败(check_run_id 记入 note,恢复时对账兜底)。
    detail:控制面事实(票据状态等),进 check-run 标题/摘要——三事实分离。"""
    rcpt = read_receipt(proj)
    if rcpt and rcpt.get("check_run_id"):
        return {"ok": True, "check_run_id": rcpt["check_run_id"],
                "url": rcpt.get("url", ""), "http": None, "adopted": True,
                "from": "receipt"}
    payload = post_check(d, verdict, report, run, detail=detail)
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    s64 = base64.b64encode(CHECK_PAYLOAD_SCRIPT.encode()).decode()
    r64 = base64.b64encode(CHECK_RECONCILE_SCRIPT.encode()).decode()
    q64 = base64.b64encode(json.dumps(
        {"repo": d["repo"], "head_sha": d["observed_head_sha"]}).encode()).decode()
    last = {"ok": False, "raw": "not attempted"}
    last_unknown = False   # 最后一次尝试是否"结果未知"(传输失败/歧义,非明确拒绝)

    def _reconcile_step():
        """单次对账:返回 {transport_failed, matches, raw}。"""
        rec_out, rec_ok = _reporter_exec(r64, q64)
        if not rec_ok:
            return {"transport_failed": True, "matches": None, "raw": rec_out[:200]}
        rec = parse_reconcile_out(rec_out)
        if not rec.get("ok"):
            return {"transport_failed": True, "matches": None, "raw": rec.get("raw", "")}
        return {"transport_failed": False, "matches": rec["matches"],
                "truncated": rec.get("truncated", False), "raw": ""}

    for i in range(1, PUBLISH_ATTEMPTS + 1):
        # 每次尝试先对账:已存在本运行 check → 采纳,不重复 POST(场景3)
        r = _reconcile_step()
        if r["transport_failed"]:
            # 对账失败/暂时不可见 ≠ 未创建:禁止盲目 POST,保持 UNKNOWN
            last = {"ok": False, "outcome": "unknown",
                    "raw": "reconcile unavailable: " + r["raw"][:120]}
            log("reconcile unavailable — no POST (unknown, manual reconcile)")
            break
        adopt_id, decision = decide_reconcile_adopt(
            r["matches"], truncated=r.get("truncated", False))
        if adopt_id is not None:
            rec = {"ok": True, "check_run_id": adopt_id, "url": "",
                   "http": 200, "adopted": True}
            write_receipt(proj, rec, d, run, verdict)
            return rec
        if decision.startswith("UNATTRIBUTED") or decision.startswith("AMBIGUOUS"):
            last = {"ok": False, "outcome": "unknown", "raw": "reconcile " + decision}
            log("reconcile %s — no POST, manual reconcile" % decision)
            break
        out, ok = _reporter_exec(s64, b64)
        last_unknown = not ok          # 传输层失败=POST 结果未知;结构化报错=可分类
        res = parse_publish_out(out) if ok else {"ok": False, "raw": out[:200]}
        if res["ok"]:
            if not write_receipt(proj, res, d, run, verdict):
                log("receipt write failed (non-fatal; reconcile covers recovery)")
            return res
        last = res
        log("publish attempt %d/%d failed: %s" % (i, PUBLISH_ATTEMPTS, res.get("raw", "")[:120]))
        if i < PUBLISH_ATTEMPTS:
            time.sleep(PUBLISH_BACKOFF_S[min(i - 1, len(PUBLISH_BACKOFF_S) - 1)])

    # 兜底对账:最后一次尝试可能有副作用(传输失败/5xx)→ 再对账一次
    if last_unknown or (last.get("outcome") == "retryable"):
        r = _reconcile_step()
        if not r["transport_failed"]:
            adopt_id, decision = decide_reconcile_adopt(r["matches"])
            if adopt_id is not None:
                rec = {"ok": True, "check_run_id": adopt_id, "url": "",
                       "http": 200, "adopted": True}
                write_receipt(proj, rec, d, run, verdict)
                return rec

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

# ── 模型目录探测(2026-09-23 切换准备;advisory,不阻断派发) ────────────────
# 网关 = worker 内 http://elemiso-controller:8080/v1(higress → api.deepseek.com)。
# key 只在 worker 容器内读取使用,绝不回传宿主机/日志。
_GATEWAY_PROBE_ENV = "MERGEPILOT_MODEL"          # 操作员请求的模型(可缺省)
# 注意:provider json 的 api_key 是 ENC: 加密态(运行时由 CoPaw 解密),直接打网关
# 会 401;生效明文键在 openclaw.json 的 models.providers["agentteams-gateway"].apiKey。
_GATEWAY_OPENCLAW_CFG = "/root/.copaw-worker/reviewer/openclaw.json"
_GATEWAY_BASE_URL = "http://elemiso-controller:8080/v1"


def _model_catalog_state(container="elemiso-worker-reviewer"):
    """派发时网关实时模型目录(advisory)。只输出模型 id,任何失败=unchecked。"""
    probe = (
        "import json,urllib.request;"
        "oc=json.load(open(%r));"
        "key=((oc.get('models') or {}).get('providers') or {})"
        ".get('agentteams-gateway',{}).get('apiKey') or '';"
        "req=urllib.request.Request('%s/models',headers={'Authorization':'Bearer '+key});"
        "r=urllib.request.urlopen(req,timeout=5);"
        "print(json.dumps(sorted(m.get('id') for m in json.load(r).get('data',[]) if m.get('id'))))"
        % (_GATEWAY_OPENCLAW_CFG, _GATEWAY_BASE_URL))
    try:
        r = subprocess.run(["docker", "exec", container, "python3", "-c", probe],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip().startswith("["):
            models = json.loads(r.stdout)
            return {"checked": True, "models": models}
    except Exception:
        pass
    return {"checked": False, "models": None}


def build_model_manifest_block(model_id, timeout_min=None):
    """manifest 的 model 块(切换准备):配置侧 primary + 派发时实时目录。"""
    requested = (os.environ.get(_GATEWAY_PROBE_ENV) or "").strip() or None
    catalog = _model_catalog_state()
    block = {"primary": model_id,
             "requested": requested,
             "catalog_state_at_dispatch": catalog,
             "generation_params": None,   # agentloop 不外露;不伪造
             "note": "primary from reviewer openclaw.json; fixer/verifier assumed同模型池; "
                     "catalog probed live via gateway /models at dispatch"}
    if requested is not None and catalog.get("checked"):
        block["requested_present"] = requested in (catalog.get("models") or [])
    return block, catalog

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
    """只读 /health 探测(宿主桥视角)。

    修复(2026-09-24, CASE2 缺陷①):宿主桥探测默认改回环地址 127.0.0.1:4184——
    旧默认 host.docker.internal 是容器网络 DNS 名,宿主侧不可解析,曾把
    "探测失败"误记为服务不可达(CASE2 实录:审计流证明当时服务可达)。
    容器侧检索地址(host.docker.internal:4184,worker 内 MCP 使用)是另一个
    网络面,由镜像配置,不在此探测、也不做全局替换。

    返回结构化结果,区分失败类型,不把 DNS/连接失败表述为服务已停止:
      {"state": "reachable" | "probe_failed", "endpoint": url,
       "failure_kind": None | "dns" | "connect" | "timeout" | "http_<n>",
       "detail": str}  # detail 不含响应体
    """
    url = url or os.environ.get(RAG_HEALTH_ENV, "http://127.0.0.1:4184/health")
    import socket
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            if r.status == 200:
                return {"state": "reachable", "endpoint": url,
                        "failure_kind": None, "detail": ""}
            return {"state": "probe_failed", "endpoint": url,
                    "failure_kind": "http_%d" % r.status,
                    "detail": "health returned http %d" % r.status}
    except urllib.error.HTTPError as e:
        return {"state": "probe_failed", "endpoint": url,
                "failure_kind": "http_%d" % e.code,
                "detail": "http %d" % e.code}
    except (socket.timeout, TimeoutError):
        return {"state": "probe_failed", "endpoint": url,
                "failure_kind": "timeout", "detail": "probe timeout"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, socket.gaierror):
            return {"state": "probe_failed", "endpoint": url,
                    "failure_kind": "dns",
                    "detail": "endpoint host not resolvable from bridge host"}
        if isinstance(reason, ConnectionRefusedError):
            return {"state": "probe_failed", "endpoint": url,
                    "failure_kind": "connect", "detail": "connection refused"}
        return {"state": "probe_failed", "endpoint": url,
                "failure_kind": "connect",
                "detail": type(reason).__name__ if reason is not None else "url error"}
    except OSError as e:
        return {"state": "probe_failed", "endpoint": url,
                "failure_kind": "connect",
                "detail": type(e).__name__}


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
    rag_probe = _rag_service_state()
    rag_required = os.environ.get(RAG_REQUIRED_ENV, "") == "1"
    model_block, model_catalog = build_model_manifest_block(model_id)
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
        "model": model_block,
        "rag": {"snapshot_id": rag_snap["snapshot_id"] if rag_snap else None,
                "chunks": rag_snap["chunks"] if rag_snap else None,
                "data_mode": rag_snap["data_mode"] if rag_snap else None,
                "retrieval_mode": rag_snap["retrieval_mode"] if rag_snap else None,
                "strategy_id": rag_snap["strategy_id"] if rag_snap else None,
                "corpus": rag_snap["source"] if rag_snap else None,
                "service_state_at_dispatch": rag_probe["state"],
                "service_probe": {"endpoint": rag_probe["endpoint"],
                                  "failure_kind": rag_probe["failure_kind"],
                                  "detail": rag_probe["detail"],
                                  "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "note": "host-side probe; container-side retrieval uses "
                                          "host.docker.internal:4184 (separate network surface)"},
                "policy": "required" if rag_required else "optional"},
        "config": {"canonical": cfg, "sha256": _sha_text(_canon(cfg))},
    }
    missing = ["model.generation_params"]
    if rag_snap is None:
        missing.append("rag.snapshot_id")   # 语料不可读:知识版本不可追溯
    if model_id is None:
        missing.append("model.primary")
    if not model_catalog.get("checked"):
        missing.append("model.catalog_state_at_dispatch")   # 网关目录未探明(advisory)
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
    probe = _rag_service_state()
    if not required:
        return True, {"policy": "optional", "snapshot": bool(snap),
                      "service": probe["state"],
                      "failure_kind": probe["failure_kind"]}
    if snap is None:
        return False, "RAG_REQUIRED_UNAVAILABLE: corpus snapshot unreadable"
    if probe["state"] != "reachable":
        # probe_failed ≠ 服务已停,但 required 语义要求"可证可达",否则 fail-closed
        return False, ("RAG_REQUIRED_UNAVAILABLE: probe %s (%s)"
                       % (probe["state"], probe["failure_kind"]))
    return True, {"policy": "required", "snapshot": snap["snapshot_id"][:12],
                  "service": probe["state"]}


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


# ── 结构化 ReviewOutcome(建票所有权冻结轮,2026-09-24) ─────────────────────
# 票据创建输入只认结构化已校验数据;绑定锚 = write-once run-manifest。
OUTCOME_ENFORCE_ENV = "MERGEPILOT_OUTCOME_ENFORCE"   # 1=无效 outcome→MANUAL_ATTENTION
TICKET_MODE_ENV = "MERGEPILOT_TICKET_MODE"           # auto(默认)/observe(只读回滚)


def outcome_enforce():
    return os.environ.get(OUTCOME_ENFORCE_ENV, "") == "1"


def _mc_cat(rel_path):
    """读 MinIO 对象文本;不存在/失败 → None(gate_marker 同通道)。"""
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/{rel_path}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return p.stdout


def fetch_review_outcome(proj, d):
    """构建 ReviewOutcome(取数优先级):review-outcome.json → leader result.md
    报告头令牌 → reviewer findings.md 令牌(leader 卡死时的确定性通道)。

    json 存在但无效 → 拒绝(不静默降级为文本解析);返回 (outcome|None, err)。
    d 需含 pr_number;可含 task_id(findings 交付物路径)。"""
    if _ro is None:
        return None, "review_outcome module unavailable"
    man = read_run_manifest(proj)
    if not isinstance(man, dict) or not (man.get("code") or {}).get("head_sha"):
        return None, "run-manifest unavailable"
    doc = _mc_cat(_ro.OUTCOME_JSON_PATH % proj)
    if doc is not None:
        return _ro.parse_outcome_json(doc, man, d["pr_number"])
    result_text = project_result(proj)
    if result_text:
        out, err = _ro.build_outcome_from_report(result_text, man, d["pr_number"])
        if out is not None:
            return out, None
        last_err = err
    else:
        last_err = "no outcome doc and no result text"
    task_id = d.get("task_id")
    if task_id:
        findings = _mc_cat("teams/elemiso-team/shared/tasks/%s/workspace/findings.md"
                           % task_id)
        if findings:
            return _ro.build_outcome_from_findings(findings, man, d["pr_number"])
    return None, last_err


def _open_approval_store():
    store_path = os.environ.get(
        "MERGEPILOT_APPROVAL_DB",
        os.path.join(os.path.expanduser("~"), ".mergepilot", "gate-tickets.db"))
    os.makedirs(os.path.dirname(store_path), exist_ok=True)
    return sys.modules["mp_approval_pkg.store_sqlite"].SQLiteTicketStore(store_path)


def ensure_ticket_for_outcome(outcome, marker, log, expected_head_sha=None):
    """确定性建票(控制面;创建≠批准)。失败返回带 reason 的结果,绝不抛出阻断。

    expected_head_sha:投递行观测 head(可信新鲜度锚;旧 outcome 重放被拒)。"""
    if _orch is None:
        log("ensure unavailable (orchestration module missing)")
        return None
    try:
        policy = sys.modules["mp_approval_pkg.policy"].load_policy()
        store = _open_approval_store()
        try:
            res = _orch.ensure_gate_ticket(
                outcome, policy, store,
                mode=os.environ.get(TICKET_MODE_ENV, "auto"), marker=marker,
                expected_head_sha=expected_head_sha)
            log("ensure_gate_ticket:", res.reason, "| action=%s marker=%s"
                % (res.action, res.marker_status),
                ("ticket=%s created=%s invalidated=%d"
                 % (res.ticket_id, res.created, len(res.invalidated))) if res.ok else "")
            return res
        finally:
            store.close()
    except Exception as e:
        log("ensure_gate_ticket error:", type(e).__name__, str(e)[:140])
        return None



def prepare_run_manifest(d, run, proj, task, kickoff_base, timeout_min):
    """派发前置:构建+持久化清单,返回 (kickoff带引用行, manifest, err) 或
    (None, None, reason)——fail-closed。"""
    man = build_manifest(d, run, proj, task, kickoff_base, timeout_min)
    mw = write_run_manifest(proj, man)
    if not mw.get("ok"):
        return None, None, mw.get("reason", "write failed")
    ref = ("\n\nrun-manifest: sha256 %s (projects/%s/run-manifest.json)\n"
           % (manifest_sha(man), proj))
    return (kickoff_base + ref), man, None


# ── 可信 run 上下文透传(R5 缺口修复,2026-09-23) ───────────────────────────
# 字段唯一来源 = run-manifest(桥 write-once) + 投递行 + 桥自身计数;
# 模型输出/普通请求参数写不进这条记录(authored_by=gh_bridge 契约)。
RUN_CONTEXT_PATH = "teams/elemiso-team/shared/projects/%s/run-context.json"
RUNCONTEXT_AUDIT_URL_ENV = "MERGEPILOT_RUNCONTEXT_AUDIT_URL"
RUNCONTEXT_AUDIT_URL_DEFAULT = "http://127.0.0.1:4184/api/rag/toolspan-audit"


def read_run_context(proj):
    p = subprocess.run(["docker", "exec", "elemiso-ctrl", "mc", "cat",
                        f"{BUCKET}/{RUN_CONTEXT_PATH % proj}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def write_run_context(proj, ctx):
    """write-once(与 run-manifest 同语义):无则写;一致→采纳;不一致→拒绝。"""
    old = read_run_context(proj)
    if old is not None:
        if _canon(old) == _canon(ctx):
            return {"ok": True, "adopted": True}
        return {"ok": False, "reason": "run-context conflict (write-once per run)"}
    return {"ok": minio_put(RUN_CONTEXT_PATH % proj,
                            json.dumps(ctx, ensure_ascii=False, indent=2))}


def audit_post_record(record, url=None, timeout_s=2.0):
    """审计流 best-effort 追加(advisory):服务不可达只返回 False,不抛不阻断。"""
    url = url or os.environ.get(RUNCONTEXT_AUDIT_URL_ENV, RUNCONTEXT_AUDIT_URL_DEFAULT)
    try:
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(record).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.status == 200
    except Exception:
        return False


def prepare_run_context(man, d, proj, attempt_no):
    """构造+持久化 run context(manifest 成功之后调用)。返回 (ctx, err)。"""
    if _rc is None:
        return None, "run_context module not synced (legacy passthrough only)"
    try:
        ctx = _rc.build_run_context(
            man, d, attempt_no=attempt_no, manifest_id=manifest_sha(man))
        wc = write_run_context(proj, ctx)
        if not wc.get("ok"):
            return None, wc.get("reason", "run-context write failed")
        rec = _rc.context_record(ctx)
        if not audit_post_record(rec):
            log_compat("run-context audit post unavailable (advisory; "
                       "MinIO write-once copy is the durable record)")
        return ctx, None
    except Exception as e:
        return None, "%s %s" % (type(e).__name__, str(e)[:140])


def emit_run_end(ctx, terminal_status):
    """终态时审计流补右边界(best-effort;失败不影响任何已有语义)。"""
    if _rc is None or not ctx:
        return
    try:
        audit_post_record(_rc.end_record(ctx, terminal_status))
    except Exception:
        pass


def log_compat(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


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


def open_gate_ticket_for_marker(proj, d, st, log):
    """gate marker → TicketStore 幂等建票(审计闭环;marker≠批准)。

    任何失败都只降级为纯标记语义并记日志(不阻断门流程/发布);决策
    (approve/reject)必须经 TicketStore CAS,属操作员本地接口(gate_cli/
    console),不在本桥自动执行。"""
    if _gt is None:
        return None
    try:
        marker, _mreason = gate_marker(proj, None, None)   # 只取内容;归属校验在下方
        if not marker:
            return None
        # 归属(fail-closed):marker.run_id 必须等于本 run 的 write-once manifest
        # 记录——manifest 缺失或不一致都拒绝建票(纯标记语义继续)。
        man = read_run_manifest(proj)
        if not isinstance(man, dict) or marker.get("run_id") != man.get("run_id"):
            log("gate ticket REFUSED: marker run_id mismatch vs run-manifest")
            return None
        store_path = os.environ.get(
            "MERGEPILOT_APPROVAL_DB",
            os.path.join(os.path.expanduser("~"), ".mergepilot", "gate-tickets.db"))
        os.makedirs(os.path.dirname(store_path), exist_ok=True)
        store_mod = sys.modules.get("mp_approval_pkg.store_sqlite")
        store = store_mod.SQLiteTicketStore(store_path)
        try:
            ticket, created, why = _gt.open_gate_ticket(
                store, marker, run_id=marker.get("run_id") or "",
                repo=d["repo"], head_sha=d["observed_head_sha"],
                task_id=marker.get("task_id") or "",
                ttl_hours=int(os.environ.get("MERGEPILOT_APPROVAL_TTL_H", "24")))
            if ticket is None:
                log("gate ticket REFUSED:", why)
                return None
            log("gate ticket %s (%s)" % (ticket.ticket_id,
                                         "created" if created else "existing"))
            return ticket.ticket_id
        finally:
            store.close()
    except Exception as e:
        log("gate ticket unavailable (degraded to marker-only):",
            type(e).__name__, str(e)[:120])
        return None


def attempt_no_for(d):
    """桥自身计数:1 + 回队次数(RQn;投递行自带 error 字段,零额外查询)。"""
    return 1 + _requeue_count(d)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def conclude(d, st, report, run, proj, cid, log, outcome=None):
    """终态→结论→发布→终结。process 与 resume 共用(场景7 语义)。

    2026-09-24 建票所有权冻结轮:st="gate" 且带结构化 outcome 时,票据由
    ensure_ticket_for_outcome 确定性创建(marker 只作兼容信号,冲突仅记录);
    建票成功先于发布,check-run 携带票据事实(action_required=等待人工审批)。
    建票被拒 → MANUAL_ATTENTION(写明确错误)。st="inconclusive"/"attention"
    为新增终态(无票据)。outcome=None 保持 marker-only 兼容语义。"""
    if st == "completed":
        verdict = "pass_verified" if gate_record(proj, "approval") else "pass"
    elif st == "blocked":
        verdict = "rejected"
    elif st == "gate":
        # 审查阶段完成并需要人工处理;业务运行尚未结束
        verdict = "gate"
    elif st == "inconclusive":
        verdict = "inconclusive"
    elif st == "attention":
        verdict = "attention"
    else:
        verdict = "timeout"
    evidence = "\n\n".join(x for x in [
        project_result(proj),
        "## gate record\n" + (gate_record(proj, "approval") or gate_record(proj, "rejection")),
        "## leader report\n" + (report or ""),
    ] if x.strip())
    log("terminal:", st, "-> verdict:", verdict)
    # R5 透传:审计流右边界(best-effort;失败不影响发布/终结语义)
    try:
        emit_run_end(read_run_context(proj), st)
    except Exception as _e:
        log("run_end audit unavailable (advisory):", type(_e).__name__)

    if st == "gate":
        # 先建票后发布:check-run 必须表达票据事实(三事实分离,2026-09-24)。
        res = None
        if outcome is not None:
            marker, _mreason = gate_marker(proj, None, None)
            res = ensure_ticket_for_outcome(outcome, marker, log,
                                            expected_head_sha=d.get("observed_head_sha"))
        if res is not None and res.ok:
            ticket_id = res.ticket_id
            detail = ("ticket PENDING %s (creator=control-plane; action=%s; marker=%s)"
                      % (res.ticket_id, res.action, res.marker_status))
        elif res is not None:
            ticket_id = None
            detail = ("ticket REFUSED: %s (marker=%s) — manual attention required"
                      % (res.reason, res.marker_status))
        else:
            # marker-only 兼容路径(无结构化 outcome;如旧运行副本/模块缺失)
            ticket_id = open_gate_ticket_for_marker(proj, d, st, log)
            detail = ("ticket PENDING %s (creator=control-plane:marker-compat)" % ticket_id
                      if ticket_id else "ticket unavailable (marker-only semantics)")
        pub = publish_with_retry(d, "gate", evidence, run, proj, log, detail=detail)
        if res is not None and not res.ok:
            # 审查确认了 HIGH 但控制面无法落票 → MANUAL_ATTENTION(明确错误)
            note = "MANUAL_ATTENTION(ticket refused: %s) check_run=%s" % (
                res.reason, pub.get("check_run_id"))
            if "OBSERVE_MODE" in (res.reason or ""):
                note += " [observe mode: no state written]"
            finish(d, False, note, cid)
            log("HUMAN GATE formed but ticket REFUSED — MANUAL_ATTENTION:", res.reason)
            return
        suffix = "; ticket=%s" % ticket_id if ticket_id else ""
        finish(d, False, "GATE_WAIT(manual) verdict=gate; check_run=%s%s"
               % (pub.get("check_run_id"), suffix), cid)
        log("HUMAN GATE reached — delivery ERROR (awaiting operator decision)"
            + ("; ticket %s" % ticket_id if ticket_id else "; ticket creation unavailable"))
        return

    if st == "inconclusive":
        pub = publish_with_retry(d, "inconclusive", evidence, run, proj, log)
        finish(d, True, "INCONCLUSIVE(no ticket); check_run=%s" % pub.get("check_run_id"), cid)
        log("review INCONCLUSIVE — neutral published, no ticket")
        return

    if st == "attention":
        err = (outcome if isinstance(outcome, str) else None) or "structured outcome failure"
        pub = publish_with_retry(d, "attention", evidence, run, proj, log,
                                 detail="manual attention: %s" % err[:160])
        finish(d, False, "MANUAL_ATTENTION(%s); check_run=%s"
               % (err[:160], pub.get("check_run_id")), cid)
        log("review outcome UNREADABLE/FAILED — MANUAL_ATTENTION:", err[:160])
        return

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
        outcome = pub.get("outcome")
        if outcome == "unknown":
            finish(d, False, "PUBLISH_UNKNOWN(manual-reconcile) %s/%s; last=%s"
                   % (st, verdict, pub.get("raw", "")[:100]), cid)
            log("PUBLISH UNKNOWN — delivery ERROR (manual reconcile before any retry)")
        elif outcome == "auth":
            # 401 历经有界重试(每次 reporter 新进程取新 token)仍失败=凭证配置问题,非过期缓存
            finish(d, False, "PUBLISH_AUTH(manual) %s/%s; last=%s"
                   % (st, verdict, pub.get("raw", "")[:100]), cid)
            log("PUBLISH AUTH — delivery ERROR (credential config, manual)")
        elif outcome == "permanent":
            finish(d, False, "PUBLISH_REJECTED(permanent) %s/%s; last=%s"
                   % (st, verdict, pub.get("raw", "")[:100]), cid)
            log("PUBLISH REJECTED — delivery ERROR (permanent, manual)")
        else:   # retryable:有界重试已耗尽且兜底对账未见 check(证明未产生副作用)
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
    kickoff2, man, merr = prepare_run_manifest(d, run, proj, task, kickoff, timeout_min)
    if kickoff2 is None or man is None:
        finish(d, False, "run-manifest failed: " + str(merr)[:120], cid)
        log("run-manifest FAILED — no dispatch (fail-closed):", merr)
        return
    kickoff = kickoff2
    ctx, rcerr = prepare_run_context(man, d, proj, attempt_no_for(d))
    if ctx is None:
        # 透传失败不静默:降级为旧行为(kickoff 文本引用),但 delivery 记录痕迹
        finish(d, False, "run-context failed: " + str(rcerr)[:120], cid)
        log("run-context FAILED — no dispatch (fail-closed):", rcerr)
        return
    log("run-context persisted (attempt %s, manifest %s)"
        % (ctx.get("attempt_no"), str(ctx.get("manifest_id"))[:12]))
    log("run-manifest persisted; sending kickoff to leader DM")
    r = mx.send(DM_ROOM, LEADER, kickoff, txn_prefix="ghbridge")
    if not r.get("event_id"):
        finish(d, False, "kickoff send failed: " + str(r.get("error"))[:120], cid)
        return
    log("kickoff sent:", r["event_id"])
    outcome_box = {"pr_number": d["pr_number"], "task_id": task,
                   "outcome": None, "err": None}
    st, report = watch_run(run, proj, time.time() + timeout_min * 60, task_id=task,
                           outcome_box=(outcome_box if _ro else None))
    conclude(d, st, report, run, proj, cid, log,
             outcome=outcome_box.get("outcome"))


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
        # 崩溃恢复:终态已到但票据可能未落——确定性通道同样适用(ensure 幂等)。
        outcome = None
        if _ro is not None:
            box = {"pr_number": d["pr_number"],
                   "task_id": (man or {}).get("prompt", {}).get("task_id")
                   if isinstance(man, dict) else None,
                   "outcome": None, "err": None}
            outcome, err = fetch_review_outcome(proj, box)
            if err is not None and outcome_enforce():
                conclude(d, "attention", None, run, proj, cid, log, outcome=err)
                return
            conclude(d, st, None, run, proj, cid, log, outcome=outcome)
            return
        conclude(d, st, None, run, proj, cid, log)
        return
    orig_run = (man or {}).get("run_id") if isinstance(man, dict) else None
    outcome_box = {"pr_number": d["pr_number"],
                   "task_id": (man or {}).get("prompt", {}).get("task_id")
                   if isinstance(man, dict) else None,
                   "outcome": None, "err": None}
    st2, report = watch_run(orig_run or run, proj, time.time() + timeout_min * 60,
                            outcome_box=(outcome_box if _ro else None))
    conclude(d, st2, report, run, proj, cid, log,
             outcome=outcome_box.get("outcome"))


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
    # §三(2026-09-24):凭证可用性预检前移——认领与任何 write-once 工件创建之前。
    # 缺失只给脱敏诊断并退出(不认领、不写 manifest/run-context),避免再次出现
    # CASE2 首试的"已认领+已写工件才发现发不出 kickoff"残留。
    ok, why = mx.preflight()
    if not ok:
        print("CREDENTIAL PREFLIGHT FAILED:", why, flush=True)
        print("(set AGENTTEAMS_ADMIN_PASSWORD env or restore the secrets file; "
              "no claim/artifacts were created)", flush=True)
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
