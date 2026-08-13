# M7-P3 Demo Rehearsal Checklist

**Status**: Active — complete this checklist **before every** competition demo.
**Milestone**: M7-P3
**Demo runtime**: Demo Console in REPLAY mode (`samples/demo-console/index.html`
or `python tools/demo_console/serve.py`).
**Target duration**: 6–7 minutes (ceiling 8m).

> This checklist exists to make the demo **boring to run and honest to deliver**.
> If any `BLOCKER` item is unchecked, do not start the demo. `WARN` items mean
> proceed with a known fallback.

---

## 1. Environment setup

Goal: the demo runs fully offline on the presenter laptop, no network needed.

- [ ] **(BLOCKER)** Laptop fully charged + charger plugged in. Power mode set
      to "best performance" (no screen dim / sleep during demo).
- [ ] **(BLOCKER)** Browser (Chrome or Edge) installed; default profile is clean
      (no heavy extensions running). Close all unrelated tabs.
- [ ] **(BLOCKER)** Demo Console verified to open offline by **double-clicking**
      `samples/demo-console/index.html` directly (file://). All 8 pages render.
- [ ] **(WARN)** Alternative: `python tools/demo_console/serve.py --port 8080`
      starts and serves `http://127.0.0.1:8080` with no errors. Python 3.10+
      available on PATH.
- [ ] **(BLOCKER)** Airplane mode / Wi-Fi off — confirm the Console still loads
      and every page still renders. The demo must not depend on the network.
- [ ] **(WARN)** External display / HDMI / USB-C adapter tested with the
      projector. Resolution set to 1920×1080 (or the projector's native).
- [ ] **(WARN)** Presentation mode: OS notifications muted (Do Not Disturb on),
      Slack / email / calendar popups off, low-battery alerts suppressed.
- [ ] **(WARN)** Font size / browser zoom set so the audience can read the
      findings table and boundary banners from the back row (typically
      Cmd/Ctrl + + once or twice).
- [ ] **(BLOCKER)** Backup browser installed (the other of Chrome/Edge, or
      Firefox) with the Console verified to open in it too.

---

## 2. File verification

Goal: every artifact the presenter points at exists and has the expected hash.

- [ ] **(BLOCKER)** `samples/demo-console/index.html` exists and opens.
- [ ] **(BLOCKER)** DemoBundle JSON exists at `samples/demo-bundles/m4f-competition.json`
      and is valid JSON (`python -c "import json;json.load(open('...'))"`).
- [ ] **(BLOCKER)** `bundle_sha256` matches the canonical JSON (re-run the
      builder's verify step; mismatch = BLOCKER, re-build the bundle).
- [ ] **(BLOCKER)** Every `evidence_files[].sha256` in the bundle matches the
      actual file on disk (render-time verification passes; no red "SHA mismatch").
- [ ] **(WARN)** Key evidence files exist on disk (pre-load as backup tabs):
      - [ ] `evidence/m4/m4f/agentteams-demo-summary.json`
      - [ ] `evidence/m6/rag/pgvector-isolated-verification.json`
      - [ ] `evidence/m7/benchmark/rag-n20-confirmatory.json`
      - [ ] `evidence/m5/0d/hiclaw-v122-true-live-pass.json`
- [ ] **(WARN)** `docs/assets/mergepilot-architecture.svg` opens in a browser
      tab (backup visual for Section 2).
- [ ] **(WARN)** `docs/M7-P3-Claim-Matrix.md` open in an editor (Q&A reference).

---

## 3. Content review

Goal: the right things render, and the boundaries are visibly on screen.

- [ ] **(BLOCKER)** All 8 pages render without errors:
      - [ ] 1. Overview (final-status badge, mode banner, DAG grid, topology)
      - [ ] 2. Workflow Timeline (span waterfall, stage markers)
      - [ ] 3. Findings & Fixes (findings table, linked fixes table)
      - [ ] 4. RAG Advisory (per-agent cards, case list, boundary banner)
      - [ ] 5. OTel Trace Tree (parent-child spans, expandable attributes)
      - [ ] 6. Policy & Safety (permission matrix, deny/timeout events, residue)
      - [ ] 7. Evidence & Provenance (evidence table, raw JSON viewer, bundle SHA)
      - [ ] 8. Benchmark Summary (metrics, cohorts, quality gate, boundary banner)
- [ ] **(BLOCKER)** REPLAY mode banner is visible on **every** page header.
- [ ] **(BLOCKER)** RAG Advisory page visibly shows `adopted=false`,
      `untrusted=true`, `runtime_consumes_rag_context=false`.
- [ ] **(BLOCKER)** Benchmark page visibly shows
      `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME` and
      "deterministic offline TokenOverlapAdapter" boundary.
- [ ] **(BLOCKER)** Evidence & Provenance page shows `bundle_sha256` and at
      least one clickable raw-JSON evidence view.
- [ ] **(WARN)** Policy & Safety page shows `secret_leaks=0` and
      `residue={containers:0, networks:0, temp_dirs:0}`.
- [ ] **(WARN)** Final-status badge reads `MERGED`.

---

## 4. Timing rehearsal

Goal: the run fits in 6–7 minutes with comfortable margin to the 8m ceiling.

- [ ] **(BLOCKER)** At least **two full timed run-throughs** recorded with a
      stopwatch. Log each section's actual time.
- [ ] **(BLOCKER)** Both runs land between 6m00s and 7m15s.
- [ ] **(WARN)** No single section exceeded its budget by more than 15s.
      Use the table below.

| # | Section | Target | Run 1 | Run 2 |
|---|---------|--------|-------|-------|
| 1 | Background & pain points | 30s | _____ | _____ |
| 2 | Architecture | 45s | _____ | _____ |
| 3 | Workflow / DAG | 45s | _____ | _____ |
| 4 | SAST / Reviewer / Fixer / Verifier | 60s | _____ | _____ |
| 5 | OTel trace & security policy | 45s | _____ | _____ |
| 6 | RAG advisory display | 45s | _____ | _____ |
| 7 | Evidence provenance & SHA | 45s | _____ | _____ |
| 8 | Benchmark results & boundaries | 30s | _____ | _____ |
| 9 | REPLAY vs production | 30s | _____ | _____ |
| 10 | Value summary | 30s | _____ | _____ |
| — | **Total** | **6m45s** | _____ | _____ |

- [ ] **(WARN)** Compression plan rehearsed: if running long, trim Sections 2
      and 3 first. **Never cut** Sections 6, 8, or 9 — they carry the boundaries.
- [ ] **(WARN)** Breathing / pacing rehearsed: no section read at sprint speed.
      The boundaries must be spoken slowly enough to be understood.

---

## 5. Backup plan

Goal: never stand in silence; every failure mode has a scripted recovery.

- [ ] **Projector / external display fails**
      - Fallback: present on the laptop screen, audience gathered closer; or
        switch to the second adapter / cable (verified in §1).
      - Script: "While we switch cables, the key point is…" (deliver Section 1
        pain points verbally).
- [ ] **Primary browser crashes / tab dies**
      - Fallback: switch to backup browser (verified in §1), reopen the Console.
      - Script: "I'll reopen the Console — meanwhile, the architecture is…"
        (point at architecture SVG, backup Tab B).
- [ ] **A Console page renders blank or errors**
      - Fallback: switch to the DemoBundle JSON (backup Tab C) and read the
        relevant array aloud. The JSON **is** the evidence; reading it is honest.
      - Script per page:
        - Timeline blank → read `spans[]` from Tab C.
        - Findings & Fixes blank → read `findings[]` + `fixes[]` from Tab C,
          or open `evidence/m4/m4f/agentteams-demo-summary.json` (Tab D).
        - RAG blank → read `rag_advisories[]` from Tab C.
        - Evidence blank → read `evidence_files[]` from Tab C.
        - Benchmark blank → open `evidence/m7/benchmark/rag-n20-confirmatory.json`
          (Tab E).
- [ ] **Laptop battery / power dies**
      - Fallback: the presenter's printed/phone copy of the runbook + claim
      matrix. The demo can be delivered as a narrated walkthrough of the
      architecture SVG and the key evidence files.
- [ ] **File not found / SHA mismatch discovered live**
      - Fallback: **stop and say so honestly.** "I'm seeing a verification
        mismatch I didn't expect — rather than show you an unverified number,
        let me move to the next section." Never paper over a SHA failure.
- [ ] **Audience question mid-demo that needs an unverified claim**
      - Fallback: defer. "Great question — I'll answer it in Q&A. I don't want
        to state a number I can't point to evidence for right now."

---

## 6. Q&A preparation

Rehearse these out loud. Each answer must end with a boundary if one exists.
Full versions in `M7-P3-Project-Portfolio-Profile.md` §5.

- [ ] "Is this deployed in production?"
      → Control plane / Gateway / DAG / rollback implemented & verified; D2B-3
        is real v1.2.2 production live (64/64); the **Console** is REPLAY, not
        a live dashboard.
- [ ] "Does RAG actually improve review?"
      → No claim. `runtime_consumes_rag_context=false`; RAG is advisory only;
        benchmark is offline adapter, not accuracy lift.
- [ ] "Is the GitHub integration real?"
      → Protocol is real SSE; the Console uses a stateful **fake** GitHub MCP
        (protocol-real). Real-GitHub E2E (PR #1 → PR #3) is a separate earlier run.
- [ ] "What is the benchmark's statistical power?"
      → Low. N=25 confirmatory / N=10×2 formal; small sample, offline adapter,
        single model, synthetic fixtures.
- [ ] "How is crash recovery handled?"
      → PostgreSQL state machine + Outbox; re-queue, not re-prompt; event de-dup.
- [ ] "Why did Manager handoff need a human nudge?"
      → Early demos yes; M5-0B made handoffs deterministic on the
        **candidate/isolated stack** (14/14 + 13/13); will not generalize.
- [ ] "What does `findings=0` mean?"
      → Storage artifact — evidence stores digests, not inline text. Not "no
        issues found."
- [ ] "What is NOT done?"
      → M6-C cloud SLS, ISOLATED_LIVE mode, Nacos/RocketMQ, Admin platform.
- [ ] "How is this different from Copilot / CodeRabbit?"
      → They stop at findings; MergePilot closes the loop (fix → verify →
        approve → rollback) with a deterministic control plane and a Policy
        Gateway. State authority is the DB, not the LLM.
- [ ] "Can Agents escape their permissions?"
      → 8 negative classes all fail-closed (50/50). Workers hold zero
        credentials. Gateway is self-built Python SSE, not Higress-native.

---

## 7. Anti-overclaiming reminders (what NOT to say)

These are the most likely overclaims. If you catch yourself starting one,
stop and restate with the boundary. Source of truth: `M7-P3-Claim-Matrix.md`.

- [ ] **Do NOT say** "this is live production data" or "this is real-time."
      → It is REPLAY. Say: "pre-generated bundle, no network, no LLM, no writes."
- [ ] **Do NOT say** "the Console is a management dashboard / admin panel."
      → It is a read-only evidence viewer. No RBAC, no Agent control, no writes.
- [ ] **Do NOT say** "RAG improves review / fix accuracy."
      → `runtime_consumes_rag_context=false`. RAG is advisory evidence only.
- [ ] **Do NOT say** "the benchmark proves multi-Agent is better / higher recall."
      → N=10×2 recall is the same. Small sample, offline, synthetic.
- [ ] **Do NOT say** "the benchmark uses real pgvector / real embeddings."
      → It uses a deterministic offline `TokenOverlapAdapter`.
- [ ] **Do NOT say** "cloud SLS observability is done."
      → M6-C real cloud SLS is **not completed**. Spans go to local collectors.
- [ ] **Do NOT say** "ISOLATED_LIVE works."
      → Designed, not implemented.
- [ ] **Do NOT say** "`findings=0` means no issues were found."
      → It means the evidence store holds digests, not inline text.
- [ ] **Do NOT say** "zero human intervention in production."
      → Early demos needed occasional nudge; deterministic handoff is isolated-stack only.
- [ ] **Do NOT say** "Nacos / RocketMQ are integrated."
      → Planned, no code.
- [ ] **Do NOT say** "the GitHub call goes to github.com" during the Console demo.
      → The Console uses a stateful fake GitHub MCP (protocol-real SSE). The
        real-github E2E is a separate earlier run.
- [ ] **Do NOT imply** the Demo Console is production-ready.
      → It is a competition evidence viewer. Admin / RBAC / write surface are
        explicitly deferred.

---

## Final go / no-go (read aloud before walking on stage)

> "Environment offline-verified. All 8 pages render. Bundle SHA and evidence
> SHAs verified. Two timed run-throughs within 6–7 minutes. Boundary banners
> visible on RAG and Benchmark pages. Backup browser and backup tabs loaded.
> I will state REPLAY, RAG advisory, offline benchmark, and findings=0
> boundaries out loud. Go."
