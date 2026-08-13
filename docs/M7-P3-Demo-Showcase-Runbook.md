# M7-P3 Demo Showcase Runbook

**Status**: Active (demo material — design freeze in `M7-P3-Demo-Console-Design.md`)
**Milestone**: M7-P3
**Target duration**: 6–7 minutes (hard ceiling 8 minutes)
**Demo runtime**: Demo Console in **REPLAY** mode
**Entry point**: `samples/demo-console/index.html` opened directly in a browser, OR served via
`python tools/demo_console/serve.py` (localhost:8080, read-only).

> This runbook is a talking script. Every claim made on stage must trace to an
> evidence file with SHA-256 provenance. When in doubt, state the boundary
> instead of the claim. See `M7-P3-Claim-Matrix.md` for the authoritative list
> of what may and may not be said.

---

## How to use this runbook

Each section below has a fixed structure:

- **Demo page**: which Console page (1 of 8) to show.
- **Demo action**: the literal click / navigation to perform.
- **Talking points**: what to say (short, presenter-voiced).
- **Evidence to display**: the concrete artifact to point at.
- **Backup if demo fails**: the fallback if the page is blank or the browser
  hangs — never dead air.
- **Suggested duration**: budget; sums to 6–7 min.

A consolidated timing table and the full honest-boundary script are at the end.

---

## Pre-demo one-line framing (say this before Section 1)

> "What you are about to see is a **REPLAY** of a completed MergePilot run. It
> is not a live production system and it does not talk to the internet. Every
> number on screen links to a verifiable evidence file with a SHA-256 digest.
> I will state the boundaries out loud as we go."

---

## Section 1 — Project background & pain points

- **Demo page**: Overview page (page 1/8), top banner only. Do not scroll yet.
- **Demo action**: Leave the Console on the Overview; face the audience.
- **Talking points**:
  - Single-agent code review only raises findings — it does not close the loop.
    Misreports pile up, fixes still fall on humans, and nobody owns verify or merge.
  - High-risk changes (secrets, dependency downgrades, dangerous deletes) lack
    approval, rollback, and audit — a compliance gap.
  - LLM orchestration is inherently unreliable: stage handoff, idempotency,
    crash recovery, and credential isolation cannot be left to prompts.
  - MergePilot's answer: a **deterministic control plane** + Agent semantic
    decisions + a tool-layer permission gate + rollback on failure + structured
    audit end to end.
- **Evidence to display**: The mode banner `REPLAY` and the final-status badge
  `MERGED` — establish "this is a finished, replayed run."
- **Backup if demo fails**: State the pain points from memory; the Overview is
  decorative for this section, not load-bearing.
- **Suggested duration**: 30s

---

## Section 2 — MergePilot architecture

- **Demo page**: Overview page (1/8), topology summary block.
- **Demo action**: Scroll to the **topology summary** and the **6-skill DAG
  status grid**.
- **Talking points**:
  - The **single source of truth** is a PostgreSQL state machine + Outbox:
    the Controller owns task state, stage transitions, event de-dup, timeout,
    and recovery. The Agent runtime is **not** the state authority.
  - AgentTeams / HiClaw is the adapted Agent runtime — it does semantic
    decisions and collaboration, but it does **not** own durable state.
  - Six functional roles collapse onto the runtime: diff-parse, risk-classify,
    sast-scan, test-runner, pr-lifecycle, case-retrieval.
  - A **minimum-privilege Policy Gateway** sits between Agents and every tool
    / GitHub call. Workers hold zero credentials.
- **Evidence to display**: Topology summary showing "real gateway.py over SSE",
  "stateful fake GitHub MCP (protocol-real SSE)", "real pgvector adapter".
- **Backup if demo fails**: Show the architecture SVG at
  `docs/assets/mergepilot-architecture.svg` opened in a browser tab (keep it
  pre-loaded as Tab B).
- **Suggested duration**: 45s

---

## Section 3 — Agent workflow / DAG

- **Demo page**: Overview page (1/8) DAG grid, then Workflow Timeline (2/8).
- **Demo action**: Point at the reviewer → fixer → verifier cycle in the DAG
  grid, then click into the **Workflow Timeline**.
