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
import os, sys, json, time, re, hashlib, psycopg2, urllib.request, urllib.error

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
# 与 l2_reconcile_executing 内 SQL 的 `interval '120 seconds'` 保持一致(UNKNOWN/超时EXECUTING 延迟对账阈值,复审 #6)
L2_RECONCILE_AGE  = int(os.environ.get("L2_RECONCILE_AGE", "120"))
L2_LEASE_SECONDS  = int(os.environ.get("L2_LEASE_SECONDS", "60"))   # outbox DISPATCHED lease
L2_DISCOVERY_MAX  = int(os.environ.get("L2_DISCOVERY_MAX", "3"))    # 0-PR 有界重试上限 → HOLD
L2_GW_TIMEOUT     = int(os.environ.get("L2_GW_TIMEOUT", "60"))      # 单次 Gateway MCP 调用总超时(含 SSE+initialize)

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


def startup_assert_l2():
    """fail-closed 启动断言(B4c-0.1 #3):
    - 非法 L2_MERGE_ENABLED 值 → 拒启动(不静默当 false)。
    - 若存在非终态 approval_required=TRUE 的 run,但缺 COORDINATOR_TOKEN/GATEWAY_URL/Gateway 不可达 →
      拒启动(否则这些 run 永久卡死,无人维护)。
    - L2_MERGE_ENABLED=1(新 run 默认进审批流)→ 额外要求 token/url/Gateway 连通/l2 函数可 EXECUTE。
    L2 维护循环本身始终运行(函数按 approval_required 过滤),开关只管新 run 默认值。"""
    if L2_MERGE_ENABLED_INVALID:
        sys.exit(f"[ctrl] FATAL: L2_MERGE_ENABLED='{_L2_RAW}' 非法(允许:0/1/true/false/yes/no/on/off)")

    # 是否存在"需要维护但尚未终结"的审批 run
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM task_runs
                       WHERE approval_required AND status NOT IN ('PASS','FAIL','HOLD','MERGED','ROLLED_BACK');""")
        pending_l2 = cur.fetchone()[0]
    conn.commit()  # 释放只读事务(防 idle-in-transaction)

    need_gateway = bool(pending_l2) or L2_MERGE_ENABLED
    if need_gateway:
        if not COORDINATOR_TOKEN:
            sys.exit(f"[ctrl] FATAL: 有 {pending_l2} 个未终结审批 run(L2_MERGE_ENABLED={'on' if L2_MERGE_ENABLED else 'off'}),但缺 COORDINATOR_TOKEN → 这些 run 会卡死")
        if not GATEWAY_URL:
            sys.exit("[ctrl] FATAL: 审批流需要 GATEWAY_URL")
        if not _gateway_reachable():
            sys.exit(f"[ctrl] FATAL: Gateway {GATEWAY_URL} 不可达(TCP 探测失败)—— 先起 policy-gw")
        with conn.cursor() as cur:
            cur.execute("SELECT has_function_privilege('mergepilot','l2_ensure_ticket(text,text,jsonb,text,integer,integer)','EXECUTE'),"
                        "       has_function_privilege('mergepilot','l2_expire_approved(text)','EXECUTE');")
            ok = cur.fetchone()
        conn.commit()
        if not ok or not (ok[0] and ok[1]):
            sys.exit(f"[ctrl] FATAL: L2 函数不可 EXECUTE(ensure={ok[0] if ok else None} expire={ok[1] if ok else None});先应用 m3b_b4c.sql")

    mode = "on" if L2_MERGE_ENABLED else "off"
    print(f"[ctrl] L2_MERGE_ENABLED={mode};未终结审批 run={pending_l2};Gateway={GATEWAY_URL if need_gateway else '(未启用)'};reconcile_age={L2_RECONCILE_AGE}s")


# ── B4c L2 主循环函数(B4c-1..4 填充实现;B4c-0.1 为安全 no-op 占位,不抛错)──
# 事务边界契约(B4c-0.1 #4):每个扫描函数只读 SELECT 后必须 commit() 释放事务,防 idle-in-transaction。
#   B4c-1+ 的写路径(建票/drain/reconcile)各自开短事务,显式 commit/rollback,不复用扫描事务。
def initiate_l2_pending():
    """扫 approval_required=TRUE 且 status='APPROVAL_PENDING' AND current_stage IN('l2_binding','l2_awaiting_ticket'):
    绑定发现(B4c-1)+ 幂等建票 l2_ensure_ticket(B4c-2)。B4c-0.1 占位。"""
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM task_runs
                       WHERE approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_binding';""")
        n = cur.fetchone()[0]
    conn.commit()
    if n:
        print(f"[ctrl][L2-stub] initiate_l2_pending: {n} 个 run 待绑定/建票(B4c-1/B4c-2 实现后处理)")


def drain_l2_outbox():
    """policy_action_outbox:PENDING_DISPATCH+APPROVED → DISPATCHED+lease → Gateway merge → 推进。B4c-0.1 占位。"""
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM policy_action_outbox o
                       JOIN approvals a ON o.ticket_id=a.ticket_id
                       WHERE o.status='PENDING_DISPATCH' AND a.status='APPROVED';""")
        n = cur.fetchone()[0]
    conn.commit()
    if n:
        print(f"[ctrl][L2-stub] drain_l2_outbox: {n} 个 APPROVED 票待派发(B4c-3 实现后处理)")


def reconcile_l2():
    """UNKNOWN/超时EXECUTING/滞留DISPATCHED/过期 状态收敛(读 GitHub 实际态)。B4c-0.1 占位。"""
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT
            (SELECT count(*) FROM approvals WHERE status='UNKNOWN'),
            (SELECT count(*) FROM approvals WHERE status='EXECUTING' AND executing_at < now() - interval '120 seconds'),
            (SELECT count(*) FROM policy_action_outbox WHERE status='DISPATCHED');""")
        unk, exe, disp = cur.fetchone()
    conn.commit()
    if unk or exe or disp:
        print(f"[ctrl][L2-stub] reconcile_l2: UNKNOWN={unk} 超时EXECUTING={exe} 滞留DISPATCHED={disp}(B4c-4 实现后处理)")


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
            initiate_l2_pending()
            drain_l2_outbox()
            reconcile_l2()
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
