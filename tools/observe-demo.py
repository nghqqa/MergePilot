#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""observe-demo.py — 登录 admin,打印涉及 manager/reviewer/fixer/verifier 的房间的最近消息。
在 hiclaw-manager 容器里运行。用法:python3 observe-demo.py <admin_password> [limit]
"""
import sys, json, urllib.request, urllib.error
HS = "http://hiclaw-controller:6167"
ADMIN = "admin"

def req(method, path, token=None, body=None, timeout=30):
    r = urllib.request.Request(HS+path, data=(json.dumps(body).encode() if body else None), method=method)
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:120]}

def login(pw):
    return req("POST", "/_matrix/client/v3/login",
               body={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

def members(tok, rid):
    m = req("GET", f"/_matrix/client/v3/rooms/{rid}/joined_members", token=tok).get("joined", {})
    return sorted(k.split(":")[0].lstrip("@") for k in m.keys())

def last_msgs(tok, rid, n=8):
    d = req("GET", f"/_matrix/client/v3/rooms/{rid}/messages?dir=b&limit={n}", token=tok)
    out = []
    for e in reversed(d.get("chunk", [])):
        if e.get("type") == "m.room.message":
            body = e.get("content", {}).get("body", "")
            sender = e.get("sender", "").split(":")[0].lstrip("@")
            out.append(f"  [{sender}] {body[:300]}")
    return out

if __name__ == "__main__":
    pw = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    tok = login(pw)
    if not tok:
        print("LOGIN FAILED"); sys.exit(1)
    rooms = req("GET", "/_matrix/client/v3/joined_rooms", token=tok).get("joined_rooms", [])
    for rid in rooms:
        mem = members(tok, rid)
        if any(w in mem for w in ["reviewer", "fixer", "verifier", "manager"]):
            print(f"\n===== members={mem} =====")
            for line in last_msgs(tok, rid, limit):
                print(line)
