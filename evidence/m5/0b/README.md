# M5-0B Agent Handoff — Release Evidence

evidence head: `da2b9c7be713ea3008162d5dce7bca909aeedeff` (commit C: `feat: add M5-0B Agent handoff workflow`)
parent: `9a4bff9c3fcb7ff583f34c1ea9dee8699863428a` (commit I: `test: isolate Docker runners in MergePilot-Test`)
base / origin/main: `124831b45a5cdbeed6fce14824246073ae926166`
generated_at: 2026-08-05T15:10Z (committed C authoritative gate verification)

## Scope honesty

- **hiclaw_live = false.** M5-0B does NOT claim real HiClaw completion and does
  not assert any "real HiClaw is wired" statement.
- M5-0B closes the **Reviewer/Fixer/Verifier handoff reconciliation loop**
  (`reconcile_m5_skill_to_review`, `reconcile_m5_handoffs`,
  `advance_m5_review_run`) on top of the M5-0A ingress path. The Matrix
  Client-Server protocol exercises an **isolated throwaway mini homeserver**
  (`tests/m5_0/fixtures/mini_matrix_hs.py`), not the online HiClaw Matrix.
  No operator-provided Matrix credentials are used.
- The Policy Gateway and GitHub MCP are the real `gateway.py` and a stateful
  protocol-real fake GitHub MCP (SSE), not the production GitHub upstream.
- Agent decisions (Skill SUCCEEDED/FAILED, Verifier VERDICT) are
  **deterministically injected** via `tests/m5_0/fixtures/inject_skill_completion.py`,
  not a live LLM. This keeps the handoff gate reproducible.
- M5-0C/M5-0D are **not started**. `hiclaw_live=true` requires all 22 machine
  formulas satisfied (design doc section 19) and is deferred to M5-0D.
- All test secrets are **runtime-generated**; no real production tokens,
  passwords, registration tokens, or Matrix credentials are written into source
  or evidence.
- Docker test/production isolation: the test daemon (`MergePilot-Test`) is a
  separate `dockerd` + VHDX; the six production containers
  (`mergepilot-controller`, `policy-gw`, `audit-pg`, `github-mcp`,
  `hiclaw-manager`, `hiclaw-controller`) are invisible from the test daemon.
  `Ubuntu-22.04` (production HiClaw WSL) was **Stopped before AND after** the
  verification; production Docker was never accessed.

## Authoritative gates (re-verified ON commit C, not referenced from old logs)

### M4-F regression — `tests/m4f1/run_all_test.sh` on C (17/17)

Run via the official isolated wrapper (`run_all_test.sh` to `wsl_test.sh` to
`mp_launch.sh` to `run_all.sh` sourcing `mp_guard.sh`) inside `MergePilot-Test`.
Published `evidence/m4/m4f/**` was backed up out-of-repo before the run and
restored byte-exact afterward (see `m4f-regression-verification.txt`).

```
gates_total: 17
gates_passed: 17
gates_failed: 0
final_rc: 0
ALL GATES PASSED
```

### Legacy functional regression — 6/6 MATCH

```
M4-A       tests/skills   win32 (m4a-venv)    75 passed                 MATCH
M4-B       tests/m4b      win32 (m4a-venv)    96 passed                 MATCH
M4-C       tests/m4c      win32 (m4a-venv)    87 passed                 MATCH
M4-D       tests/m4d      win32 (m4a-venv)    54 passed                 MATCH
M4-E-win   tests/m4e      win32 (m4a-venv)   166 passed / 3 skipped     MATCH
M4-E-posix tests/m4e      posix (container)  158 passed / 11 skipped    MATCH
legacy_regression_rc: 0
legacy_suites_matched: 6/6
```

### Docker/WSL isolation — 11/11 PASS (`tests/test_env_isolation.sh`)

Guard fail-closes (rc=2) on wrong distro before any docker call; tcp:// and
arbitrary unix:// `DOCKER_HOST` rejected; guard passes inside MergePilot-Test;
canary visible only in test daemon; no production container visible; canary
EXIT-trap cleanup residue=0; `Ubuntu-22.04` Stopped before AND after. Full
gate list in `m5-0b-isolation.txt`.

### M5-0A — 22/22 PASS (re-verified on C)

- Candidate integration: 13/13 (real Matrix `/sync` to six-Skill DAG enqueue to
  PROCESSED; `m5coordinator` provenance; cross-claim isolation; lock-backend
  disconnect fail-closed).
- Container lifecycle: 5/5 (Candidate image bakes gateway parameterization +
  raw_sender/prefix-overlap fixes; GATEWAY_TOKEN authentication).
- Advisory lock: 4/4 (PG advisory-lock mechanics: mutual exclusion,
  session-scoped isolation, disconnect auto-release, label independence).

Detailed M5-0A evidence lives at `evidence/m5/0a/`.

### M5-0B — 27/27 PASS (handoff closed loop, re-verified on C)

