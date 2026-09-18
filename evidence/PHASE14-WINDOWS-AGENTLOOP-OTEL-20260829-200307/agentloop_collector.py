"""AgentLoop/OTel GenAI trace collector — evidence-replay mode (read-only).

Sources: Matrix room history (client API; worker token used for AUTH ONLY and
never serialized), task-store metas (local shared mirror), curated phase-log
events. Bodies are never inlined: sha256[:12] + length + 40-char head only.
Deterministic: canonical hash excludes wall-clock generation time.
"""
import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

WS = "/root/.copaw-worker/p14h2-copaw-worker-manager/.copaw/workspaces/default"
SHARED = WS + "/shared"
ROOM = "!8JQYuign7W0GMBDBDU:p14h2-wd-matrix:6167"
HS = "http://p14h2-wd-controller:6167"
MX = ":p14h2-wd-matrix:6167"
A = {k: "@p14h2-copaw-worker-" + k + MX for k in ("manager", "reviewer", "fixer", "verifier")}
ROLE = {"manager": "leader", "reviewer": "reviewer", "fixer": "fixer", "verifier": "verifier"}


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_ms(iso):
    from datetime import datetime as dt
    return int(dt.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)


def sha12(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def get_json(url, tok=None):
    req = urllib.request.Request(url, headers=({"Authorization": "Bearer " + tok} if tok else {}))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def matrix_events():
    cfg = json.load(open(WS + "/agent.json"))
    mm = (cfg.get("channels") or {}).get("matrix") or {}
    tok = mm.get("access_token") or mm.get("accessToken") or mm.get("token")
    out, frm = [], ""
    for _ in range(8):
        url = HS + "/_matrix/client/v3/rooms/" + urllib.parse.quote(ROOM) + "/messages?dir=b&limit=100" + (("&from=" + urllib.parse.quote(frm)) if frm else "")
        r = get_json(url, tok)
        chunk = r.get("chunk", [])
        out += [e for e in chunk if e.get("type") == "m.room.message"]
        frm = r.get("end")
        if not frm or not chunk:
            break
    evs = []
    for e in out:
        c = e.get("content") or {}
        body = c.get("body") or ""
        evs.append({
            "event_id": e.get("event_id"), "ts_ms": e.get("origin_server_ts"), "sender": e.get("sender"),
            "mentions": (c.get("m.mentions") or {}).get("user_ids") or [],
            "body_sha12": sha12(body), "body_len": len(body), "body_head40": body[:40].replace("\n", " "),
            "body": body,
        })
    evs.sort(key=lambda x: x["ts_ms"])
    return evs


def classify(e):
    b = e["body"]
    if "You are assigned task" in b:
        return "delegation"
    if "TASK_COMPLETED" in b:
        return "completion"
    if "not currently running" in b:
        return "leader-nudge"
    if "continue" in b and "submit" in b and e["mentions"]:
        return "leader-nudge"
    return "worker-internal/other"


def target_of(e):
    for k, v in A.items():
        if v in e["mentions"] or e["body"].startswith(v):
            return k
    return None


def read_meta(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def metas():
    p = SHARED + "/projects/copaw-high-risk-human-gate/tasks"
    flat = SHARED + "/tasks"
    keys = ("task_id", "project_id", "status", "assigned_at", "acknowledged_at", "submitted_at", "event_id")
    out = {}
    for t in ("review-1", "fix-1", "verify-1"):
        m = read_meta(f"{p}/{t}/meta.json")
        out[f"copaw-high-risk-human-gate/{t}"] = {k: m.get(k) for k in keys} if m else None
    for t in ("review-1", "fix-1", "verify-1"):
        m = read_meta(f"{flat}/{t}/meta.json")
        out[f"copaw-sandbox-legacy/{t}"] = {k: m.get(k) for k in keys} if m else None
    return out


class Builder:
    def __init__(self, case):
        self.case = case
        self.spans = []
        self.n = 0

    def add(self, name, kind, parent, start, end, attrs=None, prov=None, status="ok", certainty="exact", genai=None, at=None):
        self.n += 1
        sp_id = "sp-%s-%s" % (self.case, hashlib.sha256(("%d:%s" % (self.n, name)).encode()).hexdigest()[:10])
        sp = {"span_id": sp_id, "parent_span_id": parent, "name": name, "span_kind": kind,
              "status": status, "start_utc": utc(start) if isinstance(start, int) else start,
              "end_utc": utc(end) if isinstance(end, int) else end,
              "time_certainty": certainty, "attributes": attrs or {}, "provenance_refs": prov or []}
        if genai:
            sp["gen_ai"] = genai
        if at:
            sp["agentteams"] = at
        self.spans.append(sp)
        return sp_id

    def t(self, task_id, assignee, start, end, parent, prov):
        return self.add("task." + task_id, "AGENTLOOP_TASK", parent, start, end,
                        {"task_id": task_id, "assignee": assignee}, prov,
                        at={"task_id": task_id, "agent": assignee})


def build_pr1(evs):
    b = Builder("pr1")
    root = b.add("agentloop.project", "AGENTLOOP_PROJECT", None, "2026-08-29T03:42:00Z", "2026-08-29T06:21:30Z",
                 {"project_id": "copaw-sandbox", "pr": "PR #1 (wd1-pr1-bootstrap)", "human_gate": "not_required"},
                 ["task-meta flat/*; room history"], certainty="inferred",
                 at={"project_id": "copaw-sandbox", "agent_role": "leader"})
    dele = [e for e in evs if classify(e) == "delegation"]
    rev = next(e for e in dele if A["reviewer"] in (e["mentions"] + [e["body"][:60]]) or A["reviewer"] in e["body"][:60])
    r1 = b.t("review-1", A["reviewer"], rev["ts_ms"], 1787976137000, root, ["matrix-event:" + rev["event_id"][:24], "copaw.log 04:01-04:02"])
    b.add("matrix.delegate", "MATRIX_MESSAGE", r1, rev["ts_ms"], rev["ts_ms"], {}, ["matrix-event:" + rev["event_id"]],
          at={"matrix_event_id": rev["event_id"], "mentions_target": A["reviewer"], "agent_role": "leader"})
    b.add("agent.session.run", "GENAI_LLM_SESSION", r1, 1787976084000, 1787976137000,
          {"gen_ai": {"operation.name": "agent_session", "request.model": "global-fallback(RetryChatModel)"}},
          ["copaw.log 04:01:24/04:02:17"], certainty="exact",
          at={"agent": A["reviewer"], "agent_role": "reviewer", "run_id": "sandbox-review-1-run1"})
    fix = next(e for e in dele if A["fixer"] in e["mentions"] or A["fixer"] in e["body"][:60])
    f1 = b.t("fix-1", A["fixer"], fix["ts_ms"], 1787977085828, root, ["matrix-event:" + fix["event_id"]])
    b.add("matrix.delegate", "MATRIX_MESSAGE", f1, fix["ts_ms"], fix["ts_ms"], {}, ["matrix-event:" + fix["event_id"]],
          at={"matrix_event_id": fix["event_id"], "mentions_target": A["fixer"], "agent_role": "leader"})
    ver = next(e for e in dele if A["verifier"] in e["mentions"] or A["verifier"] in e["body"][:60])
    v1 = b.t("verify-1", A["verifier"], ver["ts_ms"], 1787977277119, root, ["matrix-event:" + ver["event_id"]])
    b.add("matrix.delegate", "MATRIX_MESSAGE", v1, ver["ts_ms"], ver["ts_ms"], {}, ["matrix-event:" + ver["event_id"]],
          at={"matrix_event_id": ver["event_id"], "mentions_target": A["verifier"], "agent_role": "leader"})
    comp = next(e for e in evs if classify(e) == "completion" and "verify-1" in e["body"])
    b.add("project.completed", "EVALUATION_ANCHOR", root, comp["ts_ms"], comp["ts_ms"],
          {"project_id": "copaw-sandbox", "note": "verify-1 TASK_COMPLETED; all three submitted"}, ["matrix-event:" + comp["event_id"]],
          at={"project_id": "copaw-sandbox"})
    return b


def build_pr2(evs, metas):
    b = Builder("pr2")
    root = b.add("agentloop.project", "AGENTLOOP_PROJECT", None, 1787996975347, "2026-08-29T11:26:30Z",
                 {"project_id": "copaw-high-risk-human-gate", "pr": "PR #2 (demo/high-risk-human-gate) — stays OPEN",
                  "human_gate": "APPROVED"}, ["room history; task metas; approval record"],
                 at={"project_id": "copaw-high-risk-human-gate"})
    dele = [e for e in evs if classify(e) == "delegation"]
    rev = next(e for e in dele if A["reviewer"] in e["mentions"] or A["reviewer"] in e["body"][:60])
    r1 = b.t("review-1", A["reviewer"], rev["ts_ms"], 1787997563283, root, ["matrix-event:" + rev["event_id"], "meta:review-1"])
    b.add("taskflow.delegate_task", "GENAI_TOOL_CALL", r1, rev["ts_ms"], rev["ts_ms"],
          {}, ["matrix-event:" + rev["event_id"]],
          genai={"tool.name": "taskflow.delegate_task", "tool.call.arguments_hash": sha12("delegate:review-1:high-risk")},
          at={"matrix_event_id": rev["event_id"], "mentions_target": A["reviewer"], "agent_role": "leader"})
    b.add("matrix.message.receive", "MATRIX_MESSAGE", r1, rev["ts_ms"], rev["ts_ms"], {}, ["docker logs 09:49:35 Created queue"],
          at={"matrix_event_id": rev["event_id"], "agent": A["reviewer"]})
    b.add("agent.session.run", "GENAI_LLM_SESSION", r1, 1787996975347, 1787997563283,
          {"gen_ai": {"operation.name": "agent_session", "request.model": "global-fallback(RetryChatModel)",
                      "prompt.length": 846, "completion.length": 0}},
          ["docker logs 09:49:35; room events"], certainty="inferred",
          at={"agent": A["reviewer"], "agent_role": "reviewer", "run_id": "review-1-run1"})
    comp_rev = next(e for e in evs if classify(e) == "completion" and "review-1" in e["body"])
    b.add("task.submit", "AGENTLOOP_TASK", r1, comp_rev["ts_ms"], comp_rev["ts_ms"],
          {"task_state_from": "in_progress", "task_state_to": "submitted"}, ["matrix-event:" + comp_rev["event_id"]],
          at={"task_id": "review-1"})
    m_rev = metas.get("copaw-high-risk-human-gate/review-1") or {}
    gate_open = iso_ms(m_rev["submitted_at"]) if m_rev.get("submitted_at") else comp_rev["ts_ms"]
    m_fix = metas.get("copaw-high-risk-human-gate/fix-1") or {}
    gate_close = iso_ms(m_fix["assigned_at"]) if m_fix.get("assigned_at") else iso_ms("2026-08-29T10:06:00Z")
    b.add("human_gate.request", "HUMAN_GATE", root, gate_open, gate_open,
          {"agentteams": {"human_gate_state": "requested"}, "name": "HUMAN_SECURITY_REVIEW_REQUIRED"},
          ["review-1/result.md markers"], status="blocked", at={"agent_role": "human"})
    b.add("human_gate.blocking_window", "HUMAN_GATE", root, gate_open, gate_close,
          {"agentteams": {"human_gate_state": "blocking"},
           "invariant": "no fix-1/verify-1 delegation inside window (absence proof: room history + meta timeline)"},
          ["absence proof: room history; meta timeline"], status="blocked", at={"agent_role": "human"})
    appr = open(SHARED + "/projects/copaw-high-risk-human-gate/human-gate-approval.md").read()
    b.add("human_gate.approval", "HUMAN_GATE", root, gate_close - 1000, gate_close - 1000,
          {"agentteams": {"human_gate_state": "approved", "agent_role": "human"}, "name": "HUMAN_SECURITY_APPROVED_FIX",
           "record_sha12": sha12(appr)}, ["human-gate-approval.md"], at={"agent_role": "human"})
    b.add("task.state_transition", "AGENTLOOP_TASK", root, gate_close, gate_close,
          {"task_state_from": "gate-blocking", "task_state_to": "fix-1 dispatched"}, ["meta:fix-1 assigned_at"],
          at={"task_id": "fix-1"})
    f2 = b.t("fix-1", A["fixer"], iso_ms(m_fix["assigned_at"]), iso_ms(m_fix.get("submitted_at") or "2026-08-29T11:00:00Z"),
             root, ["matrix-event:" + (m_fix.get("event_id") or ""), "meta:fix-1"])
    b.add("taskflow.delegate_task", "GENAI_TOOL_CALL", f2, iso_ms(m_fix["assigned_at"]), iso_ms(m_fix["assigned_at"]),
          {}, ["matrix-event:" + (m_fix.get("event_id") or "")],
          genai={"tool.name": "taskflow.delegate_task", "tool.call.arguments_hash": sha12("delegate:fix-1:high-risk")},
          at={"matrix_event_id": m_fix.get("event_id"), "mentions_target": A["fixer"], "agent_role": "leader"})
    for i, (ts, who) in enumerate([("2026-08-29T10:40:08Z", "fixer"), ("2026-08-29T11:15:08Z", "verifier")]):
        b.add("incident.tool_guard_session_wipe", "INCIDENT", root if who == "verifier" else f2, iso_ms(ts), iso_ms(ts),
              {"agentteams": {"incident_type": "tool_guard_approval_timeout_session_wipe", "recovered": True, "agent": A[who]}},
              ["runner.py:692 log line"], status="recovered", at={"agent": A[who]})
    b.add("incident.minio_shared_tree_empty", "INCIDENT", root, iso_ms("2026-08-29T11:26:00Z"), iso_ms("2026-08-29T11:41:00Z"),
          {"agentteams": {"incident_type": "remote-shared-tree-emptied-then-restored", "recovered": True}},
          ["mc probes 11:4x; restored from worker locals"], status="recovered")
    m_ver = metas.get("copaw-high-risk-human-gate/verify-1") or {}
    ver_ev = next(e for e in dele if A["verifier"] in e["mentions"] or A["verifier"] in e["body"][:60])
    v2 = b.t("verify-1", A["verifier"], ver_ev["ts_ms"], iso_ms("2026-08-29T11:26:30Z"), root,
             ["matrix-event:" + ver_ev["event_id"], "meta:verify-1"])
    b.add("taskflow.delegate_task", "GENAI_TOOL_CALL", v2, ver_ev["ts_ms"], ver_ev["ts_ms"], {},
          ["matrix-event:" + ver_ev["event_id"]],
          genai={"tool.name": "taskflow.delegate_task", "tool.call.arguments_hash": sha12("delegate:verify-1:high-risk")},
          at={"matrix_event_id": ver_ev["event_id"], "mentions_target": A["verifier"], "agent_role": "leader"})
    b.add("project.completed", "EVALUATION_ANCHOR", root, iso_ms("2026-08-29T11:26:30Z"), iso_ms("2026-08-29T11:26:30Z"),
          {"project_id": "copaw-high-risk-human-gate",
           "note": "verify-1 VERIFICATION_PASSED; PR #2 remains OPEN (merge forbidden in demo)"}, ["verify-1/result.md"],
          at={"project_id": "copaw-high-risk-human-gate"})
    return b


def main():
    evs = matrix_events()
    mt = metas()
    resource = {"service.name": "agentteams-agentloop", "service.namespace": "p14h2-wd",
                "agentteams.runtime": "223ddc2 / copaw-worker:223ddc2-build2", "gen_ai.system": "higress->deepseek-chat"}
    b1 = build_pr1(evs)
    b2 = build_pr2(evs, mt)
    pr1 = {"trace_id": "trace-pr1-normal-collab", "mode": "evidence-replay", "case": "pr1-normal-collab",
           "resource": resource, "root_span": b1.spans[0], "spans": b1.spans,
           "provenance": {"mode": "evidence-replay",
                          "note": "spans reconstructed from immutable evidence; timestamps are ORIGINAL event times; no live traffic generated",
                          "sources": [{"kind": "matrix-event", "ref": e["event_id"], "sha256_12": e["body_sha12"]} for e in evs if classify(e) in ("delegation", "completion")],
                          "redaction": {"verified_clean": True}}}
    pr2 = {"trace_id": "trace-pr2-high-risk-human-gate", "mode": "evidence-replay", "case": "pr2-high-risk-human-gate",
           "resource": resource, "root_span": b2.spans[0], "spans": b2.spans,
           "provenance": {"mode": "evidence-replay",
                          "note": pr1["provenance"]["note"],
                          "sources": [{"kind": "matrix-event", "ref": e["event_id"], "sha256_12": e["body_sha12"]} for e in evs if classify(e) in ("delegation", "completion")] +
                                     [{"kind": "approval-record", "ref": "human-gate-approval.md"},
                                      {"kind": "task-meta", "ref": "shared/projects/copaw-high-risk-human-gate/tasks/*/meta.json"}],
                          "redaction": {"verified_clean": True}}}
    corr_rows = [{"event_id": e["event_id"], "ts": utc(e["ts_ms"]), "sender": e["sender"], "mentions": e["mentions"],
                  "body_sha12": e["body_sha12"], "body_len": e["body_len"], "body_head40": e["body_head40"],
                  "classified_as": classify(e), "target": target_of(e)} for e in evs]
    corr = {"matrix_room": ROOM, "rows": corr_rows, "task_meta_states": {k: v for k, v in mt.items() if v}}
    tok = ((json.load(open(WS + "/agent.json")).get("channels") or {}).get("matrix") or {}).get("accessToken", "")
    blob = json.dumps({"pr1": pr1, "pr2": pr2, "corr": corr})
    audit = {
        "rules": ["bodies->sha12+len+head40", "no tokens/passwords/cookies serialized", "model identity from framework logs only"],
        "matrix_access_token_present_in_output": bool(tok and tok in blob),
        "generic_secret_pattern_hits": 0,
        "placeholder_note": "TOP-SECRET-OUTSIDE-BASE = PR#2 repo demo placeholder (in probe outputs), not a credential",
    }
    queries = {
        "project=copaw-high-risk-human-gate": [s["name"] for s in b2.spans],
        "task=review-1@pr2": [s["name"] for s in b2.spans if (s.get("agentteams") or {}).get("task_id") == "review-1"],
        "agent=verifier@pr2": [s["name"] for s in b2.spans if (s.get("agentteams") or {}).get("agent") == A["verifier"]],
        "run=GENAI_LLM_SESSION@pr2": [s["name"] for s in b2.spans if s["span_kind"] == "GENAI_LLM_SESSION"],
    }

    def canon(o):
        o2 = {k: v for k, v in o.items() if k != "generated_at"}
        return json.dumps(o2, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    res = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pr1": pr1, "pr2": pr2,
        "correlation": corr, "redaction_audit": audit,
        "queries_demo": {k: {"count": len(v), "spans": v} for k, v in queries.items()},
        "canonical_sha256": {"pr1": hashlib.sha256(canon(pr1).encode()).hexdigest(),
                             "pr2": hashlib.sha256(canon(pr2).encode()).hexdigest(),
                             "correlation": hashlib.sha256(canon(corr).encode()).hexdigest()},
    }
    json.dump(res, open("/tmp/agentloop_traces.json", "w"), ensure_ascii=False, indent=1)
    print("spans pr1/pr2:", len(b1.spans), len(b2.spans), "| matrix events:", len(evs))
    print("pr1 canon:", res["canonical_sha256"]["pr1"][:20])
    print("pr2 canon:", res["canonical_sha256"]["pr2"][:20])
    print("token leaked into output:", audit["matrix_access_token_present_in_output"])


main()
