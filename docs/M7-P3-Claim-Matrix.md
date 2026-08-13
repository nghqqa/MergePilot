# M7-P3 Claim Matrix

**Status**: Authoritative — the single source of truth for what may and may not
be said about MergePilot (competition demo, resume, interviews, README).
**Milestone**: M7-P3
**Rule**: If a claim is not in this matrix with status `CAN_CLAIM` or
`OFFLINE_VERIFIED`, **do not say it**. When in doubt, state the boundary.

---

## Status legend

| Status | Meaning |
|--------|---------|
| `CAN_CLAIM` | Implemented and verified with evidence + git tag. May be stated plainly. |
| `OFFLINE_VERIFIED` | Verified only in an offline benchmark or isolated test stack. Must state the boundary when claiming. |
| `CANNOT_CLAIM` | Not yet true. Do not state as a capability. |
| `FUTURE` | Planned / designed but not implemented. State as "planned" or "designed," never as done. |

---

## Category A — Can claim (verified capabilities)

| # | Claim | Status | Evidence | Boundary (what it does NOT mean) |
|---|-------|--------|----------|----------------------------------|
| A1 | Deterministic PostgreSQL + Outbox control plane owns task state, stage transitions, de-dup, timeout, recovery | CAN_CLAIM | B4e 43/43 (`m3b-b4e-closed`), B5 50/50 (`m3b-b5-closed`), M3-C 33/33 (`m3c-closed`); `evidence/m3b-b4e/`, `evidence/m3b-b5/`, `evidence/m3c/` | Recovery state machine is real; rollback is currently **script-triggered**, not a verify-fail automatic branch (M3-C child-run rollback covers `POST_MERGE_VERIFY_FAILED` entry only). |
| A2 | Minimum-privilege, fail-closed Policy Gateway with role tokens, write constraints, audit | CAN_CLAIM | 8 negative classes all fail-closed (50/50); `evidence/m3b-b5/` | Gateway is self-built Python SSE, **not** Higress-native. Not a formal secrets-management certification. |
| A3 | 6-skill deterministic DAG (diff-parse, risk-classify, sast-scan, test-runner, pr-lifecycle, case-retrieval) | CAN_CLAIM | 481 deterministic tests; `evidence/m4/m4c/`, `evidence/m4/m4e/` | Skills are deterministic subprocesses with schema + size caps, **not** autonomous LLM tool calls. New and legacy SAST paths coexist (tech debt). |
| A4 | Real-protocol AgentTeams full-chain E2E | CAN_CLAIM | 16/16 gates + 6/6 regression; 6 skills SUCCEEDED; `evidence/m4/m4f/` | `hiclaw_live=false` in this evidence (protocol fixture, not real HiClaw online). |
| A5 | Real GitHub MCP loop: review → fix → verify → merge | CAN_CLAIM | PR #1 → fix PR #3, 5/5 resolved, squash merge `0dd5831`; `docs/项目状态.md` §2, `evidence/gh-pr1-demo/` | Early PR #1/#3 Manager handoff occasionally needed human nudge. Deterministic handoff closure (14/14 + 13/13) is M5-0B **candidate/isolated stack only**. |
| A6 | Rollback execution chain: bad fix → re-scan FAIL → revert commit → re-verify PASS | CAN_CLAIM | bad-fix `43eccc3`, revert `a63bfe1`; `docs/项目状态.md` §3 | Script-triggered, not verify-fail automatic. |
| A7 | Structured audit in PostgreSQL 16 + pgvector | CAN_CLAIM | 5 tasks / 6 findings / 3 decisions / 9 audit events; `evidence/audit-db/` | Local container, **not** connected to PolarDB cloud. |
| A8 | RAG CaseRetrieval core (pgvector Docker E2E) | CAN_CLAIM | 169 tests, `all_passed=true`; `evidence/m4/m4e/`; tag `m4e-case-retrieval-closed` | Agent-callable HTTP/MCP wrapper still pending. 5 small-sample queries do not represent production accuracy. |
| A9 | HiClaw isolated C3 10-round stability | CAN_CLAIM (with boundary) | 10/10 PASS; `evidence/m5/0c/c3-10x.json`; commit `4b053ef` | MergePilot-Test **isolated test stack**, not production Ubuntu-22.04; `hiclaw_live=false` for this run. |
| A10 | D2B-1 offline regression | CAN_CLAIM (with boundary) | 17/17 + 6/6; `evidence/m5/0d/offline-regression.json` | Offline regression; does not include OTel/SLS production evidence. |
| A11 | D2B-3 fail-closed Docker socket proxy on real AgentTeams v1.2.2 production live | CAN_CLAIM | 64/64 PASS, `hiclaw_live=true`; `evidence/m5/0d/hiclaw-v122-true-live-pass.json` | Proxy deployed and verified; manager auto-create root-cause path is a separate forward item. |
| A12 | OTel instrumentation (spans, parent-child, redaction, duration) | CAN_CLAIM | `tools/otel/otel_spans.py`; `docs/M6A-OTel-Observability设计冻结.md`; Demo Console trace tree | Spans exported to **local collectors**. M6-C real cloud SLS is **not completed** (see C5). |
| A13 | Evidence-driven, SHA-verified Demo Console (REPLAY) | CAN_CLAIM | `samples/demo-console/index.html`; `tools/demo_console/`; `docs/M7-P3-Demo-Console-Design.md` | REPLAY only. ISOLATED_LIVE is **not implemented** (see C6). Not a management dashboard (see C7). |

