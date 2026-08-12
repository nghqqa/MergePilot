# M7-P3 Demo Console — Design Freeze

**Status**: Frozen (design-only; no implementation this round)
**Milestone**: M7-P3
**Base**: `a2238a5` (origin/main, M7-P2 merged)
**Created**: 2026-08-12

## 1. Objective

Design a local, read-only, evidence-driven, offline-replayable Demo Console
for final competition presentation. The Console reads from a unified
`DemoBundle` data model — all pages are generated from the bundle, never
hardcoded.

## 2. What the Demo Console IS and IS NOT

### IS

- A **read-only viewer** for MergePilot workflow evidence.
- A **replay tool** — loads pre-generated DemoBundle JSON, renders pages.
- An **optional isolated-live viewer** — connects to MergePilot-Test for
  real-time observation (read-only, no writes).
- **Evidence-driven** — every displayed number, finding, and trace links back
  to a source evidence file with SHA-256 provenance.

### IS NOT

- ❌ A production management dashboard.
- ❌ An admin panel with login / authentication / RBAC.
- ❌ An Agent control surface (no start/stop/configure Agents).
- ❌ A configuration editor.
- ❌ A GitHub write surface (no PR creation, no merge button, no comment posting).
- ❌ A real-time production monitor (the default mode is offline replay).

## 3. Modes

| Mode | Data source | Network | LLM | Use case |
|------|------------|---------|-----|----------|
| **REPLAY** (default) | Pre-generated DemoBundle JSON | None | None | Competition demo, offline presentation |
| **ISOLATED_LIVE** | MergePilot-Test WSL real-time | localhost only (WSL internal) | None (fixture LLM) | Optional live demonstration |
| **HISTORICAL** | Frozen evidence from git tags | None | None | Browsing past milestone evidence |

Mode is set at startup via `--mode replay|isolated_live|historical` and is
displayed as a persistent banner on every page. The mode CANNOT be changed
at runtime — a restart is required (prevents mode confusion).

## 4. DemoBundle Schema

The DemoBundle is the single source of truth for all Console pages. It is a
JSON document with the following top-level structure:

```json
{
  "schema_version": "mergepilot.demo-bundle.v1",
  "demo_mode": "REPLAY",
  "bundle_sha256": "<SHA-256 of canonical JSON>",
  "generated_at": "2026-08-12T14:00:00Z",
  "source_commit": "<git commit that generated this bundle>",
  "verification_commit": "<git commit at generation time>",

  "repo": "test/repo-alpha",
  "pr": {"number": 42, "title": "Add user authentication", "base_sha": "...", "head_sha": "..."},
  "run": {"run_id": "run-demo-001", "trace_id": "abc123...", "entrypoint": "controller.process_event"},
  "final_status": "MERGED",

  "workflow_stages": [
    {
      "stage": "review",
      "agent_role": "reviewer",
      "status": "COMPLETED",
      "verdict": "HOLD",
      "started_at": "...",
 "completed_at": "...",
      "duration_ms": 1234,
      "skill_name": "sast-scan",
      "skill_version": "1",
      "invocation_id": "inv-...",
      "output_schema_validated": true,
      "depends_on": []
    }
  ],

  "agents": [
    {"role": "reviewer", "skill": "sast-scan", "status": "SUCCEEDED", "verdict": null},
    {"role": "fixer", "skill": "pr-lifecycle", "status": "SUCCEEDED", "verdict": null, "outcome": "CREATED"},
    {"role": "verifier", "skill": "test-runner", "status": "SUCCEEDED", "verdict": "PASS"}
  ],

  "findings": [
    {
      "finding_id": "F-001",
      "category": "sql_injection",
      "severity": "high",
      "file": "src/db.py",
      "line": 42,
      "message": "SQL injection via string concatenation",
      "remediation": "Use parameterized queries",
      "engine": "inline-sast",
      "rule_id": "SQLI-001"
    }
  ],

  "fixes": [
    {
      "fix_id": "FX-001",
      "finding_id": "F-001",
      "file": "src/db.py",
      "description": "Replaced string concatenation with parameterized query",
      "pr_created": true,
      "pr_url": "https://github.com/test/repo-alpha/pull/43"
    }
  ],

  "verifier_result": {
    "verdict": "PASS",
    "tests_run": 12,
    "tests_passed": 12,
    "tests_failed": 0,
    "duration_ms": 5678
  },

  "rag_advisories": [
    {
      "agent_role": "reviewer",
      "status": "ok",
      "hit_count": 3,
      "fallback_reason": "",
      "adopted": false,
      "untrusted": true,
      "cases": [
        {"case_id": "kb-001", "similarity": 0.92, "category": "sql_injection",
         "citation_url": "https://github.com/test/repo-alpha/pull/1"}
      ]
    }
  ],

  "spans": [
    {
      "trace_id": "...",
      "span_id": "...",
      "parent_span_id": null,
      "name": "controller.process_event",
      "status": "OK",
      "start_time": 1234567890.0,
      "end_time": 1234567891.0,
      "duration_ms": 1000,
      "attributes": {"mp.run_id": "...", "mp.agent_role": "coordinator"}
    }
  ],

  "rollback_events": [],

  "evidence_files": [
    {"path": "evidence/m4/m4f/agentteams-e2e.json", "sha256": "...", "description": "AgentTeams E2E"},
    {"path": "evidence/m6/rag/pgvector-isolated-verification.json", "sha256": "...", "description": "RAG pgvector verification"},
    {"path": "evidence/m7/benchmark/rag-n20-confirmatory.json", "sha256": "...", "description": "RAG confirmatory benchmark"}
  ],

  "secret_leaks": 0,
  "residue": {"containers": 0, "networks": 0, "temp_dirs": 0},

  "benchmark_summary": {
    "dataset_version": "rag-bench-v3-heldout",
    "unique_case_count": 25,
    "cohorts": {"positive_retrieval": 15, "abstention": 5, "fault_injection": 5},
    "retrieval_metrics": {
      "hit_at_1": 1.0,
      "hit_at_3": 1.0,
      "mean_reciprocal_rank": 1.0
    },
    "quality_gate_pass": true,
    "confirmatory_all_ok": true,
    "runtime_consumes_rag_context": false,
    "workflow_utility_status": "NOT_MEASURABLE_WITH_CURRENT_RUNTIME",
    "benchmark_phase": "CONFIRMATORY_HELDOUT"
  },

  "topology": {
    "policy_gateway": "real gateway.py over SSE",
    "github_upstream": "stateful fake GitHub MCP (protocol-real SSE)",
    "case_retrieval": "real pgvector adapter (deterministic embedding)",
    "pr_lifecycle": "real Policy Gateway as fixer",
    "hiclaw_live": false
  }
}
```

