# M7-P4 Reproduction Evidence Schema

**Status**: Design only — NOT yet generated
**Candidate path**: `evidence/m7/reproduction/demo-console-clean-replay.json`

## Schema Definition

```json
{
  "kind": "m7-p4-clean-reproduction",
  "reproduction_kind": "clean_checkout_offline_replay",
  "source_commit": "148762091447754a50790441144968a12360844f",
  "source_tag": "m7-p3-demo-console-closed",
  "clean_checkout_path_kind": "isolated_directory_outside_dev_worktree",

  "platform": "windows | posix",
  "os_version": "Windows 10 10.0.26200 | Ubuntu 22.04",
  "python_version": "3.9.25 | 3.10.12",

  "dependency_mode": "stdlib_only_no_pip",
  "dependency_lock_sha256": null,
  "dependency_bootstrap_requires_network": false,

  "artifact_replay_offline": true,
  "test_reproduction_offline": true,

  "bundle_sha256": "313fce1d30a1ed3ea04f68d77b56a2a2261f23fcf71546f3982deae2d27e11b9",
  "bundle_file_sha256": "<SHA-256 of the bundle JSON file bytes>",
  "evidence_sha256": [
    {"path": "evidence/m4/m4f/agentteams-demo-summary.json", "sha256": "783bd4c7..."},
    {"path": "evidence/m4/m4f/full-chain-e2e.json", "sha256": "7849c4e7..."},
    {"path": "evidence/m6/rag/pgvector-isolated-verification.json", "sha256": "3eb864b4..."},
    {"path": "evidence/m7/benchmark/rag-n20-confirmatory.json", "sha256": "36edc664..."},
    {"path": "evidence/m7/benchmark/rag-n20-offline.json", "sha256": "c0d53c56..."}
  ],

  "page_count": 8,
  "pages": ["overview","timeline","findings","rag","trace","safety","evidence","benchmark"],

  "test_passed": 33,
  "test_failed": 0,

  "external_network_requests": 0,
  "secret_leaks": 0,
  "temp_file_residue": 0,
  "worktree_clean_after": true,

  "deterministic_replay_match": true,

  "all_ok": true,
  "limitations": [
    "REPLAY mode only — ISOLATED_LIVE not implemented",
    "Findings/Fixes=0 — evidence stores digests, not inline findings",
    "runtime_consumes_rag_context=false — RAG advisory only",
    "workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME",
    "Demo Console is NOT a production management dashboard",
    "M7 NOT overall closed"
  ],

  "failures": [],
  "timestamp": "<ISO-8601 at reproduction time>"
}
```

## Field Rules

| Field | Type | Required | Rule |
|-------|------|----------|------|
| `reproduction_kind` | str | yes | Must be `clean_checkout_offline_replay` |
| `source_commit` | str | yes | Must match pinned SHA |
| `source_tag` | str | yes | Must be existing annotated tag |
| `clean_checkout_path_kind` | str | yes | Must be `isolated_directory_outside_dev_worktree` |
| `dependency_mode` | str | yes | `stdlib_only_no_pip` for this project |
| `dependency_bootstrap_requires_network` | bool | yes | Must be `false` |
| `artifact_replay_offline` | bool | yes | Layer A result |
| `test_reproduction_offline` | bool | yes | Layer B result |
| `bundle_sha256` | str | yes | Internal canonical SHA from bundle |
| `bundle_file_sha256` | str | yes | SHA of file bytes |
| `evidence_sha256` | array | yes | 5 entries, each with path+sha256 |
| `page_count` | int | yes | Must be 8 |
| `test_passed` | int | yes | Expected 33 |
| `test_failed` | int | yes | Must be 0 |
| `external_network_requests` | int | yes | Must be 0 |
| `secret_leaks` | int | yes | Must be 0 |
| `temp_file_residue` | int | yes | Must be 0 |
| `worktree_clean_after` | bool | yes | Must be true |
| `deterministic_replay_match` | bool | yes | Two runs must match |
| `all_ok` | bool | yes | All gates pass |
| `limitations` | array | yes | Must list all honest boundaries |
| `failures` | array | yes | List of failure status codes (empty if all_ok) |

## Failure Recording

Each failure in `failures[]` must have:
```json
{
  "status": "BUNDLE_HASH_MISMATCH",
  "expected": "313fce1d...",
  "actual": "abcdef12...",
  "detail": "Recomputed SHA does not match stored value"
}
```

Failures are never hidden by `all_ok`.
