#!/usr/bin/env python3
# R2 gate approval -> leader DM + team room (decision only; dispatch is the Leader's job)
import json, os, urllib.request, urllib.parse, time, html


def api(method, path, body=None, tok=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request("http://127.0.0.1:6167" + path, data=data, method=method, headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=15))


def send(tok, room, text, mention):
    body = (mention + " " + text) if mention else text
    payload = {"msgtype": "m.text", "body": body}
    if mention:
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = '<a href="https://matrix.to/#/' + mention + '">' + mention + '</a> ' + html.escape(text)
        payload["m.mentions"] = {"user_ids": [mention]}
    txn = "gate-r2-" + str(int(time.time() * 1000))
    resp = api("PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="") + "/send/m.room.message/" + txn, payload, tok)
    return resp.get("event_id", "NONE")


def main():
    pw = os.environ.get("AGENTTEAMS_ADMIN_PASSWORD", "")
    login = api("POST", "/_matrix/client/v3/login",
                {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": "elemiso-admin"}, "password": pw})
    tok = login.get("access_token", "")
    if not tok:
        print("ADMIN_LOGIN_FAILED")
        raise SystemExit(1)

    LEADER = "@leader:elemiso-matrix:6167"
    DM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
    TEAM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"

    decision = (
        "[HUMAN SECURITY GATE - APPROVED] run-elem-pr2r2-20260916-01, " + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ".\n"
        "The operator has reviewed the pr2r2-review-1 findings (FINDING_CONFIRMED / HIGH / CWE-22) and APPROVES remediation.\n"
        "Proceed per your role contract and the kickoff: delegate pr2r2-fix-1 yourself via taskflow (Fixer follows ~/task/ROLE-CONTRACT.md "
        "+ ~/task/PR-METADATA.md; the spec must include the task id, head SHA, and the contract constraints - no operator side-channel to the Fixer). "
        "Then check_task, accept, delegate pr2r2-verify-1, check_task, accept, and send me the final report. "
        "If verify VERDICT=FAIL: re-delegate fix ONCE with task id pr2r2-fix-2; second FAIL -> stop and escalate. "
        "PR stays OPEN; zero GitHub writes. Approval record: human-gate-approval.md in the project store."
    )
    print("DM_APPROVAL_EVENT_ID=" + send(tok, DM, decision, LEADER))

    team_note = (
        "[HUMAN SECURITY GATE APPROVED] The operator approved remediation for the confirmed HIGH CWE-22 finding. "
        "@leader will now dispatch pr2r2-fix-1 to @fixer per contract; verification follows. PR stays OPEN; zero GitHub writes."
    )
    print("TEAM_APPROVAL_EVENT_ID=" + send(tok, TEAM, team_note, None))


main()
