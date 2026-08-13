# M7-P4 Clean Environment Offline Reproduction — Design Freeze (v3 corrected)

**Status**: Frozen (design-only; no execution this round)
**Milestone**: M7-P4
**Artifact baseline commit**: `148762091447754a50790441144968a12360844f`
**Reproduction spec commit**: `null` (frozen when design PR merges)
**Branch**: `feat/m7-p4-clean-reproduction`
**Created**: 2026-08-13

## 1. Objective

Design a deterministic offline reproduction flow that rebuilds and verifies
the Demo Console REPLAY from a frozen commit/tag in a clean, isolated
directory — without relying on development artifacts, caches, or unmeasured
assumptions.

## 2. Clean Environment Definition

### 2.1 Source Cleanliness

- Clone or checkout at a pinned commit in a **new directory** outside the
  development worktree.
- Worktree must have zero modifications and zero untracked files.
- Do NOT copy generated artifacts from the development worktree.

### 2.2 Dependency Cleanliness

Demo Console has **zero third-party Python packages** — no pip install
required. However, it is NOT accurate to say "zero external dependencies."

#### Python package dependencies

| Field | Value |
|-------|-------|
| `python_package_dependencies` | `[]` (empty) |
| `python_package_dependency_mode` | `"stdlib_only"` |
| `pip_install_required` | `false` |

#### System tool dependencies

| Tool | Required by | Notes |
|------|------------|-------|
| Python interpreter (3.8+) | All modules | Runs schema/builder/render/serve/tests |
| Git CLI | `bundle_builder.py`, unittest suite, source checkout | `bundle_builder.py` calls `subprocess.check_output(["git","-C",root,"rev-parse","HEAD"])`; tests verify git blob SHAs |
| Web browser | Page display only | Not needed for schema/SHA/test verification |

#### NOT required

Docker, PostgreSQL, LLM API, SLS, GitHub (at runtime).

**Can claim**: "Zero third-party Python packages; no pip install needed."
**Cannot claim**: "Zero external dependencies" or "Only Python needed to
run all verification" (Git CLI is required for bundle builder and tests).

### 2.3 Runtime Cleanliness

- Do NOT read environment variables from the development worktree.
- Do NOT depend on Docker, PostgreSQL, LLM, SLS, or GitHub at runtime.
- REPLAY static HTML works with zero network.
- HTTP server binds to `127.0.0.1` only.

### 2.4 Evidence Integrity

- Read Bundle and evidence from the frozen source tree (git checkout).
- Do NOT regenerate historical evidence.
- SHA verification: compute SHA-256 over file **content bytes** read via
  `git show REPRO_SPEC_COMMIT:path`, compare with Bundle's recorded SHA-256.
- **File content SHA-256 ≠ Git blob object ID.** Git blob IDs include
  header bytes (`blob <size>\0`); content SHA does not.
- Historical evidence is NOT described as "this run's live results."

## 3. Reproduction Layers

### Layer A: Artifact Replay Reproduction

Validates:
- Static HTML found at `samples/demo-console/index.html`.
- DemoBundle schema valid (`mergepilot.demo-bundle.v1`).
- Bundle SHA recomputable (canonical JSON, volatile fields excluded).
- 5 evidence SHA-256 match (file content bytes from git blob).
- 8 pages present in HTML.
- `external_reference_count` from HTML static scan (not network proof).
- REPLAY mode boundary displayed.
- Findings/Fixes=0 honest explanation present.
- `secret_leaks` scanned.

`artifact_replay_offline` = `null` until formally verified.

### Layer B: Test Reproduction

Uses **unittest** (not pytest):
```
python -I -B -m unittest discover -s tests/demo_console -p "test_*.py" -v
```
- `-I`: isolated mode (no user site-packages, no PYTHONPATH).
- `-B`: no `__pycache__` generation.
- No pytest, no `.pytest_cache`.
- Result fields: `tests_run`, `test_failures`, `test_errors`, `test_skipped`.
- Expected (in `expected_gates`): `expected_tests_run = 33`.
- Actual fields `null` until execution.
- `test_execution_offline` = `null` until verified.
- `git diff --check = 0` and worktree clean after tests.

## 4. Source Acquisition Modes

| Mode | `source_acquisition_offline` | Notes |
|------|---:|---|
| GitHub HTTPS clone | `false` | Network used for source acquisition only; replay/tests offline |
| Pre-prepared verified git bundle | `true` | Bundle SHA verified before offline checkout |

Formal bundle must be based on the frozen `reproduction_spec_commit` (set
after the M7-P4 design PR merges), not the old `1487620`. The commit
`1487620` remains as `artifact_baseline_commit` only.

### Git bundle creation

The bundle is created from the named ref `origin/main` (not a bare SHA,
which git refuses to bundle standalone). Before creation, verify that
`origin/main` precisely equals the frozen `reproduction_spec_commit`.

**Do NOT use `git bundle create "$BUNDLE_PATH" "$REPRO_SPEC_COMMIT"`** —
a bare SHA produces `fatal: Refusing to create empty bundle`.

