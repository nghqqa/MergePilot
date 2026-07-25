#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""post_mention.py — 在指定房间发一条带真 @mention 胶囊的消息给 target 用户。
用法: python3 post_mention.py <pw> <room_id> <target_user> <message>
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

pw=sys.argv[1]; rid=sys.argv[2]; target=sys.argv[3]; msg=sys.argv[4]
if ":" not in rid: rid=rid+":"+SERVER
tok=login(pw)
uid="@"+target+":"+SERVER
body_text=f"@{target} {msg}"
html=f'<a href="https://matrix.to/#/{uid}">{target}</a> {msg}'
txn="pm%d"%int(time.time()*1000)
r=req("PUT",f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",t=tok,b={
    "msgtype":"m.room.text","body":body_text,
    "format":"org.matrix.custom.html","formatted_body":html,
    "m.mentions":{"user":[uid]}
})
print("room:",rid,"| mention:",uid,"| event:",r.get("event_id") or r)
