import json, urllib.request, urllib.parse, socket
socket.setdefaulttimeout(15)
cfg = json.load(open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/openclaw.json"))
m = cfg.get("channels", {}).get("matrix", {})
tok = m.get("accessToken"); hs = m.get("homeserver")
team = "!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167"
delegation = "$nadwji6tQQkl4vSWDktWAFtWs_enjxAcg23927OKmrU"
frm = ""
newer = 0
found = False
for page in range(6):
    url = hs + "/_matrix/client/v3/rooms/" + urllib.parse.quote(team) + "/messages?dir=b&limit=100" + (("&from=" + urllib.parse.quote(frm)) if frm else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    r = json.load(urllib.request.urlopen(req))
    chunk = r.get("chunk", [])
    if not chunk:
        break
    for e in chunk:
        if e.get("event_id") == delegation:
            found = True
            print("FOUND delegation. newer events after it:", newer)
            print("  sender:", e.get("sender"), "| type:", e.get("type"))
            c = e.get("content") or {}
            print("  msgtype:", c.get("msgtype"))
            print("  body[:200]:", (c.get("body") or "")[:200].replace("\n", " "))
            print("  formatted_body present:", bool(c.get("formatted_body")))
            print("  m.mentions:", c.get("m.mentions"))
            break
        if e.get("type") == "m.room.message":
            newer += 1
    if found:
        break
    frm = r.get("end")
    if not frm:
        break
if not found:
    print("delegation NOT found in", page + 1, "pages of history")
