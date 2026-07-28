#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workflow_controller.py — M3-A 确定性 Controller(PG 权威 + Outbox + /sync)。

核心原则:
  - PG 是唯一事实来源;内存不做权威去重。
  - 状态转换 + outbox 写入同一个 PG 事务;Matrix 派发在事务后异步进行。
  - Matrix 事件通过 event_id 持久化到 stage_events,重启不丢。
  - /sync 游标持久化到 controller_offsets,不依赖 baseline 屏蔽。

在 hiclab-net 独立容器运行。环境变量:
  ADMIN_PW, PG_PASS, PG_HOST(默认 audit-pg), PG_PORT(5432),
  PG_DATABASE(mergepilot_audit), PG_USER(mergepilot), MATRIX_HS(hiclaw-controller:6167)
"""
import os, sys, json, time, re, hashlib, uuid, psycopg2, urllib.request, urllib.error

# ── 配置 ──
ADMIN   = "admin"
SERVER  = os.environ.get("MATRIX_SERVER_NAME", "matrix-local.hiclaw.io:18080")
MATRIX_HS = os.environ.get("MATRIX_HS", "http://hiclaw-controller:6167")
PG_HOST = os.environ.get("PG_HOST", "audit-pg")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB   = os.environ.get("PG_DATABASE", "mergepilot_audit")
PG_USER = os.environ.get("PG_USER", "mergepilot")
PG_PASS = os.environ.get("PG_PASS", "")
ADMIN_PW = os.environ.get("ADMIN_PW", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "8"))
SYNC_TIMEOUT  = int(os.environ.get("SYNC_TIMEOUT", "30000"))

# ── B4c L2 审批流配置(复审:gating 默认关;Coordinator 持 token 调 Gateway,不持 PAT) ──
# L2_MERGE_ENABLED 语义(B4c-0.1 #3):**只决定新 run 的 approval_required 默认值**(TASK_SUBMITTED 时写)。
#   L2 维护循环(initiate/drain/reconcile)始终运行——函数内部按 approval_required=TRUE 过滤,
#   故已持久化 approval_required=TRUE 的 run 在开关关闭/重启后仍被维护,不卡死。
#   非法值(不在 {0,1,true,false,yes,no,on,off})→ startup_assert_l2 启动失败,不静默当 false。
_L2_RAW = os.environ.get("L2_MERGE_ENABLED", "0").strip().lower()
_L2_TRUE = {"1", "true", "yes", "on"}
_L2_FALSE = {"0", "false", "no", "off"}
L2_MERGE_ENABLED = _L2_RAW in _L2_TRUE
L2_MERGE_ENABLED_INVALID = _L2_RAW not in _L2_TRUE and _L2_RAW not in _L2_FALSE
GATEWAY_URL      = os.environ.get("GATEWAY_URL", "http://policy-gw:8083")
COORDINATOR_TOKEN = os.environ.get("COORDINATOR_TOKEN", "")
# B4c.1: 对账阈值固定为代码常量(与 l2_reconcile_executing SQL `interval '120 seconds'` 一致;安全下限,非部署参数)
L2_RECONCILE_MIN_AGE_SECONDS = 120
L2_LEASE_SECONDS  = int(os.environ.get("L2_LEASE_SECONDS", "90"))   # outbox DISPATCHED lease(须 ≥ L2_GW_TIMEOUT+5,启动断言)
L2_GW_TIMEOUT     = int(os.environ.get("L2_GW_TIMEOUT", "60"))      # 单次 Gateway MCP 调用总超时(含 SSE+initialize)
# B4c.1 瞬时退避(指数,上限封顶;attempts 仅在真实 Gateway 调用时 +1,等待期不长)
L2_RETRY_BASE_SECONDS = int(os.environ.get("L2_RETRY_BASE_SECONDS", "5"))
L2_RETRY_MAX_SECONDS  = int(os.environ.get("L2_RETRY_MAX_SECONDS", "300"))
# B4c.1 单循环工作预算 + 发现期限(替代旧 L2_DISCOVERY_MAX 计数 HOLD)
L2_MAINTENANCE_MAX_ITEMS      = int(os.environ.get("L2_MAINTENANCE_MAX_ITEMS", "3"))
L2_MAINTENANCE_BUDGET_SECONDS = int(os.environ.get("L2_MAINTENANCE_BUDGET_SECONDS", "60"))
L2_DISCOVERY_TIMEOUT_SECONDS  = int(os.environ.get("L2_DISCOVERY_TIMEOUT_SECONDS", "300"))


def _validate_l2_config():
    """B4c.1.2:数值配置校验(正数/上下限/关系)。非法 raise(startup_assert 转 FATAL,防静默停摆/热循环)。"""
    if L2_MAINTENANCE_MAX_ITEMS < 1: raise ValueError("L2_MAINTENANCE_MAX_ITEMS 须 ≥1")
    if L2_MAINTENANCE_BUDGET_SECONDS < 1: raise ValueError("L2_MAINTENANCE_BUDGET_SECONDS 须 ≥1")
    if L2_RETRY_BASE_SECONDS < 1 or L2_RETRY_MAX_SECONDS < 1: raise ValueError("L2_RETRY_* 须 ≥1")
    if L2_RETRY_BASE_SECONDS > L2_RETRY_MAX_SECONDS: raise ValueError("L2_RETRY_BASE 须 ≤ L2_RETRY_MAX")
    if L2_DISCOVERY_TIMEOUT_SECONDS < 1: raise ValueError("L2_DISCOVERY_TIMEOUT_SECONDS 须 ≥1")
    if L2_LEASE_SECONDS < 1 or L2_GW_TIMEOUT < 1: raise ValueError("L2_LEASE_SECONDS/L2_GW_TIMEOUT 须 ≥1")


class GatewayOutcome:
    """B4c.1 结构化 drain 结果。kind:
       SUCCESS(Gateway OK,approval 应已迁移)/ TRANSIENT(网络/超时/L2_DB_UNAVAILABLE → 退避,不终结)/
       TICKET_DENY(claim 前确定性拒绝 → l2_reject_approved → FAILED/HOLD)/
       GLOBAL_DEGRADED(全局配置故障 → 退回 outbox + 本 tick 停消费)。"""
    __slots__ = ("kind", "reason_code", "detail")
    def __init__(self, kind, reason_code="", detail=""):
        self.kind = kind; self.reason_code = reason_code; self.detail = detail


def _l2_backoff_seconds(retry_count):
    """min(MAX, BASE * 2 ** min(retry_count, 8))。retry_count 用 outbox.attempts。"""
    return min(L2_RETRY_MAX_SECONDS, L2_RETRY_BASE_SECONDS * (2 ** min(max(int(retry_count or 0), 0), 8)))


def _l2_requeue(run_id, reason, stage):
    """B4c.1.1 #1:RETRY 重新排队(retry_count++ / next_attempt_at=now+backoff),CAS current_stage=stage
    防覆盖(任务已不在该阶段则不排)。供 discover/build RETRY 路径防饿死。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT l2_retry_count FROM task_runs WHERE run_id=%s AND current_stage=%s", (run_id, stage))
            r = cur.fetchone()
            if not r:
                conn.commit(); return
            cur.execute("UPDATE task_runs SET l2_retry_count=l2_retry_count+1, l2_retry_reason=%s, l2_next_attempt_at=now()+make_interval(secs=>%s), updated_at=now() WHERE run_id=%s AND current_stage=%s",
                        ((reason or "")[:60], _l2_backoff_seconds(r[0] or 0), run_id, stage))
        conn.commit()
    except Exception:
        conn.rollback()


# B4c.1 Gateway circuit breaker(GLOBAL_DEGRADED 或 网络故障触发):本 tick 不再让其他任务连环撞 Gateway;
#   degraded_until 过后自动恢复(无需重启 Controller)。memory:failure_count/retry_at/last_error。
_L2_GW = {"degraded_until": 0.0, "failure_count": 0, "last_error": ""}


def _l2_gw_degraded():
    """Gateway 是否处于降级窗口。True ⇒ drain 本 tick 不消费 outbox(纯 DB 收敛仍继续)。"""
    return time.monotonic() < _L2_GW["degraded_until"]


def _l2_gw_mark_degraded(reason, seconds=None):
    """打开 breaker:记录 failure,degraded_until = now + 退避(默认按 failure_count 指数)。"""
    if seconds is None:
        seconds = _l2_backoff_seconds(_L2_GW["failure_count"])
    _L2_GW["failure_count"] += 1
    _L2_GW["last_error"] = (reason or "")[:80]
    _L2_GW["degraded_until"] = time.monotonic() + seconds


def _l2_gw_ok():
    """Gateway 调用成功 → 关 breaker(清 failure_count)。"""
    if _L2_GW["failure_count"] or _L2_GW["degraded_until"]:
        _L2_GW["failure_count"] = 0
        _L2_GW["degraded_until"] = 0.0
        _L2_GW["last_error"] = ""

NEXT_STAGE = {"review": "fix", "fix": "verify"}
NEXT_AGENT = {"review": "fixer", "fix": "verifier"}
STAGE_TPL = {
    "review": "{p}-review 完成,findings 见 shared/tasks/{p}-review/findings.md。请用 gh-mcp-fix.sh 提修复 PR(L2 密钥/依赖/删除类只出方案)。完成写 TASK_COMPLETED: {p}-fix。",
    "fix":    "{p}-fix 完成,修复 PR 见 shared/tasks/{p}-fix/。请用 gh-mcp-read.sh 读修复分支逐项复核。完成写 TASK_COMPLETED: {p}-verify。",
}
PAT_SUBMIT  = re.compile(r"TASK_SUBMITTED:\s*(\{.*\})", re.I | re.S)
PAT_COMPLETE = {s: re.compile(rf"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-{s}", re.I) for s in ("review", "fix", "verify")}

class MatrixUnavailable(Exception): pass
class MatrixRejected(Exception): pass

# ── Matrix API ──
_token = None
def matrix_request(method, path, body=None, timeout=30):
    """带错误分类的 Matrix API 调用。不返回空 dict 伪装成功。"""
    global _token
    url = MATRIX_HS + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if _token:
        req.add_header("Authorization", "Bearer " + _token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        code = e.code
        body_text = e.read().decode()[:200]
        if code == 401:
            _token = None  # 触发重登录
            raise MatrixUnavailable(f"401 Unauthorized — 需要重登录")
        elif code == 429:
            retry_after = e.headers.get("Retry-After", "5")
            raise MatrixUnavailable(f"429 Rate limited — Retry-After={retry_after}")
        elif code in (500, 502, 503, 504):
            raise MatrixUnavailable(f"{code} Server error — {body_text}")
        else:
            raise MatrixRejected(f"{code} {body_text}")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
        raise MatrixUnavailable(f"连接失败: {e}")

def ensure_matrix_login():
    global _token
    if _token:
        return _token
    resp = matrix_request("POST", "/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": ADMIN},
        "password": ADMIN_PW,
    })
    _token = resp.get("access_token")
    if not _token:
        raise MatrixUnavailable("login 返回无 token")
    print("[ctrl] Matrix login OK")
    return _token

