import json, urllib.request, urllib.parse, socket
socket.setdefaulttimeout(15)
cfg = json.load(open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/openclaw.json"))
m = cfg.get("channels", {}).get("matrix", {})
tok = m.get("accessToken"); hs = m.get("homeserver")
reviewer_mx = "@p14h2-copaw-worker-reviewer:p14h2-wd-matrix:6167"
team = "!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167"
dm = "!fai6WChnDcFHxNnuK2:p14h2-wd-matrix:6167"

def fetch(room, pages=6):
    out, frm = [], ""
    for _ in range(pages):
        url = hs + "/_matrix/client/v3/rooms/" + urllib.parse.quote(room) + "/messages?dir=b&limit=100" + (("&from=" + urllib.parse.quote(frm)) if frm else "")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        r = json.load(urllib.request.urlopen(req))
        chunk = r.get("chunk", [])
        out += chunk
        frm = r.get("end")
        if not frm or not chunk:
            break
    return out

print("=== TEAM ROOM: m.room.message events, newest first (top 45) ===")
evs = [e for e in fetch(team) if e.get("type") == "m.room.message"]
for i, e in enumerate(evs[:45]):
    c = e.get("content") or {}
    body = (c.get("body") or "").replace("\n", " ")[:90]
    men = "MENTION-REVIEWER" if reviewer_mx in json.dumps(c.get("m.mentions") or {}) or reviewer_mx in body else ""
    print(f"{i:3d} {e.get('origin_server_ts')} {e.get('sender','').split(':')[0]:40s} {men:17s} {body}")
    if "sandbox review task" in body and i > 40:
        break

print()
print("=== REVIEWER DM room: all events (newest first, top 20) ===")
try:
    devs = fetch(dm)
    for i, e in enumerate(devs[:20]):
        c = e.get("content") or {}
        body = (c.get("body") or "").replace("\n", " ")[:90]
        print(f"{i:3d} {e.get('type'):24s} {e.get('origin_server_ts')} {e.get('sender','').split(':')[0]:40s} {body}")
except Exception as exc:
    print("DM room fetch failed:", exc)