### DemoBundle generation rules

1. **REPLAY mode**: Bundle is pre-generated by a `bundle_builder.py` script
   that reads existing evidence files and assembles the bundle offline.
2. **ISOLATED_LIVE mode**: Bundle is assembled in real-time by polling
   MergePilot-Test audit DB (read-only SELECT queries only).
3. **HISTORICAL mode**: Bundle is loaded from a git-tagged frozen bundle file.
4. All SHA-256 values in `evidence_files` are computed at bundle build time
   and verified at render time — mismatch = render error.
5. `bundle_sha256` covers the canonical JSON (sorted keys, UTF-8) excluding
   itself.

## 5. Page Information Architecture

### 5.1 Overview

- Final status badge (MERGED / HELD / REJECTED / ROLLED_BACK)
- Run/trace/pr identification
- Mode banner (REPLAY / ISOLATED_LIVE / HISTORICAL)
- 6-skill DAG status grid (reviewer→fixer→verifier cycle)
- Topology summary (real Gateway, fake GitHub, real pgvector)
- Honest boundary callouts:
  - `hiclab_live=false`
  - `runtime_consumes_rag_context=false`

### 5.2 Workflow Timeline

- Waterfall/Gantt view of spans ordered by `start_time`
- Parent-child span tree (using `parent_span_id`)
- Per-span: name, status (OK/ERROR), duration_ms, key attributes
- Stage markers: review → fix → verify → (rollback if any)
- Click span → expand attributes (auto-redacted by OTel SpanRecord)

### 5.3 Findings & Fixes

- Findings table: finding_id, category, severity, file:line, message, remediation
- Fixes table: fix_id, finding_id (linked), file, description, PR created
- Severity color coding (critical=red, high=orange, medium=yellow, low=gray)
- All data sourced from `findings[]` and `fixes[]` arrays — no hardcoded entries

### 5.4 RAG Advisory

- Per-agent RAG advisory cards (reviewer, fixer)
- Each shows: `status`, `hit_count`, `fallback_reason`
- **Prominent labels**: `adopted=false`, `untrusted=true`
- Case list with similarity scores and citation URLs
- **Boundary banner**: `runtime_consumes_rag_context=false` — RAG does NOT
  influence Reviewer/Fixer decisions; it is advisory evidence only
- `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME`

### 5.5 OTel Trace Tree

- Hierarchical tree visualization of all spans
- Root: `controller.process_event` or `matrix.ingress`
- Children: `policy_gateway.*`, `snapshot.*`, `skill.*`, `mcp.*`
- Expandable nodes showing full attribute set (redacted)
- Duration bars proportional to `duration_ms`
- Status colors: OK=green, ERROR=red, UNSET=gray

