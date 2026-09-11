import json, urllib.request, urllib.parse, socket, hashlib
socket.setdefaulttimeout(15)
cfg = json.load(open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/openclaw.json"))
m = cfg.get("channels", {}).get("matrix", {})
tok = m.get("accessToken"); hs = m.get("homeserver")
print("homeserver:", hs, "| token sha16:", hashlib.sha256(tok.encode()).hexdigest()[:16])
team = "!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167"
local_since = open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/.copaw/matrix_sync_token").read().strip()
print("local persisted token:", local_since)
url = hs + "/_matrix/client/v3/sync?since=" + urllib.parse.quote(local_since) + "&timeout=0"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
r = json.load(urllib.request.urlopen(req))
print("next_batch:", r.get("next_batch"))
joined = r.get("rooms", {}).get("join", {})
tl = joined.get(team, {}).get("timeline", {})
print("team room present:", team in joined, "| timeline events:", len(tl.get("events", [])), "| limited:", tl.get("limited"))
url2 = hs + "/_matrix/client/v3/rooms/" + urllib.parse.quote(team) + "/messages?dir=b&limit=80"
req2 = urllib.request.Request(url2, headers={"Authorization": "Bearer " + tok})
r2 = json.load(urllib.request.urlopen(req2))
evs = [e for e in r2.get("chunk", []) if e.get("type") == "m.room.message"]
delegation = "$nadwji6tQQkl4vSWDktWAFtWs_enjxAcg23927OKmrU"
idx = None
for i, e in enumerate(evs):
    if e.get("event_id") == delegation:
        idx = i
        break
print("text events fetched (newest first):", len(evs))
print("delegation at index:", idx)
if idx is not None:
    print("newer text events after delegation:", idx)
    print("body:", (evs[idx].get("content") or {}).get("body", "")[:140].replace("\n", " "))
