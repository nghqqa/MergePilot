import json
cfg = json.load(open("/root/.copaw-worker/p14h2-copaw-worker-reviewer/openclaw.json"))
m = cfg.get("channels", {}).get("matrix", {})
for k in sorted(m.keys()):
    v = m[k]
    if isinstance(v, str) and len(v) > 40:
        v = v[:12] + "...(len=%d)" % len(v)
    if isinstance(v, list):
        v = [x[:14] + ".." if isinstance(x, str) else x for x in v]
    if isinstance(v, dict):
        v = json.dumps(v)[:220]
    print(k, "=", v)
