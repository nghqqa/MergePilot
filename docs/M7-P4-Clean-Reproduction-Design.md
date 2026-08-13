# M7-P4 Clean Environment Offline Reproduction — Design Freeze

**Status**: Frozen (design-only; no execution this round)
**Milestone**: M7-P4
**Base**: `1487620` (origin/main, M7-P3 Showcase merged)
**Created**: 2026-08-13

## 1. Objective

Design a deterministic offline reproduction flow that rebuilds and verifies
the Demo Console REPLAY from a frozen commit/tag in a clean, isolated
directory — without relying on any development artifacts, caches, or network.

## 2. Clean Environment Definition

### 2.1 Source Cleanliness

- Clone or checkout from `origin/main` at a pinned SHA (`1487620`).
- Work in a **new directory** — not inside the current development worktree.
- Worktree must have zero modifications and zero untracked files.
- Do NOT copy generated artifacts from the development worktree.

### 2.2 Dependency Cleanliness

**Key finding**: Demo Console is **100% Python stdlib** — no pip packages
required. `bundle_builder.py`, `render.py`, `serve.py`, `schema.py` import
only: `json`, `hashlib`, `os`, `re`, `sys`, `time`, `subprocess`, `pathlib`,
`http.server`, `socketserver`, `argparse`, `tempfile`, `unittest`.

- No venv required (but recommended for isolation).
- No requirements.txt needed (no third-party deps).
- No lock file needed.
- `dependency_bootstrap_requires_network = false`.
- If a venv IS used, it must be freshly created in the clean directory.

### 2.3 Runtime Cleanliness

- Do NOT read environment variables from the development worktree.
- Do NOT depend on Docker, PostgreSQL, LLM, SLS, or GitHub.
- REPLAY static HTML must work with zero network.
- The local HTTP server (`serve.py`) binds to `127.0.0.1` only.

### 2.4 Evidence Integrity

- Read Bundle and evidence from the frozen source tree (git checkout).
- Do NOT regenerate historical evidence.
- SHA verification targets the frozen files, not newly generated ones.
- Historical evidence is NOT described as "this run's live results."

## 3. Reproduction Layers

### Layer A: Artifact Replay Reproduction (offline)

Validates:
- Static HTML found at `samples/demo-console/index.html`.
- DemoBundle schema valid (`mergepilot.demo-bundle.v1`).
- Bundle SHA recomputable.
- 5 evidence SHA match actual git blob content.
- 8 pages present in HTML.
- Zero CDN, external scripts, API, or network dependencies.
- REPLAY mode boundary displayed.
- Findings/Fixes=0 honest explanation present.
- `secret_leaks=0`, `residue=0`.

`artifact_replay_offline = true` (no network needed at any point).

### Layer B: Test Reproduction (offline)

Validates:
- Python 3.8+ available (stdlib only).
- `python -m pytest tests/demo_console/test_demo_console.py` passes.
- Expected baseline: **33 passed, 0 failed**.
- `git diff --check = 0` after tests.
- Worktree clean after tests.
- No permanent cache/temp residue.
- If test count changes (e.g., new design tests added), the delta must be
  explained from commit history — never silently re-baselined.

`test_reproduction_offline = true` (stdlib only, no pip install needed).
`dependency_bootstrap_requires_network = false`.

## 4. Determinism Guarantees

- Bundle SHA is computed from canonical JSON (sorted keys, UTF-8).
- Volatile fields (`bundle_sha256`, `generated_at`) excluded from hash.
- Two independent clean checkouts produce identical bundle SHA.
- HTML is deterministic (no random IDs, no timestamps).

## 5. Isolation & Contamination Prevention

- Reproduction directory must NOT be inside `D:\goai\`.
- Do NOT reference existing venvs under `D:\goai\`.
- Do NOT inherit sensitive env vars (PAT, LLM key, SLS credentials).
- Test-generated `__pycache__` and `.pytest_cache` cleaned after measurement.
- Residue measured independently (not subtract-one heuristic).
- `origin/main` not modified.
- Frozen Bundle/evidence not modified.

## 6. Failure Semantics

| Status | Meaning |
|--------|---------|
| `SOURCE_CHECKOUT_FAILED` | Clean checkout failed |
| `COMMIT_MISMATCH` | HEAD SHA ≠ pinned SHA |
| `DEPENDENCY_NOT_OFFLINE` | Required network for deps |
| `DEPENDENCY_HASH_MISMATCH` | Lock file hash mismatch |
| `BUNDLE_HASH_MISMATCH` | Bundle SHA recomputation failed |
| `EVIDENCE_HASH_MISMATCH` | Evidence SHA ≠ git blob SHA |
| `SCHEMA_INVALID` | Bundle schema validation failed |
| `PAGE_MISSING` | Expected HTML page not found |
| `EXTERNAL_NETWORK_DETECTED` | External resource referenced |
| `TEST_FAILURE` | One or more tests failed |
| `SECRET_LEAK` | Secret pattern detected |
| `RESIDUE_DETECTED` | Temp/cache files left after run |
| `WORKTREE_DIRTY` | Worktree modified after tests |
| `PLATFORM_NOT_SUPPORTED` | OS/Python not supported |

All failures recorded independently — `all_ok` never hides individual failures.

## 7. Deliverables (this round)

- `docs/M7-P4-Clean-Reproduction-Design.md` (this file)
- `docs/M7-P4-Clean-Reproduction-Runbook.md`
- `docs/M7-P4-Reproduction-Evidence-Schema.md`
- `docs/M7-P4-Reproduction-Platform-Matrix.md`

## 8. Not Yet Done

- ❌ Formal clean reproduction NOT executed
- ❌ Candidate evidence NOT generated
- ❌ Demo video NOT recorded
- ❌ M7 NOT overall closed
- ❌ ISOLATED_LIVE NOT implemented
- ❌ Production management dashboard NOT built

## 9. Blocking Issues

**None identified.** Demo Console is stdlib-only, artifacts are in git, SHAs
verified from origin/main. Clean reproduction is executable with Python 3.8+
and a git checkout — no external dependencies.
