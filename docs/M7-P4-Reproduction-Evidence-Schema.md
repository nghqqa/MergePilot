# M7-P4 Reproduction Evidence Schema (v3 corrected)

**Status**: Design only — NOT yet generated
**Candidate path**: `evidence/m7/reproduction/demo-console-clean-replay.json`

## Schema Definition

All actual result fields are `null` in design. Expected values in `expected_gates`.

```json
{
  "kind": "m7-p4-clean-reproduction",
  "status": "DESIGN_ONLY",

  "reproduction_kind": "clean_checkout_offline_replay",

  "artifact_baseline_commit": "148762091447754a50790441144968a12360844f",
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

  "python_package_dependencies": [],
  "python_package_dependency_mode": "stdlib_only",
  "pip_install_required": false,
  "system_tool_dependencies": ["python", "git"],
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
  "historically_exercised_python_versions": ["3.9"],
  "clean_reproduction_verified_python_versions": [],
  "clean_reproduction_primary_python_version": null,
  "planned_primary_python_version": "3.9",
  "planned_primary_status": "PLANNED_NOT_YET_CLEAN_VERIFIED",

  "bundle_source_commit": null,
  "bundle_verification_commit": null,
  "bundle_sha256": null,
  "bundle_file_sha256": null,
  "evidence_file_sha256": [],

  "bundle_source_ref": null,
  "bundle_import_ref": null,
  "bundle_listed_refs": [],
  "source_ref_commit": null,
  "imported_ref_commit": null,
  "commit_object_available": null,
  "tree_object_available": null,
  "checkout_head": null,
  "checkout_head_matches_spec": null,
  "checkout_worktree_clean": null,

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
    "Python 3.8 compatibility NOT yet verified",
    "Git CLI required (bundle_builder uses subprocess git)",
    "Clean reproduction NOT yet executed on any Python version"
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

## Field Rules

### Commit provenance (4 distinct types)

| Field | Source | Design value |
|-------|--------|-------------|
| `artifact_baseline_commit` | The commit whose tree has frozen artifacts | `1487620...` (fixed) |
| `checkout_commit` | Actually checked out at reproduction | `null` (should = `reproduction_spec_commit` after merge) |
| `reproduction_spec_commit` | Commit with final Runbook/schema | `null` (frozen at PR merge) |
| `bundle_source_commit` | Read from frozen Bundle JSON | `null` (read as-is, not modified) |
| `bundle_verification_commit` | Read from frozen Bundle JSON | `null` (read as-is, not modified) |

### Dependency classification

| Field | Value | Rule |
|-------|-------|------|
| `python_package_dependencies` | `[]` | Zero third-party packages |
| `python_package_dependency_mode` | `"stdlib_only"` | All imports are stdlib |
| `pip_install_required` | `false` | No pip needed |
| `system_tool_dependencies` | `["python", "git"]` | Git CLI needed (subprocess) |
| `dependency_bootstrap_offline` | `null` | Verified at reproduction |

**Can claim**: "Zero third-party Python packages."
**Cannot claim**: "Zero external dependencies" (Git CLI required).

### Python version

| Field | Design value | Rule |
|-------|-------------|------|
| `candidate_minimum_python_version` | `"3.8"` | Hypothetical |
| `minimum_python_version_status` | `"NOT_YET_VERIFIED"` | Until tested on 3.8 |
| `historically_exercised_python_versions` | `["3.9"]` | Demo Console tests ran on 3.9.25 in dev |
| `clean_reproduction_verified_python_versions` | `[]` | Empty — no clean reproduction yet |
| `planned_primary_python_version` | `"3.9"` | Plan |
| `planned_primary_status` | `"PLANNED_NOT_YET_CLEAN_VERIFIED"` | Not yet done |

### Network measurement

| Field | Source | Design value |
|-------|--------|-------------|
| `external_reference_count` | HTML scan | `null` |
| `observed_external_network_requests` | Observer | `null` — `NOT_MEASURED` |
| `browser_network_observation_status` | — | `"NOT_MEASURED"` |

`external_reference_count = 0` (HTML scan) ≠ `observed_external_network_requests = 0` (network proof).

### Evidence SHA

SHA-256 of file **content bytes** from `git show REPRO_SPEC_COMMIT:path`.
NOT Git blob object ID (which includes `blob <size>\0` header).

## Expected vs Actual

- `expected_gates`: pre-registered values.
- Actual fields: `null` until executed.
- `all_ok`: `null` until all gates verified.
- `status`: `"DESIGN_ONLY"` → `"COMPLETED"` after reproduction.
