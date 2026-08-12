# M7 RAG N≥20 Benchmark — Design Freeze (v2, corrected)

**Status**: Frozen — Layer A complete, Layer B NOT_MEASURABLE
**Milestone**: M7-P2 candidate
**Base commit**: `9a9fc0a` (origin/main, M6-RAG merged)
**Dataset version**: `rag-bench-v2`

## Critical audit conclusion

**`core.scan()` and `core.run()` do NOT consume RAG retrieval results.**

- `core.scan(inp, trusted_workspace=None, ruleset=None, expected_rules_version=None, today=None, deadline=None)` — no RAG parameter
- `core.run(inp, *, adapter=None, trusted_env=None, deadline=None)` — no RAG parameter

RAG results are emitted as advisory `evidence[]` (`kind: "rag_advisory"`, `adopted: false`, `untrusted: true`) but are **never passed to the core decision logic**. Therefore:

- This benchmark **cannot** and **must not** claim "RAG improves Reviewer/Fixer decision accuracy."
- Layer B (Workflow Utility) is `NOT_MEASURABLE_WITH_CURRENT_RUNTIME`.

## Two-layer design

### Layer A: Retrieval & Integration Benchmark (this round)

Measures **RAG retrieval quality and integration safety** — not decision accuracy.

**Retrieval metrics:**
- `hit_at_1`, `hit_at_3`, `mean_reciprocal_rank`
- `scope_leak_count` (must be 0)
- `error_citation_count`
- `empty_count`, `fallback_count`, `timeout_count`, `malformed_count`
- `latency_p50_ms`, `latency_p95_ms`

**Integration metrics:**
- `evidence_schema_valid_rate` (must be 1.0)
- `verifier_executed_rate` (must be 1.0 — RAG never skips verifier)
- `secret_leaks` (must be 0)
- `residue` (must be 0)
- `deterministic_replay_match` (must be true)

### Layer B: Workflow Utility Benchmark (future)

Only executable when RAG advisory context is explicitly consumed by
`core.scan`/`core.run` decision logic.

**Current status:** `NOT_MEASURABLE_WITH_CURRENT_RUNTIME`

**Reason:** retrieval results are emitted as advisory evidence but are not
consumed by core.scan/core.run decision logic.

**Workflow utility metrics (all null until Layer B):**
- `reviewer_accuracy_baseline`, `reviewer_accuracy_rag`
- `fixer_accuracy_baseline`, `fixer_accuracy_rag`
- `decision_accuracy_delta`, `finding_f1_delta`, `adoption_rate`

These fields are `null` — never `0` or inferred.

## Dataset (rag-bench-v2)

- **unique_case_count**: 29 (≥ 20)
- **paired_run_count**: 29 (≥ 20)
- **total_arm_executions**: 58 (≥ 40)
- **knowledge_base_cases**: 20
- **deterministic_seed**: 42

### Category coverage (17 groups, no singleton dominance)

| Group | Samples |
|-------|---------|
| clean | 2 |
| hardcoded_secret | 2 |
| sql_injection | 2 |
| command_injection | 2 |
| path_traversal | 2 |
| dependency_vulnerability | 2 |
| test_failure | 2 |
| configuration_risk | 2 |
| prompt_injection | 2 |
| rollback_risk | 2 |
| false_positive_allowlist | 1 |
| no_history | 1 |
| empty_retrieval | 1 |
| timeout | 2 |
| adapter_unavailable | 2 |
| malformed_result | 1 |
| cross_repo_adversarial | 1 |

## Gold-label isolation

Query text sent to adapters **never** contains:
- Gold case IDs
- Expected categories/severities
- Expected fix text
- Evaluation labels
- Ground-truth findings

Gold data is stored in a separate `gold_case_ids` / `expected_status` field,
read **only** by the evaluator. Verified by `gold_label_leaks=0` check.

## Token / context honesty

No real LLM API calls are made. Therefore:
- `api_token_usage = null`
- `context_bytes_avg` and `context_chars_avg` are recorded (real byte counts)
- `estimated_context_tokens_avg` uses a fixed `word-count-heuristic` tokenizer
- `tokenizer_name = "word-count-heuristic"`, `tokenizer_version = "v1-simple-div4"`

## Prohibited operations

- ❌ Real GitHub write operations
- ❌ Production database access
- ❌ Real SLS / cloud credentials
- ❌ PAT, LLM key, or cloud credentials in dataset or logs
- ❌ Modifying historical evidence (M3–M6 frozen)
- ❌ Modifying `main` or production configuration
- ❌ Claiming RAG improves decision accuracy (not measurable)
- ❌ Filling workflow-utility metrics with 0 or inferred values

## Evidence

Candidate evidence: `evidence/m7/benchmark/rag-n20-offline.json`

Marked as **M7-P2 candidate** — not M7 complete.
