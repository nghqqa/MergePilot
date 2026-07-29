# -*- coding: utf-8 -*-
"""
MergePilot Trace Aggregator (observability v1)
把一次 MergePilot 运行的证据产物(code-audit-<project>-NN/)自动汇成结构化 Trace。
输出 trace.json(OpenTelemetry 风格 spans)+ trace.md(可读时间线)。

用法:
    python tools/trace_aggregator.py [evidence_root] [project_id]
默认 evidence_root = 包内 evidence/，也可通过参数指定。
"""
import os, re, json, glob, io, sys

PACKAGE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_ROOT = os.environ.get("MERGEPILOT_EVIDENCE", os.path.join(PACKAGE_ROOT, "evidence"))

# DAG 节点序号 -> agent(回退用;内容识别优先)
SEQ_AGENT = {1: "reviewer", 2: "fixer", 3: "verifier", 4: "fixer", 5: "verifier"}


def find_projects(root):
    dirs = [d for d in glob.glob(os.path.join(root, "*-*")) if os.path.isdir(d)]
    projects = {}
    for d in dirs:
        base = os.path.basename(d)
        m = re.match(r"(.+-\d+)-(\d{2})$", base)
        if not m:
            continue
        pid, seq = m.group(1), int(m.group(2))
        projects.setdefault(pid, []).append((seq, d))
    for pid in projects:
        projects[pid].sort()
    return projects


def infer_agent(text, seq):
    # MergePilot DAG 序号固定 -> agent,优先用序号(内容关键词容易交叉误判)
    if seq in SEQ_AGENT:
        return SEQ_AGENT[seq]
    t = text.lower()
    if "verifier" in t:
        return "verifier"
    if "fixer" in t:
        return "fixer"
    if "reviewer" in t:
        return "reviewer"
    if "coordinator" in t:
        return "coordinator"
    return "unknown"


def extract_verdict(text):
    if re.search(r"needs-approval|需人工审批|待审批|审批待办", text):
        return "needs-approval"
    if re.search(r"FAIL|失败|未通过|❌", text):
        return "fail"
    if re.search(r"PASS|通过|已完成|已修复|已消除|✅", text):
        return "pass"
    return "info"


def summarize(text, limit=140):
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and not s.startswith("|") and not s.startswith("---") and not s.startswith(">"):
            return s[:limit]
    return ""


def read(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def build_spans(task_dirs):
    spans = []
    for seq, d in task_dirs:
        files = []
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                files.append(os.path.relpath(os.path.join(dp, f), d))
        text = read(os.path.join(d, "result.md")) or read(os.path.join(d, "review-report.md"))
        spans.append({
            "seq": seq,
            "task_id": os.path.basename(d),
            "agent": infer_agent(text, seq),
            "verdict": extract_verdict(text),
            "summary": summarize(text),
            "artifacts": sorted(files),
            "artifact_count": len(files),
        })
    return spans


def render_md(project_id, spans):
    out = ["# MergePilot 执行 Trace · `%s`\n" % project_id,
           "> 由 `trace_aggregator.py` 从证据产物自动生成(可观测 v1)。\n",
           "## DAG 时间线", "", "| # | Agent | 裁定 | 摘要 |", "|---|---|---|---|"]
    for s in spans:
        out.append("| %02d | %s | %s | %s |" % (s["seq"], s["agent"], s["verdict"],
                                              s["summary"].replace("|", "/")[:90]))
    out += ["", "## 各 Span 产出物", ""]
    for s in spans:
        out.append("### %s — %s — **%s**" % (s["task_id"], s["agent"], s["verdict"]))
        out.append("- 摘要:%s" % (s["summary"] or "(无)"))
        out.append("- 产出(%d):%s" % (s["artifact_count"], ", ".join("`%s`" % f for f in s["artifacts"])))
        out.append("")
    return "\n".join(out)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    only = sys.argv[2] if len(sys.argv) > 2 else None
    projects = find_projects(root)
    if not projects:
        print("No code-audit-* projects found under", root); sys.exit(1)
    for pid, tasks in projects.items():
        if only and pid != only and not pid.startswith(only):
            continue
        spans = build_spans(tasks)
        outdir = os.path.join(root, pid)
        os.makedirs(outdir, exist_ok=True)
        with io.open(os.path.join(outdir, "trace.json"), "w", encoding="utf-8") as f:
            json.dump({"project": pid, "span_count": len(spans), "spans": spans}, f, ensure_ascii=False, indent=2)
        with io.open(os.path.join(outdir, "trace.md"), "w", encoding="utf-8") as f:
            f.write(render_md(pid, spans))
        print("TRACE %s -> %d spans -> %s/trace.{json,md}" % (pid, len(spans), outdir))


if __name__ == "__main__":
    main()
