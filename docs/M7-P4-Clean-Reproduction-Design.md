# M7-P4 Clean Environment Offline Reproduction — Design Freeze (v2 corrected)

**Status**: Frozen (design-only; no execution this round)
**Milestone**: M7-P4
**Base**: `1487620` (origin/main)
**Branch**: `feat/m7-p4-clean-reproduction`
**Created**: 2026-08-13

## 1. Objective

Design a deterministic offline reproduction flow that rebuilds and verifies
the Demo Console REPLAY from a frozen commit/tag in a clean, isolated
directory — without relying on development artifacts, caches, or unmeasured
assumptions.

## 2. Clean Environment Definition

### 2.1 Source Cleanliness

- Clone or checkout from `origin/main` at pinned SHA (`1487620`).
- Work in a **new directory** outside the development worktree.
- Worktree must have zero modifications and zero untracked files.
- Do NOT copy generated artifacts from the development worktree.

### 2.2 Dependency Cleanliness

Demo Console is **100% Python stdlib** — no pip packages required. All four
modules (`schema.py`, `bundle_builder.py`, `render.py`, `serve.py`) import
only stdlib modules (`json`, `hashlib`, `os`, `re`, `sys`, `time`,
`subprocess`, `pathlib`, `http.server`, `socketserver`, `argparse`,
`tempfile`, `unittest`).

- No venv required (but recommended for isolation).
- No requirements.txt or lock file needed.
- `dependency_bootstrap_offline = true` (verified at reproduction time).
- `dependency_bootstrap_requires_network = false`.

### 2.3 Runtime Cleanliness

- Do NOT read environment variables from the development worktree.
- Do NOT depend on Docker, PostgreSQL, LLM, SLS, or GitHub at runtime.
- REPLAY static HTML must work with zero network.
- The local HTTP server (`serve.py`) binds to `127.0.0.1` only.

### 2.4 Evidence Integrity

- Read Bundle and evidence from the frozen source tree (git checkout).
- Do NOT regenerate historical evidence.
- SHA verification: compute SHA-256 over file **content bytes** read via
  `git show <commit>:<path>`, compare with Bundle's recorded SHA-256.
- **Do NOT confuse file content SHA-256 with Git blob object ID.**
- Historical evidence is NOT described as "this run's live results."

## 3. Reproduction Layers

### Layer A: Artifact Replay Reproduction

Validates:
- Static HTML found at `samples/demo-console/index.html`.
- DemoBundle schema valid (`mergepilot.demo-bundle.v1`).
- Bundle SHA recomputable (canonical JSON, volatile fields excluded).
- 5 evidence SHA-256 match (file content bytes from git blob).
- 8 pages present in HTML.
- `external_reference_count = 0` (HTML static scan — NOT network observation).
- REPLAY mode boundary displayed.
- Findings/Fixes=0 honest explanation present.
- `secret_leaks=0`.

`artifact_replay_offline` = null until formally verified.

### Layer B: Test Reproduction

Validates using **unittest** (not pytest):
```
python -I -B -m unittest discover -s tests/demo_console -p "test_*.py" -v
```
- `-I`: isolated mode (no user site-packages, no PYTHONPATH).
- `-B`: no `__pycache__` generation.
- No pytest, no `.pytest_cache`.
- Expected baseline: `expected_tests_run = 33`.
- Actual results (`tests_run`, `test_failures`, `test_errors`,
  `test_skipped`) filled only after execution.
- `git diff --check = 0` after tests.
- Worktree clean after tests.
- `test_execution_offline = true` (stdlib only, verified at reproduction).

## 4. Source Acquisition Modes

| Mode | `source_acquisition_offline` | Notes |
|------|------------------------------|-------|
| GitHub HTTPS clone | `false` | Network used only for source fetch; subsequent replay/tests offline |
| Pre-prepared git bundle/archive | `true` | Must record `source_archive_sha256`; archive from pinned origin/main commit |

Design phase: source acquisition mode is chosen at reproduction time.

## 5. Network Proof Semantics

| Field | Source | Design value |
|-------|--------|-------------|
| `external_reference_count` | HTML static scan (regex for `src="https://..."`, `<link>`, CDN) | `0` (scanned, verifiable) |
| `observed_external_network_requests` | Browser network log / system network observer / isolated network env | `null` (NOT_MEASURED in design) |
| `browser_network_observation_status` | — | `"NOT_MEASURED"` |
| `replay_succeeded_with_network_disabled` | Actual offline test | `null` until verified |

**Rule**: HTML static scan showing 0 external references does NOT prove
zero network requests. Only a network observer or disabled-network test
can prove that.

## 6. Python Version Boundary

| Field | Design value | Rule |
|-------|-------------|------|
| `candidate_minimum_python_version` | `"3.8"` | Hypothetical (f-strings, `__future__`) |
| `minimum_python_version_status` | `"NOT_YET_VERIFIED"` | Until tested on 3.8 |
| `supported_python_versions` | `[]` (empty) | Filled only after actual platform testing |
| `primary_windows_version` | `null` | Measured at reproduction |
| `posix_version` | `null` | Measured at reproduction |