def matrix_sync(since=None, timeout=30000):
    """使用 /sync 增量拉取事件。"""
    params = f"timeout={timeout}"
    if since:
        params += f"&since={since}"
    return matrix_request("GET", f"/_matrix/client/v3/sync?{params}", timeout=timeout // 1000 + 10)

def send_mention(room_id, user, text):
    """发真 @mention 消息,返回 event_id。"""
    uid = f"@{user}:{SERVER}"
    txn = "c_" + hashlib.sha256(f"{room_id}:{user}:{text}".encode()).hexdigest()[:16]
    resp = matrix_request("PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}", {
        "msgtype": "m.room.text",
        "body": f"@{user} {text}",
        "format": "org.matrix.custom.html",
        "formatted_body": f'<a href="https://matrix.to/#/{uid}">{user}</a> {text}',
        "m.mentions": {"user": [uid]},
    })
    return resp.get("event_id")

# ── PG 连接(带重连) ──
_pg = None
def ensure_pg():
    global _pg
    if _pg is not None and not _pg.closed:
        try:
            _pg.cursor().execute("SELECT 1")
            return _pg
        except Exception:
            _pg.close()
            _pg = None
    _pg = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASS)
    _pg.autocommit = False
    print("[ctrl] PG connected")
    return _pg

def reset_pg():
    global _pg
    if _pg:
        try: _pg.close()
        except: pass
    _pg = None

# ── 事件处理(原子事务) ──
def process_event(event_id, room_id, sender, body, ts):
    """处理单个 Matrix 事件,在一个 PG 事务内完成状态转换 + outbox。"""
    conn = ensure_pg()
    cur = conn.cursor()

    # 记录到 stage_events(幂等:event_id PK)
    inserted = False
    try:
        cur.execute("""INSERT INTO stage_events(event_id, room_id, sender, event_type, raw_body, body_sha256, status)
                       VALUES(%s, %s, %s, %s, %s, %s, 'RECEIVED')
                       ON CONFLICT (event_id) DO NOTHING
                       RETURNING event_id""",
                    (event_id, room_id, sender,
                     "TASK_SUBMITTED" if PAT_SUBMIT.search(body) else "TASK_COMPLETED",
                     body[:2000], hashlib.sha256(body.encode()).hexdigest()[:16]))
        row = cur.fetchone()
        inserted = row is not None
    except Exception as e:
        conn.rollback()
        print(f"[ctrl] stage_events insert err: {e}")
        return
    if not inserted:
        return  # event_id 已处理(幂等)

    # 1. TASK_SUBMITTED
    m = PAT_SUBMIT.search(body)
    if m and sender == ADMIN:
        try:
            payload = json.loads(m.group(1))
            run_id = payload.get("run_id", "")
            if not run_id:
                mark_error(cur, event_id, "no run_id"); conn.commit(); return
            cur.execute("""INSERT INTO task_runs(run_id, room_id, repo, pr_number, branch, status, current_stage, approval_required)
                           VALUES(%s, %s, %s, %s, %s, 'RUNNING', 'review', %s)
                           ON CONFLICT(run_id) DO NOTHING""", (
                run_id, room_id, payload.get("repo"), payload.get("pr_number"), payload.get("branch"), L2_MERGE_ENABLED))
            cur.execute("""INSERT INTO stage_runs(run_id, stage, agent, attempt, status, started_at)
                           VALUES(%s, 'review', 'reviewer', 1, 'PENDING_DISPATCH', now())
                           ON CONFLICT(run_id, stage, attempt) DO NOTHING""", (run_id,))
            cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                           VALUES(%s, %s, %s, 'reviewer', 'review', 1, %s)
                           ON CONFLICT(idempotency_key) DO NOTHING""", (
                f"{run_id}:review:1", run_id, room_id,
                f"请审查 {payload.get('repo','')} PR#{payload.get('pr_number','')} (分支 {payload.get('branch','')})。用 gh-mcp-read.sh + sast-scan,findings 写 shared/tasks/{run_id}-review/findings.md。完成写 TASK_COMPLETED: {run_id}-review。"))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, "review")
            conn.commit()
            print(f"[ctrl] TASK_SUBMITTED {run_id} → task_run + review PENDING_DISPATCH")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 2. TASK_COMPLETED(review/fix)
    for stage in ("review", "fix"):
        mt = PAT_COMPLETE[stage].search(body)
        if not mt or sender not in ("reviewer", "fixer"):
            continue
        run_id = mt.group(1)
        try:
            # 锁住 task
            cur.execute("SELECT status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            task = cur.fetchone()
            if not task:
                mark_error(cur, event_id, f"unknown run_id={run_id}"); conn.commit(); return

            # 找当前 RUNNING/PENDING_DISPATCH attempt
            cur.execute("""SELECT id, attempt FROM stage_runs
                           WHERE run_id=%s AND stage=%s AND status IN ('RUNNING','PENDING_DISPATCH','DISPATCHED')
                           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""", (run_id, stage))
            current = cur.fetchone()
            if not current:
                mark_duplicate(cur, event_id); update_event_meta(cur, event_id, run_id, stage); conn.commit(); return

            # 完成当前阶段
            cur.execute("UPDATE stage_runs SET status='COMPLETED', completed_at=now() WHERE id=%s", (current[0],))

            # 创建下一阶段
            ns = NEXT_STAGE.get(stage)
            if ns:
                na = NEXT_AGENT[stage]
                cur.execute("""INSERT INTO stage_runs(run_id, stage, agent, attempt, status)
                               VALUES(%s, %s, %s, 1, 'PENDING_DISPATCH')
                               ON CONFLICT(run_id, stage, attempt) DO NOTHING""", (run_id, ns, na))
                cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                               VALUES(%s, %s, %s, %s, %s, 1, %s)
                               ON CONFLICT(idempotency_key) DO NOTHING""", (
                    f"{run_id}:{ns}:1", run_id, room_id, na, ns,
                    STAGE_TPL[stage].format(p=run_id)))
                cur.execute("UPDATE task_runs SET current_stage=%s, status='RUNNING', updated_at=now() WHERE run_id=%s", (ns, run_id))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, stage)
            conn.commit()
            print(f"[ctrl] {sender} TASK_COMPLETED {run_id}-{stage} → {ns or 'done'} PENDING_DISPATCH | PG committed")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 3. TASK_COMPLETED(verify) — 只认独立行 VERDICT=,流式快照标 PARTIAL 不终结
    mt = PAT_COMPLETE["verify"].search(body)
    if mt and sender == "verifier":
        run_id = mt.group(1)
        # 必须是独立行 VERDICT=PASS|FAIL|BLOCKED(?mi 多行+忽略大小写)
        vd = re.search(r"(?mi)^\s*VERDICT\s*=\s*(PASS|FAIL|BLOCKED)\s*$", body)
        if not vd:
            # 流式中间快照(还没到 VERDICT=)→ 标 PARTIAL,不动 stage/task
            update_event_meta(cur, event_id, run_id, "verify")
            cur.execute("UPDATE stage_events SET status='PARTIAL', processed_at=now(), error='waiting for explicit VERDICT' WHERE event_id=%s", (event_id,))
            conn.commit()
            print(f"[ctrl] {run_id}-verify partial snapshot; waiting for VERDICT")
            return
        verdict = vd.group(1).upper()
        if verdict == "BLOCKED": verdict = "blocked-needs-approval"
        try:
            cur.execute("SELECT status FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            task = cur.fetchone()
            if not task:
                mark_error(cur, event_id, f"unknown run_id={run_id}"); conn.commit(); return

            # 幂等:找当前 RUNNING/PENDING_DISPATCH/DISPATCHED 的 verify stage
            cur.execute("""SELECT id FROM stage_runs
                           WHERE run_id=%s AND stage='verify'
                             AND status IN ('RUNNING','PENDING_DISPATCH','DISPATCHED')
                           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""", (run_id,))
            current = cur.fetchone()
            if not current:
                mark_duplicate(cur, event_id)
                update_event_meta(cur, event_id, run_id, "verify")
                conn.commit()
                print(f"[ctrl] {run_id}-verify 重复事件(已 COMPLETED)→ DUPLICATE")
                return

            cur.execute("UPDATE stage_runs SET status='COMPLETED', completed_at=now(), verdict=%s WHERE id=%s",
                        (verdict, current[0]))
            # B4c(复审 #1/#2):verify PASS 且 run 级 approval_required=TRUE → 写持久化待办
            #   task=APPROVAL_PENDING + current_stage='l2_binding'。**不在事件事务内调 Gateway**
            #   (/sync 游标照推进,Gateway 临时不可达不会让事件报错卡死任务)。绑定发现/建票/drain
            #   由主循环 initiate_l2_pending 异步完成(独立故障域,复审 #3)。
            # approval_required=FALSE → 旧行为:verify PASS → task PASS(L2 关闭,不退化)。
            if verdict == "PASS":
                cur.execute("SELECT approval_required FROM task_runs WHERE run_id=%s", (run_id,))
                _ar = cur.fetchone()
                if _ar and _ar[0]:
                    cur.execute("UPDATE task_runs SET status='APPROVAL_PENDING', verdict=%s, current_stage='l2_binding', updated_at=now() WHERE run_id=%s",
                                (verdict, run_id))
                    _task_status = "APPROVAL_PENDING"
                else:
                    cur.execute("UPDATE task_runs SET status='PASS', verdict=%s, updated_at=now() WHERE run_id=%s",
                                (verdict, run_id))
                    _task_status = "PASS"
            else:
                cur.execute("UPDATE task_runs SET status='HOLD', verdict=%s, updated_at=now() WHERE run_id=%s",
                            (verdict, run_id))
                _task_status = "HOLD"
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, "verify")
            conn.commit()
            print(f"[ctrl] {run_id}-verify VERDICT={verdict} → task {_task_status} | PG committed")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 非匹配事件
    mark_processed(cur, event_id)
    conn.commit()

def mark_processed(cur, event_id):
    cur.execute("UPDATE stage_events SET status='PROCESSED', processed_at=now() WHERE event_id=%s", (event_id,))