### 5.6 Policy & Safety

- Policy Gateway audit summary: bound revisions, events, successful results
- Permission matrix display (which roles can call which tools)
- Deny/timeout/fallback events
- Rollback events (if any) with trigger reason
- Secret scan results: `secret_leaks=0`
- Residue: `containers=0, networks=0, temp_dirs=0`

### 5.7 Evidence & Provenance

- Table of all evidence files referenced in the bundle
- Each row: path, SHA-256, description, source commit
- Click → view raw JSON (read-only)
- Delivery digest display
- Git tag → evidence mapping (m6-rag-pgvector-closed, m7-p2-rag-benchmark-closed, etc.)

### 5.8 Benchmark Summary

- RAG retrieval metrics: hit@1, hit@3, MRR, category/severity match
- Cohort distribution (positive/abstention/fault)
- Quality gate: 16/16 pre-registered thresholds
- **Boundary banner**: deterministic offline TokenOverlapAdapter, NOT real
  pgvector embeddings; does NOT claim Reviewer/Fixer accuracy improvement
- `confirmatory_all_ok=true`
- `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME`

## 6. Replay / Isolated Live State Machine

```
                    ┌──────────────┐
                    │   STARTUP    │
                    └──────┬───────┘
                           │
                    --mode flag
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │  REPLAY  │ │ ISOLATED │ │HISTORICAL│
        │          │ │  _LIVE   │ │          │
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │            │            │
     load bundle    poll audit DB   load frozen
     from JSON      (read-only)     bundle from
     file           every 2s        git tag
             │            │            │
             ▼            ▼            ▼
        ┌─────────────────────────────────┐
        │         RENDER PAGES            │
        │  (from DemoBundle, read-only)   │
        └────────────┬────────────────────┘
                     │
              ┌──────┴──────┐
              │  SHUTDOWN   │
              └─────────────┘
```

**Transitions are one-way**: mode cannot change at runtime. A restart is
required to switch modes. This prevents accidental mixing of replay data
with live data.

**ISOLATED_LIVE safety constraints**:
- Only connects to MergePilot-Test WSL (localhost, no external network)
- Read-only SQL queries (`SELECT` only, enforced by `case_retrieval_reader`
  role with `default_transaction_read_only=on`)
- No GitHub writes, no Agent control, no config changes
- All displayed data is still traceable to evidence provenance
- Ubuntu-22.04 production WSL remains Stopped

## 7. 5–8 Minute Demo Script

### Act 1: Setup & Overview (1 min)

1. Open Demo Console in REPLAY mode with the M4-F competition demo bundle.
2. Show the Overview page: 6-skill DAG, final status MERGED, mode banner.
3. Point out `hiclab_live=false` and `runtime_consumes_rag_context=false` labels.

### Act 2: Workflow & Findings (2 min)

4. Navigate to Workflow Timeline — show the waterfall of spans from
   controller ingress through all 6 skills to PR creation.
5. Navigate to Findings & Fixes — show the SAST findings (SQL injection,
   hardcoded secret) and the corresponding fixes applied by the Fixer.
6. Highlight that the Fixer created a PR via the Policy Gateway (real
   gateway.py, not a mock).

### Act 3: RAG Advisory & Honest Boundaries (1.5 min)

7. Navigate to RAG Advisory — show the retrieved cases with similarity
   scores and citations.
8. **Emphasize**: `adopted=false`, `untrusted=true` — RAG is advisory only.
9. State clearly: "RAG results are NOT consumed by the Reviewer or Fixer
   decision logic. The workflow utility is not measurable with the current
   runtime."

### Act 4: Trace & Safety (1.5 min)

10. Navigate to OTel Trace Tree — show the parent-child span hierarchy,
    attribute redaction, and duration bars.
11. Navigate to Policy & Safety — show permission matrix, deny events,
    zero secret leaks, zero residue.

### Act 5: Evidence & Benchmark (1 min)

12. Navigate to Evidence & Provenance — show the evidence file table with
    SHA-256 provenance. Click one to view raw JSON.
13. Navigate to Benchmark Summary — show 16/16 quality gates, cohort
    distribution. Emphasize deterministic offline adapter boundary.
14. Close: "All claims link to verifiable evidence. Nothing is hardcoded."

## 8. Implementation Phased Plan

### Phase 1: DemoBundle Builder (REPLAY)

- Implement `tools/demo_console/bundle_builder.py`
- Reads existing evidence JSON files (M4-F demo summary, OTel spans, RAG
  advisory, benchmark summary)
- Assembles DemoBundle JSON with correct schema
- Computes `bundle_sha256` and all `evidence_files[].sha256`
- Output: `samples/demo-bundles/m4f-competition.json`

### Phase 2: Static Page Renderer

