#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""send-to-manager.py — 给 admin<->manager 房间发一条消息。用法:python3 send-to-manager.py <admin_password> <message>
"""
import sys, json, time, urllib.request, urllib.error
HS = "http://hiclaw-controller:6167"
ADMIN = "admin"

def req(method, path, token=None, body=None):
    r = urllib.request.Request(HS+path, data=(json.dumps(body).encode() if body else None), method=method)
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("Authorization", "Bearer "+token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:150]}

def login(pw):
    return req("POST","/_matrix/client/v3/login", body={"type":"m.login.password","identifier":{"type":"m.id.user","user":ADMIN},"password":pw}).get("access_token")

def find_manager_room(tok):
    rooms = req("GET","/_matrix/client/v3/joined_rooms",token=tok).get("joined_rooms",[])
    for rid in rooms:
        m = req("GET", f"/_matrix/client/v3/rooms/{rid}/joined_members", token=tok).get("joined",{})
        users = set(k.split(":")[0].lstrip("@") for k in m.keys())
        if users == {"admin","manager"}:
            return rid
    return None

if __name__=="__main__":
    pw = sys.argv[1]; msg = sys.argv[2]
    tok = login(pw)
    room = find_manager_room(tok)
    txn = "mp%d" % int(time.time()*1000)
    res = req("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}", token=tok, body={"msgtype":"m.room.text","body":msg})
    print("sent to", room, "| event_id:", res.get("event_id") or res)
