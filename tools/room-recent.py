#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印指定 agent 房间的最近 N 条消息(ASCII 安全输出)。用法:python3 room-recent.py <pw> <agent> [n]"""
import sys, json, urllib.request, urllib.error
HS="http://hiclaw-controller:6167"; ADMIN="admin"
def req(m,p,t=None,b=None):
    r=urllib.request.Request(HS+p,data=(json.dumps(b).encode() if b else None),method=m)
    r.add_header("Content-Type","application/json")
    if t: r.add_header("Authorization","Bearer "+t)
    try:
        with urllib.request.urlopen(r,timeout=30) as x: return json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e: return {}
def login(pw): return req("POST","/_matrix/client/v3/login",b={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")
pw=sys.argv[1]; agent=sys.argv[2]; n=int(sys.argv[3]) if len(sys.argv)>3 else 6
tok=login(pw)
rid=None
if agent.startswith("!"):
    # 直接按房间 ID(补全 server 后缀)
    rid=agent if ":" in agent else agent+":matrix-local.hiclaw.io:18080"
else:
    for r in req("GET","/_matrix/client/v3/joined_rooms",t=tok).get("joined_rooms",[]):
        m=req("GET",f"/_matrix/client/v3/rooms/{r}/joined_members",t=tok).get("joined",{})
        u=set(k.split(":")[0].lstrip("@") for k in m)
        if agent in u and "manager" in u: rid=r; break
if not rid: print("room not found for",agent); sys.exit(1)
d=req("GET",f"/_matrix/client/v3/rooms/{rid}/messages?dir=b&limit={n}",t=tok)
print(f"=== room: admin+manager+{agent} ({rid[:20]}...) last {n} ===")
for e in reversed(d.get("chunk",[])):
    if e.get("type")=="m.room.message":
        s=e.get("sender","").split(":")[0].lstrip("@")
        b=(e.get("content",{}).get("body","") or "").replace("\n"," ")[:140]
        print(f"[{s}] {b}")
