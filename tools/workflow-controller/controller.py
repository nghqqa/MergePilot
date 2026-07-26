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
    print(f"[ctrl] Matrix login OK (token={_token[:12]}...)")
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
            cur.execute("""INSERT INTO task_runs(run_id, room_id, repo, pr_number, branch, status, current_stage)
                           VALUES(%s, %s, %s, %s, %s, 'RUNNING', 'review')
                           ON CONFLICT(run_id) DO NOTHING""", (
                run_id, room_id, payload.get("repo"), payload.get("pr_number"), payload.get("branch")))
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
            cur.execute("UPDATE task_runs SET status=%s, verdict=%s, updated_at=now() WHERE run_id=%s",
                        ('PASS' if verdict == 'PASS' else 'HOLD', verdict, run_id))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, "verify")
            conn.commit()
            print(f"[ctrl] {run_id}-verify VERDICT={verdict} → task {'PASS' if verdict=='PASS' else 'HOLD'} | PG committed")
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

# ── 主循环 ──
def run_forever():
    backoff = 1
    print(f"[ctrl] 启动;PG={PG_HOST}:{PG_PORT};Matrix={MATRIX_HS};POLL={POLL_INTERVAL}s")
    while True:
        try:
            ensure_pg()
            ensure_matrix_login()
            consume_events()
            drain_outbox()
            backoff = 1
            time.sleep(POLL_INTERVAL)
        except MatrixUnavailable as e:
            print(f"[ctrl] Matrix degraded: {e}; backoff={backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except psycopg2.OperationalError as e:
            print(f"[ctrl] PG degraded: {e}; reconnecting in {backoff}s")
            reset_pg()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except MatrixRejected as e:
            print(f"[ctrl] Matrix rejected: {e}; skip")
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            print(f"[ctrl] unexpected: {type(e).__name__}: {e}; retry in 5s")
            time.sleep(5)

if __name__ == "__main__":
    if not ADMIN_PW or not PG_PASS:
        print("ERROR: 需 ADMIN_PW + PG_PASS 环境变量"); sys.exit(1)
    run_forever()
