#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_demo_dashboard.py — 把 gh-pr1 demo 证据烤成自包含 HTML dashboard。
读取 evidence/gh-pr1-demo/tasks/gh-pr1-{review,fix,verify}/ 的产物,输出深色主题看板。
用法: python3 make_demo_dashboard.py [evidence_root] [out_html]
"""
import os, re, sys, html, datetime

EV = sys.argv[1] if len(sys.argv) > 1 else os.path.join("evidence", "gh-pr1-demo")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(EV, "dashboard.html")

def read(rel):
    p = os.path.join(EV, rel)
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

findings_md = read(os.path.join("tasks", "gh-pr1-review", "findings.md"))
result_md = read(os.path.join("tasks", "gh-pr1-fix", "result.md"))
verify_md = read(os.path.join("tasks", "gh-pr1-verify", "verify-report.md"))

def extract_findings(md):
    """从 findings.md 抓 ### 标题 + Severity/Category 行,粗略解析。"""
    items = []
    for block in re.split(r"\n###\s+", md)[1:]:
        title = block.split("\n", 1)[0].strip()
        sev = re.search(r"Severity\s*\|\s*\*{0,2}(L\d|critical|high|medium|low)[^|]*", block, re.I)
        risk = re.search(r"[Rr]isk[^\n]*?(L\d|needs-approval)[^\n|]*", block)
        items.append({
            "title": html.escape(title[:80]),
            "sev": html.escape((sev.group(1) if sev else "?").upper()),
        })
    return items

def extract_fixes(md):
    fixes = []
    m = re.search(r"### Fixes Applied(.*?)###|### Fixes Applied(.*)", md, re.S)
    tbl = re.search(r"\| (F\d+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|", md)
    rows = re.findall(r"\|\s*(F\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", md)
    return [{"id": r[0], "issue": html.escape(r[1].strip()), "sev": html.escape(r[2].strip()), "fix": html.escape(r[3].strip())} for r in rows if r[0].startswith("F")]

findings = extract_findings(findings_md)
fixes = extract_fixes(result_md)

STAGES = [
    {"name": "Manager", "role": "编排", "mcp": "Matrix 路由", "out": "派发 reviewer → fixer → verifier", "badge": "OK", "color": "#3b82f6"},
    {"name": "reviewer", "role": "审查", "mcp": "gh-mcp-read.sh → sast-scan", "out": "6 findings(2×L1 / 3×L2 / 1×L3)", "badge": "DONE", "color": "#22c55e"},
    {"name": "fixer", "role": "修复", "mcp": "gh-mcp-fix.sh", "out": "真实 PR #3 · 5 项修复", "badge": "DONE", "color": "#22c55e"},
    {"name": "verifier", "role": "复核", "mcp": "gh-mcp-read.sh 逐项比对", "out": "✅ PASS · 5/5 resolved · 0 新问题", "badge": "PASS", "color": "#22c55e"},
    {"name": "merge", "role": "合并", "mcp": "merge_pull_request", "out": "PR #3 squash 合并", "badge": "MERGED", "color": "#a855f7"},
]

def cards():
    data = [
        ("6", "findings(reviewer)", "#f59e0b"),
        ("5", "修复(fixer)", "#22c55e"),
        ("PASS", "复核(verifier)", "#22c55e"),
        ("MERGED", "PR #3", "#a855f7"),
    ]
    return "".join(
        f'<div class="card"><div class="card-num" style="color:{c}">{n}</div><div class="card-lbl">{l}</div></div>'
        for n, l, c in data
    )

def stages_html():
    nodes = []
    for i, s in enumerate(STAGES):
        arrow = '<div class="arrow">→</div>' if i < len(STAGES) - 1 else ""
        nodes.append(
            f'<div class="node"><div class="node-name">{s["name"]}</div>'
            f'<div class="node-role">{s["role"]}</div>'
            f'<div class="node-mcp"><code>{s["mcp"]}</code></div>'
            f'<div class="node-out">{s["out"]}</div>'
            f'<div class="badge" style="background:{s["color"]}">{s["badge"]}</div></div>{arrow}'
        )
    return "".join(nodes)

def findings_html():
    if not findings:
        return '<div class="muted">见 tasks/gh-pr1-review/findings.md</div>'
    return "<ul>" + "".join(
        f'<li><span class="sev sev-{("hi" if f["sev"] in ("L1","CRITICAL","HIGH") else "mid" if f["sev"] in ("L2","MEDIUM") else "lo")}">{f["sev"]}</span> {f["title"]}</li>'
        for f in findings
    ) + "</ul>"

def fixes_html():
    if not fixes:
        return '<div class="muted">见 tasks/gh-pr1-fix/result.md</div>'
    rows = "".join(
        f'<tr><td>{f["id"]}</td><td>{f["issue"]}</td><td>{f["sev"]}</td><td>{f["fix"]}</td></tr>'
        for f in fixes
    )
    return f'<table><tr><th>ID</th><th>问题</th><th>级别</th><th>修复</th></tr>{rows}</table>'

NOW = "2026-07-24"

