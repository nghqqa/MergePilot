#!/usr/bin/env python3
"""Human security gate decision -> project store record + leader DM (@mention) + team room note.

Operator sends ONLY the decision; dispatch (or non-dispatch) is the Leader's job (R2 discipline).
Usage: python send_gate_decision.py approve|reject <run_id> <project_id> <task_prefix> <pr_label> <head_sha> <finding_summary>
"""
import html
import json
import os
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx  # noqa: E402

LEADER = "@leader:elemiso-matrix:6167"
DM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
TEAM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"
BUCKET = "agentteams/agentteams-storage"


def send(room, text, mention, txn_prefix):
    tok = mx.token()
    body = (mention + " " + text) if mention else text
    payload = {"msgtype": "m.text", "body": body}
    if mention:
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = '<a href="https://matrix.to/#/' + mention + '">' + mention + '</a> ' + html.escape(text).replace("\n", "<br>")
        payload["m.mentions"] = {"user_ids": [mention]}
    txn = txn_prefix + "-" + str(int(time.time() * 1000))
    r = mx.api("PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="") + "/send/m.room.message/" + txn, payload, tok)
    return r.get("event_id", "NONE:" + str(r.get("_body"))[:80])


def minio_put(path, content):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    p = subprocess.run(["docker", "exec", "-i", "elemiso-ctrl", "mc", "pipe", BUCKET + "/" + path],
                       input=content.encode("utf-8"), env=env, capture_output=True)
    return p.returncode == 0


def main():
    mode, run, project, tp, pr_label, head, finding = sys.argv[1:8]
    # 占位符守卫:head 必须是 40 位真实十六进制(曾发生 8 位前缀+补零的占位误填并进入门记录)
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{40}", head) or head.endswith("0" * 20):
        sys.stderr.write("REFUSED: head looks like a placeholder (%s...); pass the real 40-hex SHA
" % head[:12])
        return 4
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    out = {"mode": mode, "run": run, "project": project, "ts": ts}
    if mode == "approve":
        record = f"""# Human Security Gate APPROVAL — {run}

- Project: {project} (team elemiso-team)
- Task reviewed: {tp}-review-1 (Reviewer, copaw runtime, AgentLoop-traced)
- Head SHA under review: {head} ({pr_label})
- Gate decision: **APPROVED — remediation authorized**
- Decision time: {ts} (host clock; operator present and responsive, decision made via interactive gate prompt)
- Approver: repository/workspace owner (human operator)

## Confirmed findings (Reviewer result, real execution — SPEC was non-prescriptive)

{finding}

## Approval scope

1. Leader delegates {tp}-fix-1 (minimal fix; only Reviewer-flagged files; tests frozen; zero GitHub writes).
2. After fix acceptance: Leader delegates {tp}-verify-1 (independent clean-workspace verification).
3. On verify VERDICT=FAIL: one re-delegation as {tp}-fix-2; second FAIL -> stop, block, escalate.

## Acceptance criterion (recorded before dispatch)

After this approval, the team room MUST contain a NEW fix delegation event issued by the Leader (taskflow),
distinct from any earlier/voided event id. The Fixer must start work only after that event, with no operator
execution instructions in between. Every step is exported as OpenTelemetry spans to AgentLoop (SLS, direct).
"""
        ok = minio_put(f"teams/elemiso-team/shared/projects/{project}/human-gate-approval.md", record)
        out["record_written"] = ok
        decision = (
            f"[HUMAN SECURITY GATE - APPROVED] {run}, {ts}.\n"
            f"The operator has reviewed the {tp}-review-1 findings ({finding.splitlines()[0][:120]}) and APPROVES remediation.\n"
            f"Proceed per your role contract and the kickoff: delegate {tp}-fix-1 yourself via taskflow (Fixer follows ~/task/ROLE-CONTRACT.md "
            f"+ ~/task/PR-METADATA.md; the spec must include the task id, head SHA, and the contract constraints - no operator side-channel to the Fixer). "
            f"Then check_task, accept, delegate {tp}-verify-1, check_task, accept, and send me the final report. "
            f"If verify VERDICT=FAIL: re-delegate fix ONCE with task id {tp}-fix-2; second FAIL -> stop and escalate. "
            f"PR stays OPEN; zero GitHub writes. Approval record: human-gate-approval.md in the project store."
        )
        out["dm_event"] = send(DM, decision, LEADER, "gate-approve")
        team_note = (
            f"[HUMAN SECURITY GATE APPROVED] The operator approved remediation for the confirmed finding ({finding.splitlines()[0][:100]}). "
            f"@leader will now dispatch {tp}-fix-1 to @fixer per contract; verification follows. PR stays OPEN; zero GitHub writes."
        )
        out["team_event"] = send(TEAM, team_note, None, "gate-approve-team")
    else:
        record = f"""# Human Security Gate REJECTION — {run}

- Project: {project} (team elemiso-team)
- Task reviewed: {tp}-review-1 (Reviewer, copaw runtime, AgentLoop-traced)
- Head SHA under review: {head} ({pr_label})
- Gate decision time: {ts} (host clock; operator present, decision made via interactive gate prompt)
- Approver: repository/workspace owner (human operator)
- Decision: **HUMAN_SECURITY_REJECTED — no remediation authorized**

## Confirmed findings (Reviewer result, real execution)

{finding}

## Binding effects ordered by the operator

1. {tp}-fix-1: **rejected — MUST NOT be delegated**; plan line marked `[-] rejected`.
2. {tp}-verify-1: **locked — MUST NOT be delegated** (depends on an unauthorized fix); plan line marked `[!] locked`.
3. Project status: **blocked** — the system stops; no further dispatch of any task in this project.
4. All review evidence is retained; nothing is pushed/merged/closed on GitHub; the PR stays OPEN.
"""
        ok = minio_put(f"teams/elemiso-team/shared/projects/{project}/human-gate-rejection.md", record)
        out["record_written"] = ok
        decision = (
            f"[HUMAN SECURITY GATE - REJECTED] {run}, {ts}.\n"
            f"The operator has reviewed the {tp}-review-1 findings ({finding.splitlines()[0][:120]}) and REJECTS remediation: HUMAN_SECURITY_REJECTED.\n"
            f"Binding effects, per your role contract and the kickoff: do NOT delegate {tp}-fix-1 (mark the plan line [-] rejected); "
            f"do NOT delegate {tp}-verify-1 (mark [!] locked); set the project status to blocked; dispatch nothing further in this project. "
            f"Then send me the final report (disposition PROJECT_BLOCKED_HUMAN_REJECTED, task states). PR stays OPEN; zero GitHub writes. "
            f"Rejection record: human-gate-rejection.md in the project store."
        )
        out["dm_event"] = send(DM, decision, LEADER, "gate-reject")
        team_note = (
            f"[HUMAN SECURITY GATE REJECTED] The operator rejected remediation for the confirmed finding ({finding.splitlines()[0][:100]}). "
            f"{tp}-fix-1 will NOT be delegated, {tp}-verify-1 is locked, project blocked. PR stays OPEN; zero GitHub writes."
        )
        out["team_event"] = send(TEAM, team_note, None, "gate-reject-team")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