def mark_error(cur, event_id, err):
    cur.execute("UPDATE stage_events SET status='ERROR', error=%s WHERE event_id=%s", (err[:500], event_id))

def mark_duplicate(cur, event_id):
    cur.execute("UPDATE stage_events SET status='DUPLICATE', processed_at=now() WHERE event_id=%s", (event_id,))

def update_event_meta(cur, event_id, run_id, stage):
    """处理完成后回填 stage_events 的 run_id/stage,形成审计链。"""
    cur.execute("UPDATE stage_events SET run_id=%s, stage=%s WHERE event_id=%s", (run_id, stage, event_id))

# ── Outbox 派发 ──
def drain_outbox():
    """处理 PENDING/RETRY 的 outbox 条目。"""
    conn = ensure_pg()
    cur = conn.cursor()
    cur.execute("""SELECT id, idempotency_key, room_id, target_agent, body, retry_count
                   FROM dispatch_outbox
                   WHERE status IN ('PENDING', 'RETRY') AND next_retry_at <= now()
                   ORDER BY id LIMIT 20""")
    items = cur.fetchall()
    for oid, ikey, room_id, agent, body, rc in items:
        try:
            # 检查 stage 状态(如果已 DISPATCHED/RUNNING,跳过)
            stage = body.split("TASK_COMPLETED:")[1].split("-")[0] if "TASK_COMPLETED:" in body else "review"
            # 发送
            eid = send_mention(room_id, agent, body)
            if eid:
                cur.execute("""UPDATE dispatch_outbox SET status='DISPATCHED', matrix_event_id=%s, dispatched_at=now()
                               WHERE id=%s""", (eid, oid))
                # 更新 stage 状态
                cur.execute("""SELECT run_id, target_stage, attempt FROM dispatch_outbox WHERE id=%s""", (oid,))
                r = cur.fetchone()
                if r:
                    cur.execute("""UPDATE stage_runs SET status='RUNNING', started_at=COALESCE(started_at, now())
                                   WHERE run_id=%s AND stage=%s AND attempt=%s AND status='PENDING_DISPATCH'""",
                                (r[0], r[1], r[2]))
                conn.commit()
                print(f"[ctrl] outbox #{oid} → {agent} @ {room_id[:14]} (eid={eid[:16]})")
            else:
                raise Exception("send_mention returned no event_id")
        except MatrixUnavailable as e:
            conn.rollback()
            delay = min(5 * (2 ** rc), 60)
            cur.execute("""UPDATE dispatch_outbox SET status='RETRY', retry_count=retry_count+1,
                           next_retry_at=now()+interval '%s seconds', last_error=%s WHERE id=%s""", (delay, str(e)[:300], oid))
            conn.commit()
            print(f"[ctrl] outbox #{oid} Matrix 不可达 → RETRY({delay}s): {e}")
            raise  # 让外层 backoff
        except Exception as e:
            conn.rollback()
            if rc >= 10:
                cur.execute("""UPDATE dispatch_outbox SET status='FAILED', last_error=%s WHERE id=%s""", (str(e)[:300], oid))
                conn.commit()
                print(f"[ctrl] outbox #{oid} FAILED (>10 retries)")
            else:
                delay = min(5 * (2 ** rc), 60)
                cur.execute("""UPDATE dispatch_outbox SET status='RETRY', retry_count=retry_count+1,
                               next_retry_at=now()+interval '%s seconds', last_error=%s WHERE id=%s""", (delay, str(e)[:300], oid))
                conn.commit()
                print(f"[ctrl] outbox #{oid} err → RETRY({delay}s): {e}")

# ── /sync 消费 ──
def consume_events():
    """使用 /sync 拉取新事件,逐个处理。"""
    conn = ensure_pg()
    cur = conn.cursor()
    cur.execute("SELECT sync_token FROM controller_offsets WHERE consumer_name='controller'")
    row = cur.fetchone()
    since = row[0] if row else None

    data = matrix_sync(since=since, timeout=SYNC_TIMEOUT)
    next_batch = data.get("next_batch")
    joined = data.get("rooms", {}).get("join", {})
    event_count = 0

    for room_id, room_data in joined.items():
        # 不按 room 成员过滤(增量 /sync 不返回 state 事件,会导致漏处理)
        # 直接遍历 timeline 事件,只匹配 TASK_SUBMITTED / TASK_COMPLETED
        for evt in room_data.get("timeline", {}).get("events", []):
            if evt.get("type") != "m.room.message":
                continue
            eid = evt.get("event_id")
            sender = evt.get("sender", "").split(":")[0].lstrip("@")
            body = evt.get("content", {}).get("body", "") or ""
            ts = evt.get("origin_server_ts", 0)

            if "TASK_SUBMITTED" in body or "TASK_COMPLETED" in body:
                process_event(eid, room_id, sender, body, ts)
                event_count += 1

    # 保存游标
    if next_batch:
        cur.execute("""INSERT INTO controller_offsets(consumer_name, sync_token, updated_at)
                       VALUES('controller', %s, now())
                       ON CONFLICT(consumer_name) DO UPDATE SET sync_token=EXCLUDED.sync_token, updated_at=now()""",
                    (next_batch,))
        conn.commit()

    if event_count:
        print(f"[ctrl] /sync: 处理了 {event_count} 个事件, next_batch={next_batch[:16] if next_batch else 'none'}")

# ════════════════════════════════════════════════════════════════════
# B4c · L2 审批流(Controller 侧)
# ════════════════════════════════════════════════════════════════════
# 设计(M3-B4 v2 §11 + 复审 9 条):
#   verify PASS + approval_required → task APPROVAL_PENDING + current_stage='l2_binding'
#     → initiate_l2_pending():discover_binding(B4c-1)→ l2_ensure_ticket(B4c-2)
#   approve CLI → APPROVED
#     → drain_l2_outbox():DISPATCHED+lease → Gateway merge → 推进 outbox/task(B4c-3)
#   reconcile_l2():UNKNOWN/超时EXECUTING/滞留DISPATCHED/过期(B4c-4)
# 绝不自动重试 UNKNOWN L2 动作;绑定来源不信任 LLM(GitHub 读回)。

def _gateway_reachable(timeout=5):
    """TCP 连通性探测 policy-gw host:port(不验鉴权——鉴权在首次真实 MCP 调用时验)。
    返回 True/False。用于 startup_assert:容器不可达即 fail-closed。"""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(GATEWAY_URL)
        host = u.hostname; port = u.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_pg(max_attempts=30, delay=2):
    """B4c-2.2:等 PG ready(SELECT 1 成功)。返回 (conn, ready)。ready=True 时 conn 可用。
    提取为独立函数便于单元测试(monkeypatch ensure_pg/time.sleep)。
    ready 标志(仅 SELECT 1 成功后 True;异常 conn=None)——不用 conn is not None。"""
    conn = None; ready = False
    for _ in range(max_attempts):
        try:
            conn = ensure_pg()
            with conn.cursor() as _c:
                _c.execute("SELECT 1")
            ready = True
            break
        except psycopg2.OperationalError as e:
            conn = None
            reset_pg()
            print(f"[ctrl] startup: PG 未就绪({str(e)[:80]}),{delay}s 后重试...")
            time.sleep(delay)
    return conn, ready


