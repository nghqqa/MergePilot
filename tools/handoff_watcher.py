#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""handoff_watcher.py — 确定性阶段交接 watcher(M1 兜底)。
监听 admin+manager+{reviewer,fixer,verifier} 房间的 TASK_COMPLETED 消息,
一旦出现就自动 nudge manager 派下一阶段,保证 review→fix→verify 零人工 nudge。
在 hiclaw-manager 容器里常驻运行。用法:python3 handoff_watcher.py <admin_password>
"""
import sys, json, time, re, os, urllib.request, urllib.error

HS = "http://hiclaw-controller:6167"
ADMIN = "admin"
POLL = 8  # 秒
# M5-0B §14: M5 Candidate runs (m5live-*) are dispatched solely by the Candidate
# Controller + dispatch_outbox. The watcher MUST NOT nudge manager for them.
# `m5live-` is CODE-FORCED and can never be removed; the env var only APPENDS
# extra prefixes (P1-5). Exclusion happens at the claim/select stage, before send.
_M5_FORCED_EXCLUDE = ("m5live-",)
def _build_exclude():
    extra = []
    for p in os.environ.get("M5_WATCHER_EXCLUDE_PREFIXES", "").split(","):
        p = p.strip()
        if p and p not in _M5_FORCED_EXCLUDE and p not in extra:
            extra.append(p)
    return _M5_FORCED_EXCLUDE + tuple(extra)
_M5_EXCLUDE_PREFIXES = _build_exclude()
def _m5_excluded(prefix):
    return any(prefix.startswith(p) for p in _M5_EXCLUDE_PREFIXES)

def req(method, path, token=None, body=None, timeout=30):
    r = urllib.request.Request(HS+path, data=(json.dumps(body).encode() if body else None), method=method)
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("Authorization", "Bearer "+token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"_error": e.code}

def login(pw):
    return req("POST","/_matrix/client/v3/login", body={
        "type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

def joined_rooms(tok):
    return req("GET","/_matrix/client/v3/joined_rooms",token=tok).get("joined_rooms",[])

def room_members(tok, rid):
    m = req("GET", f"/_matrix/client/v3/rooms/{rid}/joined_members", token=tok).get("joined",{})
    return set(k.split(":")[0].lstrip("@") for k in m.keys())

def manager_room(tok):
    for rid in joined_rooms(tok):
        if room_members(tok, rid) == {"admin","manager"}:
            return rid
    return None

def watched_rooms(tok):
    """返回 [(room_id, members_set)] 涉及 manager+worker 的房间。"""
    out=[]
    for rid in joined_rooms(tok):
        m = room_members(tok, rid)
        if "manager" in m and any(w in m for w in ("reviewer","fixer","verifier")):
            out.append((rid, m))
    return out

def recent_msgs(tok, rid, n=12):
    d = req("GET", f"/_matrix/client/v3/rooms/{rid}/messages?dir=b&limit={n}", token=tok)
    evs=[]
    for e in reversed(d.get("chunk",[])):
        if e.get("type")=="m.room.message":
            evs.append((e.get("event_id"), e.get("sender","").split(":")[0].lstrip("@"),
                        e.get("content",{}).get("body",""), e.get("origin_server_ts",0)))
    return evs

def send(tok, room, text):
    txn="hw%d" % int(time.time()*1000)
    return req("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
               token=tok, body={"msgtype":"m.room.text","body":text})

# TASK_COMPLETED 阶段 → 给 manager 的交接指令模板
TRANSITIONS = [
    (re.compile(r"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-review", re.I),
     "[handoff-watcher] reviewer 完成 {p}-review。请**立即**派 fixer:据已产出的 findings 用 gh-mcp-fix.sh 提修复 PR(L2 密钥/依赖/删除类只出方案标 needs-approval)。不要等 admin。"),
    (re.compile(r"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-fix", re.I),
     "[handoff-watcher] fixer 完成 {p}-fix。请**立即**派 verifier:用 gh-mcp-read.sh 读修复分支 + 逐项比对。不要等 admin。"),
    (re.compile(r"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-verify", re.I),
     "[handoff-watcher] verifier 完成 {p}-verify。请**立即**汇总出最终裁定:全 PASS 且无 L2→建议 merge;有 L2→hold;有 fail→rollback/hold。"),
]

def process_batch(tok, seen, mroom, watched):
    """One selection pass over watched rooms. Extracted from main() so the
    m5live-* exclusion can be tested end-to-end with mocked I/O (P1-5). Returns
    the number of sends actually performed."""
    sends = 0
    for rid, members in watched:
        for eid, sender, body, ts in recent_msgs(tok, rid, 12):
            if eid in seen:
                continue
            seen.add(eid)
            # 只对 worker 发的 TASK_COMPLETED 反应
            if sender in ("reviewer", "fixer", "verifier"):
                for pat, tpl in TRANSITIONS:
                    m = pat.search(body)
                    if m:
                        prefix = m.group(1)
                        if _m5_excluded(prefix):
                            continue  # M5-0B §14: m5live-* 是 Candidate Controller 唯一调度权威
                        msg = tpl.format(p=prefix)
                        send(tok, mroom, msg)
                        sends += 1
                        print(f"[watcher] {sender} TASK_COMPLETED → 派 manager 推进({prefix})")
    return sends


def main():
    pw = os.environ.get("ADMIN_PW") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not pw:
        print("ERROR: 需通过 ADMIN_PW 环境变量或 argv 提供管理员密码"); sys.exit(1)
    tok = login(pw)
    if not tok: print("LOGIN FAILED"); sys.exit(1)
    mroom = manager_room(tok)
    watched = watched_rooms(tok)
    print(f"[watcher] manager_room={mroom}; watched worker rooms={len(watched)}")

    # 基线:只屏蔽 5 分钟前的旧消息(避免反应昨天的 gh-pr1 等);
    now_ms = time.time() * 1000
    BASELINE_MS = 5 * 60 * 1000
    seen=set()
    for rid,_ in watched:
        for eid,_,_,ts in recent_msgs(tok, rid, 20):
            if ts < now_ms - BASELINE_MS:
                seen.add(eid)
    print(f"[watcher] baseline(>{int(BASELINE_MS/60000)}min 前)屏蔽 {len(seen)} 条;近 5min + 未来 TASK_COMPLETED 会反应")

    while True:
        try:
            process_batch(tok, seen, mroom, watched)
        except Exception as e:
            print(f"[watcher] loop error: {e}")
        time.sleep(POLL)

if __name__ == "__main__":
    main()