---

## Category B — Offline / isolated verified only (state the boundary)

| # | Claim | Status | Evidence | Boundary (what it does NOT mean) |
|---|-------|--------|----------|----------------------------------|
| B1 | RAG retrieval meets pre-registered quality thresholds on held-out data | OFFLINE_VERIFIED | `evidence/m7/benchmark/rag-n20-confirmatory.json`; 25 cases, seed 99; `docs/M7-RAG-N20-Confirmatory-Benchmark-Design.md` | Uses deterministic offline `TokenOverlapAdapter`, **not** real pgvector embeddings or real LLM. N=25 confirmatory small sample. Does **not** claim Reviewer/Fixer accuracy improvement. `runtime_consumes_rag_context=false`. |
| B2 | RAG retrieval development calibration (v2) | OFFLINE_VERIFIED | `rag-bench-v2`; development results **not merged** into confirmatory verdict | Calibration set; must be reported separately from confirmatory (v3). |
| B3 | Single-Agent vs multi-Agent formal benchmark (N=10×2) | OFFLINE_VERIFIED | `benchmark/formal-summary.json`, `benchmark/raw-runs/`; 20 raw runs | Controlled local orchestration, **not** real Gateway/controller/GitHub/HiClaw E2E. N=10 small sample, single model `deepseek-v4-flash`, synthetic fixtures, one run per pair. Does **not** prove multi-Agent raises recall (recall same). Semantic case pass 5/20. Do **not** conflate with C3 10/10. |
| B4 | Topology: real gateway.py over SSE + stateful fake GitHub MCP (protocol-real) + real pgvector adapter (deterministic embedding) | OFFLINE_VERIFIED | DemoBundle `topology` block; `samples/demo-bundles/m4f-competition.json` | Protocol-real, **not** a call to github.com. Deterministic embedding, not production embedding quality. |

---

## Category C — Cannot claim (not yet)