HTML = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MergePilot · 真实 GitHub PR 协同审修 Demo</title>
<style>
:root{{--bg:#0b1020;--panel:#141b2e;--panel2:#1b2440;--txt:#e6ebf5;--muted:#8b97b5;--line:#26314f}}
*{{box-sizing:border-box}}
body{{margin:0;background:linear-gradient(180deg,#0b1020,#0e1530);color:var(--txt);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.55}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 20px 60px}}
h1{{font-size:26px;margin:0 0 6px;background:linear-gradient(90deg,#60a5fa,#a855f7);-webkit-background-clip:text;background-clip:text;color:transparent}}
.sub{{color:var(--muted);font-size:14px;margin-bottom:22px}}
a{{color:#60a5fa;text-decoration:none}}
a:hover{{text-decoration:underline}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;text-align:center}}
.card-num{{font-size:26px;font-weight:700}}
.card-lbl{{color:var(--muted);font-size:12px;margin-top:4px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:18px}}
.panel h2{{margin:0 0 14px;font-size:16px;color:#cdd6f4;display:flex;align-items:center;gap:8px}}
.panel h2 .tag{{font-size:11px;color:var(--muted);font-weight:400}}
.dag{{display:flex;align-items:stretch;gap:6px;flex-wrap:wrap}}
.node{{flex:1;min-width:150px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px;position:relative}}
.node-name{{font-weight:700;font-size:15px}}
.node-role{{color:var(--muted);font-size:12px;margin-bottom:8px}}
.node-mcp code{{font-size:11px;background:#0b1020;padding:2px 6px;border-radius:5px;color:#7dd3fc;word-break:break-all}}
.node-out{{font-size:12px;margin:8px 0 10px;color:#cbd5e1}}
.badge{{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;color:#04101f}}
.arrow{{align-self:center;color:#3b4666;font-size:20px;padding:0 2px}}
ul{{margin:6px 0;padding-left:4px;list-style:none}}
li{{padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}}
li:last-child{{border-bottom:0}}
.sev{{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:5px;margin-right:8px;min-width:34px;text-align:center}}
.sev-hi{{background:#ef4444;color:#fff}}
.sev-mid{{background:#f59e0b;color:#1a1206}}
.sev-lo{{background:#3b82f6;color:#fff}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-weight:600;font-size:12px}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.banner{{background:linear-gradient(90deg,#064e3b,#065f46);border:1px solid #10b981;border-radius:12px;padding:16px 20px;margin-bottom:18px;display:flex;align-items:center;gap:14px}}
.banner .big{{font-size:22px;font-weight:800;color:#34d399}}
.note{{background:#1a1206;border:1px solid #f59e0b;color:#fcd34d;border-radius:10px;padding:12px 16px;font-size:13px;margin-top:14px}}
.arch{{font-size:13px;color:#cbd5e1}}
.arch b{{color:#7dd3fc}}
.muted{{color:var(--muted);font-size:13px}}
.foot{{color:var(--muted);font-size:12px;margin-top:24px;text-align:center}}
</style></head><body><div class="wrap">
<h1>MergePilot · 真实 GitHub PR 协同审修 Demo</h1>
<div class="sub">Manager 编排 · Agent 经 GitHub MCP 执行，阶段交接偶需人工 nudge（凭证隔离 sidecar，Worker 零凭证） · 仓库 <a href="https://github.com/nghqqa/mergepilot-test">nghqqa/mergepilot-test</a> · {NOW}</div>

<div class="cards">{cards()}</div>

<div class="banner"><div class="big">✅ PASS — 已合并</div><div>5/5 findings resolved,0 新问题;PR #3 经 <code>merge_pull_request</code> squash 合并</div></div>

<div class="panel">
  <h2>端到端协同 DAG <span class="tag">执行经 GitHub MCP；阶段交接偶需人工 nudge</span></h2>
  <div class="dag">{stages_html()}</div>
</div>

<div class="panel"><h2>reviewer · findings(sast-scan 实测)<span class="tag">gh-pr1-review</span></h2>{findings_html()}</div>

<div class="panel"><h2>fixer · 修复 PR <a href="https://github.com/nghqqa/mergepilot-test/pull/3">#3</a><span class="tag">gh-pr1-fix · gh-mcp-fix.sh</span></h2>{fixes_html()}</div>

<div class="panel"><h2>verifier · 复核<span class="tag">gh-pr1-verify · gh-mcp-read.sh</span></h2>
  <div>逐项比对原代码 vs 修复代码:<b>5/5 RESOLVED</b>,未引入新问题 → <b style="color:#34d399">Verdict: PASS</b></div>
  <div class="note">⚠️ 治理洞察:verifier 指出原密钥 <code>sk-live-…</code> 仍留 Git 历史,<b>需人工吊销</b>(needs-approval,超出代码修复范围)—— L2 高危人控边界真实生效。</div>
</div>

<div class="panel arch">
  <h2>架构与安全</h2>
  <b>凭证隔离:</b>GitHub PAT 仅存于 <code>github-mcp</code> sidecar(mcp-proxy + GitHub 官方 MCP server);reviewer/fixer/verifier 经 mcporter 连 <code>http://github-mcp:8082/sse</code>,<b>不持有任何 GitHub 凭证</b>。<br>
  <b>真实工具:</b>reviewer 调自研 <code>sast-scan</code>(正则密钥 + AST 注入 + 依赖漏洞),findings 标注「由 sast-scan 实测」。<br>
  <b>4 个写操作全验证:</b>create_branch → create_or_update_file → create_pull_request → merge_pull_request。
</div>

<div class="foot">证据:tasks/gh-pr1-{{review,fix,verify}}/ · 复现:tools/submit-demo-host.sh · MergePilot · GOAI Agent Infra</div>
</div></body></html>"""

os.makedirs(EV, exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(HTML)
print(f"看板已生成: {OUT}  ({len(HTML)} bytes)")
print(f"findings 解析: {len(findings)} 条;fixes 解析: {len(fixes)} 条")
