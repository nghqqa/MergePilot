#!/usr/bin/env python3
"""Assemble FINALS-ELEM-PR2-RAG-TRACED and FINALS-ELEM-PR3-RAG-TRACED packages."""
import hashlib
import json
import os
import shutil
import time
from collections import Counter

SRC = "D:/goai/r3work"
E = SRC + "/evidence"
WT = "D:/goai/mp-worktrees/finals-opt-20260914"
P2 = WT + "/evidence/FINALS-ELEM-PR2-RAG-TRACED"
P3 = WT + "/evidence/FINALS-ELEM-PR3-RAG-TRACED"
PR3_T0 = "2026-09-16T17:25:00Z"
WORKERS = ("leader", "reviewer", "fixer", "verifier")
IMG = [("Dockerfile", "../image-rag/Dockerfile"), ("zz_agentloop_rag.py", SRC + "/image/zz_agentloop_rag.py"),
       ("zzz_rag_hook.pth", SRC + "/image/zzz_rag_hook.pth"), ("agentloop-rag.json", SRC + "/image/agentloop-rag.json"),
       ("rag-mcp-server.mjs", "D:/goai/p14-demo/agentloop-preflight/rag-mcp-server.mjs")]


def cp(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def cptree(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)


def iso_to_ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


def slice_json(src, dst, t0):
    ev = json.load(open(src, encoding="utf-8"))
    out = [e for e in ev if e.get("origin_server_ts", 0) / 1000 >= iso_to_ts(t0)]
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return len(out)


def slice_audit(src, dst, t0):
    lines = [l for l in open(src, encoding="utf-8").read().splitlines() if l[:20] >= t0]
    open(dst, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return len(lines)


def sha256sums(root):
    entries = []
    for dp, _, fns in os.walk(root):
        for fn in sorted(fns):
            if fn == "SHA256SUMS":
                continue
            p = os.path.join(dp, fn)
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            entries.append(f"{h}  {os.path.relpath(p, root).replace(os.sep, '/')}")
    open(os.path.join(root, "SHA256SUMS"), "w", encoding="utf-8", newline="\n").write("\n".join(sorted(entries, key=lambda x: x.split('  ', 1)[1])) + "\n")
    return len(entries)


def main():
    for p in (P2, P3):
        os.makedirs(p, exist_ok=True)

    for pkg in (P2, P3):
        for f in ("enable_otel.py", "matrix.py", "monitor.py", "send_gate_decision.py", "otel_direct_probe.py"):
            cp(f"{SRC}/scripts/{f}", f"{pkg}/scripts/{f}")
        for fn, src in IMG:
            cp(src, f"{pkg}/image/{fn}")
        for w in WORKERS:
            cp(f"{E}/rag-final/agentloop-audit-{w}.log", f"{pkg}/agentloop/audit-{w}-final.log")
            cp(f"{E}/rag-final/agentloop-spans-{w}.log", f"{pkg}/agentloop/spans-{w}-final.log")
            cp(f"{E}/rag-final/rag-hook-{w}.log", f"{pkg}/agentloop/rag-hook-{w}.log")
            cp(f"{E}/rag-final/direct-probe-{w}.json", f"{pkg}/agentloop/direct-probe-{w}.json")
        cp(f"{SRC}/scripts/send_gate_decision.py", f"{pkg}/scripts/send_gate_decision.py")
        cp(f"{E}/rag-final/higress-gateway-final.log", f"{pkg}/higress-gateway-log-final.log")
        cp(f"{E}/rag-usage-summary.json", f"{pkg}/usage-summary.json")
        cp(f"{E}/github-branches-after-rag.txt", f"{pkg}/github-branches-after-rag.txt")
        cp(f"{E}/rag-final/rag-tool-spans.jsonl", f"{pkg}/rag/rag-tool-spans.jsonl")
        cp(f"{SRC}/rag-live/rag-live-corpus.json", f"{pkg}/rag/rag-live-corpus.json")
        cp(f"{SRC}/rag-live/rag-live-server.mjs", f"{pkg}/rag/rag-live-server.mjs")

    # PR2 package
    cp(f"{SRC}/scripts/send_kickoff_pr2rag.py", f"{P2}/scripts/send_kickoff_pr2rag.py")
    cp(f"{E}/pr2rag/gate-approval-sent.json", f"{P2}/gate-approval-sent.json")
    cptree(f"{E}/rag-minio/elemiso-pr2rag-gate", f"{P2}/project")
    for t in ("pr2rag-review-1", "pr2rag-fix-1", "pr2rag-verify-1"):
        cptree(f"{E}/rag-minio/tasks/{t}", f"{P2}/tasks/{t}")
    cp(f"{E}/rag-team-room-full.json", f"{P2}/team-room-messages.json")
    cp(f"{E}/rag-leader-dm-full.json", f"{P2}/leader-dm-messages.json")
    cp(f"{E}/rag-final/controller-project-elemiso-pr2rag-gate.json", f"{P2}/controller-project.json")
    summary = {}
    for w in WORKERS:
        c = Counter(open(f"{E}/rag-final/agentloop-spans-{w}.log", encoding="utf-8").read().split())
        audit = open(f"{E}/rag-final/agentloop-audit-{w}.log", encoding="utf-8").read().splitlines()
        summary[w] = {"spans_created_session": sum(c.values()), "rag_retrieve_spans": c.get("tool.rag_retrieve", 0),
                      "by_name": dict(c.most_common()),
                      "export_batches_ok": sum(1 for l in audit if "OTEL_EXPORT SUCCESS" in l),
                      "spans_exported": sum(int(l.split("n=")[1]) for l in audit if "OTEL_EXPORT SUCCESS" in l),
                      "export_failures": sum(1 for l in audit if "FAILURE" in l or "EXC" in l or "ERROR" in l)}
    json.dump(summary, open(f"{P2}/agentloop/span-summary.json", "w"), indent=1)

    # PR3 package
    cp(f"{SRC}/scripts/send_kickoff_pr3rag.py", f"{P3}/scripts/send_kickoff_pr3rag.py")
    cp(f"{E}/pr3rag/gate-rejection-sent.json", f"{P3}/gate-rejection-sent.json")
    cptree(f"{E}/rag-minio/elemiso-pr3rag-reject", f"{P3}/project")
    cptree(f"{E}/rag-minio/tasks/pr3rag-review-1", f"{P3}/tasks/pr3rag-review-1")
    n1 = slice_json(f"{E}/rag-team-room-full.json", f"{P3}/team-room-messages-pr3rag-window.json", PR3_T0)
    n2 = slice_json(f"{E}/rag-leader-dm-full.json", f"{P3}/leader-dm-messages-pr3rag-window.json", PR3_T0)
    cp(f"{E}/rag-final/controller-project-elemiso-pr3rag-reject.json", f"{P3}/controller-project.json")
    s3 = {"window_start": PR3_T0, "team_room_events": n1, "dm_events": n2,
          "note": "span logs are session-cumulative (incl. PR2-RAG run and smokes); PR3-RAG window deltas see PR2 pkg summary + audit slices"}
    json.dump(s3, open(f"{P3}/agentloop/span-summary.json", "w"), indent=1)
    for w in WORKERS:
        slice_audit(f"{E}/rag-final/agentloop-audit-{w}.log", f"{P3}/agentloop/audit-{w}-pr3rag-window.log", PR3_T0)

    print("PR2 pkg:", sha256sums(P2), "files | PR3 pkg:", sha256sums(P3), "files")


if __name__ == "__main__":
    main()