- **Talking points**:
  - The workflow is a deterministic DAG, not a free-form Agent conversation.
    Each stage has an explicit `depends_on`.
  - The Controller drives stage transitions; Agents consume work items and
    emit verdicts. An Agent crash or timeout is recovered by the state
    machine, not by re-prompting.
  - The same run produces the same span order on replay — this is what makes
    the demo reproducible offline.
- **Evidence to display**: Waterfall of spans ordered by `start_time`; stage
  markers review → fix → verify.
- **Backup if demo fails**: Read the `workflow_stages[]` and `agents[]` arrays
  from the DemoBundle JSON (Tab C, pre-loaded) — the structure speaks for itself.
- **Suggested duration**: 45s

---

## Section 4 — SAST / Reviewer / Fixer / Verifier flow

- **Demo page**: Findings & Fixes page (3/8).
- **Demo action**: Walk the findings table, then the linked fixes table.
- **Talking points**:
  - **Reviewer** runs `sast-scan` (deterministic subprocess, not an LLM
    free-form call) and emits structured findings — e.g. SQL injection via
    string concatenation, hardcoded secret.
  - **Fixer** consumes the findings, produces a patch, and creates a PR
    **through the Policy Gateway** (real `gateway.py` over SSE, not a mock).
  - **Verifier** re-runs `sast-scan` + tests on the patched branch; only a
    clean re-scan and passing tests advance the run to merge.
  - This is the closed loop that single-agent review does not provide.
- **Evidence to display**:
  - Findings table: `F-001` sql_injection / high / `src/db.py:42`.
  - Fixes table: `FX-001` linked to `F-001`, `pr_created=true`.
  - Verifier result: `PASS`, `tests_run=12`, `tests_passed=12`, `tests_failed=0`.
