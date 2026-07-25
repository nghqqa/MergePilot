#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""create_task_room.py — 为一个 PR 建全新的 Matrix 任务房间,邀请参与者,发任务消息。
用法: python3 create_task_room.py <pw> <room_name> <invitees_csv> <message>
  invitees_csv: reviewer,fixer,verifier,manager (逗号分隔)
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

def login(pw):
    return req("POST","/_matrix/client/v3/login",b={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

pw=sys.argv[1]; name=sys.argv[2]; invitees=sys.argv[3].split(","); msg=sys.argv[4]
tok=login(pw)
inv=[f"@{u}:{SERVER}" for u in invitees]
res=req("POST","/_matrix/client/v3/createRoom",t=tok,b={"name":name,"preset":"trusted_private_chat","invite":inv})
rid=res.get("room_id")
print("created:",rid,"invite:",inv, "err:" if not rid else "", res if not rid else "")
if not rid: sys.exit(1)
# 等被邀请者自动 join
for i in range(8):
    time.sleep(2)
    m=req("GET",f"/_matrix/client/v3/rooms/{rid}/joined_members",t=tok).get("joined",{})
    members=[k.split(":")[0].lstrip("@") for k in m]
    print(f"  t+{(i+1)*2}s joined: {members}")
    if all(u in members for u in invitees): break
# 发任务
txn="tr%d"%int(time.time()*1000)
r=req("PUT",f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",t=tok,b={"msgtype":"m.room.text","body":msg})
print("posted event:",r.get("event_id") or r)
print("ROOM_ID="+str(rid))