- Implement `tools/demo_console/render.py`
- Reads DemoBundle JSON, renders 8 pages as static HTML
- Self-contained: inline CSS, no external dependencies, no JS framework
- Vanilla JS for interactivity (span tree expand/collapse, tab switching)
- Output: `samples/demo-console/index.html` + page HTML files

### Phase 3: ISOLATED_LIVE Mode (optional)

- Implement `tools/demo_console/live_poller.py`
- Read-only SELECT queries against MergePilot-Test audit DB
- Assembles DemoBundle in real-time (same schema)
- 2-second polling interval
- Launches read-only HTTP server on localhost

### Phase 4: HISTORICAL Mode

- Freeze DemoBundle JSONs at milestone tags
- Load from `evidence/<milestone>/demo-bundle.json`

### Phase 5: Polish & Demo Prep

- Visual polish, responsive layout
- Demo bundle selection UI
- Record demo video (positive PASS/MERGE + negative HOLD/ROLLBACK)

## 9. Test & Authenticity Gates

### Authenticity gates (enforced at build time)

| Gate | Rule |
|------|------|
| `no_hardcoded_results` | All page data must trace to DemoBundle fields |
| `evidence_sha_verified` | Every `evidence_files[].sha256` must match actual file |
| `bundle_sha_verified` | `bundle_sha256` must match canonical JSON |
| `mode_banner_displayed` | Every page must show mode banner |
| `rag_boundary_displayed` | RAG page must show `adopted=false`, `untrusted=true`, `runtime_consumes_rag_context=false` |
| `benchmark_boundary_displayed` | Benchmark page must show workflow_utility_status and adapter type |
| `no_production_claims` | No page may claim production performance or live HiClaw |
| `secret_scan` | `scan_secrets(bundle_json)` must return 0 |

### Test suite

- `test_bundle_schema.py`: DemoBundle schema validation (all required fields present)
- `test_bundle_builder.py`: Builder produces valid bundle from evidence files
- `test_render.py`: All 8 pages render without error from valid bundle
- `test_authenticity.py`: All authenticity gates pass
- `test_replay_mode.py`: REPLAY mode loads and renders correctly
- `test_mode_isolation.py`: Mode cannot change at runtime

## 10. Management Platform — Deferred to Post-Competition

The Demo Console is explicitly **NOT** a management platform. The following
capabilities are deferred to a post-competition "MergePilot Admin" project:

- User authentication / RBAC
- Agent lifecycle management (start/stop/configure)
- Configuration editing
- GitHub write operations (PR creation, merge, comment)
- Real-time production monitoring
- Multi-tenant dashboard
- Alerting / notification system

**Rationale**: The competition requires demonstrating capability and
evidence integrity, not a production-ready admin UI. A read-only evidence
console is sufficient for the demo and avoids over-claiming production
readiness.

## 11. Technology Choice

- **Backend**: Python 3.10+ (consistent with existing codebase)
- **Frontend**: Static HTML + vanilla JS + inline CSS (no build step, no
  npm dependencies, no framework — consistent with existing `make_dashboard.py`)
- **Visualization**: Custom CSS/flexbox for timeline/tree (no D3/Chart.js
  runtime dependency; keeps it self-contained and offline-capable)
- **Server (ISOLATED_LIVE only)**: Python `http.server` on localhost
- **Bundle format**: JSON (canonical, SHA-256 verifiable)

## 12. Prohibited in This Design

- ❌ No login / authentication / session management
- ❌ No write operations of any kind
- ❌ No external network calls (except localhost in ISOLATED_LIVE)
- ❌ No LLM API calls
- ❌ No real GitHub / production HiClaw / production DB / real SLS
- ❌ No claiming production performance or multi-repo stability
- ❌ No hardcoded demo results (all from DemoBundle)
- ❌ No claiming RAG improves decision accuracy

## 13. Existing Assets to Reuse

| Asset | Path | Reuse |
|-------|------|-------|
| OTel SpanRecord | `tools/otel/otel_spans.py` | Span data model for trace tree |
| Demo summary | `evidence/m4/m4f/agentteams-demo-summary.json` | Source for DemoBundle |
| Full-chain E2E | `evidence/m4/m4f/full-chain-e2e.json` | Observation stream for timeline |
| RAG evidence | `evidence/m6/rag/pgvector-isolated-verification.json` | RAG advisory page data |
| Benchmark evidence | `evidence/m7/benchmark/rag-n20-confirmatory.json` | Benchmark summary page |
| Dashboard generator | `tools/make_dashboard.py` | CSS theme, layout patterns |
| Trace aggregator | `tools/trace_aggregator.py` | Span tree construction logic |
| Evidence index | `docs/初赛证据索引.md` | Evidence & Provenance page links |
