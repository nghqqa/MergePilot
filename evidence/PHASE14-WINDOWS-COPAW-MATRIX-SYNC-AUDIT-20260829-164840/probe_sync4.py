import json, urllib.request, urllib.parse, socket
socket.setdefaulttimeout(15)
cfg = json.load(open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/openclaw.json"))
m = cfg.get("channels", {}).get("matrix", {})
tok = m.get("accessToken"); hs = m.get("homeserver")
dm = "!fai6WChnDcFHxNnuK2:p14h2-wd-matrix:6167"
url = hs + "/_matrix/client/v3/rooms/" + urllib.parse.quote(dm) + "/messages?dir=b&limit=30"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
r = json.load(urllib.request.urlopen(req))
for e in r.get("chunk", []):
    if e.get("type") == "m.room.message":
        c = e.get("content") or {}
        print("event_id:", e.get("event_id"))
        print("sender:", e.get("sender"), "| ts:", e.get("origin_server_ts"))
        print("m.mentions:", json.dumps(c.get("m.mentions")))
        print("formatted_body[:400]:", (c.get("formatted_body") or "")[:400])
        print("body[:600]:", (c.get("body") or "")[:600])
        break
# member count of DM room
url2 = hs + "/_matrix/client/v3/rooms/" + urllib.parse.quote(dm) + "/state/m.room.member/" + urllib.parse.quote("@p14h2-copaw-worker-manager:p14h2-wd-matrix:6167")
req2 = urllib.request.Request(url2, headers={"Authorization": "Bearer " + tok})
try:
    print("\nmanager membership in DM:", json.load(urllib.request.urlopen(req2)).get("membership"))
except Exception as exc:
    print("\nmanager membership check failed:", exc)