- **Honest boundary (say it)**: "The Fixer creates a PR via a **stateful fake
  GitHub MCP** that speaks the real SSE protocol. It is protocol-real, not a
  call to github.com. The real-GitHub E2E (PR #1 → PR #3, 5/5 resolved) is a
  separate earlier evidence run, not this replay."
- **Backup if demo fails**: Open `evidence/m4/m4f/agentteams-demo-summary.json`
  (Tab D) and read the findings / fixes / verifier blocks.
- **Suggested duration**: 60s

---

## Section 5 — OTel trace & security policy

- **Demo page**: OTel Trace Tree (5/8), then Policy & Safety (6/8).
- **Demo action**: Expand the root span `controller.process_event`, show one
  child span's redacted attributes; then switch to Policy & Safety and show
  the permission matrix and deny/timeout events.
- **Talking points**:
  - Every stage is an OpenTelemetry span with parent-child linkage, status,
    and duration. Attributes are auto-redacted by the OTel SpanRecord — no
    secret ever lands in a span attribute.
  - The Policy Gateway logs every permission decision: which role called which
    tool, bound revisions, deny / timeout / fallback events.
  - Post-run hygiene: `secret_leaks=0`, `residue={containers:0, networks:0,
    temp_dirs:0}`. The run leaves nothing behind.
- **Evidence to display**:
  - Trace tree with OK (green) spans; one expanded span showing redacted attrs.
  - Permission matrix; a deny or timeout event row.
  - `secret_leaks: 0` and the residue block.
- **Honest boundary (say it)**: "These are OTel spans captured in the
  replayed/isolated run. M6-C real **cloud SLS** ingestion is **not completed**
  — spans are exported to local collectors, not to a cloud observability
  backend."
- **Backup if demo fails**: Open the `spans[]` array in the DemoBundle JSON
  (Tab C) and read 2–3 span entries aloud.
- **Suggested duration**: 45s

---

## Section 6 — RAG advisory display

- **Demo page**: RAG Advisory page (4/8).
- **Demo action**: Show the per-agent advisory cards and the case list with
  similarity scores and citation URLs. Point at the boundary banner.
- **Talking points**:
  - RAG (`case-retrieval`) retrieves similar past cases and attaches them to
    the Reviewer / Fixer as **advisory context** — with a similarity score and
    a citation URL for every case.
  - This is evidence the human reviewer (or a future runtime) could use. It is
    surfaced, labeled, and citable.
- **Evidence to display**:
  - Reviewer card: `status=ok`, `hit_count=3`, cases with `similarity=0.92`.
  - Citation URL per case.
- **Honest boundary (say it — this is the most important boundary in the demo)**:
  - "`adopted=false`, `untrusted=true` — RAG results are **advisory evidence
    only**."
  - "`runtime_consumes_rag_context=false` — the Reviewer and Fixer decision
    logic does **not** read RAG context today."
  - "`workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME` — we cannot
    and do not claim that RAG improves Reviewer/Fixer accuracy."
- **Backup if demo fails**: Read the `rag_advisories[]` array from the
  DemoBundle JSON (Tab C).
- **Suggested duration**: 45s

---

## Section 7 — Evidence provenance & SHA verification

- **Demo page**: Evidence & Provenance page (7/8).
- **Demo action**: Show the evidence file table; click one row to reveal its
  raw JSON (read-only). Point at the `bundle_sha256` and per-file SHA-256.
- **Talking points**:
  - Every number on every page traces to an evidence file. Nothing is
    hardcoded into the Console.
  - Each evidence file carries a SHA-256 computed at bundle-build time and
    re-verified at render time. A mismatch is a render error, not a warning.
  - `bundle_sha256` covers the canonical JSON (sorted keys, UTF-8) excluding
    itself — the whole bundle is tamper-evident.
  - Git tags map to evidence: `m6-rag-pgvector-closed`, `m7-p2-rag-benchmark-closed`.
- **Evidence to display**:
  - Evidence table rows: path, SHA-256, description, source commit.
  - One expanded raw JSON view.
  - The `bundle_sha256` value.
- **Honest boundary (say it)**: "Note that on this replay bundle `findings=0`
  in the DemoBundle's provenance summary — that is because the evidence store
  records **digests**, not inline finding text. The findings you saw on page 3
  are rendered from the `findings[]` array, which itself points at a sourced
  evidence file. `findings=0` does **not** mean no issues were found."
- **Backup if demo fails**: Open the `evidence_files[]` array in the DemoBundle
  JSON (Tab C) and read 2 rows.
- **Suggested duration**: 45s

---

## Section 8 — Benchmark results & boundaries

- **Demo page**: Benchmark Summary page (8/8).
- **Demo action**: Show retrieval metrics, cohort distribution, and the quality
  gate. Point at both boundary banners.
- **Talking points**:
  - On a **held-out** dataset (`rag-bench-v3-heldout`, 25 cases) that was not
    used during development calibration, RAG retrieval hits the pre-registered
    thresholds: hit@1, hit@3, MRR all meet the frozen bars.
  - Cohorts: 15 positive retrieval, 5 abstention, 5 fault injection.
  - All pre-registered quality gates pass; `confirmatory_all_ok=true`.
- **Evidence to display**:
  - Retrieval metrics block (hit@1, hit@3, MRR).
  - Cohort distribution.
  - `quality_gate_pass=true`, `confirmatory_all_ok=true`.
- **Honest boundary (say it)**:
  - "This benchmark uses a **deterministic offline `TokenOverlapAdapter`**, not
    real pgvector embeddings and not a real LLM. It validates retrieval
    mechanics and safety, not production embedding quality."
  - "`runtime_consumes_rag_context=false` — we do **not** claim Reviewer/Fixer
    accuracy improvement from this benchmark."
  - "N=25 is a confirmatory small sample; it is not a production-scale claim."
- **Backup if demo fails**: Open `evidence/m7/benchmark/rag-n20-confirmatory.json`
  (Tab E) and read the metrics block.
- **Suggested duration**: 30s

---

## Section 9 — REPLAY vs production distinction

- **Demo page**: Overview page (1/8), mode banner. Stand alone; no scrolling.
- **Demo action**: Point at the persistent `REPLAY` mode banner.
- **Talking points**:
  - The Console has three modes — **REPLAY**, **ISOLATED_LIVE**, and
    **HISTORICAL** — and the mode is set at startup, shown on every page, and
    **cannot** change at runtime. This prevents mixing replay data with live data.
  - Today's demo is REPLAY: pre-generated DemoBundle JSON, no network, no LLM,
    no writes.
  - **ISOLATED_LIVE** (read-only polling of the MergePilot-Test audit DB) is
    designed but **not implemented** in this milestone.
  - The Demo Console is **not** a production management dashboard — no login,
    no RBAC, no Agent start/stop, no GitHub writes, no real-time production
    monitor.
- **Evidence to display**: The `REPLAY` banner on the page header.
- **Backup if demo fails**: This section is verbal; no page dependency.
- **Suggested duration**: 30s

---

## Section 10 — Value summary

- **Demo page**: Overview page (1/8), full screen, final-status badge `MERGED`.
- **Demo action**: Stop navigating; deliver the close.
- **Talking points**:
  - MergePilot turns PR review into a governed closed loop: review → fix →
    verify → approve → merge, with rollback on failure and structured audit
    throughout.
  - The control plane is deterministic; the Agents do semantics; the Policy
    Gateway enforces least privilege; everything is evidenced and SHA-verified.
  - We show what is real today and we label what is not. Every claim links to
    verifiable evidence — nothing is hardcoded.
- **Evidence to display**: The `MERGED` badge + the evidence-provenance reminder.
- **Backup if demo fails**: Deliver the close from memory.
- **Suggested duration**: 30s

---

## Consolidated timing table

| # | Section | Page | Target |
|---|---------|------|--------|
| 1 | Background & pain points | Overview banner | 30s |
| 2 | Architecture | Overview topology | 45s |
| 3 | Workflow / DAG | Overview + Timeline | 45s |
| 4 | SAST / Reviewer / Fixer / Verifier | Findings & Fixes | 60s |
| 5 | OTel trace & security policy | Trace Tree + Policy | 45s |
| 6 | RAG advisory display | RAG Advisory | 45s |
| 7 | Evidence provenance & SHA | Evidence & Provenance | 45s |
| 8 | Benchmark results & boundaries | Benchmark Summary | 30s |
| 9 | REPLAY vs production | Overview banner | 30s |
| 10 | Value summary | Overview badge | 30s |
| — | **Total** | | **6m 45s** |

Ceiling: 8m 00s. If running long, compress Sections 2 and 3 first; never cut a
boundary statement (Sections 6, 8, 9).

---

## Honest-boundary script (must be spoken, not paraphrased away)

Deliver these verbatim or very close. They are the load-bearing honesty of the
demo and map 1:1 to rows in `M7-P3-Claim-Matrix.md`.

1. **REPLAY, not live production**: "This is REPLAY mode — a pre-generated
   bundle, no network, no LLM, no writes. It is not a live production system."
2. **RAG is advisory only**: "`adopted=false`, `untrusted=true`. RAG produces
   advisory evidence; it does not influence Reviewer/Fixer decisions."
3. **Runtime does not consume RAG**: "`runtime_consumes_rag_context=false`.
   `core.scan` / `core.run` do not read RAG context today."
4. **Benchmark is offline**: "The benchmark uses a deterministic offline
   `TokenOverlapAdapter`, not real pgvector embeddings. It does not claim
   Reviewer/Fixer accuracy improvement."
5. **Findings=0 is a storage artifact**: "`findings=0` in the provenance
   summary means the evidence store holds digests, not inline text. It does
   not mean no issues were found."
6. **Cloud SLS not completed**: "M6-C real cloud SLS ingestion is not
   completed; spans go to local collectors."
7. **ISOLATED_LIVE not implemented**: "ISOLATED_LIVE mode is designed but not
   implemented in this milestone."
8. **Not a management dashboard**: "The Demo Console is a read-only evidence
   viewer, not a production management dashboard."

---

## Backup tabs to pre-load before going on stage

| Tab | Content | Path |
|-----|---------|------|
| A | Demo Console (primary) | `samples/demo-console/index.html` |
| B | Architecture SVG | `docs/assets/mergepilot-architecture.svg` |
| C | DemoBundle JSON | `samples/demo-bundles/m4f-competition.json` |
| D | M4-F demo summary | `evidence/m4/m4f/agentteams-demo-summary.json` |
| E | Confirmatory benchmark | `evidence/m7/benchmark/rag-n20-confirmatory.json` |
| F | Claim matrix (for Q&A) | `docs/M7-P3-Claim-Matrix.md` |

If the primary tab dies, switch to the relevant backup tab and keep talking.
Never stand in silence — the boundary statements and the architecture SVG
alone can carry 60–90s while a tab reloads.
