#!/usr/bin/env python3
"""Matrix helper for the elemiso stack (admin account, host-side via elemiso-proxy :18167).

Subcommands (all print JSON; secrets never printed):
  rooms                          list joined rooms (id, name, member count)
  recent <room_id> [n]           last n messages (sender, ts, event_id, body head)
  send <room_id> <mention_user> <text_file|-> [--txn PREFIX]
                                 send m.text with m.mentions + HTML mention pill (dispatch-grade)
  members <room_id>              joined members
  since <room_id> <ISO-UTC>      messages since timestamp (for evidence export)
  export <room_id> <out.json>    full room export (paginated /messages)

Admin password is read from AGENTTEAMS_ADMIN_PASSWORD or the secrets env file
D:/mp-finals-tmp/elemiso-secrets-20260915/ctrl.env (never echoed).
"""
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("ELEMISO_MATRIX_URL", "http://127.0.0.1:18167")
ADMIN_USER = os.environ.get("AGENTTEAMS_ADMIN_USER", "elemiso-admin")
SECRETS = os.environ.get("ELEMISO_SECRETS", "D:/mp-finals-tmp/elemiso-secrets-20260915/ctrl.env")
_TOK = None


def _password():
    pw = os.environ.get("AGENTTEAMS_ADMIN_PASSWORD")
    if pw:
        return pw
    with open(SECRETS, encoding="utf-8") as f:
        for line in f:
            if line.startswith("AGENTTEAMS_ADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("admin password not found")


def api(method, path, body=None, tok=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:300]}


def token():
    global _TOK
    if _TOK:
        return _TOK
    r = api("POST", "/_matrix/client/v3/login",
            {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": ADMIN_USER},
             "password": _password()})
    _TOK = r.get("access_token")
    if not _TOK:
        raise SystemExit("ADMIN_LOGIN_FAILED " + json.dumps(r)[:200])
    return _TOK


def q(s):
    return urllib.parse.quote(s, safe="")


def rooms():
    tok = token()
    out = []
    for rid in api("GET", "/_matrix/client/v3/joined_rooms", tok=tok).get("joined_rooms", []):
        name = api("GET", f"/_matrix/client/v3/rooms/{q(rid)}/state/m.room.name", tok=tok).get("name", "")
        mem = api("GET", f"/_matrix/client/v3/rooms/{q(rid)}/joined_members", tok=tok).get("joined", {})
        out.append({"room_id": rid, "name": name, "members": sorted(mem.keys())})
    return out


def messages(rid, limit=20, from_tok=None, direction="b"):
    tok = token()
    path = f"/_matrix/client/v3/rooms/{q(rid)}/messages?dir={direction}&limit={limit}"
    if from_tok:
        path += "&from=" + q(from_tok)
    return api("GET", path, tok=tok)


def recent(rid, n=20):
    r = messages(rid, limit=n)
    out = []
    for ev in reversed(r.get("chunk", [])):
        if ev.get("type") != "m.room.message":
            continue
        c = ev.get("content", {})
        out.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["origin_server_ts"] / 1000)),
                    "sender": ev["sender"], "event_id": ev["event_id"],
                    "mentions": (c.get("m.mentions") or {}).get("user_ids", []),
                    "body": (c.get("body") or "")[:400]})
    return out


def export(rid, path):
    allev, frm = [], None
    while True:
        r = messages(rid, limit=200, from_tok=frm)
        chunk = r.get("chunk", [])
        allev.extend(chunk)
        frm = r.get("end")
        if not chunk or not frm:
            break
    allev.sort(key=lambda e: e.get("origin_server_ts", 0))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(allev, f, ensure_ascii=False, indent=1)
    return {"room_id": rid, "events": len(allev), "path": path}


def send(rid, mention_user, text, txn_prefix="r3"):
    tok = token()
    display = mention_user.split(":")[0].lstrip("@")
    pill = f'<a href="https://matrix.to/#/{html.escape(mention_user)}">{html.escape(display)}</a>'
    formatted = pill + " " + html.escape(text).replace("\n", "<br>")
    body = f"{display}: {text}" if not text.startswith("@") else text
    content = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
               "formatted_body": formatted, "m.mentions": {"user_ids": [mention_user]}}
    txn = f"{txn_prefix}-{int(time.time() * 1000)}"
    r = api("PUT", f"/_matrix/client/v3/rooms/{q(rid)}/send/m.room.message/{q(txn)}", content, tok=tok)
    return {"event_id": r.get("event_id"), "txn": txn, "room_id": rid, "mention": mention_user,
            "body_len": len(body), "error": r.get("_body")}


def members(rid):
    return sorted(api("GET", f"/_matrix/client/v3/rooms/{q(rid)}/joined_members", tok=token()).get("joined", {}).keys())


def since(rid, iso):
    t0 = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    out, frm = [], None
    while True:
        r = messages(rid, limit=100, from_tok=frm)
        chunk = r.get("chunk", [])
        stop = False
        for ev in chunk:
            if ev.get("origin_server_ts", 0) / 1000 < t0:
                stop = True
                break
            if ev.get("type") == "m.room.message":
                c = ev.get("content", {})
                out.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["origin_server_ts"] / 1000)),
                            "sender": ev["sender"], "event_id": ev["event_id"],
                            "mentions": (c.get("m.mentions") or {}).get("user_ids", []),
                            "body": (c.get("body") or "")})
        frm = r.get("end")
        if stop or not chunk or not frm:
            break
    return list(reversed(out))


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    cmd = a[0]
    if cmd == "rooms":
        res = rooms()
    elif cmd == "recent":
        res = recent(a[1], int(a[2]) if len(a) > 2 else 20)
    elif cmd == "members":
        res = members(a[1])
    elif cmd == "since":
        res = since(a[1], a[2])
    elif cmd == "export":
        res = export(a[1], a[2])
    elif cmd == "send":
        txn = "r3"
        if "--txn" in a:
            i = a.index("--txn")
            txn = a[i + 1]
            a = a[:i] + a[i + 2:]
        src = a[3]
        if src == "-":
            text = sys.stdin.read()
        elif os.path.exists(src):
            text = open(src, encoding="utf-8").read()
        else:
            text = src  # inline text
        res = send(a[1], a[2], text.rstrip("\n"), txn)
    else:
        print(__doc__)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