| # | Claim (do NOT make) | Status | Why not / precise state |
|---|---------------------|--------|--------------------------|
| C1 | "The Demo Console is a production management dashboard" | CANNOT_CLAIM | It is a **read-only evidence viewer**. No login, no RBAC, no Agent start/stop, no config edit, no GitHub writes, no real-time production monitor. |
| C2 | "REPLAY shows real-time production data" | CANNOT_CLAIM | REPLAY loads a pre-generated DemoBundle JSON — no network, no LLM, no writes. It is a finished, replayed run. |
| C3 | "RAG improves Reviewer/Fixer decision accuracy" | CANNOT_CLAIM | `runtime_consumes_rag_context=false`; `core.scan` / `core.run` do not consume RAG context. `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME`. |
| C4 | "The benchmark uses real pgvector / real embeddings / real LLM" | CANNOT_CLAIM | Confirmatory benchmark uses deterministic offline `TokenOverlapAdapter`. `api_token_usage=null`; `tokenizer_name="word-count-heuristic"`. |
| C5 | "M6-C real cloud SLS observability is complete" | CANNOT_CLAIM | **M6-C real cloud SLS not completed.** OTel spans go to local collectors. Official `alibabacloud-sls-query` Skill planned, not integrated. |
| C6 | "ISOLATED_LIVE Console mode works" | CANNOT_CLAIM | **ISOLATED_LIVE not implemented.** Designed in `M7-P3-Demo-Console-Design.md` (read-only polling of MergePilot-Test audit DB); code not written this milestone. |
| C7 | "The benchmark proves multi-Agent superiority / higher recall" | CANNOT_CLAIM | N=10×2 recall is **the same** between single- and multi-Agent. Does not prove recall lift. Small sample, synthetic fixtures. |
| C8 | "`findings=0` means no issues were found" | CANNOT_CLAIM | `findings=0` in the DemoBundle provenance summary is a **storage artifact** — the evidence store holds digests, not inline text. The findings table renders from `findings[]`, which points at a sourced evidence file. |
| C9 | "RAG context is adopted / trusted by the runtime" | CANNOT_CLAIM | `adopted=false`, `untrusted=true`. RAG produces **advisory evidence only**. |
| C10 | "Zero human intervention in production" | CANNOT_CLAIM | Early real-GitHub demos needed occasional Manager nudge. Deterministic handoff is candidate/isolated-stack only. |
| C11 | "Nacos / RocketMQ are integrated" | CANNOT_CLAIM | Planned, code not written. |
| C12 | "MIG-B4-001 chain migration is idempotent / reversible" | CANNOT_CLAIM | Early `m3b_b4.sql` cannot CREATE OR REPLACE rollback; support path is forward-only. Needs formal migration runner before clean-env reproduction. |

---

## Category D — Future production needs

| # | Claim | Status | What is needed |
|---|-------|--------|----------------|
| D1 | ISOLATED_LIVE Console mode | FUTURE | Implement `tools/demo_console/live_poller.py` (read-only SELECT against MergePilot-Test audit DB; 2s polling; localhost-only). Designed in `M7-P3-Demo-Console-Design.md` §8 Phase 3. |
| D2 | M6-C real cloud SLS ingestion | FUTURE | Integrate official `alibabacloud-sls-query` Skill; wire OTel exporter to cloud backend. |
| D3 | Production HiClaw live window (broaden `hiclab_live`) | FUTURE | D2B-3 is `hiclab_live=true` on v1.2.2 (A11); broaden to the full 22-formula set (currently 5 true / 0 false / 17 unproven). |
| D4 | MergePilot Admin (post-competition) | FUTURE | RBAC, Agent lifecycle management, config editing, GitHub write surface, multi-tenant dashboard, alerting. Explicitly deferred — see `M7-P3-Demo-Console-Design.md` §10. |
| D5 | Nacos / RocketMQ integration | FUTURE | Planned; no code. |
| D6 | Formal migration runner (MIG-B4-001) | FUTURE | Required before clean-environment reproduction; forward-only today. |
| D7 | RAG runtime consumption + workflow-utility measurement | FUTURE | Would require `runtime_consumes_rag_context=true` and a runtime that can be measured for Reviewer/Fixer accuracy lift. Currently `NOT_MEASURABLE_WITH_CURRENT_RUNTIME`. |
| D8 | Benchmark scale-up (N≥broader, real pgvector, multi-repo) | FUTURE | Current benchmarks are small-sample, offline-adapter, single-model, synthetic. Production-scale claims need real embeddings + multi-repo E2E. |

---

## Quick reference: the boundaries you must be able to say aloud

These map 1:1 to the demo runbook (`M7-P3-Demo-Showcase-Runbook.md`).

1. **Demo Console is NOT a production management dashboard.** (C1)
2. **REPLAY is NOT real-time production data.** (C2)
3. **M6-C real cloud SLS not completed.** (C5)
4. **ISOLATED_LIVE not implemented.** (C6)
5. **RAG only produces advisory evidence** (`adopted=false`, `untrusted=true`). (C9)
6. **`core.scan` / `core.run` do not consume RAG context** (`runtime_consumes_rag_context=false`). (C3)
7. **Cannot claim Reviewer/Fixer accuracy improvement.** (C3, C7)
8. **`findings=0` doesn't mean no issues found** — evidence stores digests, not inline. (C8)
