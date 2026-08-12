#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo Console Renderer — generates self-contained static HTML from a DemoBundle.

Zero external dependencies: inline CSS, vanilla JS, no npm, no CDN, no network.
All data comes from the DemoBundle JSON — no hardcoded results.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _esc(text) -> str:
    """HTML-escape text."""
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _mode_banner(mode: str) -> str:
    colors = {"REPLAY": "#2563eb", "ISOLATED_LIVE": "#d97706", "HISTORICAL": "#6b7280"}
    color = colors.get(mode, "#6b7280")
    return f'''<div class="mode-banner" style="background:{color}">MODE: {mode}</div>'''


def _status_badge(status: str) -> str:
    colors = {"MERGED": "#16a34a", "HELD": "#d97706", "REJECTED": "#dc2626",
              "ROLLED_BACK": "#7c3aed", "SUCCEEDED": "#16a34a", "PASS": "#16a34a",
              "FAIL": "#dc2626", "CREATED": "#2563eb", "OK": "#16a34a",
              "ERROR": "#dc2626", "UNKNOWN": "#6b7280"}
    color = colors.get(str(status).upper(), "#6b7280")
    return f'<span class="badge" style="background:{color}">{_esc(status)}</span>'


def build_span_tree(spans: list[dict]) -> dict:
    """Build a hierarchical tree from spans using parent_span_id.

    Roots are spans with no parent_span_id (or parent not in the set).
    """
    by_id = {}
    for sp in spans:
        sid = sp.get("span_id", "")
        by_id[sid] = {**sp, "children": []}

    roots = []
    for sid, node in by_id.items():
        parent = node.get("parent_span_id")
        if parent and parent in by_id:
            by_id[parent]["children"].append(node)
        else:
            roots.append(node)
    return {"children": roots}


def render_span_tree_node(node: dict, depth: int = 0) -> str:
    """Recursively render a span tree node as HTML."""
    indent = depth * 20
    name = _esc(node.get("name", "unknown"))
    status = node.get("status", "UNSET")
    dur = node.get("duration_ms", 0)
    attrs = node.get("attributes", {})
    attr_html = ""
    if attrs:
        attr_items = "".join(
            f"<li><code>{_esc(k)}</code>: {_esc(v)}</li>"
            for k, v in sorted(attrs.items()) if not str(k).startswith("mp.secret")
        )
        if attr_items:
            attr_html = f'<ul class="span-attrs">{attr_items}</ul>'

    children_html = ""
    for child in node.get("children", []):
        children_html += render_span_tree_node(child, depth + 1)

    # Build inner content, skipping empty sections to avoid trailing whitespace
    lines = [
        f'      <span class="span-status span-status-{status.lower()}">●</span>',
        f'      <strong>{name}</strong>',
        f'      <span class="span-duration">{dur}ms</span>',
        f'      <span class="span-status-text">{_esc(status)}</span>',
    ]
    if attr_html:
        lines.append(f'      {attr_html}')
    if children_html:
        lines.append(f'      {children_html}')

    inner = "\n".join(lines)
    return f'''<div class="span-node" style="margin-left:{indent}px">
{inner}
    </div>'''


# ── Page renderers ─────────────────────────────────────────────────────────

def render_overview(bundle: dict) -> str:
    b = bundle
    topo = b.get("topology", {})
    skills = b.get("agents", [])
    skill_grid = "".join(
        f'<div class="skill-card"><strong>{_esc(s.get("skill"))}</strong><br>'
        f'<span class="skill-role">{_esc(s.get("role"))}</span><br>'
        f'{_status_badge(s.get("status","UNKNOWN"))}</div>'
        for s in skills
    )
    return f'''
    <section id="overview" class="page active">
      <h2>Overview</h2>
      <div class="final-status">Final Status: {_status_badge(b.get("final_status","UNKNOWN"))}</div>
      <div class="meta">
        <p><strong>Run:</strong> {_esc(b.get("run",{}).get("run_id"))}</p>
        <p><strong>Repo:</strong> {_esc(b.get("repo"))}</p>
        <p><strong>Trace:</strong> <code>{_esc(b.get("run",{}).get("trace_id",""))}</code></p>
        <p><strong>Source commit:</strong> <code>{_esc(b.get("source_commit",""))[:12]}</code></p>
      </div>
      <h3>6-Skill DAG</h3>
      <div class="skill-grid">{skill_grid}</div>
      <h3>Topology</h3>
      <ul>
        <li>Policy Gateway: {_esc(topo.get("policy_gateway",""))}</li>
        <li>GitHub upstream: {_esc(topo.get("github_upstream",""))}</li>
        <li>Case retrieval: {_esc(topo.get("case_retrieval",""))}</li>
        <li>PR lifecycle: {_esc(topo.get("pr_lifecycle",""))}</li>
      </ul>
      <div class="boundary-banner">
        <strong>Honest boundaries:</strong>
        <code>hiclab_live={_esc(topo.get("hiclab_live",False))}</code> ·
        <code>runtime_consumes_rag_context=false</code>
      </div>
    </section>'''