def startup_assert_l2():
    """fail-closed 启动断言(B4c-0.1 #3):
    - 非法 L2_MERGE_ENABLED 值 → 拒启动(不静默当 false)。
    - 若存在非终态 approval_required=TRUE 的 run,但缺 COORDINATOR_TOKEN/GATEWAY_URL/Gateway 不可达 →
      拒启动(否则这些 run 永久卡死,无人维护)。
    - L2_MERGE_ENABLED=1(新 run 默认进审批流)→ 额外要求 token/url/Gateway 连通/l2 函数可 EXECUTE。
    L2 维护循环本身始终运行(函数按 approval_required 过滤),开关只管新 run 默认值。
    B4c.1.2:数值配置校验 + migration 检查**始终**(不 gated by need_gateway,防 L2 关闭时放过缺调度列)。"""
    try:
        _validate_l2_config()
    except (ValueError, TypeError) as e:
        sys.exit(f"[ctrl] FATAL: L2 配置非法: {e}")
    if L2_MERGE_ENABLED_INVALID:
        sys.exit(f"[ctrl] FATAL: L2_MERGE_ENABLED='{_L2_RAW}' 非法(允许:0/1/true/false/yes/no/on/off)")

    # 是否存在"需要维护但尚未终结"的审批 run
    # B4c-2.2:等 PG ready(_wait_for_pg,提取为独立函数;ready 标志仅 SELECT 1 成功后 True)
    conn, ready = _wait_for_pg()
    if not ready:
        sys.exit("[ctrl] FATAL: PG 60s 内未就绪(startup_assert)—— 先起 audit-pg")
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM task_runs
                       WHERE approval_required AND status NOT IN ('PASS','FAIL','HOLD','MERGED','ROLLED_BACK');""")
        pending_l2 = cur.fetchone()[0]
    conn.commit()  # 释放只读事务(防 idle-in-transaction)

    # B4c.1.2 #7:migration/lease 校验**始终**(不 gated by need_gateway;防 L2 关闭时放过缺调度列)
    with conn.cursor() as cur:
        # 函数缺失时 has_function_privilege(text::regprocedure) 会 ERROR → 用 EXISTS+CASE 短路到 FALSE(干净 FATAL)
        cur.execute("SELECT "
                    "(CASE WHEN EXISTS(SELECT 1 FROM pg_proc WHERE proname='l2_ensure_ticket') THEN has_function_privilege('mergepilot','l2_ensure_ticket(text,text,jsonb,text,integer,integer)','EXECUTE') ELSE FALSE END),"
                    "(CASE WHEN EXISTS(SELECT 1 FROM pg_proc WHERE proname='l2_expire_approved') THEN has_function_privilege('mergepilot','l2_expire_approved(text)','EXECUTE') ELSE FALSE END),"
                    "(CASE WHEN EXISTS(SELECT 1 FROM pg_proc WHERE proname='l2_reject_approved') THEN has_function_privilege('mergepilot','l2_reject_approved(text,text)','EXECUTE') ELSE FALSE END),"
                    "(SELECT count(*)::int FROM information_schema.columns WHERE table_schema='public' AND table_name='task_runs'"
                    " AND column_name IN ('l2_next_attempt_at','l2_retry_count','l2_discovery_deadline_at'));")
        ok = cur.fetchone()
    conn.commit()
    _ensure, _expire, _reject, _sched = (ok + (None, None, None, None))[:4] if ok else (None, None, None, None)
    if not (_ensure and _expire and _reject and _sched == 3):
        sys.exit(f"[ctrl] FATAL: B4c/B4c.1 migration 未应用完整(ensure={_ensure} expire={_expire} reject={_reject} sched_cols={_sched}/3);依次应用 m3b_b4.sql + m3b_b4c.sql + m3b_b4c1.sql + m3b_b4c1_1.sql")
    if L2_LEASE_SECONDS < L2_GW_TIMEOUT + 5:
        sys.exit(f"[ctrl] FATAL: L2_LEASE_SECONDS({L2_LEASE_SECONDS}) < L2_GW_TIMEOUT+5({L2_GW_TIMEOUT+5});lease 须 ≥ Gateway 超时 + 安全余量(防双发)")

    need_gateway = bool(pending_l2) or L2_MERGE_ENABLED
    if need_gateway:
        if not COORDINATOR_TOKEN:
            sys.exit(f"[ctrl] FATAL: 有 {pending_l2} 个未终结审批 run(L2_MERGE_ENABLED={'on' if L2_MERGE_ENABLED else 'off'}),但缺 COORDINATOR_TOKEN → 这些 run 会卡死")
        if not GATEWAY_URL:
            sys.exit("[ctrl] FATAL: 审批流需要 GATEWAY_URL")
        # B4c.1: Gateway TCP 不可达 → DEGRADED_NETWORK(**不 fatal**)。纯 DB 收敛继续;恢复后 circuit breaker 自动放行(无需重启)。
        if not _gateway_reachable():
            _l2_gw_mark_degraded("STARTUP:Gateway TCP 不可达", seconds=L2_RETRY_BASE_SECONDS)
            print(f"[ctrl] DEGRADED_NETWORK: Gateway {GATEWAY_URL} TCP 不可达 → L2 外部调用降级(纯 DB 收敛继续;恢复后自动续)")

    mode = "on" if L2_MERGE_ENABLED else "off"
    dg = " DEGRADED_NETWORK" if _l2_gw_degraded() else ""
    print(f"[ctrl] L2_MERGE_ENABLED={mode};未终结审批 run={pending_l2};Gateway={GATEWAY_URL if need_gateway else '(未启用)'};reconcile_min_age={L2_RECONCILE_MIN_AGE_SECONDS}s;budget={L2_MAINTENANCE_BUDGET_SECONDS}s×{L2_MAINTENANCE_MAX_ITEMS}{dg}")


# ── B4c L2 主循环函数(B4c-1.1:权威身份 + 原子 CAS + branch 双源)──
# 事务边界(B4c-0.1 #4 / B4c-1.1 P1-3):Gateway 调用不持 PG 事务;
#   binding 写入 + 阶段推进 + attempts++ 同一短事务(SELECT task FOR UPDATE + CAS current_stage=l2_binding)。
def _atomic_advance(run_id, status, info, candidate=None):
    """B4c-1.1 P1-3:原子推进 task 状态(单短事务,advisory_xact_lock per run + SELECT FOR UPDATE + CAS)。
    status 分支:FOUND/UPDATED(写 binding + l2_awaiting_ticket)/ NOT_FOUND(attempts++达阈值→HOLD)/
      AMBIGUOUS|HOLD_*(→HOLD)/ RETRY|RETRY_HEAD_UNSETTLED|CONCURRENT(不动,下轮重试)。
    candidate:FOUND 时的权威绑定数据{pr_num,head_sha,head_ref,base_ref,repo}。
    返回 (status, info)(写库后)。CONCURRENT=CAS 失败(task 已被并发推进)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("disc:" + run_id,))
            cur.fetchone()
            cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            t = cur.fetchone()
            if not t:
                conn.commit(); return ("CONCURRENT", {"reason": "task 消失"})
            ar, status_db, cstage = t
            cas_ok = bool(ar) and status_db == 'APPROVAL_PENDING' and cstage == 'l2_binding'
            if status in ("RETRY", "RETRY_HEAD_UNSETTLED", "RETRY_INCONSISTENT"):
                # B4c.1.1 #1:gateway 瞬时 → 重新排队(retry_count++/next_attempt_at=now+backoff),防老任务占满 LIMIT 饿死后续
                if cas_ok:
                    cur.execute("SELECT l2_retry_count FROM task_runs WHERE run_id=%s", (run_id,))
                    _rc = cur.fetchone(); _rc = _rc[0] if _rc else 0
                    cur.execute("UPDATE task_runs SET l2_retry_count=l2_retry_count+1, l2_retry_reason=%s, l2_next_attempt_at=now()+make_interval(secs=>%s), updated_at=now() WHERE run_id=%s",
                                (status, _l2_backoff_seconds(_rc), run_id))
                conn.commit()
                return (status, info)
            if status == "CONCURRENT" or not cas_ok:
                conn.commit()
                return ("CONCURRENT", {"reason": f"task 已变 status={status_db} stage={cstage}", **(info or {})})
            if status in ("FOUND", "UPDATED") and candidate:
                pr_num = candidate["pr_num"]; head_sha = candidate["head_sha"]
                head_ref = candidate["head_ref"]; base_ref = candidate["base_ref"]; repo = candidate["repo"]
                cur.execute("SELECT binding_id, pr_number, fix_branch, base_branch, repo, head_sha FROM run_pr_bindings WHERE run_id=%s", (run_id,))
                ex = cur.fetchone()
                if ex:
                    ebid, epr, ebr, eba, erepo, esha = ex
                    if epr == pr_num and ebr == head_ref and eba == base_ref and erepo == repo:
                        if esha != head_sha:
                            cur.execute("UPDATE run_pr_bindings SET head_sha=%s, repo=%s, recorded_at=now() WHERE binding_id=%s", (head_sha, repo, ebid))
                            out = ("UPDATED", {"binding_id": ebid, "head_sha": head_sha, "pr_number": pr_num})
                        else:
                            out = ("FOUND", {"binding_id": ebid, "head_sha": esha, "pr_number": pr_num})
                        cur.execute("UPDATE task_runs SET current_stage='l2_awaiting_ticket', l2_discovery_attempts=0, l2_retry_count=0, l2_retry_reason=NULL, l2_discovery_deadline_at=NULL, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (run_id,))
                    else:
                        # B4c-1.2 P1-1:身份冲突 → 同事务置 HOLD(否则每 tick 重复冲突)
                        cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                                    (f"HOLD_BINDING_CONFLICT existing_pr={epr} existing_branch={ebr} new_pr={pr_num} new_branch={head_ref}", run_id))
                        out = ("HOLD_BINDING_CONFLICT", {"existing": ebid, "existing_pr": epr, "new_pr": pr_num,
                                                          "reason": "PR 身份(pr/branch/base/repo)变更,不静默覆盖"})
                else:
                    bid = "bnd-" + str(uuid.uuid4())
                    cur.execute("""INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s)""", (bid, run_id, repo, pr_num, head_ref, base_ref, head_sha))
                    out = ("FOUND", {"binding_id": bid, "head_sha": head_sha, "pr_number": pr_num})
                    cur.execute("UPDATE task_runs SET current_stage='l2_awaiting_ticket', l2_discovery_attempts=0, l2_retry_count=0, l2_retry_reason=NULL, l2_discovery_deadline_at=NULL, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (run_id,))
            elif status == "NOT_FOUND":
                # B4c.1: 发现期限(l2_discovery_deadline_at,惰性初始化 now+timeout)代替旧 L2_DISCOVERY_MAX 计数 HOLD;
                #   l2_discovery_attempts 仅审计;公平退避 l2_next_attempt_at=now+backoff(retry_count++)。
                cur.execute("SELECT l2_retry_count FROM task_runs WHERE run_id=%s", (run_id,))
                rc = cur.fetchone()[0] or 0
                cur.execute("""UPDATE task_runs
                               SET l2_discovery_attempts = l2_discovery_attempts + 1,
                                   l2_retry_count = l2_retry_count + 1, l2_retry_reason = 'NOT_FOUND',
                                   l2_discovery_deadline_at = COALESCE(l2_discovery_deadline_at, now() + make_interval(secs => %s)),
                                   l2_next_attempt_at = now() + make_interval(secs => %s), updated_at = now()
                               WHERE run_id=%s""", (L2_DISCOVERY_TIMEOUT_SECONDS, _l2_backoff_seconds(rc), run_id))
                cur.execute("SELECT (l2_discovery_deadline_at < now()), l2_discovery_attempts FROM task_runs WHERE run_id=%s", (run_id,))
                past, attempts = cur.fetchone()
                if past:
                    cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                                (f"无 fix PR(达发现期限 {L2_DISCOVERY_TIMEOUT_SECONDS}s,attempts={attempts})", run_id))
                    out = ("HOLD", {"reason": f"discovery deadline reached (attempts={attempts})", "attempts": attempts})
                else:
                    out = ("NOT_FOUND", {"attempts": attempts, "deadline_s": L2_DISCOVERY_TIMEOUT_SECONDS})
            else:   # AMBIGUOUS | HOLD_*
                reason = status + (" " + json.dumps(info, default=str) if info else "")
                cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s", (reason[:300], run_id))
                out = (status, info)
        conn.commit()
        return out
    except Exception as e:
        conn.rollback()
        return ("RETRY", {"reason": f"_atomic_advance 异常: {e}"})


def discover_binding_for_run(run_id, deadline=None):
    """B4c-1.2:GitHub 权威绑定发现 + 原子推进。返回 (status, info)。
    B4c.1.1 #3:包一层 catcher —— GatewayGlobalDegraded(坏 token/角色路径)→ 打开 circuit breaker
      + 返回 ("DEGRADED", ...),由 initiate 停止本 tick discover(防连环撞 Gateway)。"""
    import gateway_client
    try:
        result = _discover_binding_for_run_inner(run_id, deadline)
        _l2_gw_ok()   # B4c.1.3 P2:discover 成功 → 清 breaker failure_count(恢复后重新计数)
        return result
    except gateway_client.GatewayGlobalDegraded as e:
        _l2_gw_mark_degraded(e.reason_code)
        print(f"[ctrl][L2] {run_id} discover: Gateway GLOBAL_DEGRADED({e.reason_code}) → circuit breaker")
        return ("DEGRADED", {"reason": e.reason_code})
    except gateway_client.GatewayUnavailable as e:
        # B4c.1.2 #2:网络/401 瞬时 → 重排该 task + 打开 breaker(本 tick 停 discover,防连环撞)
        _l2_requeue(run_id, "gw unavailable", "l2_binding")
        _l2_gw_mark_degraded("UNAVAILABLE", seconds=L2_RETRY_BASE_SECONDS)
        print(f"[ctrl][L2] {run_id} discover: Gateway unavailable({str(e)[:50]}) → 重排 + circuit breaker")
        return ("DEGRADED", {"reason": "gateway unavailable"})
    except gateway_client.GatewayDenied as e:
        # B4c.1.2 #2:discovery 级确定性拒绝(REPO_NOT_ALLOWED 等)→ HOLD(收敛,不无限重试)
        _c = ensure_pg()
        with _c.cursor() as _cur:
            _cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s AND status='APPROVAL_PENDING' AND current_stage='l2_binding'", (f"gateway denied:{e.reason_code}", run_id))
        _c.commit()
        print(f"[ctrl][L2] {run_id} discover: Gateway denied({e.reason_code}) → HOLD(收敛,不无限重试)")
        return ("HOLD", {"reason": f"gateway denied:{e.reason_code}"})


