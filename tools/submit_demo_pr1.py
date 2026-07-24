#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""submit_demo_pr1.py — 把「真实 GitHub PR#1 审查」任务发给 Manager,让它编排 reviewer→fixer→verifier,
全程用 github MCP(不贴代码)。在 hiclaw-manager 容器里运行。用法:python3 submit_demo_pr1.py <admin_password>
"""
import sys, json, time, urllib.request, urllib.error

HS = "http://hiclaw-controller:6167"
ADMIN = "admin"

MSG = """[NEW TASK: GitHub PR#1 审查(真实 GitHub,经 github MCP)] 请编排 reviewer → fixer → verifier 处理一个真实 GitHub PR。全程用 github MCP 读/写,不要贴代码。

仓库:nghqqa/mergepilot-test,PR #1(分支 feature/vulnerable-pr → main),改动文件:user_service.py

1) reviewer:用 `bash gh-mcp-read.sh nghqqa mergepilot-test user_service.py feature/vulnerable-pr` 经 github MCP 把真实代码拉到 /tmp/review/,再按你的审查流程跑 sast-scan,产出 findings(标注真实文件名 user_service.py 与行号)。
2) fixer:据 findings,在真实仓库提修复 PR。把修复后的完整 user_service.py 写到 /tmp/fix/user_service.py、PR 说明写到 /tmp/fix/pr-body.md,然后执行 `bash gh-mcp-fix.sh nghqqa mergepilot-test feature/vulnerable-pr fix/security-demo user_service.py /tmp/fix/user_service.py "fix(security): SQLi 参数化 + 密钥改读环境变量" "[MergePilot] demo 安全修复" /tmp/fix/pr-body.md`。本次 demo 已授权对 SQLi 与硬编码密钥做自动修复(密钥本身仍需人审轮换,但代码侧改读环境变量可自动修)。
3) verifier:用 `bash gh-mcp-read.sh nghqqa mergepilot-test user_service.py fix/security-demo` 读修复分支代码,复核 SQLi 已参数化、密钥已改读环境变量,产出 verify 报告。
4) 汇总:全 pass → 建议 merge;否则 hold 并说明。

worker 已配好 github MCP(44 工具,PAT 存于隔离 sidecar,worker 零 GitHub 凭证);封装脚本 gh-mcp-read.sh / gh-mcp-fix.sh 已在 worker 的 /usr/local/bin。逐步执行,等前一步完成再进下一步。"""


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
    print(">>> 已提交 PR#1 审查任务。盯 Manager / reviewer / fixer / verifier 房间看编排。")