def render_timeline(bundle: dict) -> str:
    spans = sorted(bundle.get("spans", []), key=lambda s: s.get("start_time", 0))
    if not spans:
        return '<section id="timeline" class="page"><h2>Workflow Timeline</h2><p>No span data available.</p></section>'
    max_dur = max((s.get("duration_ms", 1) for s in spans), default=1)
    bars = ""
    for sp in spans:
        dur = sp.get("duration_ms", 0)
        width = max(5, int(dur / max_dur * 100)) if max_dur > 0 else 5
        bars += f'''<div class="timeline-bar">
          <span class="timeline-label">{_esc(sp.get("name",""))}</span>
          <div class="timeline-bar-fill timeline-status-{sp.get('status','unset').lower()}"
               style="width:{width}%">{dur}ms</div>
        </div>'''
    return f'''
    <section id="timeline" class="page">
      <h2>Workflow Timeline</h2>
      <p class="hint">Span waterfall ordered by start_time. Durations from OTel SpanRecord.</p>
      <div class="timeline">{bars}</div>
    </section>'''


def render_findings(bundle: dict) -> str:
    findings = bundle.get("findings", [])
    fixes = bundle.get("fixes", [])
    vr = bundle.get("verifier_result", {})
    if not findings:
        findings_html = '<p class="hint">No inline findings in this evidence set. Skill outputs are stored as response digests in the full-chain E2E evidence.</p>'
    else:
        rows = "".join(
            f'<tr><td>{_esc(f.get("finding_id"))}</td><td>{_esc(f.get("category"))}</td>'
            f'<td>{_esc(f.get("severity"))}</td><td>{_esc(f.get("file"))}:{_esc(f.get("line"))}</td>'
            f'<td>{_esc(f.get("message"))}</td></tr>'
            for f in findings
        )
        findings_html = f'<table class="data-table"><tr><th>ID</th><th>Category</th><th>Severity</th><th>Location</th><th>Message</th></tr>{rows}</table>'
    if not fixes:
        fixes_html = '<p class="hint">No inline fixes in this evidence set.</p>'
    else:
        frows = "".join(
            f'<tr><td>{_esc(fx.get("fix_id"))}</td><td>{_esc(fx.get("finding_id"))}</td>'
            f'<td>{_esc(fx.get("file"))}</td><td>{_esc(fx.get("description"))}</td>'
            f'<td>PR created: {_esc(fx.get("pr_created"))}</td></tr>'
            for fx in fixes
        )
        fixes_html = f'<table class="data-table"><tr><th>Fix ID</th><th>Finding</th><th>File</th><th>Description</th><th>PR</th></tr>{frows}</table>'
    return f'''
    <section id="findings" class="page">
      <h2>Findings &amp; Fixes</h2>
      <h3>Verifier Result</h3>
      <p>Verdict: {_status_badge(vr.get("verdict","UNKNOWN"))}</p>
      <h3>Findings ({len(findings)})</h3>
      {findings_html}
      <h3>Fixes ({len(fixes)})</h3>
      {fixes_html}
    </section>'''


def render_rag(bundle: dict) -> str:
    rags = bundle.get("rag_advisories", [])
    cards = ""
    for r in rags:
        cases = r.get("cases", [])
        case_html = ""
        for c in cases:
            case_html += f'<li>{_esc(c.get("case_id",""))} (sim={_esc(c.get("similarity",""))}) — <a href="{_esc(c.get("citation_url",""))}">{_esc(c.get("citation_url",""))}</a></li>'
        cards += f'''<div class="rag-card">
          <h4>{_esc(r.get("agent_role",""))}</h4>
          <p>Status: {_esc(r.get("status",""))} · Hits: {_esc(r.get("hit_count",0))}</p>
          <p><code>adopted={_esc(r.get("adopted"))}</code> · <code>untrusted={_esc(r.get("untrusted"))}</code></p>
          {f"<ul>{case_html}</ul>" if case_html else "<p>No cases retrieved.</p>"}
        </div>'''
    return f'''
    <section id="rag" class="page">
      <h2>RAG Advisory</h2>
      <div class="boundary-banner warning">
        <strong>Runtime boundary:</strong>
        <code>runtime_consumes_rag_context=false</code> — RAG results are NOT consumed by
        core.scan/core.run decision logic. RAG is advisory evidence only.<br>
        <code>workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME</code>
      </div>
      {cards if cards else "<p>No RAG advisory data.</p>"}
    </section>'''