def _gw_timeout_for(deadline):
    """B4c.1.2 #3:剩余预算内的单次 Gateway 超时 = min(L2_GW_TIMEOUT, 剩余),**无 5s 下限**(下限 0.1s 防 0)。
    调用方 loop 已用 _budget_exhausted 确保不在 <1s 时启动新项;故运行中剩余≥0.1s,单调用 ≤ 剩余。"""
    return min(L2_GW_TIMEOUT, max(deadline - time.monotonic(), 0.1)) if deadline else L2_GW_TIMEOUT


def _budget_exhausted(deadline):
    """B4c.1.2 #3:剩余预算不足以启动新项(<1s)或已过。loop 顶用,防启动后单调用超整轮预算。"""
    return deadline is not None and (deadline - time.monotonic()) < 1.0


def _tick_take(budget):
    """B4c.1.3:从每-tick 共享 item 预算取一项(扣减)。True=有额度(已扣)/无预算限制;False=耗尽。"""
    if budget is None:
        return True
    if budget[0] <= 0:
        return False
    budget[0] -= 1
    return True


def _discover_binding_for_run_inner(run_id, deadline=None):
    """B4c-1.2:GitHub 权威绑定发现 + 原子推进。返回 (status, info)。
    P1-2 全字段严格(gateway_read_pr/branch 已强制 40hex + 字段必存在);
    P1-3 binding 写入+阶段推进同一短事务(_atomic_advance CAS),冲突也同事务置 HOLD;
    P1-4 PR head.sha == branch ref sha 双源校验;
    B4c-1.2 P1-3 已有 binding:**直验**其 PR(identity+state+branch 双源 SHA),list 仅用于检测**额外**匹配 PR;
      已绑定 open PR + list 返回 0 匹配(缓存/瞬态不一致)→ RETRY_INCONSISTENT(不累计 NOT_FOUND);
      list 出现额外匹配 PR → AMBIGUOUS。"""
    import gateway_client

    def validate_pr(pr_num):
        """权威读回 + 严格身份 + branch 双源。返回 (status, info, candidate)。status=OK 时 candidate 含绑定数据。"""
        rstatus, prd = gateway_client.gateway_read_pr(owner, repo_name, pr_num, timeout=_gw_timeout_for(deadline))
        if rstatus == "RETRY":
            return ("RETRY", {"reason": "pull_request_read 失败"}, None)
        head_sha = prd["head_sha"]; base_ref = prd["base"]; state = prd["state"]
        head_ref = prd["head_ref"]; head_repo = prd["head_repo_full_name"]
        if state != "open":
            return ("HOLD_PR_NOT_OPEN", {"pr_number": pr_num, "state": state}, None)
        if base_ref != "main":
            return ("HOLD_IDENTITY", {"reason": f"base_ref={base_ref} 非 main", "pr_number": pr_num}, None)
        if not head_ref.startswith(f"fix/{run_id}-"):
            return ("HOLD_IDENTITY", {"reason": f"head_ref={head_ref} 不匹配 fix/{run_id}-", "pr_number": pr_num}, None)
        if prd["pr_number"] != pr_num:
            return ("RETRY", {"reason": "PR 返回 number 与请求不一致"}, None)
        if head_repo != repo:
            return ("HOLD_IDENTITY", {"reason": f"head.repo={head_repo} != 目标 {repo}(防 fork PR)", "pr_number": pr_num}, None)
        bstatus, branch_sha = gateway_client.gateway_read_branch(owner, repo_name, head_ref, timeout=_gw_timeout_for(deadline), deadline=deadline)
        if bstatus == "RETRY":
            return ("RETRY", {"reason": "list_branches 失败/未找到 branch"}, None)
        if branch_sha != head_sha:
            return ("RETRY_HEAD_UNSETTLED", {"pr_head": head_sha, "branch_sha": branch_sha, "reason": "PR head.sha != branch ref sha"}, None)
        return ("OK", None, {"pr_num": pr_num, "head_sha": head_sha, "head_ref": head_ref, "base_ref": base_ref, "repo": repo})

    # Phase 1: repo + existing binding(短事务)
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("SELECT repo FROM task_runs WHERE run_id=%s", (run_id,))
        row = cur.fetchone()
        cur.execute("SELECT binding_id, pr_number FROM run_pr_bindings WHERE run_id=%s", (run_id,))
        existing = cur.fetchone()
    conn.commit()
    if not row or not row[0]:
        return _atomic_advance(run_id, "HOLD_NO_REPO", {"reason": "task 无 repo"})
    repo = row[0]
    owner, _, repo_name = repo.partition("/")
    if not owner or not repo_name:
        return _atomic_advance(run_id, "HOLD_NO_REPO", {"reason": f"repo 非法: {repo}"})

    if existing:
        # 已有 binding:直验其 PR(identity+state+branch 双源),不靠 list 判存在
        ebid, epr = existing
        st, info, cand = validate_pr(epr)
        if st != "OK":
            info = dict(info or {}); info.setdefault("existing", ebid)
            return _atomic_advance(run_id, st, info)
        # list 仅检测**额外**匹配 PR(防 AMBIGUOUS 漏检)
        lstatus, prs = gateway_client.gateway_list_prs(owner, repo_name, run_id, timeout=_gw_timeout_for(deadline), deadline=deadline)
        if lstatus == "RETRY":
            return _atomic_advance(run_id, "RETRY", {"reason": "list_pull_requests 失败(检测额外 PR)"})
        extra = [p["number"] for p in prs if p.get("number") != epr]
        if extra:
            return _atomic_advance(run_id, "AMBIGUOUS", {"bound_pr": epr, "extra_prs": extra, "count": len(prs)})
        # 已绑定 open PR 但 list 返回 0 匹配 → 缓存/瞬态不一致 → RETRY_INCONSISTENT(不累计 NOT_FOUND)
        if len(prs) == 0:
            return _atomic_advance(run_id, "RETRY_INCONSISTENT", {"reason": f"bound PR {epr} open 但 list 返回 0 匹配", "bound_pr": epr})
        return _atomic_advance(run_id, "FOUND", {"pr_number": epr}, candidate=cand)

    # 无 existing binding → list 主导
    lstatus, prs = gateway_client.gateway_list_prs(owner, repo_name, run_id, timeout=_gw_timeout_for(deadline), deadline=deadline)
    if lstatus == "RETRY":
        return _atomic_advance(run_id, "RETRY", {"reason": "list_pull_requests 失败"})
    if lstatus == "AMBIGUOUS":
        return _atomic_advance(run_id, "AMBIGUOUS", {"count": len(prs), "prs": [p.get("number") for p in prs]})
    if lstatus == "NOT_FOUND":
        return _atomic_advance(run_id, "NOT_FOUND", {})
    pr = prs[0]; pr_num = pr["number"]
    st, info, cand = validate_pr(pr_num)
    if st != "OK":
        return _atomic_advance(run_id, st, info)
    return _atomic_advance(run_id, "FOUND", {"pr_number": pr_num}, candidate=cand)


