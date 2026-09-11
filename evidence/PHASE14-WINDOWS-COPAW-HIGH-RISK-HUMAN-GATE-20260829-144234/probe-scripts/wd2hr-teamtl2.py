import json,sys,urllib.request,urllib.parse
tok=sys.stdin.read().strip()
base=sys.argv[1] if len(sys.argv)>1 else "http://p14h2-wd-controller:6167"
rid="!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167"
q=urllib.parse.quote(rid,safe="")
r=urllib.request.Request(base+f"/_matrix/client/v3/rooms/{q}/messages?limit=10&dir=b", headers={"Authorization":"Bearer "+tok})
d=json.load(urllib.request.urlopen(r,timeout=10))
for e in d.get("chunk",[]):
    if e.get("type")=="m.room.message":
        s=e.get("sender","?").split(":")[0]
        b=(e.get("content",{}).get("body","") or "").replace("\n"," ")[:180]
        ts=e.get("origin_server_ts",0)
        import datetime
        t=datetime.datetime.utcfromtimestamp(ts/1000).strftime("%H:%M:%S")
        print(f"[{t}] {s}: {b}")
