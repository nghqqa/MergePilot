# M7 Final Submission Checklist

**Status**: Audit complete — Demo video DEFERRED

## Technical Evidence (all verified from origin/main)

- [x] M7-P2 RAG Benchmark confirmatory evidence (`evidence/m7/benchmark/rag-n20-confirmatory.json`)
  - 16/16 pre-registered gates passed
  - `confirmatory_all_ok=true`
  - SHA: `36edc664...`
- [x] M7-P2 RAG Benchmark development evidence (`evidence/m7/benchmark/rag-n20-offline.json`)
  - `quality_gate_pass=null` (not confirmatory)
  - SHA: `c0d53c56...`
- [x] M7-P3 Demo Console REPLAY merged (`m7-p3-demo-console-closed`)
  - 33 unittest passed, 8 pages, 0 external deps
  - Bundle SHA recomputable, 5 evidence SHA match
- [x] M7-P4 Clean Reproduction evidence (`evidence/m7/reproduction/demo-console-clean-replay.json`)
  - `all_ok=true`, `status=COMPLETED`
  - SHA: `79237b4c...`
  - Python 3.9.25 verified, 2x deterministic test runs

## Demo Console

- [x] Static HTML (`samples/demo-console/index.html`) — 19KB, self-contained
- [x] DemoBundle JSON (`samples/demo-bundles/m7-rag-replay.json`) — SHA verified
- [x] 8 pages: Overview, Timeline, Findings, RAG Advisory, Trace Tree, Policy & Safety, Evidence, Benchmark
- [x] REPLAY mode banner on every page
- [x] `adopted=false`, `untrusted=true`, `runtime_consumes_rag_context=false`
- [x] `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME`
- [x] Findings/Fixes=0 honest explanation
- [x] Zero external dependencies (no CDN, no npm, no network)
- [x] Launch: `python -m http.server 8080 --bind 127.0.0.1` (1 command)

## Showcase Materials

- [x] Demo Showcase Runbook (10-section 6-7 min script)
- [x] Project Portfolio Profile (bilingual intro, resume, interview Q&A)
- [x] Claim Matrix (37 claims: 13 CAN_CLAIM / 4 OFFLINE / 12 CANNOT / 8 FUTURE)
- [x] Demo Rehearsal Checklist (environment, timing, backup, anti-overclaim)

## Clean Reproduction

- [x] Design docs (Design, Runbook, Schema, Platform Matrix)
- [x] Clean reproduction evidence with structured fields
- [x] Windows `core.autocrlf=false` requirement documented
- [x] PowerShell server measurement (PID, port owner, residue)
- [x] Artifact baseline 7/7 byte-exact match
- [x] All 6 residue fields independently measured = 0

## Documentation

- [x] README.md updated with M6-RAG + M7 boundaries
- [x] docs/项目状态.md §九 (M7 section)
- [x] docs/复赛路线图.md M7 checklist items
- [x] LICENSE (Apache-2.0)

## Gaps

- [ ] `.env.example` — Low priority (Demo Console needs no env vars)
- [ ] `NOTICE` — Low priority (Apache-2.0 standard)
- [ ] `THIRD_PARTY` — Low priority (zero third-party deps)
- [x] **Demo video** — **DEFERRED** (`DEFERRED_NOT_REQUIRED_FOR_CURRENT_TECHNICAL_GATE`)

## Integrity Gates

- [x] Protected evidence (M3-M6) unchanged
- [x] No secret leaks in any evidence or HTML
- [x] git diff --check = 0
- [x] No overstatement in README or Claim Matrix
- [x] No M7 overall close tag created (correctly deferred)
- [x] origin/main = `0bc2e69`

## Conclusion

**Technical submission package is complete** for code, evidence, documentation,
and reproducibility. Demo video is deferred as a presentation enhancement,
not a technical gate blocker.
