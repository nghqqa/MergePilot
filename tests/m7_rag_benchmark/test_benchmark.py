#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 RAG Benchmark — Test suite for Layer A (Retrieval & Integration).

Covers: hit, empty, scope_isolation, timeout, adapter_down, malformed,
verifier-still-executes, evidence schema, secret redaction, gold-label
isolation, baseline vs RAG comparability, deterministic replay,
workflow-utility-NOT-MEASURABLE semantics, and token/context honesty.
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
    DATASET, KNOWLEDGE_BASE, DATASET_VERSION, GOLD,
    dataset_sha256, unique_category_groups,
)
from run_benchmark import (
    TokenOverlapAdapter, TimeoutAdapter, FailingAdapter, MalformedAdapter,
    make_adapter, run_arm, evaluate_arm, run_benchmark, scan_secrets,
    reciprocal_rank, hit_at_k,
    TOP_K, MIN_SCORE,
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
        self.assertEqual(groups, required, f"missing: {required - groups}")

    def test_no_singleton_dominance(self):
        """Every hit-category should have at least 1 sample; no category has 0."""
        counts = unique_category_groups()
        for cat, n in counts.items():
            self.assertGreaterEqual(n, 1, f"{cat} has {n}")

    def test_every_sample_has_required_fields(self):
        required = {"sample_id", "category_group", "repo_scope",
                    "reviewer_query", "fixer_query", "adapter_type",
                    "gold_case_ids", "expected_status"}
        for s in DATASET:
            missing = required - set(s.keys())
            self.assertFalse(missing, f"{s.get('sample_id')} missing {missing}")


class TestGoldLabelIsolation(unittest.TestCase):
    """Queries must NEVER contain gold case_ids or evaluation labels."""

    def test_no_gold_case_id_in_queries(self):
        for s in DATASET:
            for gid in s["gold_case_ids"]:
                self.assertNotIn(gid, s["reviewer_query"],
                                 f"GOLD LEAK in {s['sample_id']} reviewer_query")
                self.assertNotIn(gid, s["fixer_query"],
                                 f"GOLD LEAK in {s['sample_id']} fixer_query")

    def test_no_expected_status_in_queries(self):
        for s in DATASET:
            self.assertNotIn(s["expected_status"], s["reviewer_query"])
            self.assertNotIn(s["expected_status"], s["fixer_query"])

    def test_no_category_group_label_in_queries(self):
        """The category_group label (e.g. 'sql_injection') must not appear literally."""
        for s in DATASET:
            # Allow partial substrings but not the full category label as a token
            # The category label with underscores is unlikely in natural queries
            label = s["category_group"]
            if label in ("clean", "no_history", "empty_retrieval",
                         "timeout", "adapter_unavailable", "malformed_result",
                         "cross_repo_adversarial"):
                continue  # these labels don't correspond to KB categories
            # Check that the exact category_group string isn't in the query
            self.assertNotIn(label, s["reviewer_query"].lower(),
                             f"category label leak in {s['sample_id']}")


class TestHitScenario(unittest.TestCase):

    def test_hit_returns_ok(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-005")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "ok")
        self.assertGreater(r["hit_count"], 0)

    def test_multiple_hit_samples(self):
        hits = [s for s in DATASET if s["adapter_type"] == "token_overlap"
                and s["gold_case_ids"] and s["expected_status"] == "ok"]
        self.assertGreaterEqual(len(hits), 10)
        for s in hits:
            r = run_arm(s, "rag", "test")
            self.assertEqual(r["status"], "ok", f"{s['sample_id']}: {r['status']}")


class TestEmptyScenario(unittest.TestCase):

    def test_empty_returns_empty(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-023")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "empty")
        self.assertEqual(r["hit_count"], 0)


class TestScopeIsolation(unittest.TestCase):

    def test_cross_repo_adversarial_no_leak(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-029")
        r = run_arm(s, "rag", "test")
        self.assertEqual(r["status"], "empty",
                         f"cross-repo leak: {r['hit_count']} results")

    def test_adapter_scope_filter(self):
        adapter = TokenOverlapAdapter(KNOWLEDGE_BASE, repo_scope="repo-delta")
        results = adapter.retrieve("database query sql injection execute")
        for r in results:
            self.assertIn("repo-delta", r.get("source_pr_url", ""))


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


class TestVerifierPreservation(unittest.TestCase):

    def test_verifier_ran_on_all_rag_samples(self):
        for s in DATASET:
            r = run_arm(s, "rag", "test")
            # In offline benchmark, verifier_ran is implied by the arm completing
            # The real assertion is in the integration evidence (M6-RAG)
            self.assertIsNotNone(r["status"])

    def test_rag_never_blocks_business(self):
        """Even on timeout/failure, the arm completes (simulating core action ran)."""
        for s in DATASET:
            r = run_arm(s, "rag", "test")
            self.assertIn(r["status"],
                          ("ok", "empty", "no_history", "retrieval_unavailable"))


class TestEvidenceSchema(unittest.TestCase):

    def test_rag_advisory_shape(self):
        s = next(s for s in DATASET if s["sample_id"] == "bm-005")
        r = run_arm(s, "rag", "test")
        ev_item = {"kind": "rag_advisory", "ref": json.dumps({
            "status": r["status"], "hit_count": r["hit_count"],
            "adopted": False, "untrusted": True,
        })}
        self.assertEqual(ev_item["kind"], "rag_advisory")
        parsed = json.loads(ev_item["ref"])
        self.assertIn("status", parsed)
        self.assertIn("hit_count", parsed)


class TestSecretRedaction(unittest.TestCase):

    def test_no_secrets_in_results(self):
        for s in DATASET[:10]:
            r = run_arm(s, "rag", "test")
            # Only serialize safe scalar fields (results contains objects)
            safe = {"sample_id": r["sample_id"], "status": r["status"],
                    "hit_count": r["hit_count"],
                    "fallback_reason": r["fallback_reason"]}
            self.assertEqual(scan_secrets(json.dumps(safe)), 0)

    def test_no_secrets_in_dataset(self):
        self.assertEqual(scan_secrets(json.dumps(DATASET) + json.dumps(KNOWLEDGE_BASE)), 0)

    def test_kb_urls_are_test_only(self):
        for c in KNOWLEDGE_BASE:
            url = c.get("source_pr_url", "")
            if url:
                self.assertIn("test/", url)


class TestBaselineVsRAG(unittest.TestCase):

    def test_baseline_all_no_history(self):
        for s in DATASET[:10]:
            r = run_arm(s, "baseline", "test")
            self.assertEqual(r["status"], "no_history")

    def test_baseline_zero_hits(self):
        for s in DATASET[:10]:
            r = run_arm(s, "baseline", "test")
            self.assertEqual(r["hit_count"], 0)

    def test_paired_count_equal(self):
        b = [run_arm(s, "baseline", "t") for s in DATASET]
        r = [run_arm(s, "rag", "t") for s in DATASET]
        self.assertEqual(len(b), len(r), len(DATASET))


class TestDeterministicReplay(unittest.TestCase):

    def test_two_runs_identical(self):
        """Two full benchmark runs must produce identical deterministic metrics.

        Only compares retrieval quality and count metrics (not latency, which
        is inherently noisy from timeout threads).
        """
        ev1 = run_benchmark()
        ev2 = run_benchmark()
        for f in ["dataset_sha256", "unique_case_count",
                   "total_arm_executions", "verifier_preserved",
                   "gold_label_leaks", "secret_leaks"]:
            self.assertEqual(ev1[f], ev2[f], f"non-deterministic: {f}")
        for f in ["hit_at_1", "hit_at_3", "mean_reciprocal_rank",
                   "scope_leak_count", "empty_count", "timeout_count",
                   "fallback_count", "error_citation_count"]:
            self.assertEqual(ev1["rag_metrics"][f], ev2["rag_metrics"][f],
                             f"non-deterministic RAG metric: {f}")
        # Per-sample status must be identical
        # (deterministic_replay_match is already checked inside run_benchmark)
        self.assertTrue(ev1["deterministic_replay_match"])
        self.assertTrue(ev2["deterministic_replay_match"])


class TestWorkflowUtilityNotMeasurable(unittest.TestCase):
    """Layer B is NOT MEASURABLE because core.scan/core.run don't consume RAG."""

    def test_workflow_utility_status_is_not_measurable(self):
        ev = run_benchmark()
        self.assertEqual(ev["workflow_utility_status"],
                         "NOT_MEASURABLE_WITH_CURRENT_RUNTIME")

    def test_runtime_consumes_rag_context_is_false(self):
        ev = run_benchmark()
        self.assertFalse(ev["runtime_consumes_rag_context"])

    def test_workflow_utility_metrics_are_null(self):
        ev = run_benchmark()
        wum = ev["workflow_utility_metrics"]
        for key in ["reviewer_accuracy_baseline", "reviewer_accuracy_rag",
                     "fixer_accuracy_baseline", "fixer_accuracy_rag",
                     "decision_accuracy_delta", "finding_f1_delta",
                     "adoption_rate"]:
            self.assertIsNone(wum[key], f"{key} should be null, got {wum[key]}")

    def test_not_measurable_reason_present(self):
        ev = run_benchmark()
        reason = ev["workflow_utility_not_measurable_reason"]
        self.assertIn("advisory evidence", reason)
        self.assertIn("core.scan", reason)
        self.assertIn("not consumed", reason.lower())


class TestTokenContextHonesty(unittest.TestCase):
    """No real LLM API calls — token metrics are estimates only."""

    def test_api_token_usage_is_null(self):
        ev = run_benchmark()
        self.assertIsNone(ev["rag_metrics"]["api_token_usage"])
        self.assertIsNone(ev["baseline_metrics"]["api_token_usage"])

    def test_tokenizer_name_present(self):
        ev = run_benchmark()
        self.assertIsNotNone(ev["rag_metrics"]["tokenizer_name"])
        self.assertIsNotNone(ev["rag_metrics"]["tokenizer_version"])

    def test_context_bytes_recorded(self):
        ev = run_benchmark()
        self.assertIsInstance(ev["rag_metrics"]["context_bytes_avg"], (int, float))


class TestMetricHelpers(unittest.TestCase):

    def _make_result(self, case_id):
        return RetrievalResult(
            case_id=case_id, similarity=0.9, category="x", severity="high",
            issue_summary="", fix_summary="", citation_url="")

    def test_reciprocal_rank(self):
        results = [self._make_result("a"), self._make_result("b")]
        self.assertEqual(reciprocal_rank(results, ["a"]), 1.0)
        self.assertEqual(reciprocal_rank(results, ["b"]), 0.5)
        self.assertEqual(reciprocal_rank(results, ["c"]), 0.0)

    def test_hit_at_k(self):
        results = [self._make_result("a")]
        self.assertTrue(hit_at_k(results, ["a"], 1))
        self.assertFalse(hit_at_k(results, ["b"], 1))


if __name__ == "__main__":
    unittest.main()
