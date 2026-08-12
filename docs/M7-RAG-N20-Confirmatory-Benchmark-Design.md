# M7 RAG N≥20 Confirmatory Benchmark — Design Freeze

**Status**: Frozen (design + dataset + thresholds; execution deferred)
**Milestone**: M7-P2-confirmatory
**Base**: `9a9fc0a` (origin/main)
**Held-out dataset version**: `rag-bench-v3-heldout`
**Pre-registered**: BEFORE any confirmatory execution

## 1. Objective

Validate RAG retrieval quality and integration safety on a **held-out** dataset
that was NOT used during development calibration (rag-bench-v2). Pre-registered
quality thresholds are frozen before execution — no post-hoc adjustment.

## 2. Separation from development calibration

| Property | v2 (Development) | v3 (Confirmatory) |
|----------|------------------|-------------------|
| dataset_version | `rag-bench-v2` | `rag-bench-v3-heldout` |
| case_id prefix | `kb-*` | `kb-ho-*` |
| sample_id prefix | `bm-*` | `ho-*` |
| seed | 42 | 99 |
| issue text | independently authored | independently authored (different wording) |
| query text | independently authored | independently authored (different wording) |

**Separation verification**: `verify_separation_from_v2()` checks zero overlap
in case_ids, sample_ids, and query strings. Validated by 6 dedicated tests.

**Results must NOT be merged**: development calibration metrics are reported
separately from confirmatory metrics. `development_results_not_merged = true`.

## 3. Dataset (rag-bench-v3-heldout)

- **unique_case_count**: 25 (≥20)
- **knowledge_base_cases**: 20 (all `kb-ho-*` prefix)
- **deterministic_seed**: 99
- **dataset_sha256**: `5227c653ef39e718cb315e10cc0571bee60210412f643738b71fb534ee86c98b`

### Cohort distribution

| Cohort | Count | Denominator for |
|--------|-------|-----------------|
| positive_retrieval | 15 | Hit@K, MRR, category/severity match |
| abstention | 5 | abstention_accuracy, scope_leak |
| fault_injection | 5 | timeout/fallback/malformed correctness |
| **Total** | **25** | |

### Category coverage

sql_injection, hardcoded_secret, command_injection, path_traversal,
dependency_vulnerability, test_failure, configuration_risk, prompt_injection,
rollback_risk, false_positive_allowlist, clean, no_history, empty_retrieval,
cross_repo_adversarial, timeout, adapter_unavailable, malformed_result.

## 4. Pre-registered quality thresholds

Frozen BEFORE execution. These represent minimum acceptable quality for a
confirmatory pass. They are NOT derived from v2 calibration results.

| Threshold | Value |
|-----------|-------|
| min_hit_at_1 | ≥ 0.70 |
| min_hit_at_3 | ≥ 0.80 |
| min_mrr | ≥ 0.75 |
| min_top1_category_match_rate | ≥ 0.70 |
| min_top1_severity_match_rate | ≥ 0.70 |
| min_abstention_accuracy | ≥ 0.60 |
| max_scope_leak_count | = 0 |
| min_timeout_semantics_correct_rate | = 1.0 |
| min_adapter_unavailable_fail_closed_rate | = 1.0 |
| min_malformed_result_fail_closed_rate | = 1.0 |
| min_fault_fallback_accuracy | = 1.0 |
| require_deterministic_replay | = True |
| max_gold_label_leaks | = 0 |
| max_secret_leaks | = 0 |
| max_worker_thread_delta | = 0 |
| max_temp_dir_residue | = 0 |

## 5. Gold-label isolation

Query text sent to adapters NEVER contains:
- Gold case_ids
- Expected status
- Category group labels
- Evaluator-only field names (`gold_case_ids`, `expected_status`, etc.)

Gold data is stored in `GOLD_HELDOUT` dict, read ONLY by the evaluator.
Verified by structural provenance check + 3 dedicated tests.

## 6. Three-layer gate

| Gate | Composition | Confirmatory role |
|------|-------------|-------------------|
| `execution_all_ok` | All execution checks pass | Necessary |
| `safety_gate_pass` | Gold/secret/residue/separation checks pass | Necessary |
| `quality_gate_pass` | ALL pre-registered thresholds met | Necessary |
| `confirmatory_all_ok` | execution AND safety AND quality | **Final verdict** |

`confirmatory_all_ok` is the ONLY field that can certify confirmatory pass.
`development_all_ok` is reported but does NOT contribute to confirmatory verdict.

## 7. Runtime invariants (unchanged from development)

- `runtime_consumes_rag_context = false`
- `workflow_utility_status = NOT_MEASURABLE_WITH_CURRENT_RUNTIME`
- `verifier_execution_status = NOT_MEASURED`
- `database_residue_status = NOT_APPLICABLE` (Fake/TokenOverlap adapter)

## 8. Token/context honesty

- `api_token_usage = null` (no real LLM API calls)
- `tokenizer_name = "word-count-heuristic"`
- Only `context_bytes_avg` and `estimated_context_tokens_avg` are reported

## 9. Execution protocol (deferred)

When authorized to execute:
1. Run `run_confirmatory.py` on the frozen held-out dataset
2. Record all metrics against pre-registered thresholds
3. `confirmatory_all_ok = true` only if ALL three gates pass
4. Publish evidence as `evidence/m7/benchmark/rag-n20-confirmatory.json`
5. Evidence binds to the execution commit
6. Do NOT adjust thresholds after seeing results

## 10. Prohibited operations

- ❌ Merging development (v2) and confirmatory (v3) results
- ❌ Adjusting thresholds after execution
- ❌ Using v2 case_ids, sample_ids, or queries in held-out
- ❌ Real GitHub / production DB / real SLS
- ❌ Modifying historical evidence
- ❌ Claiming workflow utility improvement

## 11. Deliverables (this design freeze)

| File | Purpose |
|------|---------|
| `docs/M7-RAG-N20-Confirmatory-Benchmark-Design.md` | This document |
| `tests/m7_rag_benchmark/dataset_heldout.py` | Held-out dataset + thresholds |
| `tests/m7_rag_benchmark/run_confirmatory.py` | Confirmatory runner (not executed) |
| `tests/m7_rag_benchmark/test_confirmatory.py` | Design validation tests (27 passed) |
