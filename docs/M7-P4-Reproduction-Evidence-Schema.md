# M7-P4 Reproduction Evidence Schema (v2 corrected)

**Status**: Design only — NOT yet generated
**Candidate path**: `evidence/m7/reproduction/demo-console-clean-replay.json`

## Schema Definition

All actual result fields are `null` in the design schema.
Expected values go in `expected_gates`.

```json
{
  "kind": "m7-p4-clean-reproduction",
  "status": "DESIGN_ONLY",

  "reproduction_kind": "clean_checkout_offline_replay",

  "checkout_commit": null,
  "reproduction_spec_commit": null,
  "source_tag": "m7-p3-demo-console-closed",
  "clean_checkout_path_kind": "isolated_directory_outside_dev_worktree",

  "platform": null,
  "os_version": null,
  "python_version": null,

  "source_acquisition_mode": null,
  "source_acquisition_offline": null,
  "source_archive_sha256": null,

  "dependency_bootstrap_offline": null,
  "dependency_bootstrap_requires_network": null,
  "test_execution_offline": null,
  "artifact_replay_offline": null,

  "browser_network_observation_status": "NOT_MEASURED",
  "observed_external_network_requests": null,
  "replay_succeeded_with_network_disabled": null,
  "external_reference_count": null,

  "candidate_minimum_python_version": "3.8",
  "minimum_python_version_status": "NOT_YET_VERIFIED",
  "supported_python_versions": [],
  "primary_windows_version": null,
  "posix_version": null,

  "bundle_source_commit": null,
  "bundle_verification_commit": null,
  "bundle_sha256": null,
  "bundle_file_sha256": null,
  "evidence_file_sha256": [],

  "page_count": null,
  "pages": [],

  "tests_run": null,
  "test_failures": null,
  "test_errors": null,
  "test_skipped": null,

  "pycache_residue": null,
  "test_cache_residue": null,
  "temp_file_residue": null,
  "server_process_residue": null,
  "listening_port_residue": null,
  "worktree_dirty_after": null,

  "secret_leaks": null,
  "deterministic_replay_match": null,

  "all_ok": null,
  "failures": [],
  "limitations": [
    "REPLAY mode only — ISOLATED_LIVE not implemented",
    "Findings/Fixes=0 — evidence stores digests, not inline findings",
    "runtime_consumes_rag_context=false — RAG advisory only",
    "workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME",
    "Demo Console is NOT a production management dashboard",
    "M7 NOT overall closed",
    "Python 3.8 compatibility NOT yet verified (only 3.9 tested)"
  ],

  "expected_gates": {
    "expected_tests_run": 33,
    "expected_test_failures": 0,
    "expected_test_errors": 0,
    "expected_external_reference_count": 0,
    "expected_page_count": 8,
    "expected_secret_leaks": 0,
    "expected_pycache_residue": 0,
    "expected_server_process_residue": 0
  },

  "timestamp": null
}
```

## Provenance Field Rules

| Field | Source | Rule |
|-------|--------|------|
| `checkout_commit` | Measured at checkout | The origin/main commit checked out |
| `reproduction_spec_commit` | Current HEAD | Commit containing this Runbook/runner |
| `bundle_source_commit` | Read from frozen Bundle JSON | `bundle["source_commit"]` — NOT modified |
| `bundle_verification_commit` | Read from frozen Bundle JSON | `bundle["verification_commit"]` — NOT modified |
| `bundle_sha256` | Read from frozen Bundle JSON | `bundle["bundle_sha256"]` — internal canonical SHA |
| `bundle_file_sha256` | Computed at reproduction | SHA-256 of bundle file bytes on disk |
| `evidence_file_sha256[]` | Computed at reproduction | SHA-256 of file **content** from `git show <commit>:<path>` |

**Critical**: File content SHA-256 ≠ Git blob object ID. Git blob IDs
include header bytes (`blob <size>\0`). Evidence SHAs are computed over
raw file content only.

## Source Acquisition Modes

| Mode | `source_acquisition_offline` | When |
|------|------------------------------|------|
| GitHub HTTPS clone | `false` | Network used for clone; replay/tests still offline |
| Pre-prepared git bundle | `true` | Must record `source_archive_sha256` |

## Network Measurement Rules

| Field | Source | Design value |
|-------|--------|-------------|
| `external_reference_count` | HTML static regex scan | `null` until scanned at reproduction |
| `observed_external_network_requests` | Browser log / system observer / isolated net | `null` — NOT_MEASURED |
| `browser_network_observation_status` | — | `"NOT_MEASURED"` |
| `replay_succeeded_with_network_disabled` | Actual offline test | `null` until tested |

**Rule**: `external_reference_count = 0` (HTML scan) does NOT equal
`observed_external_network_requests = 0` (network proof). These are
independent measurements.

## Expected vs Actual

- `expected_gates`: Pre-registered expected values (e.g., `expected_tests_run: 33`).
- Actual result fields (e.g., `tests_run`): `null` until formally executed.
- `all_ok`: `null` until all gates verified at reproduction time.
- `status`: `"DESIGN_ONLY"` until reproduction, then `"COMPLETED"`.

## Failure Recording

Each failure in `failures[]`:
```json
{
  "status": "BUNDLE_HASH_MISMATCH",
  "expected": "313fce1d...",
  "actual": "abcdef12...",
  "detail": "Recomputed SHA does not match stored value"
}
```

Failures never hidden by `all_ok`. If `failures` is non-empty, `all_ok` must be `false`.
