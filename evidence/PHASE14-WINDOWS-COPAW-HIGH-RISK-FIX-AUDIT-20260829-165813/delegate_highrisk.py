import asyncio
import json
import os

os.environ.setdefault(
    "COPAW_WORKING_DIR",
    "/root/.copaw-worker/p14h2-copaw-worker-manager/.copaw",
)

from copaw_worker.hooks.tools.taskflow import taskflow  # noqa: E402

WS = os.environ["COPAW_WORKING_DIR"] + "/workspaces/default/shared"
spec = open(WS + "/tasks/review-1/spec.md", encoding="utf-8").read()

resp = asyncio.run(taskflow(
    action="delegate_task",
    payload={
        "projectId": "copaw-high-risk-human-gate",
        "taskId": "review-1",
        "roomId": "room:!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167",
        "spec": spec,
    },
))
item = resp.content[0]
text = item.get("text") if isinstance(item, dict) else item.text
data = json.loads(text)
notif = data.get("notification") or {}
task = data.get("task") or {}
print(json.dumps({
    "ok": data.get("ok"),
    "error": data.get("error"),
    "eventId": notif.get("eventId"),
    "reused": notif.get("reused"),
    "roomId": notif.get("roomId"),
    "taskStatus": task.get("status"),
    "taskEventId": task.get("event_id"),
}, indent=2))