def _hold_ticket_atomic(run_id, status, reason):
    """B4c-2.2:建票异常路径(22023 第二事务)原子置 task HOLD。**完整 CAS**:
    approval_required=TRUE AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_ticket'
    (不只检查 stage,防覆盖已非 APPROVAL_PENDING 的任务)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ticket:" + run_id,))
            cur.fetchone()
            cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            t = cur.fetchone()
            if not t:
                conn.commit(); return (status, {"reason": reason, "note": "task 消失"})
            ar, status_db, cstage = t
            if not (ar and status_db == "APPROVAL_PENDING" and cstage == "l2_awaiting_ticket"):
                conn.commit(); return ("CONCURRENT", {"reason": f"task 已变 status={status_db} stage={cstage}", "orig": status})
            cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_ticket_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                        (f"{status}: {reason}"[:300], run_id))
        conn.commit()
        return (status, {"reason": reason})
    except Exception as e:
        conn.rollback()
        return ("RETRY", {"reason": f"_hold_ticket_atomic 异常: {e}"})


def create_ticket_for_run(run_id):
    """B4c-2(+2.2):固化双源 SHA(承 discover)→ canonical_payload + args_hash → l2_ensure_ticket 幂等建票
    (活动票返回旧的;同事务 outbox)→ 推进 l2_awaiting_approval。
    **B4c-2.2 P1:全部在同一锁事务内**(ticket:<run_id> xact lock + SELECT task FOR UPDATE + 完整 CAS +
    binding 查询 + HOLD/l2_ensure_ticket/推进),无跨事务窗口:
      无 binding → 同事务 HOLD_NO_BINDING;
      l2_ensure_ticket 抛 22023 → (rollback 后)_hold_ticket_atomic 第二事务 HOLD_TICKET_CONFLICT;
      瞬时 DB 错 → RETRY。
    返回 (status, info)。"""
    import gateway_client
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ticket:" + run_id,))
            cur.fetchone()
            # 完整 CAS(同事务)
            cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            t = cur.fetchone()
            if not t:
                conn.commit(); return ("CONCURRENT", {"reason": "task 消失"})
            ar, status, cstage = t
            if not (ar and status == "APPROVAL_PENDING" and cstage == "l2_awaiting_ticket"):
                conn.commit(); return ("CONCURRENT", {"reason": f"task 已变 status={status} stage={cstage}"})
            # binding 查询 **在同一事务内**(B4c-2.2 P1)
            cur.execute("SELECT binding_id, repo, pr_number, base_branch, head_sha FROM run_pr_bindings WHERE run_id=%s", (run_id,))
            b = cur.fetchone()
            if not b:
                cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_ticket_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                            ("HOLD_NO_BINDING: 无 binding(应先经 l2_binding 发现)"[:300], run_id))
                conn.commit()
                return ("HOLD_NO_BINDING", {"reason": "无 binding(应先经 l2_binding 发现)"})
            binding_id, repo, pr_num, base_ref, head_sha = b
            owner, _, repo_name = repo.partition("/")
            payload = {"owner": owner, "repo": repo_name, "pullNumber": int(pr_num),
                       "commit_title": f"Merge fix {run_id}", "merge_method": "squash"}
            args_hash = gateway_client.canonical_args_hash(payload)
            cur.execute("SELECT l2_ensure_ticket(%s, 'merge', %s::jsonb, %s, 24, 1)",
                        (binding_id, json.dumps(payload), args_hash))
            ticket_id = cur.fetchone()[0]
            cur.execute("UPDATE task_runs SET current_stage='l2_awaiting_approval', l2_retry_count=0, l2_retry_reason=NULL, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (run_id,))
        conn.commit()
        return ("CREATED", {"ticket_id": ticket_id, "binding_id": binding_id,
                            "args_hash": args_hash[:12], "pr_number": pr_num, "payload": payload})
    except psycopg2.Error as e:
        # B4c-2.1 P1-2:按 pgcode 分类。22023(payload/hash/TTL 确定性冲突)→ HOLD_TICKET_CONFLICT(第二事务,完整 CAS)
        conn.rollback()
        if getattr(e, "pgcode", None) == "22023":
            return _hold_ticket_atomic(run_id, "HOLD_TICKET_CONFLICT", f"l2_ensure_ticket 22023: {str(e)[:200]}")
        _l2_requeue(run_id, f"建票DB:{e.pgcode}", "l2_awaiting_ticket")   # B4c.1.1 #1:重新排队防饿死
        return ("RETRY", {"reason": f"建票 DB 错误(pgcode={e.pgcode}): {str(e)[:150]}"})
    except Exception as e:
        conn.rollback()
        _l2_requeue(run_id, "建票异常", "l2_awaiting_ticket")   # B4c.1.1 #1
        return ("RETRY", {"reason": f"建票失败: {e}"})


def initiate_l2_pending(deadline=None, budget=None):
    """扫 approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_binding':
    B4c-1.1 绑定发现。**per-run session advisory lock**(pg_advisory_lock)序列化多 Controller 发现,
    防一次真实 NOT_FOUND 被并发累计多次。discover 内部 _atomic_advance 做 CAS 原子写推进。"""
    if _l2_gw_degraded():
        return   # B4c.1.1 #3:circuit breaker 打开 → 跳过本 tick discover(防连环撞 Gateway)
    conn = ensure_pg()
    with conn.cursor() as cur:
        # B4c.1 公平调度 + B4c.1.1 #4 单循环预算:仅到期候选,按到期/更新序,LIMIT MAX_ITEMS
        cur.execute("""SELECT run_id FROM task_runs
                       WHERE approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_binding'
                         AND l2_next_attempt_at <= now()
                       ORDER BY l2_next_attempt_at, updated_at, run_id LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
        runs = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT run_id FROM task_runs
                       WHERE approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_ticket'
                         AND l2_next_attempt_at <= now()
                       ORDER BY l2_next_attempt_at, updated_at, run_id LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
        ticket_runs = [r[0] for r in cur.fetchall()]
    conn.commit()
    for run_id in runs:
        if _budget_exhausted(deadline) or not _tick_take(budget):
            break   # B4c.1 工作预算到期,剩余候选下 tick 处理(已持久化 next_attempt_at)
        if _l2_gw_degraded():
            break   # B4c.1.1 #3:discover 途中 breaker 打开 → 停
        # per-run session advisory lock(跨 Controller 序列化整个 discover;crash 时连接断开自动释放)
        lc = ensure_pg()
        try:
            with lc.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", ("disc:" + run_id,))
                got = cur.fetchone()[0]
            lc.commit()
            if not got:
                continue   # B4c.1:另一 Controller 持锁 → 跳过(不阻塞;持锁者处理)
        except Exception as e:
            print(f"[ctrl][L2] {run_id} advisory_lock 异常: {e}"); continue
        try:
            st, info = discover_binding_for_run(run_id, deadline)
            if st in ("FOUND", "UPDATED"):
                print(f"[ctrl][L2] {run_id} 绑定 {st}: {str(info.get('binding_id',''))[:16]} pr={info.get('pr_number')} head={str(info.get('head_sha',''))[:12]} → l2_awaiting_ticket")
            elif st == "NOT_FOUND":
                print(f"[ctrl][L2] {run_id} 绑定 0 PR(attempts={info.get('attempts')},deadline={info.get('deadline_s')}s),退避重试")
            elif st == "HOLD":
                print(f"[ctrl][L2] {run_id} HOLD: {info}")
            elif st in ("RETRY", "RETRY_HEAD_UNSETTLED", "RETRY_INCONSISTENT"):
                print(f"[ctrl][L2] {run_id} {st}({info.get('reason','')}),不累加,下轮重试")
            elif st == "CONCURRENT":
                print(f"[ctrl][L2] {run_id} CONCURRENT({info.get('reason','')}),跳过")
            elif st == "DEGRADED":
                print(f"[ctrl][L2] {run_id} discover DEGRADED({info.get('reason','')}) → 本 tick 停 discover")
                break   # B4c.1.1 #3:breaker 已开,finally 解锁后退出 disc 循环
            else:   # AMBIGUOUS / HOLD_PR_NOT_OPEN / HOLD_IDENTITY / HOLD_NO_REPO / HOLD_BINDING_CONFLICT
                print(f"[ctrl][L2] {run_id} HOLD: {st} {info}")
        finally:
            try:
                lc2 = ensure_pg()
                with lc2.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("disc:" + run_id,))
                    cur.fetchone()
                lc2.commit()
            except Exception:
                pass

    # B4c-2:l2_awaiting_ticket → 幂等建票(per-run session advisory lock)
    for run_id in ticket_runs:
        if _budget_exhausted(deadline) or not _tick_take(budget):
            break   # B4c.1 工作预算到期
        if _l2_gw_degraded():
            break   # B4c.1.1 #3:circuit breaker 打开 → 停建票
        lc = ensure_pg()
        try:
            with lc.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", ("ticket:" + run_id,))
                got = cur.fetchone()[0]
            lc.commit()
            if not got:
                continue   # B4c.1:另一 Controller 持锁 → 跳过(不阻塞)
        except Exception as e:
            print(f"[ctrl][L2] {run_id} ticket advisory_lock 异常: {e}"); continue
        try:
            st, info = create_ticket_for_run(run_id)
            if st == "CREATED":
                print(f"[ctrl][L2] {run_id} 建票 {st}: ticket={str(info.get('ticket_id',''))[:20]} pr={info.get('pr_number')} hash={info.get('args_hash')} → l2_awaiting_approval")
            elif st == "CONCURRENT":
                print(f"[ctrl][L2] {run_id} 建票 CONCURRENT({info.get('reason','')}),跳过")
            elif st in ("HOLD_NO_BINDING", "HOLD_TICKET_CONFLICT"):
                print(f"[ctrl][L2] {run_id} 建票 HOLD: {st} {info}")
            else:   # RETRY
                print(f"[ctrl][L2] {run_id} 建票 {st}({info.get('reason','')}),下轮重试")
        finally:
            try:
                lc2 = ensure_pg()
                with lc2.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("ticket:" + run_id,))
                    cur.fetchone()
                lc2.commit()
            except Exception:
                pass


L2_ACTION_TOOL = {"merge": "merge_pull_request", "close": "update_pull_request"}


