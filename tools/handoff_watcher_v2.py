#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""handoff_watcher_v2.py — per-task-room 版 handoff watcher。
每轮动态发现房间(含新建任务房间);在任务房间里检测到 TASK_COMPLETED,
就向同一房间的下一阶段 worker 发 真 @mention 任务(不靠 manager 中转)。
worker 已验证会响应 admin 的真 @mention。在 hiclaw-manager 容器常驻。
用法: ADMIN_PW=xxx python3 -u handoff_watcher_v2.py
"""
import sys, json, time, re, os, urllib.request, urllib.error
HS="http://hiclaw-controller:6167"; ADMIN="admin"; SERVER="matrix-local.hiclaw.io:18080"
POLL=8
# M5-0B §14: M5 Candidate runs (m5live-*) are dispatched solely by the Candidate
# Controller + dispatch_outbox. The watcher MUST NOT create a second dispatch
# for them. `m5live-` is CODE-FORCED and can never be removed; the env var only
# APPENDS extra prefixes (P1-5). Exclusion happens at the claim/select stage,
# before the fired set is touched.
_M5_FORCED_EXCLUDE=("m5live-",)
def _build_exclude():
    extra=[]
    for p in os.environ.get("M5_WATCHER_EXCLUDE_PREFIXES","").split(","):
        p=p.strip()
        if p and p not in _M5_FORCED_EXCLUDE and p not in extra:
            extra.append(p)
    return _M5_FORCED_EXCLUDE+tuple(extra)
_M5_EXCLUDE_PREFIXES=_build_exclude()
def _m5_excluded(prefix):
    return any(prefix.startswith(p) for p in _M5_EXCLUDE_PREFIXES)

def req(m,p,t=None,b=None,timeout=30):
    r=urllib.request.Request(HS+p,data=(json.dumps(b).encode() if b else None),method=m)
    r.add_header("Content-Type","application/json")
    if t: r.add_header("Authorization","Bearer "+t)
    try:
        with urllib.request.urlopen(r,timeout=timeout) as x: return json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e: return {}

def login(pw):
    return req("POST","/_matrix/client/v3/login",b={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

def room_members(t,rid):
    m=req("GET",f"/_matrix/client/v3/rooms/{rid}/joined_members",t=t).get("joined",{})
    return set(k.split(":")[0].lstrip("@") for k in m)

def discover_rooms(t):
    """每轮重新发现:含 manager + 至少一个 worker 的房间(含新建任务房间)。"""
    out=[]
    for rid in req("GET","/_matrix/client/v3/joined_rooms",t=t).get("joined_rooms",[]):
        m=room_members(t,rid)
        if "manager" in m and (m & {"reviewer","fixer","verifier"}):
            out.append((rid,m))
    return out

def recent(t,rid,n=12):
    d=req("GET",f"/_matrix/client/v3/rooms/{rid}/messages?dir=b&limit={n}",t=t)
    evs=[]
    for e in reversed(d.get("chunk",[])):
        if e.get("type")=="m.room.message":
            evs.append((e.get("event_id"),e.get("sender","").split(":")[0].lstrip("@"),e.get("content",{}).get("body","") or "",e.get("origin_server_ts",0)))
    return evs

def send_mention(t,rid,user,text):
    uid=f"@{user}:{SERVER}"
    txn="w2_%d"%int(time.time()*1000000)
    return req("PUT",f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",t=t,b={
        "msgtype":"m.room.text","body":f"@{user} {text}",
        "format":"org.matrix.custom.html","formatted_body":f'<a href="https://matrix.to/#/{uid}">{user}</a> {text}',
        "m.mentions":{"user":[uid]}
    })

# 阶段转移:(from_stage, 正则, next_worker, 任务模板)。检测到 from-stage 的 TASK_COMPLETED → 向 next_worker 发任务。
TRANSITIONS=[
    ("review", re.compile(r"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-review",re.I), "fixer",
     "{p}-review 完成,findings 见 shared/tasks/{p}-review/findings.md。请用 gh-mcp-fix.sh 在 nghqqa/mergepilot-test 提修复 PR(L2 密钥/依赖/删除类只出方案)。完成写 TASK_COMPLETED: {p}-fix。"),
    ("fix", re.compile(r"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-fix",re.I), "verifier",
     "{p}-fix 完成,修复 PR 见 shared/tasks/{p}-fix/result.md。请用 gh-mcp-read.sh 读修复分支逐项复核。完成写 TASK_COMPLETED: {p}-verify。"),
    ("verify", re.compile(r"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-verify",re.I), None,
     "{p}-verify 完成,流水线结束(verify 已出裁定)。"),
]

def process_batch(t, seen, fired):
    """One selection pass over discovered rooms. Extracted from main() so the
    m5live-* exclusion can be tested end-to-end with mocked I/O (P1-5). Returns
    the number of @mention sends actually performed."""
    sends = 0
    for rid, members in discover_rooms(t):
        for eid, sender, body, ts in recent(t, rid, 12):
            if eid in seen:
                continue
            seen.add(eid)
            if sender not in ("reviewer", "fixer", "verifier"):
                continue
            for from_stage, pat, next_w, tpl in TRANSITIONS:
                mt = pat.search(body)
                if not mt:
                    continue
                prefix = mt.group(1)
                if _m5_excluded(prefix):
                    continue  # M5-0B §14: m5live-* is the Candidate Controller's sole authority
                key = (rid, from_stage)
                if key in fired:
                    continue
                fired.add(key)
                msg = tpl.format(p=prefix)
                if next_w and (next_w in members):
                    send_mention(t, rid, next_w, msg)
                    sends += 1
                    print(f"[w2] {sender}→{next_w} ({prefix}-{from_stage}) @ {rid[:14]}")
                elif next_w:
                    print(f"[w2] {prefix}-{from_stage} 完成,但 {next_w} 不在房间,跳过")
                else:
                    print(f"[w2] {prefix}-{from_stage} 完成(终态,流水线结束)")
    return sends


def main():
    pw=os.environ.get("ADMIN_PW") or (sys.argv[1] if len(sys.argv)>1 else "")
    if not pw: print("ERROR: 需 ADMIN_PW"); sys.exit(1)
    t=login(pw)
    seen=set()       # 已处理的 event_id(不重复扫)
    fired=set()      # (rid, from_stage) 已触发的阶段转移(幂等:同房间同阶段只 @mention 一次)
    # 基线:屏蔽 >5min 旧消息
    now=time.time()*1000; BL=5*60*1000
    for rid,_ in discover_rooms(t):
        for eid,_,_,ts in recent(t,rid,20):
            if ts<now-BL: seen.add(eid)
    print(f"[w2] 启动;baseline 屏蔽 {len(seen)} 旧条目;动态发现房间 + 任务房间内 @mention 驱动;阶段幂等去重")
    while True:
        try:
            process_batch(t, seen, fired)
        except Exception as e:
            print(f"[w2] loop err: {e}")
        time.sleep(POLL)

if __name__=="__main__": main()