def render_trace(bundle: dict) -> str:
    spans = bundle.get("spans", [])
    if not spans:
        return '<section id="trace" class="page"><h2>OTel Trace Tree</h2><p>No span data.</p></section>'
    tree = build_span_tree(spans)
    tree_html = ""
    for root in tree["children"]:
        tree_html += render_span_tree_node(root, 0)
    return f'''
    <section id="trace" class="page">
      <h2>OTel Trace Tree</h2>
      <p class="hint">Hierarchical span tree built from parent_span_id. Click nodes to expand attributes.</p>
      <div class="trace-tree">{tree_html}</div>
    </section>'''


def render_safety(bundle: dict) -> str:
    residue = bundle.get("residue", {})
    return f'''
    <section id="safety" class="page">
      <h2>Policy &amp; Safety</h2>
      <h3>Residue</h3>
      <ul>
        <li>Containers: {_esc(residue.get("containers","N/A"))}</li>
        <li>Networks: {_esc(residue.get("networks","N/A"))}</li>
        <li>Temp dirs: {_esc(residue.get("temp_dirs","N/A"))}</li>
      </ul>
      <h3>Secret Scan</h3>
      <p>secret_leaks: <strong>{_esc(bundle.get("secret_leaks","N/A"))}</strong></p>
      <h3>Rollback Events</h3>
      <p>{len(bundle.get("rollback_events",[]))} rollback event(s).</p>
    </section>'''


def render_evidence(bundle: dict) -> str:
    ev_files = bundle.get("evidence_files", [])
    rows = "".join(
        f'<tr><td>{_esc(ef.get("path"))}</td><td><code>{_esc(ef.get("sha256",""))[:24]}...</code></td><td>{_esc(ef.get("description"))}</td></tr>'
        for ef in ev_files
    )
    return f'''
    <section id="evidence" class="page">
      <h2>Evidence &amp; Provenance</h2>
      <p><strong>Bundle SHA-256:</strong> <code>{_esc(bundle.get("bundle_sha256",""))}</code></p>
      <p><strong>Source commit:</strong> <code>{_esc(bundle.get("source_commit",""))}</code></p>
      <p><strong>Verification commit:</strong> <code>{_esc(bundle.get("verification_commit",""))}</code></p>
      <table class="data-table">
        <tr><th>Path</th><th>SHA-256</th><th>Description</th></tr>
        {rows}
      </table>
    </section>'''


def render_benchmark(bundle: dict) -> str:
    bs = bundle.get("benchmark_summary", {})
    dev = bs.get("development_calibration", {})
    rm = bs.get("retrieval_metrics", {})
    cohorts = bs.get("cohorts", {})
    return f'''
    <section id="benchmark" class="page">
      <h2>Benchmark Summary</h2>
      <div class="boundary-banner warning">
        <strong>Benchmark boundary:</strong> Uses deterministic offline TokenOverlapAdapter (token-Jaccard),
        NOT real pgvector embeddings. Does NOT claim Reviewer/Fixer accuracy improvement.<br>
        <code>workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME</code> ·
        Retrieval/integration quality ≠ workflow utility.
      </div>
      <h3>Confirmatory Held-out</h3>
      <ul>
        <li>Dataset: {_esc(bs.get("dataset_version",""))}</li>
        <li>Unique cases: {_esc(bs.get("unique_case_count",0))}</li>
        <li>Cohorts: {_esc(cohorts)}</li>
        <li>hit@1: {_esc(rm.get("hit_at_1","N/A"))}</li>
        <li>hit@3: {_esc(rm.get("hit_at_3","N/A"))}</li>
        <li>MRR: {_esc(rm.get("mean_reciprocal_rank","N/A"))}</li>
        <li>quality_gate_pass: <strong>{_esc(bs.get("quality_gate_pass"))}</strong></li>
        <li>confirmatory_all_ok: <strong>{_esc(bs.get("confirmatory_all_ok"))}</strong></li>
      </ul>
      <h3>Development Calibration (not confirmatory)</h3>
      <ul>
        <li>Dataset: {_esc(dev.get("dataset_version",""))}</li>
        <li>Unique cases: {_esc(dev.get("unique_case_count",0))}</li>
        <li>quality_gate_pass: <strong>{_esc(dev.get("quality_gate_pass"))}</strong> (null = not confirmatory)</li>
      </ul>
    </section>'''


# ── Full page assembly ─────────────────────────────────────────────────────

