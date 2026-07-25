#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""submit_pr_taskroom.py — 为一个 PR 建专属任务房间,邀请 reviewer+fixer+verifier+manager,
发 @reviewer 真 mention 审查任务。隔离:每个 PR 独立房间 = 独立 session(OpenClaw 房间级隔离)。
用法: python3 submit_pr_taskroom.py <pw> <room_name> <prefix> <branch> <pr_number> [repo]
"""
import sys, json, time, urllib.request, urllib.error
HS="http://hiclaw-controller:6167"; ADMIN="admin"; SERVER="matrix-local.hiclaw.io:18080"

def req(m,p,t=None,b=None):
    r=urllib.request.Request(HS+p,data=(json.dumps(b).encode() if b else None),method=m)
    r.add_header("Content-Type","application/json")
    if t: r.add_header("Authorization","Bearer "+t)
    try:
        with urllib.request.urlopen(r,timeout=30) as x: return json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e: return {"_err":e.code,"_body":e.read().decode()[:200]}

def login(pw): return req("POST","/_matrix/client/v3/login",b={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

def send_mention(t,rid,user,text):
    uid=f"@{user}:{SERVER}"
    txn="s_%d"%int(time.time()*1000000)
    return req("PUT",f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",t=t,b={
        "msgtype":"m.room.text","body":f"@{user} {text}",
        "format":"org.matrix.custom.html","formatted_body":f'<a href="https://matrix.to/#/{uid}">{user}</a> {text}',
        "m.mentions":{"user":[uid]}
    })

pw=sys.argv[1]; name=sys.argv[2]; prefix=sys.argv[3]; branch=sys.argv[4]; pr=sys.argv[5]
repo=sys.argv[6] if len(sys.argv)>6 else "nghqqa/mergepilot-test"
tok=login(pw)
inv=[f"@{u}:{SERVER}" for u in ("reviewer","fixer","verifier","manager")]
res=req("POST","/_matrix/client/v3/createRoom",t=tok,b={"name":name,"preset":"trusted_private_chat","invite":inv})
rid=res.get("room_id")
print("created:",rid,"err:" if not rid else "",res if not rid else "")
if not rid: sys.exit(1)
# 等 worker 自动 join
for i in range(10):
    time.sleep(2)
    m=req("GET",f"/_matrix/client/v3/rooms/{rid}/joined_members",t=tok).get("joined",{})
    members=[k.split(":")[0].lstrip("@") for k in m]
    print(f"  t+{(i+1)*2}s joined: {members}")
    if all(u in members for u in ("reviewer","fixer","verifier")): break
# 发 @reviewer 审查任务
msg=(f"请审查 {repo} PR#{pr}(分支 {branch})的 user_service.py。"
     f"用 gh-mcp-read.sh 读 {branch} 的代码 + sast-scan,findings 写 shared/tasks/{prefix}-review/findings.md,"
     f"完成后写 TASK_COMPLETED: {prefix}-review。本房间专属任务。")
send_mention(tok,rid,"reviewer",msg)
print("posted @reviewer task. ROOM_ID="+str(rid))
