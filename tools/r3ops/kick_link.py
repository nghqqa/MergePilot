# -*- coding: utf-8 -*-
"""Minimal link-verification kickoff: leader delegates one ack-only review task."""
import html
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx  # noqa: E402

LEADER = "@leader:elemiso-matrix:6167"
ROOM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
TEAM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"
RUN = "run-elem-link-20260917-01"
SPEC = ('link-review-1 / ' + RUN + ' - trace verification ONLY: taskflow(ack_task) taskId "link-review-1"; '
        'then reply in THIS room with exactly RECEIPT-OK; do NOT perform any review; stop.')
K = ('[kickoff ' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + '] ' + RUN +
     ' - minimal trace-verification run, project elemiso-link-test.\n\n'
     'You are the Team Leader. The project DAG has exactly one task. Do NOT create projects or tasks beyond it.\n\n'
     '1. projectflow(ready_nodes) projectId "elemiso-link-test" -> expect link-review-1.\n'
     '2. taskflow(delegate_task) projectId "elemiso-link-test" taskId "link-review-1" roomId "room:' + TEAM +
     '" spec = SPEC below, EXACTLY.\n'
     '3. After delegating, message ME here with the delegation event id and STOP. No further action.\n\n'
     '=== SPEC (link-review-1 -> @reviewer) ===\n' + SPEC)


def main():
    tok = mx.token()
    pill = '<a href="https://matrix.to/#/' + LEADER + '">' + LEADER + '</a>'
    body = LEADER + ' ' + K
    payload = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
               "formatted_body": pill + ' ' + html.escape(K).replace("\n", "<br>"),
               "m.mentions": {"user_ids": [LEADER]}}
    txn = "kickoff-" + RUN + "-" + str(int(time.time()))
    r = mx.api("PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(ROOM, safe="") + "/send/m.room.message/" + txn, payload, tok)
    print(json.dumps({"KICKOFF_EVENT_ID": r.get("event_id", "NONE"),
                      "TS": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                      "error": r.get("_body")}, indent=1))


if __name__ == "__main__":
    main()