def render_html(bundle: dict) -> str:
    mode = bundle.get("demo_mode", "REPLAY")
    pages = [
        ("overview", "Overview", render_overview(bundle)),
        ("timeline", "Timeline", render_timeline(bundle)),
        ("findings", "Findings", render_findings(bundle)),
        ("rag", "RAG Advisory", render_rag(bundle)),
        ("trace", "Trace Tree", render_trace(bundle)),
        ("safety", "Policy & Safety", render_safety(bundle)),
        ("evidence", "Evidence", render_evidence(bundle)),
        ("benchmark", "Benchmark", render_benchmark(bundle)),
    ]

    nav = "".join(
        f'<button class="nav-btn" onclick="showPage(\'{pid}\')">{label}</button>'
        for pid, label, _ in pages
    )
    page_html = "\n".join(html for _, _, html in pages)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MergePilot · Demo Console ({mode})</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }}
.mode-banner {{ color: #fff; padding: 6px 20px; font-weight: bold; font-size: 14px; letter-spacing: 1px; }}
.header {{ background: #1e293b; padding: 12px 20px; display: flex; align-items: center; gap: 16px; }}
.header h1 {{ font-size: 18px; }}
.header .commit {{ font-size: 12px; color: #94a3b8; }}
nav {{ background: #334155; padding: 8px 20px; display: flex; gap: 4px; flex-wrap: wrap; }}
.nav-btn {{ background: #475569; color: #e2e8f0; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-size: 13px; }}
.nav-btn:hover {{ background: #64748b; }}
.nav-btn.active {{ background: #2563eb; }}
.content {{ padding: 20px; max-width: 1200px; margin: 0 auto; }}
.page {{ display: none; }}
.page.active {{ display: block; }}
h2 {{ margin-bottom: 12px; color: #f1f5f9; border-bottom: 1px solid #334155; padding-bottom: 8px; }}
h3 {{ margin: 16px 0 8px; color: #cbd5e1; }}
.meta p {{ margin: 4px 0; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; color: #fff; font-size: 12px; font-weight: bold; }}
.final-status {{ font-size: 20px; margin: 12px 0; }}
.skill-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; margin: 12px 0; }}
.skill-card {{ background: #1e293b; padding: 12px; border-radius: 6px; text-align: center; }}
.skill-role {{ color: #94a3b8; font-size: 12px; }}
.data-table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
.data-table th, .data-table td {{ padding: 8px; border: 1px solid #334155; text-align: left; font-size: 13px; }}
.data-table th {{ background: #1e293b; }}
.boundary-banner {{ background: #7f1d1d33; border: 1px solid #7f1d1d; padding: 12px; border-radius: 6px; margin: 12px 0; }}
.boundary-banner.warning {{ background: #78350f33; border-color: #78350f; }}
.timeline {{ margin: 12px 0; }}
.timeline-bar {{ display: flex; align-items: center; margin: 4px 0; }}
.timeline-label {{ width: 250px; font-size: 12px; color: #cbd5e1; }}
.timeline-bar-fill {{ height: 20px; border-radius: 3px; display: flex; align-items: center; padding: 0 8px; font-size: 11px; color: #fff; min-width: 30px; }}
.timeline-status-ok {{ background: #16a34a; }}
.timeline-status-error {{ background: #dc2626; }}
.timeline-status-unset {{ background: #6b7280; }}
.trace-tree {{ font-family: monospace; font-size: 13px; }}
.span-node {{ padding: 4px 0; }}
.span-status-ok {{ color: #16a34a; }}
.span-status-error {{ color: #dc2626; }}
.span-status-unset {{ color: #6b7280; }}
.span-duration {{ color: #94a3b8; margin-left: 8px; font-size: 11px; }}
.span-status-text {{ color: #64748b; margin-left: 4px; font-size: 11px; }}
.span-attrs {{ margin: 4px 0 4px 20px; color: #94a3b8; font-size: 11px; }}
.span-attrs li {{ list-style: none; }}
.rag-card {{ background: #1e293b; padding: 12px; border-radius: 6px; margin: 8px 0; }}
.hint {{ color: #94a3b8; font-size: 13px; font-style: italic; }}
code {{ background: #0f172a; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
a {{ color: #3b82f6; }}
</style>
</head>
<body>
{_mode_banner(mode)}
<div class="header">
  <h1>MergePilot · Demo Console</h1>
  <span class="commit">source: <code>{_esc(bundle.get("source_commit",""))[:12]}</code></span>
</div>
<nav>{nav}</nav>
<div class="content">
{page_html}
</div>
<script>
function showPage(id) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
document.querySelector('.nav-btn').classList.add('active');
</script>
</body>
</html>'''


def render_to_file(bundle_path: str, output_path: str):
    """Render a DemoBundle JSON to a self-contained HTML file."""
    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    html = render_html(bundle)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    return output_path


def main():
    root = Path(__file__).resolve().parent.parent.parent
    bundle_path = root / "samples/demo-bundles/m7-rag-replay.json"
    output_path = root / "samples/demo-console/index.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_to_file(str(bundle_path), str(output_path))
    print(f"rendered to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
