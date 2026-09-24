# -*- coding: utf-8 -*-
"""PR1SK5 kickoff: low-risk auto path."""
import html, json, os, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx

LEADER = "@leader:elemiso-matrix:6167"
ROOM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
TEAM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"
RUN = "run-elem-pr1sk5-20260919-01"
SPEC = ('pr1sk5-review-1 / ' + RUN + ' - Routine security review (LOW-RISK expected).\n'
        '1) taskflow(ack_task) taskId "pr1sk5-review-1".\n'
        '2) Follow ~/task/PR-METADATA.md: clone, checkout 4cd5bf0, review the PR files.\n'
        '3) taskflow(submit_task) with your independent conclusion (bootstrap PR; if no issue, NOT_CONFIRMED / LOW).\n'
        '4) Reply in team room: TASK_COMPLETED: ' + RUN + '-review\n'
        'Constraints: no repo modification; zero GitHub writes; own workspace only.')
K = ('[kickoff ' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + '] ' + RUN + ' - project elemiso-pr1sk5-auto.\n\n'
     'You are the Team Leader. The DAG has ONE review task. Do NOT create projects/tasks beyond it.\n\n'
     '1. projectflow(ready_nodes) projectId "elemiso-pr1sk5-auto" -> expect pr1sk5-review-1.\n'
     '2. taskflow(delegate_task) projectId "elemiso-pr1sk5-auto" taskId "pr1sk5-review-1" roomId "room:' + TEAM + '" spec = SPEC below.\n'
     '3. Wait; check_task; if NOT_CONFIRMED/LOW: mark completed (no human gate needed for non-HIGH). Message me with final report.\n'
     '=== SPEC ===\n' + SPEC)

def main():
    tok = mx.token()
    pill = '<a href="https://matrix.to/#/' + LEADER + '">' + LEADER + '</a>'
    body = LEADER + ' ' + K
    payload = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
               "formatted_body": pill + ' ' + html.escape(K).replace("\n", "<br>"), "m.mentions": {"user_ids": [LEADER]}}
    txn = "kickoff-" + RUN + "-" + str(int(time.time()))
    r = mx.api("PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(ROOM, safe="") + "/send/m.room.message/" + txn, payload, tok)
    print(json.dumps({"id": r.get("event_id"), "ts": time.strftime('%H:%M:%SZ', time.gmtime())}))

if __name__ == "__main__":
    main()
