# -*- coding: utf-8 -*-
"""
make_dashboard.py — 把一次 MergePilot 运行的 trace.json + 证据烤进一个自包含 HTML dashboard(演示用)。
用法:python tools/make_dashboard.py [project_id]
默认读取包内 evidence/，输出到 samples/output/dashboard-<project>.html。
"""
import os, sys, json, glob, io, re

PACKAGE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVIDENCE = os.environ.get("MERGEPILOT_EVIDENCE", os.path.join(PACKAGE_ROOT, "evidence"))
DEMO = os.environ.get("MERGEPILOT_DEMO", os.path.join(PACKAGE_ROOT, "samples", "output"))
os.makedirs(DEMO, exist_ok=True)

AGENT_COLOR = {
    "reviewer": "#2563eb", "fixer": "#059669", "verifier": "#7c3aed",
    "coordinator": "#d97706", "unknown": "#6b7280",
}
VERDICT_LABEL = {
    "pass": ("✓ PASS", "#059669"), "fail": ("✗ FAIL", "#dc2626"),
    "needs-approval": ("⚠ HOLD", "#d97706"), "merge-after-approval": ("✓ MERGE（人审后）", "#059669"),
    "info": ("• INFO", "#6b7280"),
}


def latest_project(only=None):
    # a project = a dir containing trace.json
    traces = glob.glob(os.path.join(EVIDENCE, "*", "trace.json"))
    best = None
    for t in traces:
        proj = os.path.dirname(t)
        if only and only not in os.path.basename(proj):
            continue
        try:
            tm = os.path.getmtime(t)
        except OSError:
            continue
        if best is None or tm > best[0]:
            best = (tm, proj)
    return best[1] if best else None


def load_trace(root):
    p = os.path.join(root, "trace.json")
    if os.path.exists(p):
        with io.open(p, encoding="utf-8") as f:
            return json.load(f).get("spans", [])
    return []


def extract_findings(root):
    """scan sub-task findings.md / result.md (project-NN dirs) for finding lines."""
    proj = os.path.basename(root)
    out = []
    for md in glob.glob(os.path.join(EVIDENCE, proj + "-*", "findings.md")) + glob.glob(os.path.join(EVIDENCE, proj + "-*", "result.md")):
        try:
            text = io.open(md, encoding="utf-8").read()
        except Exception:
            continue
        for line in text.splitlines():
            s = line.strip()
            if re.search(r"F-\d+|critical|high|medium|low|L[012]|密钥|注入|漏洞|依赖", s, re.I) and len(s) < 200:
                if s not in out:
                    out.append(s)
    return out[:12]


HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>MergePilot · 执行 Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif}}
body{{background:#0f172a;color:#e2e8f0;padding:32px}}
h1{{font-size:26px;margin-bottom:4px}} .sub{{color:#94a3b8;margin-bottom:24px;font-size:14px}}
.banner{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px 24px;margin-bottom:28px;display:flex;align-items:center;gap:16px}}
.banner .v{{font-size:22px;font-weight:700}}
.timeline{{display:flex;gap:14px;overflow-x:auto;padding-bottom:12px}}
.node{{background:#1e293b;border-radius:12px;padding:16px;min-width:220px;border-top:4px solid #334155;position:relative}}
.node .seq{{color:#64748b;font-size:12px}} .node .agent{{font-weight:700;font-size:16px;margin:2px 0 6px}}
.node .sum{{font-size:13px;color:#cbd5e1;line-height:1.5}}
.badge{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;margin-top:8px}}
.arrow{{align-self:center;color:#475569;font-size:22px}}
.section{{margin-top:32px}} .section h2{{font-size:18px;margin-bottom:12px;color:#f1f5f9}}
.findings li{{background:#1e293b;border-radius:8px;padding:10px 14px;margin-bottom:8px;list-style:none;font-size:13px;color:#cbd5e1;border-left:3px solid #334155}}
.findings li.crit{{border-left-color:#dc2626}} .findings li.med{{border-left-color:#d97706}}
</style></head><body>
<h1>MergePilot · 执行 Dashboard</h1>
<div class="sub">{subtitle}</div>
<div class="banner"><div class="v" style="color:{vc}">{vlabel}</div><div style="color:#94a3b8;font-size:13px">{spans} 个任务 · 全程可审计 · 证据已沉淀</div></div>
<div class="section"><h2>DAG 执行时间线</h2><div class="timeline">{nodes}</div></div>
<div class="section"><h2>Findings(实测)</h2><ul class="findings">{findings}</ul></div>
<div class="sub" style="margin-top:32px">由 make_dashboard.py 从运行证据自动生成 · MergePilot</div>
</body></html>"""


def render(project_id, spans, findings):
    # Overall verdict follows the final workflow outcome, not the worst intermediate span.
    verdicts = [s.get("verdict", "info") for s in spans]
    if "fail" in verdicts:
        overall = "fail"
    elif "needs-approval" in verdicts:
        last_hold = max(i for i, verdict in enumerate(verdicts) if verdict == "needs-approval")
        later = verdicts[last_hold + 1:]
        overall = "merge-after-approval" if later and all(verdict == "pass" for verdict in later) else "needs-approval"
    elif verdicts and verdicts[-1] == "pass":
        overall = "pass"
    else:
        overall = "info"
    vlabel, vcolor = VERDICT_LABEL.get(overall, ("• INFO", "#6b7280"))
    nodes_html = []
    for i, s in enumerate(spans):
        agent = s.get("agent", "?")
        topc = AGENT_COLOR.get(agent, "#6b7280")
        vl, vc = VERDICT_LABEL.get(s.get("verdict"), ("•", "#6b7280"))
        nodes_html.append(
            f'<div class="node" style="topc-placeholder"><div class="seq">步骤 {s.get("seq","?"):02d}</div>'
            f'<div class="agent">{agent}</div><div class="sum">{s.get("summary","")[:90] or "—"}</div>'
            f'<span class="badge" style="background:{vc}22;color:{vc}">{vl}</span></div>'.replace("topc-placeholder", f"border-top-color:{topc}")
        )
        if i < len(spans) - 1:
            nodes_html.append('<div class="arrow">→</div>')
    find_html = []
    for ftext in findings:
        cls = "crit" if re.search(r"critical|密钥|注入|L2", ftext, re.I) else ("med" if re.search(r"medium|L1", ftext, re.I) else "")
        find_html.append(f'<li class="{cls}">{ftext}</li>')
    if not find_html:
        find_html.append('<li>(无 findings)</li>')
    return HTML.format(
        subtitle=f"项目 <code>{project_id}</code> · AgentTeams(HiClaw)+ DeepSeek · 自动生成",
        vc=vcolor, vlabel=vlabel, spans=len(spans),
        nodes="".join(nodes_html), findings="".join(find_html),
    )


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    root = latest_project(only)
    if not root:
        print("no project found under", EVIDENCE); sys.exit(1)
    project_id = os.path.basename(root)
    spans = load_trace(root)
    findings = extract_findings(root)
    html = render(project_id, spans, findings)
    out = os.path.join(DEMO, f"dashboard-{project_id}.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("DASHBOARD ->", out, "(%d spans, %d findings)" % (len(spans), len(findings)))


if __name__ == "__main__":
    main()