**`--all` works but includes all refs/tags** — use only as a diagnostic
fallback, not as the formal minimal reproduction bundle.

On a networked machine (POSIX):

```bash
# Set the reproduction spec commit (frozen after M7-P4 design PR merges)
REPRO_SPEC_COMMIT="REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
BUNDLE_PATH="mergepilot-${REPRO_SPEC_COMMIT}.bundle"

git fetch origin main
ACTUAL_MAIN="$(git rev-parse main)"
test "$ACTUAL_MAIN" = "$REPRO_SPEC_COMMIT"

git bundle create "$BUNDLE_PATH" main
git bundle verify "$BUNDLE_PATH"
sha256sum "$BUNDLE_PATH"
```

On a networked machine (Windows PowerShell):

```powershell
# Set the reproduction spec commit
$ReproSpecCommit = "REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
$BundlePath = "mergepilot-$ReproSpecCommit.bundle"

git fetch origin main
$ActualMain = (git rev-parse main).Trim()
if ($ActualMain -ne $ReproSpecCommit) {
    throw "local main does not match reproduction spec commit"
}

git bundle create $BundlePath main
if ($LASTEXITCODE -ne 0) { throw "git bundle create failed" }

git bundle verify $BundlePath
if ($LASTEXITCODE -ne 0) { throw "git bundle verify failed" }

Get-FileHash -Algorithm SHA256 $BundlePath
```

### Offline clone from bundle

Bundle clone may not set a default remote HEAD, so explicit detached
checkout is part of the formal flow.

On the offline machine (POSIX):

```bash
# Set the reproduction spec commit
REPRO_SPEC_COMMIT="REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
BUNDLE_PATH="mergepilot-${REPRO_SPEC_COMMIT}.bundle"
CHECKOUT_DIR="mergepilot-clean-reproduction"

# Re-verify SHA-256 matches recorded value
sha256sum "$BUNDLE_PATH"
# Verify bundle integrity
git bundle verify "$BUNDLE_PATH"
# Clone from bundle
git clone "$BUNDLE_PATH" "$CHECKOUT_DIR"
cd "$CHECKOUT_DIR"
# Detached checkout to the exact commit
git checkout --detach "$REPRO_SPEC_COMMIT"
# Verify HEAD matches
test "$(git rev-parse HEAD)" = "$REPRO_SPEC_COMMIT"
```

On the offline machine (Windows PowerShell):

```powershell
# Set the reproduction spec commit
$ReproSpecCommit = "REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
$BundlePath = "mergepilot-$ReproSpecCommit.bundle"
$CheckoutDir = "mergepilot-clean-reproduction"

Get-FileHash -Algorithm SHA256 $BundlePath
git bundle verify $BundlePath
git clone $BundlePath $CheckoutDir
Set-Location $CheckoutDir
git checkout --detach $ReproSpecCommit

$ActualHead = (git rev-parse HEAD).Trim()
if ($ActualHead -ne $ReproSpecCommit) {
    throw "checkout commit mismatch"
}
```

The offline machine must recompute the bundle SHA-256 and compare with the
value recorded before transfer. Mismatch = source corrupted.

## 5. Network Proof Semantics

| Field | Source | Design value |
|-------|--------|-------------|
| `external_reference_count` | HTML static regex scan | `null` until scanned |
| `observed_external_network_requests` | Browser/system network observer | `null` — `NOT_MEASURED` |
| `browser_network_observation_status` | — | `"NOT_MEASURED"` |
| `replay_succeeded_with_network_disabled` | Actual offline test | `null` |

**Rule**: `external_reference_count = 0` (HTML scan) does NOT equal
`observed_external_network_requests = 0` (network proof). Independent.

## 6. Python Version Boundary

| Field | Design value |
|-------|-------------|
| `candidate_minimum_python_version` | `"3.8"` |
| `minimum_python_version_status` | `"NOT_YET_VERIFIED"` |
| `historically_exercised_python_versions` | `["3.9"]` (Windows only — Demo Console tests run on 3.9.25) |
| `clean_reproduction_verified_python_versions` | `[]` (empty — no clean reproduction executed yet) |
| `clean_reproduction_primary_python_version` | `null` |
| `planned_primary_python_version` | `"3.9"` |
| `planned_primary_status` | `"PLANNED_NOT_YET_CLEAN_VERIFIED"` |

**Rules**:
- Python 3.9 is `HISTORICALLY_EXERCISED` (tests ran on 3.9.25 in development).
- Only a formal clean reproduction run can add to
  `clean_reproduction_verified_python_versions`.
- Python 3.8 is a candidate only — NOT verified.
- Python 3.10 is NOT listed in `historically_exercised_python_versions`
  for Demo Console (no Demo Console test run on 3.10 is traceable).
- `supported_python_versions` remains empty until clean verification.

## 7. Commit Provenance (4 distinct types)

