"""v3 控制台(只读):run 记录与阶段状态的可观测接口。

**边界(硬性)**:仅 GET;无批准/拒绝/派发/GitHub 写端点;默认绑定 127.0.0.1。
数据来自本地 RunStore;每条记录带 mode(shadow/fixture)标签——
shadow/fixture 数据永不显示为真实运行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.normpath(os.path.join(_HERE, "..", "orchestrator")),):
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.insert(0, _cand)

from adapter import build_read_model  # noqa: E402  (桥侧同款独立加载)
from runstore import RunStore  # noqa: E402

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>MergePilot v3 console (read-only)</title>
<style>
body{font-family:Consolas,monospace;margin:24px;background:#111;color:#ddd}
h1{font-size:18px} .badge{display:inline-block;padding:1px 8px;margin-right:6px;
border:1px solid #888;font-size:12px}
.badge.shadow{border-color:#e6b800;color:#e6b800}
.badge.fixture{border-color:#4aa3ff;color:#4aa3ff}
.badge.on{border-color:#3ad17c;color:#3ad17c}
.partial{color:#e6b800}.manual{color:#ff6b6b}.ok{color:#3ad17c}
table{border-collapse:collapse;width:100%;margin-top:12px}
td,th{border:1px solid #333;padding:4px 8px;font-size:13px;text-align:left}
a{color:#7ab8ff} pre{background:#181818;padding:8px;overflow:auto}
.note{color:#888;font-size:12px}
</style></head><body>
<h1>MergePilot v3 console <span class="badge">READ-ONLY</span></h1>
<p class="note">全部数据为 shadow / fixture 模式产物:无真实 Agent、无 GitHub 写入、
不构成真实审查结论。本页面无任何写操作端点。</p>
<div id="runs">loading...</div>
<pre id="detail"></pre>
<script>
fetch('/api/runs').then(r=>r.json()).then(d=>{
  const t=['<table><tr><th>run</th><th>mode</th><th>repo/PR</th><th>head</th>'+
  '<th>risk</th><th>outcome</th><th>coverage</th></tr>'];
  for(const r of d.runs){
    const cls=r.outcome&&r.outcome.startsWith('REVIEW_COMPLETED')?'ok':
             (r.outcome&&r.outcome.includes('MANUAL')?'manual':'partial');
    t.push('<tr><td><a href="javascript:show(\\''+r.run_id+'\\')">'+r.run_id+
      '</a></td><td><span class="badge '+r.mode+'">'+r.mode+'</span></td>'+
      '<td>'+r.repo+' #'+r.pr_number+'</td><td>'+(r.head_sha||'').slice(0,8)+'</td>'+
      '<td>'+(r.risk_tier||'-')+'</td><td class="'+cls+'">'+r.outcome+'</td>'+
      '<td>'+(r.coverage_missing&&r.coverage_missing.length?
             r.coverage_missing.join(', '):'complete')+'</td></tr>');
  }
  t.push('</table>');
  document.getElementById('runs').innerHTML=t.join('');
});
function show(id){
  fetch('/api/runs/'+id).then(r=>r.json()).then(d=>
    document.getElementById('detail').textContent=JSON.stringify(d,null,1));
}
</script></body></html>"""


def _by_source(aggregates):
    counts = {}
    for f in (aggregates or {}).get("findings", []):
        for src in f.get("sources", []):
            counts[src["reviewer"]] = counts.get(src["reviewer"], 0) + 1
    return counts


def make_handler(store: RunStore):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(
                body, ensure_ascii=False, indent=1).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, {"ok": True, "mode": "read-only"})
            if path == "/api/hook-errors":
                return self._send(200, {"errors": store.list_hook_errors()})
            if path == "/api/runs":
                runs = [{"run_id": r["run_id"], "mode": r["mode"],
                         "repo": r["repo"], "pr_number": r["pr_number"],
                         "head_sha": r["head_sha"], "risk_tier": r["risk_tier"],
                         "outcome": (r.get("outcome") or {}).get("outcome"),
                         "review_complete": (r.get("outcome") or {}).get("review_complete"),
                         "coverage_missing": r.get("coverage_missing") or [],
                         "superseded": bool(r.get("superseded")),
                         "updated_at": r["updated_at"]}
                        for r in store.list_runs()]
                return self._send(200, {"runs": runs, "data_mode": "shadow/fixture only"})
            if path.startswith("/api/runs/"):
                rid = path[len("/api/runs/"):].strip("/")
                rec = store.get_run(rid)
                if rec is None:
                    return self._send(404, {"error": "NOT_FOUND"})
                payload = build_read_model(rec)
                payload["findings"] = {
                    "total": (rec.get("aggregates") or {}).get("total", 0),
                    "by_source": _by_source(rec.get("aggregates")),
                    "dropped_duplicates":
                        (rec.get("aggregates") or {}).get("dropped_duplicates", 0)}
                payload["mode"] = rec["mode"]
                payload["superseded"] = bool(rec.get("superseded"))
                payload["downgrade_reason"] = rec.get("downgrade_reason")
                payload["finding_validation_status"] = rec.get("finding_validation")
                payload["patch_validation_status"] = rec.get("patch_validation")
                payload["manifest_hash"] = rec.get("manifest_hash")
                return self._send(200, payload)
            if path == "/" or path == "/index.html":
                return self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return self._send(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            self._send(405, {"error": "READ_ONLY console: no write endpoints"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt, *args):  # 安静模式
            pass

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(prog="console_v3", description=__doc__)
    ap.add_argument("--db", default=os.environ.get("MERGEPILOT_V3_RUNSTORE",
                                                   os.path.join(os.path.expanduser("~"),
                                                                ".mergepilot", "v3-runs.db")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4190)
    a = ap.parse_args(argv)
    store = RunStore(a.db)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(store))
    print("v3 console (READ-ONLY) on http://%s:%d  db=%s" % (a.host, a.port, a.db),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