**Verified so far**: Python 3.9.25 (Windows). Only 3.9 may be claimed as
verified until 3.8 is actually tested.

## 7. Provenance Fields

| Field | Meaning | Source |
|-------|---------|--------|
| `checkout_commit` | The origin/main commit checked out for this reproduction | Measured at checkout |
| `reproduction_spec_commit` | The commit containing this Runbook/runner code | Current HEAD |
| `bundle_source_commit` | From frozen DemoBundle JSON | Read as-is from `bundle["source_commit"]` |
| `bundle_verification_commit` | From frozen DemoBundle JSON | Read as-is from `bundle["verification_commit"]` |
| `bundle_sha256` | Internal canonical SHA from bundle | Read from `bundle["bundle_sha256"]` |
| `bundle_file_sha256` | SHA-256 of bundle file bytes | Computed at reproduction |
| `evidence_file_sha256[]` | SHA-256 of each evidence file's content | Computed from `git show <commit>:<path>` bytes |

**Rule**: Bundle's internal commit fields are NOT modified for this
reproduction. They are read as-is from the frozen artifact.

## 8. Residue Design

Independent measurements:

| Field | How measured |
|-------|-------------|
| `pycache_residue` | Count `__pycache__` dirs after run (use `-B` to prevent) |
| `test_cache_residue` | Count `.pytest_cache` dirs (none if using unittest) |
| `temp_file_residue` | Count temp files created by reproduction |
| `server_process_residue` | Verify HTTP server PID terminated |
| `listening_port_residue` | Verify port 8080 not listening after server stop |
| `worktree_dirty_after` | `git status --porcelain` after tests |

**No subtract-one heuristic.** Each measured independently before and after.

HTTP server lifecycle:
1. Record server PID at start.
2. Bind to `127.0.0.1` only.
3. On shutdown (Ctrl+C), verify PID no longer exists.
4. Verify port not listening.
5. All checks must pass for `server_process_residue = 0`.

## 9. Design-Phase Evidence Example

All actual result fields are `null` in the design schema. Expected values
go in `expected_gates`.

```json
{
  "status": "DESIGN_ONLY",
  "tests_run": null,
  "test_failures": null,
  "test_errors": null,
  "test_skipped": null,
  "external_network_requests": null,
  "artifact_replay_offline": null,
  "test_reproduction_offline": null,
  "all_ok": null,
  "expected_gates": {
    "expected_tests_run": 33,
    "expected_test_failures": 0,
    "expected_test_errors": 0,
    "expected_external_reference_count": 0,
    "expected_page_count": 8,
    "expected_secret_leaks": 0
  }
}
```

## 10. Failure Semantics

| Status | Meaning |
|--------|---------|
| `SOURCE_CHECKOUT_FAILED` | Clean checkout failed |
| `COMMIT_MISMATCH` | HEAD SHA ≠ pinned SHA |
| `DEPENDENCY_NOT_OFFLINE` | Required network for deps |
| `BUNDLE_HASH_MISMATCH` | Bundle SHA recomputation failed |
| `EVIDENCE_HASH_MISMATCH` | Evidence SHA ≠ git blob content SHA |
| `SCHEMA_INVALID` | Bundle schema validation failed |
| `PAGE_MISSING` | Expected HTML page not found |
| `EXTERNAL_NETWORK_DETECTED` | Network observer detected requests |
| `TEST_FAILURE` | One or more unittest cases failed |
| `SECRET_LEAK` | Secret pattern detected |
| `RESIDUE_DETECTED` | Temp/cache/process/port left after run |
| `WORKTREE_DIRTY` | Worktree modified after tests |
| `PLATFORM_NOT_SUPPORTED` | OS/Python not supported |

All failures recorded independently — `all_ok` never hides individual failures.

## 11. Isolation & Contamination Prevention

- Reproduction directory NOT inside `D:\goai\`.
- No reference to existing venvs under `D:\goai\`.
- No inherited sensitive env vars (PAT, LLM key, SLS credentials).
- `-I` flag prevents `PYTHONPATH` and user site-packages contamination.
- Test artifacts cleaned after measurement.
- `origin/main` not modified.
- Frozen Bundle/evidence not modified.

## 12. Not Yet Done

- ❌ Formal clean reproduction NOT executed
- ❌ Candidate evidence NOT generated
- ❌ Demo video NOT recorded
- ❌ M7 NOT overall closed
- ❌ ISOLATED_LIVE NOT implemented
- ❌ Production management dashboard NOT built
- ❌ Python 3.8 compatibility NOT verified (only 3.9 tested)

## 13. Blocking Issues

**None identified.** Demo Console is stdlib-only, artifacts in git, SHAs
verified from origin/main. Clean reproduction needs Python 3.8+ and git
checkout — no external dependencies at runtime.
