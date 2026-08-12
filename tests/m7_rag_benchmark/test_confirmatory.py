#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 RAG Confirmatory Benchmark — Design-freeze test suite.

Validates dataset integrity, v2 separation, pre-registered thresholds,
and gold-label isolation WITHOUT executing the confirmatory benchmark.
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

from dataset_heldout import (
    DATASET_HELDOUT, KNOWLEDGE_BASE_HELDOUT, DATASET_VERSION,
    DETERMINISTIC_SEED, dataset_heldout_sha256,
    heldout_cohort_counts, PRE_REGISTERED_THRESHOLDS,
    verify_separation_from_v2,
)
from dataset import DATASET as V2_DATASET, KNOWLEDGE_BASE as V2_KB


class TestHeldoutDatasetIntegrity(unittest.TestCase):

    def test_unique_case_count_ge_20(self):
        self.assertGreaterEqual(len(DATASET_HELDOUT), 20)

    def test_dataset_version(self):
        self.assertEqual(DATASET_VERSION, "rag-bench-v3-heldout")

    def test_seed_different_from_v2(self):
        self.assertEqual(DETERMINISTIC_SEED, 99)
        self.assertNotEqual(DETERMINISTIC_SEED, 42)

    def test_sha256_deterministic(self):
        self.assertEqual(dataset_heldout_sha256(), dataset_heldout_sha256())

    def test_sample_ids_unique(self):
        ids = [s["sample_id"] for s in DATASET_HELDOUT]
        self.assertEqual(len(ids), len(set(ids)))


class TestCohortDistribution(unittest.TestCase):

    def test_all_three_cohorts_present(self):
        counts = heldout_cohort_counts()
        self.assertGreater(counts["positive_retrieval"], 0)
        self.assertGreater(counts["abstention"], 0)
        self.assertGreater(counts["fault_injection"], 0)

    def test_cohorts_sum_to_total(self):
        counts = heldout_cohort_counts()
        self.assertEqual(sum(counts.values()), len(DATASET_HELDOUT))

    def test_positive_retrieval_ge_10(self):
        counts = heldout_cohort_counts()
        self.assertGreaterEqual(counts["positive_retrieval"], 10)

    def test_fault_injection_has_all_three_types(self):
        """Timeout, adapter_unavailable, malformed_result must all be present."""
        groups = {s["category_group"] for s in DATASET_HELDOUT
                  if s["adapter_type"] in ("timeout", "failing", "malformed")}
        self.assertIn("timeout", groups)
        self.assertIn("adapter_unavailable", groups)
        self.assertIn("malformed_result", groups)


class TestV2Separation(unittest.TestCase):

    def test_no_v2_separation_violations(self):
        violations = verify_separation_from_v2()
        self.assertEqual(len(violations), 0,
                         f"separation violations: {violations}")

    def test_no_case_id_overlap(self):
        v2_ids = {c["case_id"] for c in V2_KB}
        for c in KNOWLEDGE_BASE_HELDOUT:
            self.assertNotIn(c["case_id"], v2_ids,
                             f"case_id overlap: {c['case_id']}")

    def test_no_sample_id_overlap(self):
        v2_ids = {s["sample_id"] for s in V2_DATASET}
        for s in DATASET_HELDOUT:
            self.assertNotIn(s["sample_id"], v2_ids)

    def test_no_query_overlap(self):
        v2_queries = set()
        for s in V2_DATASET:
            v2_queries.add(s["reviewer_query"])
            v2_queries.add(s["fixer_query"])
        for s in DATASET_HELDOUT:
            self.assertNotIn(s["reviewer_query"], v2_queries)
            self.assertNotIn(s["fixer_query"], v2_queries)

    def test_heldout_case_ids_use_ho_prefix(self):
        for c in KNOWLEDGE_BASE_HELDOUT:
            self.assertTrue(c["case_id"].startswith("kb-ho-"),
                            f"non-ho case_id: {c['case_id']}")

    def test_heldout_sample_ids_use_ho_prefix(self):
        for s in DATASET_HELDOUT:
            self.assertTrue(s["sample_id"].startswith("ho-"),
                            f"non-ho sample_id: {s['sample_id']}")


class TestPreRegisteredThresholds(unittest.TestCase):

    def test_thresholds_are_frozen_dict(self):
        """Thresholds must be a static dict, not computed at runtime."""
        self.assertIsInstance(PRE_REGISTERED_THRESHOLDS, dict)

    def test_min_hit_at_1_threshold(self):
        self.assertGreaterEqual(PRE_REGISTERED_THRESHOLDS["min_hit_at_1"], 0.5)
        self.assertLessEqual(PRE_REGISTERED_THRESHOLDS["min_hit_at_1"], 1.0)

    def test_min_hit_at_3_threshold(self):
        self.assertGreaterEqual(PRE_REGISTERED_THRESHOLDS["min_hit_at_3"], 0.5)

    def test_min_mrr_threshold(self):
        self.assertGreaterEqual(PRE_REGISTERED_THRESHOLDS["min_mrr"], 0.5)

    def test_max_scope_leak_is_zero(self):
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["max_scope_leak_count"], 0)

    def test_fault_thresholds_are_strict(self):
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["min_timeout_semantics_correct_rate"], 1.0)
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["min_adapter_unavailable_fail_closed_rate"], 1.0)
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["min_malformed_result_fail_closed_rate"], 1.0)
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["min_fault_fallback_accuracy"], 1.0)

    def test_safety_thresholds_are_zero(self):
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["max_gold_label_leaks"], 0)
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["max_secret_leaks"], 0)
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["max_worker_thread_delta"], 0)
        self.assertEqual(PRE_REGISTERED_THRESHOLDS["max_temp_dir_residue"], 0)

    def test_determinism_required(self):
        self.assertTrue(PRE_REGISTERED_THRESHOLDS["require_deterministic_replay"])


class TestGoldLabelIsolation(unittest.TestCase):

    def test_no_gold_case_id_in_queries(self):
        for s in DATASET_HELDOUT:
            for gid in s["gold_case_ids"]:
                self.assertNotIn(gid, s["reviewer_query"],
                                 f"GOLD LEAK in {s['sample_id']}")
                self.assertNotIn(gid, s["fixer_query"])

    def test_no_expected_status_in_queries(self):
        for s in DATASET_HELDOUT:
            self.assertNotIn(s["expected_status"], s["reviewer_query"])

    def test_queries_contain_no_evaluator_fields(self):
        """Query text must not contain field names like 'gold_case_ids'."""
        forbidden_tokens = ["gold_case_ids", "expected_status", "category_group",
                           "gold_case_id", "adapter_type"]
        for s in DATASET_HELDOUT:
            for token in forbidden_tokens:
                self.assertNotIn(token, s["reviewer_query"],
                                 f"evaluator field '{token}' in {s['sample_id']} query")
                self.assertNotIn(token, s["fixer_query"])


class TestCategoryCoverage(unittest.TestCase):

    def test_covers_required_categories(self):
        groups = {s["category_group"] for s in DATASET_HELDOUT}
        required = {
            "sql_injection", "hardcoded_secret", "command_injection",
            "path_traversal", "dependency_vulnerability", "test_failure",
            "configuration_risk", "prompt_injection", "rollback_risk",
            "clean", "no_history", "empty_retrieval", "timeout",
            "adapter_unavailable", "malformed_result",
            "cross_repo_adversarial",
        }
        missing = required - groups
        self.assertFalse(missing, f"missing categories: {missing}")


if __name__ == "__main__":
    unittest.main()