def _advance_outbox_by_approval(oid, ticket_id, action, outcome):
    """B4c-3 边界② + B4c.1 结构化 outcome:Gateway 返回/超时后读 **approvals 权威态**(l2_* 已写 status)。
    **关键顺序**:先读 approval 状态;只有**仍 APPROVED**(Gateway 未 claim)时才按 outcome 分类;
    approval 已迁移(USED/FAILED/UNKNOWN/EXECUTING)时**绝不**被 outcome 覆盖(复审 #4)。
    返回 "DEGRADED"(drain 应本 tick 停消费)或 None。
    action-aware:merge USED→task MERGED/l2_done;close USED→task HOLD/verified-closed。
    task 终态用完整 CAS(approval_required+APPROVAL_PENDING+l2_awaiting_approval);CAS 失败→CONCURRENT_STATE_CHANGE。
    边界③:UNKNOWN/EXECUTING/APPROVED 绝不自动重 merge。"""
    if outcome is None:
        outcome = GatewayOutcome("SUCCESS")   # 兼容对账收敛调用(无 outcome)
    conn = ensure_pg()
    signal = None
    with conn.cursor() as cur:
        cur.execute("SELECT status, result_sha, run_id FROM approvals WHERE ticket_id=%s", (ticket_id,))
        r = cur.fetchone()
        if not r:
            conn.commit(); return None
        astatus, result_sha, run_id = r
        cas_clause = "approval_required=TRUE AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_approval'"
        if astatus == "APPROVED":
            # Gateway 未 claim(approval 仍 APPROVED)→ 按 outcome 分类(绝不覆盖已迁移态)
            cur.execute("SELECT attempts FROM policy_action_outbox WHERE id=%s", (oid,))
            _ar = cur.fetchone(); delay = _l2_backoff_seconds(_ar[0] if _ar else 0)
            if outcome.kind == "TICKET_DENY":
                # B4c.1.3:完整三字段 CAS —— 锁 task FOR UPDATE + 校验 approval_required+status+current_stage;
                #   再 l2_reject_approved(approval CAS);task UPDATE 断言 rowcount==1;**任一不命中 → 回滚,outbox 不动**。
                cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
                _tr = cur.fetchone()
                if not _tr or not (bool(_tr[0]) and _tr[1] == 'APPROVAL_PENDING' and _tr[2] == 'l2_awaiting_approval'):
                    conn.rollback()   # task 已并发迁移或缺 approval_required → 不终结
                    return None
                cur.execute("SELECT l2_reject_approved(%s,%s)", (ticket_id, outcome.reason_code))
                if not cur.fetchone()[0]:
                    conn.rollback()   # approval 已并发迁移(claim/恰好过期)→ 不终结(task/outbox 不动)
                    return None
                cur.execute(f"UPDATE task_runs SET status='HOLD', current_stage='l2_drain_denied', last_error=%s, updated_at=now() WHERE run_id=%s AND {cas_clause}", (f"preclaim denied:{outcome.reason_code}", run_id))
                if cur.rowcount != 1:
                    conn.rollback()   # task CAS 未命中(并发)→ 不终结 outbox
                    return None
                cur.execute("UPDATE policy_action_outbox SET status='FAILED', completed_at=now(), last_error_code=%s, error=%s WHERE id=%s", (outcome.reason_code, f"preclaim denied:{outcome.reason_code}", oid))
            elif outcome.kind == "GLOBAL_DEGRADED":
                # 全局配置故障:退回 PENDING_DISPATCH + 退避;signal 让 drain 本 tick 停消费
                cur.execute("UPDATE policy_action_outbox SET status='PENDING_DISPATCH', last_error_code=%s, error=%s, next_retry_at=now()+make_interval(secs=>%s) WHERE id=%s", (outcome.reason_code, f"global degraded:{outcome.reason_code}", delay, oid))
                signal = "DEGRADED"
            else:  # TRANSIENT(网络/超时/L2_DB_UNAVAILABLE)或 SUCCESS-race(未 claim)→ 退避重试
                # approval 留 APPROVED,outbox 留 DISPATCHED,设 next_retry_at(attempts 已在领取 +1,等待期不再 +)
                cur.execute("UPDATE policy_action_outbox SET last_error_code=%s, error=%s, next_retry_at=now()+make_interval(secs=>%s) WHERE id=%s", (outcome.kind, outcome.detail[:200], delay, oid))
        elif astatus == "USED":
            cur.execute("UPDATE policy_action_outbox SET status='SUCCEEDED', result_sha=%s, completed_at=now() WHERE id=%s", (result_sha, oid))
            if action == "close":
                cur.execute(f"UPDATE task_runs SET status='HOLD', current_stage='verified-closed', updated_at=now() WHERE run_id=%s AND {cas_clause}", (run_id,))
            else:  # merge
                cur.execute(f"UPDATE task_runs SET status='MERGED', current_stage='l2_done', updated_at=now() WHERE run_id=%s AND {cas_clause}", (run_id,))
            if cur.rowcount == 0:
                cur.execute("UPDATE policy_action_outbox SET error=%s WHERE id=%s", ("CONCURRENT_STATE_CHANGE: task 已脱离 APPROVAL_PENDING/l2_awaiting_approval,未覆盖"[:200], oid))
        elif astatus == "FAILED":
            # Gateway claim 后失败(approval 已 FAILED):task HOLD(l2_drain_failed)+ outbox FAILED
            cur.execute(f"UPDATE task_runs SET status='HOLD', current_stage='l2_drain_failed', last_error=%s, updated_at=now() WHERE run_id=%s AND {cas_clause}", ((f"{action} FAILED: {outcome.detail or ''}")[:300], run_id))
            err_msg = (outcome.detail or f"{action} FAILED")
            if cur.rowcount == 0:
                err_msg = f"{err_msg} | CONCURRENT_STATE_CHANGE: task 未同步(已脱离 APPROVAL_PENDING/l2_awaiting_approval)"
            cur.execute("UPDATE policy_action_outbox SET status='FAILED', completed_at=now(), last_error_code=%s, error=%s WHERE id=%s", ("CLAIM_FAILED", err_msg[:300], oid))
        elif astatus == "UNKNOWN":
            cur.execute("UPDATE policy_action_outbox SET status='UNKNOWN', error=%s WHERE id=%s", ((outcome.detail or "marked unknown")[:200], oid))
            # task 留 APPROVAL_PENDING,交对账(绝不重 merge)
        # EXECUTING → outbox 留 DISPATCHED(不动),lease/对账兜底
    conn.commit()
    print(f"[ctrl][L2] drain ticket={ticket_id[:16]} action={action} → approval={astatus}" + (f" outcome={outcome.kind}/{outcome.reason_code}" if outcome and outcome.kind != "SUCCESS" else ""))
    return signal


def drain_l2_outbox(deadline=None, budget=None):
    """B4c-3 lease drain + B4c.1 typed outcome / 退避 / 确定性拒绝 / circuit breaker / 工作预算。
    派发条件:APPROVED 且 expires_at>now 且 (next_retry_at IS NULL OR next_retry_at<=now)。
    边界:① 领取事务调 Gateway 前提交;② 读 approvals.status 权威态;③ UNKNOWN/EXECUTING 不重 merge;
          ④ 仅 approval 仍 APPROVED 时按 outcome 分类。attempts 每次真实派发 +1;等待期不 +1。
    circuit breaker:降级期(_l2_gw_degraded)整体跳过;TRANSIENT/GLOBAL_DEGRADED → 打开 breaker + 本 tick 停;
      SUCCESS → 关 breaker。工作预算:deadline(run_forever 传)到期不领下一条;单次 GW 超时 ≤ 剩余预算。"""
    import gateway_client
    if _l2_gw_degraded():
        print(f"[ctrl][L2] drain 跳过:Gateway degraded({_L2_GW['last_error'][:50]}),待恢复")
        return
    for _ in range(L2_MAINTENANCE_MAX_ITEMS):
        if _budget_exhausted(deadline) or not _tick_take(budget):
            break
        conn = ensure_pg()
        with conn.cursor() as cur:
            cur.execute("""SELECT o.id, o.ticket_id, a.canonical_payload, a.action
                           FROM policy_action_outbox o JOIN approvals a ON o.ticket_id=a.ticket_id
                           WHERE a.status='APPROVED' AND a.expires_at > now()
                             AND (o.next_retry_at IS NULL OR o.next_retry_at <= now())
                             AND ( (o.status='PENDING_DISPATCH')
                                OR (o.status='DISPATCHED' AND o.lease_expires_at IS NOT NULL AND o.lease_expires_at < now()) )
                           ORDER BY o.id FOR UPDATE SKIP LOCKED LIMIT 1""")
            item = cur.fetchone()
            if not item:
                conn.commit(); break
            oid, ticket_id, payload, action = item
            # 每次真实派发 attempts +1(首派 + lease 重派;SKIP LOCKED 未取得的不在 items,不加);重置 next_retry_at=now(立即再合格;outbox.next_retry_at NOT NULL)
            cur.execute("""UPDATE policy_action_outbox SET status='DISPATCHED',
                             lease_expires_at = now() + make_interval(secs => %s),
                             dispatched_at = now(), attempts = attempts + 1, next_retry_at = now()
                           WHERE id=%s""", (L2_LEASE_SECONDS, oid))
        conn.commit()   # 边界①:提交领取(逐条),释放 FOR UPDATE,再调 Gateway
        gw_to = _gw_timeout_for(deadline)   # B4c.1.3:统一用 _gw_timeout_for(≤剩余,无 5s 下限)
        tool = L2_ACTION_TOOL.get(action)
        if not tool:
            _advance_outbox_unknown(oid, ticket_id, f"未知 action {action}"); continue
        call_args = dict(payload) if isinstance(payload, dict) else {}
        call_args["approval_ticket"] = ticket_id
        outcome = GatewayOutcome("SUCCESS")
        try:
            gateway_client.gateway_call(tool, call_args, timeout=gw_to)
        except gateway_client.GatewayDenied as e:           # 票据级确定性拒绝
            outcome = GatewayOutcome("TICKET_DENY", e.reason_code, e.detail)
        except gateway_client.GatewayGlobalDegraded as e:   # 全局配置故障
            outcome = GatewayOutcome("GLOBAL_DEGRADED", e.reason_code, e.detail)
        except gateway_client.GatewayUnavailable as e:      # 瞬时(网络/超时/L2_DB_UNAVAILABLE)
            outcome = GatewayOutcome("TRANSIENT", "", str(e)[:160])
        except Exception as e:                              # 未分类异常 → 保守瞬时
            outcome = GatewayOutcome("TRANSIENT", "", f"{type(e).__name__}: {str(e)[:120]}")
        # 边界②:读 approvals 权威态推进(边界③:UNKNOWN/EXECUTING 不重 merge;④ 仅 APPROVED 按 outcome 分类)
        sig = _advance_outbox_by_approval(oid, ticket_id, action, outcome)
        # circuit breaker:成功关;瞬时/全局降级 → 打开 breaker + 本 tick 停消费(防连环撞 Gateway)
        if outcome.kind == "SUCCESS":
            _l2_gw_ok()
        elif outcome.kind in ("TRANSIENT", "GLOBAL_DEGRADED"):
            _l2_gw_mark_degraded(outcome.reason_code or outcome.kind)
            print(f"[ctrl][L2] Gateway {outcome.kind}({outcome.reason_code or outcome.detail[:40]}) → circuit breaker 打开,本 tick 停止消费 outbox")
            break
        if sig == "DEGRADED":
            break


def _advance_outbox_unknown(oid, ticket_id, reason):
    """未知 action → outbox UNKNOWN(不调 Gateway)。"""
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("UPDATE policy_action_outbox SET status='UNKNOWN', error=%s WHERE id=%s", (reason[:200], oid))
    conn.commit()


