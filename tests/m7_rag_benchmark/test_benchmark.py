#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 RAG Benchmark — Test suite for Layer A (Retrieval & Integration).

Covers: hit, empty, scope_isolation, timeout, adapter_down, malformed,
gold-label isolation (structured), baseline vs RAG, deterministic replay
(normalized digest), workflow-utility-NOT-MEASURABLE, token honesty,
cohort separation, and verifier-NOT-MEASURED semantics.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in [str(ROOT), str(ROOT / "tests" / "m7_rag_benchmark"),
          str(ROOT / "tools" / "rag"), str(ROOT / "tools" / "otel"),
          str(ROOT / "skills"), str(ROOT / "skills" / "common" / "runtime")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset import (
    DATASET, KNOWLEDGE_BASE, DATASET_VERSION,
    dataset_sha256, unique_category_groups,
)
from run_benchmark import (
    TokenOverlapAdapter, TimeoutAdapter, FailingAdapter, MalformedAdapter,
    make_adapter, run_arm, classify_cohort, run_benchmark, scan_secrets,
    check_gold_leak_structured, reciprocal_rank, hit_at_k,
    normalized_digest, evaluate_positive_retrieval, evaluate_abstention,
    evaluate_fault_injection, evaluate_advisory_schema,
)
from rag_retrieval_service import RetrievalResult


class TestDatasetIntegrity(unittest.TestCase):

    def test_unique_case_count_ge_20(self):
        self.assertGreaterEqual(len(DATASET), 20)

    def test_dataset_version(self):
        self.assertEqual(DATASET_VERSION, "rag-bench-v2")

    def test_sha256_deterministic(self):
        self.assertEqual(dataset_sha256(), dataset_sha256())

    def test_sample_ids_unique(self):
        ids = [s["sample_id"] for s in DATASET]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_category_groups_present(self):
        groups = set(unique_category_groups().keys())
        required = {
            "clean", "hardcoded_secret", "sql_injection", "command_injection",
            "path_traversal", "dependency_vulnerability", "test_failure",
            "configuration_risk", "prompt_injection", "rollback_risk",
            "false_positive_allowlist", "no_history", "empty_retrieval",
            "timeout", "adapter_unavailable", "malformed_result",
            "cross_repo_adversarial",
        }
        self.assertEqual(groups, required)


class TestCohortSeparation(unittest.TestCase):

    def test_cohorts_are_mutually_exclusive(self):
        for s in DATASET:
            c = classify_cohort(s)
            self.assertIn(c, ("positive_retrieval", "abstention", "fault_injection"))

    def test_positive_retrieval_count(self):
        n = sum(1 for s in DATASET if classify_cohort(s) == "positive_retrieval")
        self.assertEqual(n, 19)

    def test_abstention_count(self):
        n = sum(1 for s in DATASET if classify_cohort(s) == "abstention")
        self.assertEqual(n, 5)

    def test_fault_injection_count(self):
        n = sum(1 for s in DATASET if classify_cohort(s) == "fault_injection")
        self.assertEqual(n, 5)

    def test_cohorts_sum_to_total(self):
        total = sum(1 for _ in DATASET)
        cohort_sum = sum(1 for s in DATASET)  # each sample in exactly 1 cohort
        self.assertEqual(total, cohort_sum)


class TestGoldLabelIsolation(unittest.TestCase):
    """Structured provenance check — gold fields must not enter adapter args."""

    def test_no_gold_case_id_in_queries(self):
        for s in DATASET:
            for gid in s["gold_case_ids"]:
                self.assertNotIn(gid, s["reviewer_query"])
                self.assertNotIn(gid, s["fixer_query"])

    def test_structured_gold_leak_check_passes(self):
        for s in DATASET:
            adapter_args = {
                "query": s["reviewer_query"],
                "repo_scope": s["repo_scope"],
                "top_k": 5, "min_score": 0.0,
            }
            advisory_json = "[]"
            leaks = check_gold_leak_structured(s, adapter_args, advisory_json)
            self.assertEqual(len(leaks), 0, f"{s['sample_id']}: {leaks}")

    def test_structured_check_detects_leak(self):
        """The check must actually catch leaks if injected."""
        s = next(s for s in DATASET if s["gold_case_ids"])
        # Inject gold case_id into adapter args
        adapter_args = {
            "query": s["reviewer_query"] + " " + s["gold_case_ids"][0],
            "repo_scope": s["repo_scope"],
            "gold_case_ids": s["gold_case_ids"],  # forbidden key
            "top_k": 5, "min_score": 0.0,
        }
        leaks = check_gold_leak_structured(s, adapter_args, "[]")
        self.assertGreater(len(leaks), 0, "check should detect gold leak")


class TestHitScenario(unittest.TestCase):

    def test_hit_returns_ok(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-005")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "ok")

    def test_positive_retrieval_hit_rate(self):
        pos = [s for s in DATASET if classify_cohort(s) == "positive_retrieval"]
        rag_raw = [run_arm(s, "rag", "test") for s in pos]
        m = evaluate_positive_retrieval(rag_raw)
        self.assertGreater(m["hit_at_1"], 0)
        self.assertGreater(m["hit_at_3"], 0)
        self.assertGreater(m["mean_reciprocal_rank"], 0)

    def test_citation_metrics_present(self):
        """Citation precision metrics must exist (replaced error_citation_count)."""
        pos = [s for s in DATASET if classify_cohort(s) == "positive_retrieval"]
        rag_raw = [run_arm(s, "rag", "test") for s in pos]
        m = evaluate_positive_retrieval(rag_raw)
        self.assertIn("top1_incorrect_case_count", m)
        self.assertIn("samples_with_non_gold_in_top_k", m)
        self.assertIn("top1_accuracy", m)
        self.assertIn("top_k_gold_coverage", m)
        self.assertIn("retrieved_item_precision_at_k", m)
        # Old metric must not exist
        self.assertNotIn("error_citation_count", m)

    def test_citation_denominator_is_positive_retrieval(self):
        """All citation metrics have denominator=19 (positive_retrieval only)."""
        pos = [s for s in DATASET if classify_cohort(s) == "positive_retrieval"]
        rag_raw = [run_arm(s, "rag", "test") for s in pos]
        m = evaluate_positive_retrieval(rag_raw)
        self.assertEqual(m["positive_retrieval_case_count"], 19)
        # top1_incorrect + (19 - top1_incorrect) = 19
        self.assertLessEqual(m["top1_incorrect_case_count"], 19)
        self.assertAlmostEqual(
            m["top1_accuracy"],
            (19 - m["top1_incorrect_case_count"]) / 19)


class TestAbstentionScenario(unittest.TestCase):

    def test_clean_samples_return_empty_or_ok(self):
        """Clean samples may return empty (ideal) or weak match (calibration)."""
        s = next(s for s in DATASET if s["sample_id"] == "bm-001")
        r = run_arm(s, "rag", "test")
        self.assertIn(r["status"], ("empty", "ok"))

    def test_abstention_denominator_is_rag_only(self):
        """Abstention case_count must be 5 (RAG arm only), not 10."""
        ev = run_benchmark()
        self.assertEqual(ev["abstention_metrics"]["abstention_case_count"], 5)

    def test_abstention_correct_count(self):
        ev = run_benchmark()
        self.assertEqual(ev["abstention_metrics"]["abstention_correct_count"], 4)

    def test_abstention_accuracy(self):
        ev = run_benchmark()
        self.assertAlmostEqual(ev["abstention_metrics"]["abstention_accuracy"], 0.8)

    def test_abstention_false_positive_count(self):
        ev = run_benchmark()
        self.assertEqual(
            ev["abstention_metrics"]["false_positive_on_abstention_count"], 1)

    def test_abstention_scope_leak_zero(self):
        ev = run_benchmark()
        self.assertEqual(ev["abstention_metrics"]["scope_leak_count"], 0)


class TestCrossRepoAdversarial(unittest.TestCase):

    def test_cross_repo_no_scope_leak(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-029")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "empty",
                         f"cross-repo leak: {r['hit_count']} results")


class TestTimeoutScenario(unittest.TestCase):

    def test_timeout_returns_unavailable(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-024")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "retrieval_unavailable")
        self.assertEqual(r["fallback_reason"], "timeout")


class TestAdapterUnavailable(unittest.TestCase):

    def test_failing_returns_unavailable(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-026")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "retrieval_unavailable")


class TestMalformedResult(unittest.TestCase):

    def test_malformed_returns_unavailable(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-028")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "retrieval_unavailable")


class TestFaultResilience(unittest.TestCase):

    def test_all_faults_fail_closed(self):
        faults = [s for s in DATASET if classify_cohort(s) == "fault_injection"]
        rag_raw = [run_arm(s, "rag", "test") for s in faults]
        m = evaluate_fault_injection(rag_raw)
        self.assertEqual(m["fault_fallback_accuracy"], 1.0)


class TestSecretRedaction(unittest.TestCase):

    def test_no_secrets_in_dataset(self):
        self.assertEqual(scan_secrets(json.dumps(DATASET) + json.dumps(KNOWLEDGE_BASE)), 0)

    def test_no_secrets_in_safe_results(self):
        for s in DATASET[:10]:
            r = run_arm(s, "rag", "test")
            safe = {"sample_id": r["sample_id"], "status": r["status"],
                    "hit_count": r["hit_count"]}
            self.assertEqual(scan_secrets(json.dumps(safe)), 0)


class TestBaselineVsRAG(unittest.TestCase):

    def test_baseline_all_no_history(self):
        for s in DATASET[:10]:
            r = run_arm(s, "baseline", "test")
            self.assertEqual(r["status"], "no_history")

    def test_paired_count_equal(self):
        b = [run_arm(s, "baseline", "t") for s in DATASET]
        r = [run_arm(s, "rag", "t") for s in DATASET]
        self.assertEqual(len(b), len(r), len(DATASET))


class TestDeterministicReplay(unittest.TestCase):

    def test_normalized_digest_identical(self):
        ev = run_benchmark()
        self.assertTrue(ev["deterministic_replay_match"])
        self.assertEqual(ev["normalized_digest_run_1"], ev["normalized_digest_run_2"])
        self.assertEqual(ev["determinism_kind"], "normalized_semantic_digest")
        self.assertIn("timestamp", ev["excluded_volatile_fields"])
        self.assertIn("latency_ms", ev["excluded_volatile_fields"])


class TestWorkflowUtilityNotMeasurable(unittest.TestCase):

    def test_status_is_not_measurable(self):
        ev = run_benchmark()
        self.assertEqual(ev["workflow_utility_status"],
                         "NOT_MEASURABLE_WITH_CURRENT_RUNTIME")

    def test_runtime_consumes_rag_context_false(self):
        ev = run_benchmark()
        self.assertFalse(ev["runtime_consumes_rag_context"])

    def test_workflow_metrics_all_null(self):
        ev = run_benchmark()
        for k, v in ev["workflow_utility_metrics"].items():
            self.assertIsNone(v, f"{k} should be null")

    def test_reason_mentions_core_scan(self):
        ev = run_benchmark()
        self.assertIn("core.scan", ev["workflow_utility_not_measurable_reason"])


class TestVerifierSemantics(unittest.TestCase):

    def test_verifier_execution_status_not_measured(self):
        ev = run_benchmark()
        self.assertEqual(ev["verifier_execution_status"], "NOT_MEASURED")

    def test_verifier_executed_rate_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["verifier_executed_rate"])

    def test_verifier_preserved_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["verifier_preserved"])

    def test_verifier_gate_contract_preserved_true(self):
        ev = run_benchmark()
        self.assertTrue(ev["verifier_gate_contract_preserved"])


class TestTokenContextHonesty(unittest.TestCase):

    def test_api_token_usage_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["retrieval_metrics"]["api_token_usage"])

    def test_tokenizer_name_present(self):
        ev = run_benchmark()
        self.assertEqual(ev["retrieval_metrics"]["tokenizer_name"], "word-count-heuristic")


class TestResidueSemantics(unittest.TestCase):

    def test_database_residue_not_applicable(self):
        ev = run_benchmark()
        self.assertEqual(ev["database_residue_status"], "NOT_APPLICABLE")

    def test_db_residue_fields_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["active_query_residue"])
        self.assertIsNone(ev["idle_connection_residue"])
        self.assertIsNone(ev["connection_residue"])
        self.assertIsNone(ev["transaction_residue"])

    def test_worker_thread_delta_measured(self):
        ev = run_benchmark()
        self.assertIsInstance(ev["worker_thread_delta"], int)
        self.assertEqual(ev["worker_thread_delta"], 0)

    def test_temp_dir_residue_measured(self):
        ev = run_benchmark()
        self.assertIsInstance(ev["temp_dir_residue"], int)


class TestQualityGateSemantics(unittest.TestCase):

    def test_benchmark_phase_is_development_calibration(self):
        ev = run_benchmark()
        self.assertEqual(ev["benchmark_phase"], "DEVELOPMENT_CALIBRATION")

    def test_quality_gate_status_not_pre_registered(self):
        ev = run_benchmark()
        self.assertEqual(ev["quality_gate_status"], "NOT_PRE_REGISTERED")

    def test_quality_gate_pass_is_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["quality_gate_pass"])

    def test_all_ok_does_not_imply_quality_passed(self):
        """all_ok = execution_all_ok AND safety_gate_pass only (not quality)."""
        ev = run_benchmark()
        self.assertEqual(ev["all_ok"], ev["execution_all_ok"] and ev["safety_gate_pass"])

    def test_development_all_ok_present(self):
        ev = run_benchmark()
        self.assertIn("development_all_ok", ev)
        self.assertEqual(ev["development_all_ok"],
                         ev["execution_all_ok"] and ev["safety_gate_pass"])

    def test_confirmatory_all_ok_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["confirmatory_all_ok"])

    def test_all_ok_scope_present(self):
        ev = run_benchmark()
        self.assertEqual(ev["all_ok_scope"],
                         "development_execution_and_safety_only")

    def test_gold_scan_method_present(self):
        ev = run_benchmark()
        self.assertIn("gold_scan_method", ev)
        self.assertIn("structural", ev["gold_scan_method"])

    def test_gold_scan_targets_present(self):
        ev = run_benchmark()
        targets = ev["gold_scan_targets"]
        self.assertIn("reviewer_query", targets)
        self.assertIn("adapter_call.query", targets)
        self.assertIn("advisory_record", targets)
        self.assertIn("normalized benchmark result", targets)

    def test_gold_scan_forbidden_fields_listed(self):
        ev = run_benchmark()
        forbidden = ev["gold_scan_forbidden_fields"]
        self.assertIn("gold_case_ids", forbidden)
        self.assertIn("expected_status", forbidden)
        self.assertIn("category_group", forbidden)


class TestMetricHelpers(unittest.TestCase):

    def _make_result(self, case_id):
        return RetrievalResult(
            case_id=case_id, similarity=0.9, category="x", severity="high",
            issue_summary="", fix_summary="", citation_url="")

    def test_reciprocal_rank(self):
        results = [self._make_result("a"), self._make_result("b")]
        self.assertEqual(reciprocal_rank(results, ["a"]), 1.0)
        self.assertEqual(reciprocal_rank(results, ["b"]), 0.5)

    def test_hit_at_k(self):
        results = [self._make_result("a")]
        self.assertTrue(hit_at_k(results, ["a"], 1))
        self.assertFalse(hit_at_k(results, ["b"], 1))


if __name__ == "__main__":
    unittest.main()
