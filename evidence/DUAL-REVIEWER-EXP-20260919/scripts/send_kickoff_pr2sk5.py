#!/usr/bin/env python3
"""R3-TRACED kickoff: run-elem-pr2sk5-20260919-01 (admin -> CoPaw leader DM).

Mirrors the R2 kickoff (evidence FINALS-ELEM-PR2-LIVE-20260916-R2/scripts/send_kickoff.py)
character-for-character in structure; only run/project/task ids change (pr2r2 -> pr2r3t).
The Reviewer SPEC is non-prescriptive (no vulnerability class named) — R2 discipline.
"""
import html
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx  # noqa: E402

LEADER = "@leader:elemiso-matrix:6167"
ROOM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"        # admin <-> leader DM
TEAM_ROOM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"   # Team: elemiso-team
RUN = "run-elem-pr2sk5-20260919-01"
PROJECT = "elemiso-pr2sk5-gate"

SPEC_REVIEW = (
    'pr2sk5-review-1 / ' + RUN + ' - Reviewer independent security review.\n'
    '1) taskflow(ack_task) taskId "pr2sk5-review-1".\n'
    '2) Follow your role contract at ~/task/ROLE-CONTRACT.md and the parameters in ~/task/PR-METADATA.md: '
    'clone in your own workspace, checkout head SHA 1dedf5e1992c950557064d8f4fb9039d1523deb3 (verify with git rev-parse HEAD), '
    'review ALL files added by this PR (confirm scope with git diff 4cd5bf0..HEAD --stat), run the tests the PR adds, '
    'and produce your own independent findings per the contract (impact, affected file/function/lines, your own severity and CWE rating, '
    'one reproducible command, real output summary). Use /opt/venv/standard/bin/python.\n'
    '3) taskflow(submit_task) taskId "pr2sk5-review-1" with result per contract (status SUCCESS/BLOCKED; summary containing '
    'STATUS: FINDING_CONFIRMED or NOT_CONFIRMED; SEVERITY: <your rating>; HUMAN_VERIFICATION_REQUIRED: YES or NO).\n'
    '4) Reply in THIS team room mentioning @leader: run_id, role=Reviewer, head SHA first 8 chars, findings; '
    'last line exactly: TASK_COMPLETED: ' + RUN + '-review\n'
    'Constraints per contract: do NOT modify repository files; do NOT include fix/patch code; zero GitHub writes; own workspace only.'
)

KICKOFF = (
    '[kickoff ' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + '] ' + RUN + ' - project ' + PROJECT + ' on team elemiso-team.\n\n'
    'You are the Team Leader. The project DAG already exists. Do NOT create projects or tasks beyond it; do NOT replan. '
    'One delegation at a time, strictly in dependency order.\n\n'
    '1. projectflow(ready_nodes) projectId "' + PROJECT + '" -> expect pr2sk5-review-1.\n'
    '2. taskflow(delegate_task) projectId "' + PROJECT + '" taskId "pr2sk5-review-1" roomId "room:' + TEAM_ROOM + '" '
    'spec = SPEC REVIEW below, passed EXACTLY character-for-character.\n'
    '3. Wait for the Reviewer; taskflow(check_task) taskId "pr2sk5-review-1"; on acceptance mark the plan line [x].\n'
    '4. HUMAN SECURITY GATE (per your role contract): after the Reviewer result, if it contains any HIGH/CRITICAL '
    'confirmed finding or HUMAN_VERIFICATION_REQUIRED: YES - do NOT delegate anything. Message ME in THIS DM with the finding summary '
    'and WAIT for my explicit decision message here. If my decision is APPROVE: delegate pr2sk5-fix-1 (taskflow, SAME rules, spec per '
    'your contract and the manifest - Fixer follows ~/task/ROLE-CONTRACT.md + ~/task/PR-METADATA.md; the spec must include the task id, '
    'head SHA, and that only files the Reviewer flagged may be changed, tests frozen, zero GitHub writes). If my decision is REJECT: '
    'never delegate; mark fix [-] rejected, verify [!] locked, project blocked, and send me the final report.\n'
    '5. After fix accepted: delegate pr2sk5-verify-1 (independent clean workspace verification per contract). If VERDICT=FAIL: '
    're-delegate fix ONCE with a NEW task id pr2sk5-fix-2 (never reuse a voided task id); second FAIL -> stop, block, escalate.\n'
    '6. Final: message ME here: final disposition, task states, patch sha256 (if any).\n'
    'Budget discipline: no redundant tool calls; report BLOCKED instead of improvising.\n\n'
    '=== SPEC REVIEW (pr2sk5-review-1 -> @reviewer) ===\n' + SPEC_REVIEW
)


def main():
    tok = mx.token()
    pill = '<a href="https://matrix.to/#/' + LEADER + '">' + LEADER + '</a>'
    body = LEADER + ' ' + KICKOFF
    payload = {"msgtype": "m.text", "body": body,
               "format": "org.matrix.custom.html",
               "formatted_body": pill + ' ' + html.escape(KICKOFF).replace("\n", "<br>"),
               "m.mentions": {"user_ids": [LEADER]}}
    txn = "kickoff-" + RUN + "-" + str(int(time.time()))
    resp = mx.api("PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(ROOM, safe="") + "/send/m.room.message/" + txn, payload, tok)
    out = {"KICKOFF_EVENT_ID": resp.get("event_id", "NONE"),
           "KICKOFF_TS_UTC": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           "KICKOFF_BYTES": len(body), "run": RUN, "project": PROJECT, "room": ROOM, "error": resp.get("_body")}
    print(json.dumps(out, indent=1))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence", "pr2sk5", "kickoff.json"), "w") as f:
        json.dump(out, f, indent=1)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence", "pr2sk5", "kickoff-as-sent.txt"), "w", encoding="utf-8") as f:
        f.write(body)


if __name__ == "__main__":
    main()
