#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect_e2e_evidence.py — 从指定任务房间 dump 完整消息流(不截断),用于证据包。
用法: python3 collect_e2e_evidence.py <pw> <room_id> <output_dir>
"""
import sys, json, os, urllib.request, urllib.error
HS="http://hiclaw-controller:6167"; ADMIN="admin"; SERVER="matrix-local.hiclaw.io:18080"

def req(m,p,t=None,b=None):
    r=urllib.request.Request(HS+p,data=(json.dumps(b).encode() if b else None),method=m)
    r.add_header("Content-Type","application/json")
    if t: r.add_header("Authorization","Bearer "+t)
    try:
        with urllib.request.urlopen(r,timeout=30) as x: return json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e: return {}

def login(pw):
    return req("POST","/_matrix/client/v3/login",b={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

pw=sys.argv[1]; rid=sys.argv[2]; outdir=sys.argv[3]
if ":" not in rid: rid=rid+":"+SERVER
tok=login(pw)
os.makedirs(outdir,exist_ok=True)

# 翻页拉全部消息(dir=b backward,用 from token 翻页)
messages=[]
end=None
for _ in range(20):  # 最多 20 页
    url=f"/_matrix/client/v3/rooms/{rid}/messages?dir=b&limit=50"
    if end: url+=f"&from={end}"
    d=req("GET",url,t=tok)
    chunk=d.get("chunk",[])
    if not chunk: break
    for e in chunk:
        if e.get("type")=="m.room.message":
            sender=e.get("sender","").split(":")[0].lstrip("@")
            body=e.get("content",{}).get("body","") or ""
            ts=e.get("origin_server_ts",0)
            messages.append((ts,sender,body))
    end=d.get("end")
    if not end: break

messages.sort(key=lambda x:x[0])  # 按时间正序

with open(os.path.join(outdir,"matrix-flow.txt"),"w",encoding="utf-8") as f:
    f.write(f"# 任务房间消息流({rid})\n# 共 {len(messages)} 条消息\n\n")
    for ts,sender,body in messages:
        f.write(f"[{sender}]\n{body}\n\n{'─'*60}\n\n")

print(f"dumped {len(messages)} messages → {outdir}/matrix-flow.txt")