- Handoff integration: 14/14 (`run_m5_0b_integration.sh`). Skill to review to
  fix to verify DAG: exactly one review stage + one reviewer dispatch; COMPLETED
  review to fix stage + fixer dispatch; fixer to verify stage + verifier
  dispatch; Verifier VERDICT=PASS to HOLD/m5_verify_passed; replay idempotency;
  VERDICT=BLOCKED/FAIL to HOLD/m5_verify_failed; PARTIAL (no VERDICT) does not
  finalize; non-m5live sentinel rows untouched; wrong-sender handoff
  fail-closed; no production container visible. Residue containers=0 networks=0.
  Full gate list in `m5-0b-handoff-e2e.txt`.
- Concurrency + negative: 13/13 (`run_m5_0b_concurrency.sh`). P1-1 concurrent
  reconcile (one bridge/stage/dispatch, no deadlock, no lock leak, no
  idle-in-transaction leak); P1-2 four binding negatives (zero stage/dispatch,
  no advance); P1-3 room mismatch to ERROR + HOLD not resumed; P1-4 dispatch/
  stage_run payload conflict to rollback (existing rows untouched);
  skill_failed to HOLD/m4f_skill_failed. Full gate list in `m5-0b-concurrency.txt`.

### M5-0B unit tests

```
tests/m5_0/test_m5_0b_delivery_digest.py:  71 passed
tests/m5_0/ (full):                       184 passed / 0 failed / 0 skipped
```

The task baseline expected `183 passed / 1 skipped`; on Windows the conditional
skip did not trigger and the test ran and passed, so 184/0 with no failure.

## Delivery integrity

```
delivery_digest: 918691c4364e851117b856339d326b9ea06e6b9e435b6dde03c256509dee9d06
delivery_files: 92
delivery_scope: M5-0B handoff + isolated test-daemon delivery surface, including M4-F regression base
delivery_digest_check: OK (recomputed == stored, two-pass stable)
manifest_check: OK (missing=0, unexpected=0)
m4f_digest: 7439b54883aafc92bf95bd414113ed2cf2fabf3e62406c367bdf077d4776a0ee
m4f_files: 57
m4f_subset_of_m5_0b: true (M4-F \ M5-0B = 0)
m4f1/delivery_digest.py: unmodified since Base
```

The delivery digest is computed by `tests/m5_0/m5_0b_delivery_digest.py`, which
reuses `tests/m4f1/delivery_digest.delivery_files` verbatim (definition
unchanged) and unions the M5-0B additions. Evidence under `evidence/m5/0b/` is
excluded from the surface (generated artifacts), so this evidence does not
influence the digest.

Source SHA-256 (on C):

- `tools/workflow-controller/controller.py`: `68166471ebcf05adc3a033c3cc5dcb5d7f1efd7b9187fce8a528e45cb7a2c832`
- `tools/handoff_watcher.py`:                   `d2af74f88e27a7ff7effedc4ec5c247fac9bbf5995474cb58a0ec54800eb6b7c`
- `tools/handoff_watcher_v2.py`:                `f0903dceea67887d0be30cf905df893cceb524b6331b2c1d80472479a15e997c`
- `config/souls/reviewer/SOUL.md`:              `6b371e4203645a608715a483c359b0b63d2fe3808f144f73ca32f0f306759a89`
- `config/souls/fixer/SOUL.md`:                 `209ae9473c27b65d26b6f391b41075ef5e2993c551f1eaea45dafed3123a6eca`
- `config/souls/verifier/SOUL.md`:              `3e133ec362b837098cd776557888ea4896caa7bc7b062d89775bb797272fef35`
- `tests/m5_0/m5_0b_delivery_digest.py`:        `479d8062bfb74c3dddf22eb9340013201652defe262c23e8a115f4c0004a34ad`

## Safety

```
secret_leaks: 0
residue: containers=0 networks=0 temp_dirs=0
hiclaw_live: False
external_credentials: False
ubuntu_22_04_before: Stopped
ubuntu_22_04_after: Stopped
production_docker_accessed: False
```

## Files in this directory

- `README.md` — this release record.
- `m5-0b-release-summary.json` — machine-readable consistency summary (head, parent, digests, gates, safety).
- `m5-0b-handoff-e2e.txt` — M5-0B handoff integration 14/14 gate extract.
- `m5-0b-concurrency.txt` — M5-0B concurrency + negative 13/13 gate extract.
- `m5-0b-delivery-digest.txt` — M5-0B delivery digest verification (digest, surface, manifest, M4-F subset).
- `m5-0b-isolation.txt` — Docker/WSL test-environment isolation 11/11 gate extract.
- `m5-0b-unit-tests.txt` — M5-0B unit tests (delivery digest 71, tests/m5_0 184).
- `m4f-regression-verification.txt` — full 17-gate rc table from `run_all.sh` on C.
- `m4f-regression-legacy.txt` — legacy 6/6 MATCH suite table.
