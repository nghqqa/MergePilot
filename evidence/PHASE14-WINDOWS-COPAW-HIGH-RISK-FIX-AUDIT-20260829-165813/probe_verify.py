import json, urllib.request, urllib.parse, socket
socket.setdefaulttimeout(15)
cfg = json.load(open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/openclaw.json"))
m = cfg.get("channels", {}).get("matrix", {})
tok = m.get("accessToken"); hs = m.get("homeserver")
reviewer_mx = "@p14h2-copaw-worker-reviewer:p14h2-wd-matrix:6167"
team = "!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167"
url = hs + "/_matrix/client/v3/rooms/" + urllib.parse.quote(team) + "/messages?dir=b&limit=10"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
r = json.load(urllib.request.urlopen(req))
for e in r.get("chunk", []):
    if e.get("type") != "m.room.message":
        continue
    c = e.get("content") or {}
    print("event_id:", e.get("event_id"))
    print("  sender:", e.get("sender"), "| ts:", e.get("origin_server_ts"))
    print("  m.mentions:", json.dumps(c.get("m.mentions")))
    print("  body_head:", (c.get("body") or "")[:100].replace("\n", " "))
    if c.get("m.mentions") and reviewer_mx in (c["m.mentions"].get("user_ids") or []):
        print("  >>> MENTION-HITS-REVIEWER:", reviewer_mx in (c.get("body") or ""))
        break
