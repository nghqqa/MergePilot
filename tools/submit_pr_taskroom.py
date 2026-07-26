#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""submit_pr_taskroom.py — 为一个 PR 建专属任务房间,邀请 reviewer+fixer+verifier+manager,
发送结构化 TASK_SUBMITTED 消息(供 Controller 原子注册)+ 发 @reviewer 真 mention 任务。
用法: python3 submit_pr_taskroom.py <pw> <room_name> <prefix> <branch> <pr_number> [repo]
"""
import sys, json, time, urllib.request, urllib.error
HS = os.environ.get("MATRIX_HS", "http://hiclaw-controller:6167") if (os := __import__("os")) else "http://hiclaw-controller:6167"
ADMIN = "admin"; SERVER = "matrix-local.hiclaw.io:18080"

def req(m, p, t=None, b=None):
    r = urllib.request.Request(HS+p, data=(json.dumps(b).encode() if b else None), method=m)
    r.add_header("Content-Type", "application/json")
    if t: r.add_header("Authorization", "Bearer "+t)
    try:
        with urllib.request.urlopen(r, timeout=30) as x: return json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e: return {"_err": e.code, "_body": e.read().decode()[:200]}

def login(pw):
    return req("POST", "/_matrix/client/v3/login", b={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

def send_mention(t, rid, user, text):
    uid = f"@{user}:{SERVER}"; txn = "s_%d" % int(time.time()*1000000)
    return req("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}", t=t, b={
        "msgtype":"m.room.text","body":f"@{user} {text}",
        "format":"org.matrix.custom.html","formatted_body":f'<a href="https://matrix.to/#/{uid}">{user}</a> {text}',
        "m.mentions":{"user":[uid]}
    })

pw = sys.argv[1]; name = sys.argv[2]; prefix = sys.argv[3]; branch = sys.argv[4]; pr = sys.argv[5]
repo = sys.argv[6] if len(sys.argv) > 6 else "nghqqa/mergepilot-test"
tok = login(pw)
inv = [f"@{u}:{SERVER}" for u in ("reviewer","fixer","verifier","manager")]
res = req("POST", "/_matrix/client/v3/createRoom", t=tok, b={"name":name,"preset":"trusted_private_chat","invite":inv})
rid = res.get("room_id")
print("created:", rid, "err:" if not rid else "", res if not rid else "")
if not rid: sys.exit(1)

# 严格成员门禁:4 个必须成员全部 join 才发 TASK_SUBMITTED
REQUIRED = {"reviewer", "fixer", "verifier", "manager"}
for i in range(15):
    time.sleep(2)
    m = req("GET", f"/_matrix/client/v3/rooms/{rid}/joined_members", t=tok).get("joined", {})
    members = set(k.split(":")[0].lstrip("@") for k in m)
    missing = REQUIRED - members
    print(f"  t+{(i+1)*2}s joined={sorted(members)} missing={sorted(missing) if missing else 'none'}")
    if not missing:
        break
else:
    print(f"ERROR: room not ready after 30s, missing={sorted(missing)}")
    print("Room left open for debugging. Fix membership then re-run with a new room.")
    sys.exit(2)

# 1. 结构化 TASK_SUBMITTED(供 Controller 原子注册 + Outbox 派发 @reviewer)
#    只发 TASK_SUBMITTED,不发 @reviewer mention(Controller Outbox 负责派发)
payload = {"run_id": prefix, "repo": repo, "pr_number": int(pr), "branch": branch}
submit_body = "TASK_SUBMITTED: " + json.dumps(payload, ensure_ascii=False, separators=(",",":"))
txn1 = "sub_%d" % int(time.time()*1000000)
req("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn1}", t=tok, b={"msgtype":"m.room.text","body":submit_body})
print(f"posted TASK_SUBMITTED: {submit_body[:80]}...")
print(f"ROOM_ID={rid}")
print("(Controller Outbox 将自动 @reviewer 派发审查任务)")
