#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""submit_manager_orchestrate.py — 让 Manager(openclaw)直接编排独立 Worker(review→fix→verify)。
不用 Team、不用 copaw。在 hiclaw-manager 容器里运行。用法:python3 submit_manager_orchestrate.py <admin_password>
"""
import sys, json, time, urllib.request, urllib.error

HS = "http://hiclaw-controller:6167"
ADMIN = "admin"

MSG = """[NEW TASK: PR#42] 请按以下流程处理这个 PR(注意:按顺序逐步执行,等前一步完成再进下一步):

1) 让 reviewer 审查这段代码的安全与质量问题,产出 findings
2) 收到 findings 后,让 fixer 修复(注意:依赖/密钥/删除类高风险变更只出方案不执行,标 needs-approval)
3) 修复后,让 verifier 验证(跑测试 + 重扫)
4) 汇总:全 pass 且无 L2 → merge;有 L2 → hold(等人审);有 fail → reject

PR 代码(repo=mergepilot/demo-service, PR #42):
import sqlite3
API_KEY = "sk-live-1234567890abcdef"
def get_user(name):
    conn = sqlite3.connect("db.sqlite")
    return conn.execute("SELECT * FROM users WHERE name='" + name + "'").fetchall()"""


def req(method, path, token=None, body=None):
    r = urllib.request.Request(HS + path, data=(json.dumps(body).encode() if body else None), method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:200]}


def login(pw):
    return req("POST", "/_matrix/client/v3/login",
               body={"type": "m.login.password", "identifier": {"type": "m.id.user", "user": ADMIN}, "password": pw}).get("access_token")


def find_manager_room(tok):
    rooms = req("GET", "/_matrix/client/v3/joined_rooms", token=tok).get("joined_rooms", [])
    for rid in rooms:
        m = req("GET", f"/_matrix/client/v3/rooms/{rid}/joined_members", token=tok).get("joined", {})
        users = set(k.split(":")[0].lstrip("@") for k in m.keys())
        if users == {"admin", "manager"}:
            return rid
    return None


def send(tok, room, text):
    txn = "mp%d" % int(time.time() * 1000)
    return req("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
               token=tok, body={"msgtype": "m.room.text", "body": text})


if __name__ == "__main__":
    pw = sys.argv[1]
    tok = login(pw)
    if not tok:
        print("LOGIN FAILED"); sys.exit(1)
    room = find_manager_room(tok)
    if not room:
        print("manager room not found"); sys.exit(1)
    res = send(tok, room, MSG)
    eid = res.get("event_id")
    print("manager room:", room)
    print("event_id:", eid) if eid else print("send result:", res)
    print(">>> Manager(openclaw)收到工作流指令。盯 Manager / reviewer / fixer / verifier 房间看编排。")
