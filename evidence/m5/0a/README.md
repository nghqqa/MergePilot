# M5-0A HiClaw Live Candidate — Release Evidence

evidence head: `c8b4c3dfcc7384675c963a52bd873dd24feeb8f2` (commit C: `feat: add M5-0A HiClaw Live candidate`)
parent: `0197565274039cc0344ea74f5e5fcd4f0e2386dc` (docs: freeze M5-0 HiClaw Live design)
generated_at: 2026-08-04T14:03Z (run_all) / 2026-08-04T13:07Z (integration) / 2026-08-04T13:04Z (unit)

## Scope honesty

- **hiclaw_live = false.** M5-0A does NOT claim real HiClaw completion.
- The Candidate exercises a **real Matrix Client-Server protocol** against an
  **isolated throwaway mini homeserver** (`tests/m5_0/fixtures/mini_matrix_hs.py`),
  not the online HiClaw Matrix. No operator-provided Matrix credentials are used.
- The Policy Gateway and GitHub MCP are the real `gateway.py` and a stateful
  protocol-real fake GitHub MCP (SSE), not the production GitHub upstream.
- M5-0B is **not started**. M5-0A covers only the ingress path
  (`/sync` M4F_RUN → claim → six-Skill DAG enqueue → PROCESSED). The
  Reviewer/Fixer/Verifier handoff reconciliation (`reconcile_m5_skill_to_review`,
  `reconcile_m5_handoffs`) is deferred to M5-0B and intentionally a no-op here.
- All test secrets are **runtime-generated** (`rand_hex`); no real production
  tokens, passwords, or Matrix credentials are written into source or evidence.

## Authoritative gates (re-verified ON commit C, not referenced from old logs)

### M4-F regression — `tests/m4f1/run_all.sh` on C (isolated temp copy)

Run in a throwaway full copy at commit C so that `evidence/m4/m4f/`
(published M4-F evidence) is not rewritten. The freshly generated regression
evidence is preserved here under `evidence/m5/0a/`.

```
gates_total: 17
gates_passed: 17
gates_failed: 0
final_rc: 0
ALL GATES PASSED
```

All 17 gates rc=0 (schema foundation, JCS Profile, producer SD APIs, producer
two-connection concurrency, claim/heartbeat/fail state machines, atomic
completion APIs, purge and reference counting, build host runtime fixture,
release evidence negatives, release evidence unit tests, gate-log cleanup
counterexample, host Skill worker unit tests, text/cache/credential/attribution
hygiene, M4-F tracked whitespace, six-Skill full-chain Demo, AgentTeams
protocol E2E, M4-A~E legacy functional regression). Full per-gate rc list in
`m4f-regression-verification.txt`.

### Legacy functional regression — 6/6 MATCH

```
M4-A      tests/skills   win32 (m4a-venv)   75 passed                       MATCH
M4-B      tests/m4b      win32 (m4a-venv)   96 passed                       MATCH
M4-C      tests/m4c      win32 (m4a-venv)   87 passed                       MATCH
M4-D      tests/m4d      win32 (m4a-venv)   54 passed                       MATCH
M4-E-win  tests/m4e      win32 (m4a-venv)   166 passed / 3 skipped          MATCH
M4-E-posix tests/m4e     posix (container)  158 passed / 11 skipped         MATCH
legacy_regression_rc: 0
legacy_suites_matched: 6/6
```

### M5-0A Candidate integration — 13/13 PASS (`run_m5_candidate_integration.sh`)

Real Matrix `/sync` against mini homeserver; isolated labeled temp stack;
cleanup trap → residue 0/0/0. Gates: Candidate healthy (advisory lock + Matrix
login), 2nd Candidate advisory-lock denied, `/sync` consumed real M4F_RUN
event_id, event_id matches Matrix send return, sender is full
`@manager:<hs>`, stage_events reaches PROCESSED, independent
`controller_offsets` row, exactly six expected Skill jobs enqueued
(case-retrieval, diff-parse, pr-lifecycle, risk-classify, sast-scan,
test-runner), Gateway audit `caller_agent=m5coordinator`, `m5coordinator`
provenance rejects non-get PR reads (`M4F_PROVENANCE_CONTEXT_DENIED`),
Candidate leaves non-prefix stage/outbox rows untouched (cross_claim=0),
exact advisory-lock-backend disconnect → Candidate non-zero exit, production
`mergepilot-controller` PID/StartedAt unchanged.

### M5-0A unit tests — 68 passed (`tests/m5_0/test_m5_strict_parser.py`)

Strict parser (M4F_RUN / TASK_COMPLETED / verify), sender verification, config
validation, outbox SQL partition, M4F claim prefix scope, manager identity,
candidate self-exclusion, prefix overlap, prefix charset, raw-sender contract,
gateway-client parameterization.

## Delivery integrity

```
delivery_digest: b8a72b6dcf0435d4a1cd754b8f2a14a4a598ad5d6d69b8689f67ae28841d9c44
delivery_files: 56
delivery_scope: M4-F delivery surface (schema/runtime/controller/gateway/worker/tests-m4f1)
delivery_digest_check: OK (recomputed == stored)
```

Source SHA-256 (on C):
- controller: `55a896aad53fdc8103ee08577d47d259979259765c80d05816dbceabfe59ce57`
- gateway:    `a3cbb5a3de9807b2ebedaa46e8ca4083494f336238f3788e06fbff5c3b1aaf0b`

## Safety

```
secret_leaks: 0
residue: containers=0 networks=0 temp_dirs=0
hiclaw_live: False
external_credentials: False
```

Six production containers (`mergepilot-controller`, `policy-gw`, `audit-pg`,
`github-mcp`, `hiclaw-manager`, `hiclaw-controller`) remain present and were
not replaced, renamed, or deleted by the Candidate test stack. The integration
test recorded the production `mergepilot-controller` PID/StartedAt unchanged
across the run.

## Files in this directory

- `README.md` — this release record.
- `m4f-regression-verification.txt` — full 17-gate rc table from `run_all.sh` on C.
- `m4f-regression-agentteams-e2e.json` — AgentTeams E2E summary (six Skills, OTel spans).
- `m4f-regression-legacy.txt` — legacy 6/6 MATCH suite table.
- `m5-0a-candidate-integration.txt` — Candidate integration 13/13 gate extract.
- `m5-0a-unit-tests.txt` — strict-parser unit tests (68 passed).