| Field | Meaning | Design value |
|-------|---------|-------------|
| `artifact_baseline_commit` | The commit whose tree contains the frozen artifacts (HTML, Bundle, evidence) | `148762091447754a50790441144968a12360844f` |
| `checkout_commit` | The commit actually checked out for clean reproduction | `null` (measured at reproduction; should equal `reproduction_spec_commit` after merge) |
| `reproduction_spec_commit` | The commit containing the final Runbook/schema/runner | `null` (frozen when design PR merges to main) |
| `bundle_source_commit` | From frozen DemoBundle JSON | Read as-is from `bundle["source_commit"]` |
| `bundle_verification_commit` | From frozen DemoBundle JSON | Read as-is from `bundle["verification_commit"]` |

**Rules**:
- Formal reproduction should checkout `reproduction_spec_commit` (the final
  merged commit with these docs), not necessarily `artifact_baseline_commit`.
- Artifact files must match `artifact_baseline_commit` tree content.
- M7-P4 docs must come from `reproduction_spec_commit`.
- Bundle internal C4 provenance (`bundle_source_commit` /
  `bundle_verification_commit`) is NOT modified for this reproduction.

### Provenance Execution Order

1. Merge M7-P4 design PR to main.
2. Freeze the PR merge commit as `reproduction_spec_commit`.
3. Create a git bundle containing that commit.
4. On the offline machine, verify the bundle file SHA-256 matches the
   recorded value.
5. Checkout `reproduction_spec_commit`.
6. Verify artifact files (HTML, Bundle, evidence) still match
   `artifact_baseline_commit` (`1487620`) tree content.
7. Do NOT modify DemoBundle internal C4 provenance.

## 8. Residue Design

6 independent measurements:

| Field | How measured |
|-------|-------------|
| `pycache_residue` | Count `__pycache__` dirs after run (use `-B` to prevent) |
| `test_cache_residue` | Count `.pytest_cache` dirs (none if using unittest) |
| `temp_file_residue` | Count temp files created by reproduction |
| `server_process_residue` | Verify HTTP server PID terminated |
| `listening_port_residue` | Verify port 8080 not listening after server stop |
| `worktree_dirty_after` | `git status --porcelain` after tests |

**No subtract-one heuristic.** Each measured independently.

HTTP server lifecycle:
1. Record server PID at start.
2. Bind to `127.0.0.1` only.
3. On shutdown, verify PID no longer exists.
4. Verify port not listening.

## 9. Design-Phase Evidence Status

All actual result fields are `null`. Expected values in `expected_gates`.

```json
{
  "status": "DESIGN_ONLY",
  "checkout_commit": null,
  "reproduction_spec_commit": null,
  "clean_reproduction_verified_python_versions": [],
  "dependency_bootstrap_offline": null,
  "test_execution_offline": null,
  "artifact_replay_offline": null,
  "tests_run": null,
  "observed_external_network_requests": null,
  "all_ok": null,
  "expected_gates": {
    "expected_tests_run": 33,
    "expected_test_failures": 0,
    "expected_external_reference_count": 0,
    "expected_page_count": 8
  }
}
```

## 10. Failure Semantics

| Status | Meaning |
|--------|---------|
| `SOURCE_CHECKOUT_FAILED` | Clean checkout failed |
| `COMMIT_MISMATCH` | HEAD SHA ≠ expected commit |
| `DEPENDENCY_NOT_OFFLINE` | Required network for deps |
| `BUNDLE_HASH_MISMATCH` | Bundle SHA recomputation failed |
| `EVIDENCE_HASH_MISMATCH` | Evidence content SHA ≠ recorded |
| `SCHEMA_INVALID` | Bundle schema validation failed |
| `PAGE_MISSING` | Expected HTML page not found |
| `EXTERNAL_NETWORK_DETECTED` | Network observer detected requests |
| `TEST_FAILURE` | One or more unittest cases failed |
| `SECRET_LEAK` | Secret pattern detected |
| `RESIDUE_DETECTED` | Temp/cache/process/port left after run |
| `WORKTREE_DIRTY` | Worktree modified after tests |
| `PLATFORM_NOT_SUPPORTED` | OS/Python not supported |

All failures recorded independently.

## 11. Conclusion

- **No blocking issues identified** for design freeze.
- Formal reproduction requires a working Python interpreter and Git CLI.
- The minimum supported Python version is **NOT yet verified**.
- Plan: first execute clean reproduction on **Python 3.9**.
- "stdlib-only" means zero third-party Python packages; it does NOT mean
  "only Python needed" — Git CLI is a required system tool.
- Formal offline conclusions must wait until actual execution.

## 12. Not Yet Done

- ❌ Formal clean reproduction NOT executed
- ❌ Candidate evidence NOT generated
- ❌ Demo video NOT recorded
- ❌ M7 NOT overall closed
- ❌ ISOLATED_LIVE NOT implemented
- ❌ Production management dashboard NOT built
- ❌ Python 3.8 compatibility NOT verified
- ❌ Clean reproduction on any Python version NOT yet done
