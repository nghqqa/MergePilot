#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
submit_pr_manager.py — 把一条 fixture PR 经【系统 Manager】路由给 mergepilot team(可靠触发路径)。
在 hiclaw-manager 容器里运行。用法:python3 submit_pr_manager.py <fixture.md> <admin_password>
"""
import sys, json, time, re, urllib.request, urllib.error

HS = "http://hiclaw-controller:6167"
ADMIN = "admin"


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
    r = req("POST", "/_matrix/client/v3/login",
            body={"type": "m.login.password", "identifier": {"type": "m.id.user", "user": ADMIN}, "password": pw})
    return r.get("access_token")


def find_manager_room(token):
    rooms = req("GET", "/_matrix/client/v3/joined_rooms", token=token).get("joined_rooms", [])
    for rid in rooms:
        m = req("GET", f"/_matrix/client/v3/rooms/{rid}/joined_members", token=token).get("joined", {})
        users = set(k.split(":")[0].lstrip("@") for k in m.keys())
        if users == {"admin", "manager"}:
            return rid
    return None


def send(token, room, text):
    txn = "mp%d" % int(time.time() * 1000)
    return req("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
               token=token, body={"msgtype": "m.room.text", "body": text})


def extract_code(fixture_text):
    m = re.search(r"```\n(.*?)\n```", fixture_text, re.S)
    return m.group(1) if m else fixture_text


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: submit_pr_manager.py <fixture.md> <admin_password>"); sys.exit(1)
    fixture, pw = sys.argv[1], sys.argv[2]
    code = extract_code(open(fixture, encoding="utf-8").read())
    msg = "请让 mergepilot team 的 coordinator 处理以下 PR 的完整审查闭环(派 reviewer 审查 → fixer 修复 → verifier 验证 → 给 merge/hold/reject 报告):\n\n" + code
    tok = login(pw)
    if not tok:
        print("LOGIN FAILED"); sys.exit(1)
    room = find_manager_room(tok)
    if not room:
        print("manager room not found"); sys.exit(1)
    print("manager room:", room)
    res = send(tok, room, msg)
    eid = res.get("event_id")
    print("event_id:", eid) if eid else print("send result:", res)
    print(">>> 已经理 Manager 路由给 mergepilot team。盯各房间;跑完看 shared/tasks 新项目 + python tools/trace_aggregator.py")