def _l2_outbox_backoff(oid, reason):
    """B4c.1.1 #2:outbox 退避(next_retry_at=now+backoff),供 reconcile 读失败持久化退避(防反复阻塞后续对账项)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT attempts FROM policy_action_outbox WHERE id=%s", (oid,))
            r = cur.fetchone()
            cur.execute("UPDATE policy_action_outbox SET next_retry_at=now()+make_interval(secs=>%s), last_error_code=%s WHERE id=%s",
                        (_l2_backoff_seconds(r[0] if r else 0), (reason or "")[:40], oid))
        conn.commit()
    except Exception:
        conn.rollback()


def _reconcile_ticket(ticket_id, run_id, action, astatus, pr_num, repo, oid, deadline=None):
    """B4c-4:对账单张 UNKNOWN/超时EXECUTING 票。读 GitHub 权威态 → l2_reconcile_*。
    effect_applied:merge→prd.merged;close→state==closed AND NOT merged(收紧 #7)。
    B4c.1.1 #2:gateway_read_pr RETRY → 持久化退避(outbox next_retry_at)防反复阻塞后续对账项;
    B4c.1.1 #3:GatewayGlobalDegraded → 打开 circuit breaker + 返回 False。
    返回 bool(l2_reconcile_* 返回值):True=已迁移(USED/FAILED),False=未迁移/读失败(下轮重试)。"""
    import gateway_client
    owner, _, repo_name = repo.partition("/")
    try:
        rstatus, prd = gateway_client.gateway_read_pr(owner, repo_name, pr_num, timeout=_gw_timeout_for(deadline))
        _l2_gw_ok()   # B4c.1.3 P2:reconcile 读成功 → 清 breaker failure_count
    except gateway_client.GatewayGlobalDegraded as e:
        _l2_gw_mark_degraded(e.reason_code)
        print(f"[ctrl][L2] reconcile {ticket_id[:16]}: Gateway GLOBAL_DEGRADED({e.reason_code}) → breaker")
        return False
    except gateway_client.GatewayUnavailable as e:
        # B4c.1.2 #2:网络/401 瞬时 → outbox 退避 + 打开 breaker
        _l2_outbox_backoff(oid, "reconcile unavailable"); _l2_gw_mark_degraded("UNAVAILABLE", seconds=L2_RETRY_BASE_SECONDS)
        print(f"[ctrl][L2] reconcile {ticket_id[:16]}: Gateway unavailable → 退避 + breaker"); return False
    except gateway_client.GatewayDenied as e:
        # B4c.1.2 #2:确定性拒绝(REPO_NOT_ALLOWED 等)→ outbox 退避(不无限撞;UNKNOWN/EXECUTING 票留待人工)
        _l2_outbox_backoff(oid, f"reconcile denied:{e.reason_code}")
        print(f"[ctrl][L2] reconcile {ticket_id[:16]}: Gateway denied({e.reason_code}) → 退避"); return False
    if rstatus == "RETRY":
        _l2_outbox_backoff(oid, "reconcile read RETRY")   # B4c.1.1 #2
        return False   # 读失败,退避后下轮重试
    if action == "close":
        effect_applied = (prd["state"] == "closed" and not prd["merged"])
    else:  # merge
        effect_applied = prd["merged"]
    actual_sha = prd.get("merge_commit_sha") or prd.get("head_sha") or ""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            if astatus == "UNKNOWN":
                cur.execute("SELECT l2_reconcile_unknown(%s,%s,%s)", (ticket_id, effect_applied, actual_sha))
            else:  # EXECUTING
                cur.execute("SELECT l2_reconcile_executing(%s,%s,%s)", (ticket_id, effect_applied, actual_sha))
            ok_bool = cur.fetchone()[0]
        conn.commit()
        new_st = "USED" if effect_applied else "FAILED"
        print(f"[ctrl][L2] reconcile {ticket_id[:16]} {astatus}→{new_st if ok_bool else '(未迁移)'} (effect={effect_applied})")
        return bool(ok_bool)
    except Exception as e:
        conn.rollback()
        print(f"[ctrl][L2] reconcile {ticket_id[:16]} 异常: {e}")
        return False


def reconcile_l2(deadline=None, budget=None):
    """B4c-4(+4.1):延迟对账 + 过期收敛 + 全状态收敛(读 GitHub 实际态,**绝不自动重 merge**)。
    ① UNKNOWN/EXECUTING 且 executing_at<now()-120s(延迟防竞态;B4c-4.1 P1-1:UNKNOWN 也需延迟)
       → gateway_read_pr → l2_reconcile_* → **复用 _advance_outbox_by_approval 收敛 outbox+task**(P1-2);
    ③ PENDING 过期 → l2_expire_pending → EXPIRED + outbox FAILED + task HOLD;
    ④ APPROVED 过期 → l2_expire_approved → EXPIRED + outbox FAILED + task HOLD;
    ⑤ 滞留 outbox(DISPATCHED 或 UNKNOWN)+ approval 已终结(USED/FAILED)
       → **复用 _advance_outbox_by_approval**(P1-3:读 action + action-aware task 推进 + 完整 CAS)。"""
    # ① + ②:UNKNOWN/超时EXECUTING 延迟对账(B4c-4.1:两者都需 executing_at<now()-120s)
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT a.ticket_id, a.run_id, a.action, a.status, a.pr_number, a.repo, o.id
                       FROM approvals a JOIN policy_action_outbox o ON a.ticket_id=o.ticket_id
                       WHERE a.executing_at IS NOT NULL AND a.executing_at < now() - interval '120 seconds'
                         AND a.status IN ('UNKNOWN','EXECUTING')
                         AND a.pr_number IS NOT NULL AND a.repo IS NOT NULL
                         AND o.next_retry_at <= now()
                       ORDER BY o.next_retry_at, o.id LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
        reconcile_items = cur.fetchall()
    conn.commit()
    for ticket_id, run_id, action, astatus, pr_num, repo, oid in reconcile_items:
        if _budget_exhausted(deadline) or not _tick_take(budget):
            break   # B4c.1 工作预算到期,剩余对账项下 tick
        if _l2_gw_degraded():
            break   # B4c.1.1 #3:circuit breaker 打开 → 停对账
        changed = _reconcile_ticket(ticket_id, run_id, action, astatus, pr_num, repo, oid, deadline)
        if changed:
            # B4c-4.1 P1-2:对账后复用 _advance_outbox_by_approval 收敛 outbox+task(action-aware + 完整 CAS)
            _advance_outbox_by_approval(oid, ticket_id, action, None)

    # ③ + ④:过期 PENDING/APPROVED → EXPIRED + outbox FAILED + task HOLD
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT ticket_id FROM approvals
                       WHERE status='PENDING' AND approval_expires_at < now() LIMIT 100""")
        for (tid,) in cur.fetchall():
            cur.execute("SELECT l2_expire_pending(%s)", (tid,))
        cur.execute("""SELECT ticket_id FROM approvals
                       WHERE status='APPROVED' AND expires_at IS NOT NULL AND expires_at < now() LIMIT 100""")
        for (tid,) in cur.fetchall():
            cur.execute("SELECT l2_expire_approved(%s)", (tid,))
        cur.execute("""SELECT a.ticket_id, a.run_id FROM approvals a
                       WHERE a.status='EXPIRED'
                         AND EXISTS (SELECT 1 FROM policy_action_outbox o WHERE o.ticket_id=a.ticket_id AND o.status != 'FAILED') LIMIT 100""")
        for tid, run_id in cur.fetchall():
            # B4c-4.2 P1:task 用**完整 CAS**(approval_required+APPROVAL_PENDING+l2_awaiting_approval);
            # CAS 失败(rowcount=0)→ 不覆盖 task,outbox.error 追加 CONCURRENT_STATE_CHANGE(对称于 drain USED/FAILED)
            cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_expired', last_error='ticket EXPIRED', updated_at=now() WHERE run_id=%s AND approval_required=TRUE AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_approval'", (run_id,))
            exp_err = "ticket EXPIRED"
            if cur.rowcount == 0:
                exp_err = "ticket EXPIRED | CONCURRENT_STATE_CHANGE: task 已脱离 APPROVAL_PENDING/l2_awaiting_approval,未覆盖"
            cur.execute("UPDATE policy_action_outbox SET status='FAILED', completed_at=now(), error=%s WHERE ticket_id=%s AND status != 'FAILED'", (exp_err[:300], tid))
            print(f"[ctrl][L2] expire {tid[:16]} → outbox FAILED" + (" (task CAS 失败,未覆盖)" if cur.rowcount == 0 else " + task HOLD"))
    conn.commit()

    # ⑤ 滞留 outbox(DISPATCHED 或 UNKNOWN)+ approval 已终结(USED/FAILED)
    #   B4c-4.1 P1-3:复用 _advance_outbox_by_approval(action-aware + 完整 CAS,不再手写映射)
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT o.id, o.ticket_id, a.action
                       FROM policy_action_outbox o JOIN approvals a ON o.ticket_id=a.ticket_id
                       WHERE o.status IN ('DISPATCHED','UNKNOWN') AND a.status IN ('USED','FAILED') LIMIT 100""")
        stranded = cur.fetchall()
    conn.commit()
    for oid, ticket_id, action in stranded:
        _advance_outbox_by_approval(oid, ticket_id, action, GatewayOutcome("SUCCESS", "", "reconcile stranded convergence"))


# ── 主循环(故障域分离,复审 #3;L2 维护始终运行,B4c-0.1 #3)──
#   域 A(PG-驱动 L2):绑定/建票/drain/对账 —— 始终运行(按 approval_required 自过滤),Matrix 挂也照跑。
#   域 B(Matrix):login/consume/dispatch —— 独立;Matrix 不可用不阻断 L2 恢复。
def run_forever():
    backoff = 1
    print(f"[ctrl] 启动;PG={PG_HOST}:{PG_PORT};Matrix={MATRIX_HS};POLL={POLL_INTERVAL}s;L2_MERGE_ENABLED={'on' if L2_MERGE_ENABLED else 'off'}")
    while True:
        # ── 故障域 A:PG-驱动 L2(始终运行,不 gated by 开关;函数内部按 approval_required 过滤)──
        try:
            ensure_pg()
            l2_deadline = time.monotonic() + L2_MAINTENANCE_BUDGET_SECONDS   # B4c.1 单循环工作预算(共享)
            l2_budget = [L2_MAINTENANCE_MAX_ITEMS]   # B4c.1.3:每 tick 共享 item 预算(整轮硬边界,跨阶段)
            initiate_l2_pending(l2_deadline, l2_budget)
            drain_l2_outbox(l2_deadline, l2_budget)
            reconcile_l2(l2_deadline, l2_budget)
        except psycopg2.OperationalError as e:
            print(f"[ctrl] L2 域 PG degraded: {e}; reconnecting in {backoff}s")
            reset_pg()
            time.sleep(backoff); backoff = min(backoff * 2, 30)
            continue
        except Exception as e:
            print(f"[ctrl] L2 域 unexpected: {type(e).__name__}: {e}; retry in 5s")
            time.sleep(5)

        # ── 故障域 B:Matrix(login/consume/dispatch)──
        try:
            ensure_matrix_login()
            consume_events()
            drain_outbox()
            backoff = 1
        except MatrixUnavailable as e:
            print(f"[ctrl] Matrix degraded: {e}; backoff={backoff}s (L2 域继续运行)")
            time.sleep(backoff); backoff = min(backoff * 2, 30)
            continue
        except MatrixRejected as e:
            print(f"[ctrl] Matrix rejected: {e}; skip")
        except psycopg2.OperationalError as e:
            print(f"[ctrl] Matrix 域 PG degraded: {e}; reconnecting in {backoff}s")
            reset_pg()
            time.sleep(backoff); backoff = min(backoff * 2, 30)
            continue
        except Exception as e:
            print(f"[ctrl] Matrix 域 unexpected: {type(e).__name__}: {e}; retry in 5s")
            time.sleep(5)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    if not ADMIN_PW or not PG_PASS:
        print("ERROR: 需 ADMIN_PW + PG_PASS 环境变量"); sys.exit(1)
    startup_assert_l2()
    # B4c-0.2:部署预检模式——仅跑 startup_assert_l2 后 exit 0/1,供 start 脚本替换前
    #   用同一镜像+env 预检(startup_assert 失败时上面已 sys.exit 非零)。通过后才替换旧容器。
    if os.environ.get("STARTUP_CHECK_ONLY", "0").strip().lower() in ("1", "true", "yes"):
        print("[ctrl] STARTUP_CHECK_ONLY: startup_assert passed → exit 0")
        sys.exit(0)
    run_forever()
