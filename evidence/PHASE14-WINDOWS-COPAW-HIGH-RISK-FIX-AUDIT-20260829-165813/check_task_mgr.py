import asyncio, json, os
os.environ.setdefault("COPAW_WORKING_DIR", "/root/.copaw-worker/p14h2-copaw-worker-manager/.copaw")
os.environ.setdefault("QWENPAW_WORKING_DIR", os.environ["COPAW_WORKING_DIR"])
os.environ.setdefault("AGENTTEAMS_MATRIX_USER_ID", "@p14h2-copaw-worker-manager:p14h2-wd-matrix:6167")
from copaw_worker.hooks.tools.taskflow import taskflow
resp = asyncio.run(taskflow(action="check_task", payload={"taskId": "review-1", "projectId": "copaw-high-risk-human-gate"}))
item = resp.content[0]
data = json.loads(item.get("text") if isinstance(item, dict) else item.text)
print(json.dumps({
  "ok": data.get("ok"),
  "error": data.get("error"),
  "status": (data.get("task") or {}).get("status"),
  "resultStatus": (data.get("result") or {}).get("status"),
  "summary": ((data.get("result") or {}).get("summary") or "")[:140],
  "effective": data.get("effective"),
}, indent=2, ensure_ascii=False))
