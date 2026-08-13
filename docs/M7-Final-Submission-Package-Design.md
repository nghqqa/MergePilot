# M7 Final Submission Package Design

**Status**: Design (Demo video DEFERRED); these 3 docs pending PR merge
**Base**: `0bc2e69` (origin/main, M7-P4 evidence merged)
**Created**: 2026-08-13

> **Note**: M7-P2/P3/P4 technical evidence is already in origin/main.
> These three Final Submission documents themselves are NOT yet in main
> until this PR merges. Do not claim "all final materials are in main"
> until merge is complete.

## 1. Current State Summary

| Milestone | Status | Evidence |
|-----------|--------|----------|
| M7-P2 RAG Benchmark | ✅ Closed (`m7-p2-rag-benchmark-closed`) | Confirmatory 16/16 gates, held-out N=25 |
| M7-P3 Demo Console REPLAY | ✅ Closed (`m7-p3-demo-console-closed`) | 33 tests, 8 pages, Bundle SHA verified |
| M7-P3 Showcase Package | ✅ Merged (PR #153) | Runbook, Portfolio, Claim Matrix, Checklist |
| M7-P4 Clean Reproduction | ✅ Merged (PR #155) | `all_ok=true`, Windows Python 3.9.25 |
| Demo Video | ⏸ **DEFERRED** | Not recorded; not required for current technical gate |
| M7 Overall Close | ❌ Not created | Awaiting video decision |

## 2. Existing Materials Audit

### Already present in origin/main

| Category | Items | Status |
|----------|-------|--------|
| Evidence | M7-P2 confirmatory + offline benchmark, M6-RAG pgvector, M7-P4 clean reproduction | ✅ All SHA verified |
| Demo Console | HTML (19KB), Bundle JSON, schema/builder/render/serve, 33 tests | ✅ |
| Design docs | M7-P3 Console Design, M7-P4 Clean Repro Design/Runbook/Schema/Matrix | ✅ |
| Showcase | Runbook (346 lines), Portfolio (269 lines), Claim Matrix (98 lines), Rehearsal Checklist (238 lines) | ✅ |
| README | Updated with M6-RAG + M7-P2 boundaries | ✅ |
| Project status | docs/项目状态.md §九 | ✅ |
| Roadmap | docs/复赛路线图.md M7 section | ✅ |
| LICENSE | Apache-2.0 | ✅ |
| Launch commands | 1-line `python -m http.server --bind 127.0.0.1` documented | ✅ |

### Gaps identified

| Gap | Severity | Action |
|-----|----------|--------|
| `.env.example` | Low | Demo Console needs no env vars (stdlib only). Create minimal stub. |
| `NOTICE` | Low | Create Apache-2.0 NOTICE file. |
| `THIRD_PARTY` | Low | Demo Console has zero third-party deps. Create minimal file. |
| Demo video | **DEFERRED** | Not a technical gate blocker. Status: `DEFERRED_NOT_REQUIRED_FOR_CURRENT_TECHNICAL_GATE`. |

## 3. Demo Video Status

```
demo_video_status = "DEFERRED_NOT_REQUIRED_FOR_CURRENT_TECHNICAL_GATE"
```

### Why video is deferred

- The competition's technical evaluation focuses on reproducible evidence and
  code quality, not video production.
- Demo Console REPLAY can be demonstrated live by opening `index.html` —
  no video needed for live presentation.
- A recorded video is a **presentation enhancement**, not a technical gate.
- The Showcase Runbook (5-8 min script) is ready; video can be recorded
  at any time using the existing REPLAY Console.

### Impact on M7 closure

- **Does NOT block M7 technical closure.** All technical evidence is complete
  and verified (M7-P2/P3/P4).
- Video recording can be done as a standalone post-close activity.
- Recommend: `M7_DEMO_VIDEO_DEFERRED` status for project tracking.

## 4. Required Honest Boundaries (all already documented)

All boundaries are already present in README, Claim Matrix, and evidence:

- Demo Console: REPLAY, read-only, evidence-driven (NOT production dashboard)
- `adopted=false`, `untrusted=true`, `runtime_consumes_rag_context=false`
- `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME`
- Findings/Fixes=0 (evidence stores digests, not inline)
- ISOLATED_LIVE not implemented
- M6-C real cloud SLS not completed
- Clean reproduction requires Windows `core.autocrlf=false` + `core.eol=lf`
- `browser_network_observation_status=NOT_MEASURED`

## 5. Remaining Actions

| # | Action | Priority | Blocks M7? |
|---|--------|----------|------------|
| 1 | Create `.env.example` (minimal stub) | Low | No |
| 2 | Create `NOTICE` (Apache-2.0) | Low | No |
| 3 | Create `THIRD_PARTY` (zero third-party Python packages; Git CLI required) | Low | No |
| 4 | Demo video recording | Deferred | No |
| 5 | M7 overall close tag | After above | — |

**None of items 1-3 block M7 technical closure.** They are packaging
 niceties for formal submission.
