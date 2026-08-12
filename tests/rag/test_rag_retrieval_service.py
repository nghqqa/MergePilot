#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-RAG · Retrieval service tests (v2: real timeout + OTel parent-child).

Covers: hit, empty, multi-result sort, malformed query, timeout (real
wall-clock), unreachable, redaction, no-skip-verify, OTel parent-child.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OTEL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "otel"))
RAG = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "rag"))
for p in (OTEL, RAG):
    if p not in sys.path:
        sys.path.insert(0, p)

import otel_spans as otel
from rag_retrieval_service import (
    query_for_reviewer, query_for_fixer, FakeRetrievalAdapter,
    PgVectorBridge, RetrievalResult, RetrievalResponse,
)

SAMPLE_CASES = [
    {"case_id": "case-001", "score": 0.95, "category": "sql_injection",
     "severity": "high", "issue": "SQL injection in user input",
     "fix": "Use parameterized queries", "source_pr_url": "https://github.com/repo/pull/1"},
    {"case_id": "case-002", "score": 0.88, "category": "hardcoded_secret",
     "severity": "critical", "issue": "Hardcoded API key in source",
     "fix": "Move to environment variable", "source_pr_url": "https://github.com/repo/pull/2"},
    {"case_id": "case-003", "score": 0.72, "category": "xss",
     "severity": "medium", "issue": "Reflected XSS in search",
     "fix": "HTML encode output", "source_pr_url": ""},
]


class TestHit(unittest.TestCase):

    def test_reviewer_gets_results(self):
        adapter = FakeRetrievalAdapter(SAMPLE_CASES)
        resp = query_for_reviewer("sql injection", "run-1", "trace-1",
                                  adapter=adapter, top_k=5)
        self.assertEqual(resp.status, "ok")
        self.assertGreater(resp.hit_count, 0)
        self.assertEqual(resp.results[0].case_id, "case-001")
        self.assertEqual(resp.results[0].similarity, 0.95)

    def test_fixer_gets_results(self):
        adapter = FakeRetrievalAdapter(SAMPLE_CASES)
        resp = query_for_fixer("hardcoded", "run-2", "trace-2",
                               adapter=adapter, top_k=3)
        self.assertEqual(resp.status, "ok")
        self.assertGreater(resp.hit_count, 0)
        self.assertIn("api key", resp.results[0].issue_summary.lower())


class TestEmpty(unittest.TestCase):

    def test_no_match(self):
        adapter = FakeRetrievalAdapter(SAMPLE_CASES)
        resp = query_for_reviewer("nonexistent_xyz", "r", "t", adapter=adapter)
        self.assertEqual(resp.status, "empty")

    def test_empty_adapter(self):
        resp = query_for_reviewer("x", "r", "t", adapter=FakeRetrievalAdapter([]))
        self.assertEqual(resp.status, "empty")


class TestMultiSort(unittest.TestCase):

    def test_sorted_by_similarity(self):
        adapter = FakeRetrievalAdapter(SAMPLE_CASES)
        resp = query_for_reviewer("", "r", "t", adapter=adapter, top_k=3)
        scores = [r.similarity for r in resp.results]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestMalformed(unittest.TestCase):

    def test_empty_query(self):
        resp = query_for_reviewer("", "r", "t",
                                  adapter=FakeRetrievalAdapter(SAMPLE_CASES))
        self.assertIn(resp.status, ("ok", "empty"))

    def test_unicode(self):
        resp = query_for_reviewer("注入", "r", "t",
                                  adapter=FakeRetrievalAdapter(SAMPLE_CASES))
        self.assertIn(resp.status, ("ok", "empty"))

    def test_very_long(self):
        resp = query_for_reviewer("a" * 10000, "r", "t",
                                  adapter=FakeRetrievalAdapter(SAMPLE_CASES))
        self.assertIn(resp.status, ("ok", "empty"))


class TestRealTimeout(unittest.TestCase):
    """Real bounded timeout via threading + join (not pre-check only)."""

    def test_slow_adapter_times_out_at_100ms(self):
        """Adapter sleeps 6s; timeout_ms=100 must return in <2s."""
        adapter = FakeRetrievalAdapter(SAMPLE_CASES, latency_ms=6000)
        start = time.monotonic()
        resp = query_for_reviewer("test", "r-to", "t-to",
                                  adapter=adapter, timeout_ms=100)
        elapsed = time.monotonic() - start
        self.assertEqual(resp.status, "retrieval_unavailable")
        self.assertEqual(resp.fallback_reason, "timeout")
        self.assertEqual(resp.hit_count, 0)
        # Wall-clock: must be well under 6s (the adapter's full latency)
        self.assertLess(elapsed, 2.0,
                        "timeout didn't fire: %.2fs elapsed" % elapsed)

    def test_fast_adapter_returns_ok(self):
        adapter = FakeRetrievalAdapter(SAMPLE_CASES, latency_ms=10)
        resp = query_for_reviewer("sql", "r", "t",
                                  adapter=adapter, timeout_ms=5000)
        self.assertEqual(resp.status, "ok")
        self.assertGreater(resp.hit_count, 0)

    def test_timeout_does_not_block_business(self):
        """Business logic returns quickly even with a 6s adapter."""
        adapter = FakeRetrievalAdapter(SAMPLE_CASES, latency_ms=6000)
        start = time.monotonic()
        resp = query_for_fixer("test", "r", "t",
                               adapter=adapter, timeout_ms=200)
        elapsed = time.monotonic() - start
        self.assertEqual(resp.status, "retrieval_unavailable")
        self.assertLess(elapsed, 2.0)


class TestUnreachable(unittest.TestCase):

    def test_adapter_raises(self):
        adapter = FakeRetrievalAdapter(fail_with=ConnectionError("DB down"))
        resp = query_for_reviewer("x", "r", "t", adapter=adapter)
        self.assertEqual(resp.status, "retrieval_unavailable")
        self.assertEqual(resp.fallback_reason, "ConnectionError")

    def test_no_adapter(self):
        resp = query_for_reviewer("x", "r", "t", adapter=None)
        self.assertEqual(resp.status, "no_history")
        self.assertEqual(resp.fallback_reason, "no_adapter")


class TestRedaction(unittest.TestCase):

    def test_summary_bounded(self):
        cases = [{"case_id": "c1", "score": 0.9,
                  "issue": "x" * 5000, "fix": "y" * 5000}]
        resp = query_for_reviewer("x", "r", "t",
                                  adapter=FakeRetrievalAdapter(cases))
        self.assertLessEqual(len(resp.results[0].issue_summary), 200)
        self.assertLessEqual(len(resp.results[0].fix_summary), 200)

    def test_untrusted_always_true(self):
        resp = query_for_reviewer("sql", "r", "t",
                                  adapter=FakeRetrievalAdapter(SAMPLE_CASES))
        for r in resp.results:
            self.assertTrue(r.untrusted)


class TestNoSkipVerifier(unittest.TestCase):

    def test_perfect_match_still_untrusted(self):
        cases = [{"case_id": "perfect", "score": 1.0,
                  "issue": "exact", "fix": "exact"}]
        resp = query_for_reviewer("exact", "r", "t",
                                  adapter=FakeRetrievalAdapter(cases))
        self.assertEqual(resp.results[0].similarity, 1.0)
        self.assertTrue(resp.results[0].untrusted)
        self.assertFalse(resp.results[0].adopted)


class TestOTelParentChild(unittest.TestCase):
    """rag.query must be a CHILD of the current Agent span."""

    def test_rag_query_is_child_of_controller(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="r-otel", trace_id="",
                                      agent_role="reviewer",
                                      stage="review") as ctrl:
                tid = ctrl.trace_id
                query_for_reviewer("sql", "r-otel", tid,
                                   adapter=FakeRetrievalAdapter(SAMPLE_CASES))
            by_name = {s.name: s for s in c.spans}
            ctrl_s = by_name["controller.process_event"]
            rag_q = by_name.get("rag.query")
            self.assertIsNotNone(rag_q, "rag.query span not emitted")
            # rag.query must be a child of controller
            self.assertEqual(rag_q.parent_span_id, ctrl_s.span_id)
            self.assertEqual(rag_q.trace_id, ctrl_s.trace_id)
        finally:
            otel.set_collector(None)

    def test_rag_result_child_of_rag_query(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            query_for_reviewer("sql", "r-otel2", "trace-otel2",
                               adapter=FakeRetrievalAdapter(SAMPLE_CASES))
            by_name = {s.name: s for s in c.spans}
            rag_q = by_name.get("rag.query")
            rag_r = by_name.get("rag.result")
            self.assertIsNotNone(rag_q)
            self.assertIsNotNone(rag_r)
            self.assertEqual(rag_r.parent_span_id, rag_q.span_id)
        finally:
            otel.set_collector(None)

    def test_rag_fallback_on_timeout(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            query_for_reviewer("x", "r-otel3", "t3",
                               adapter=FakeRetrievalAdapter(SAMPLE_CASES,
                                                            latency_ms=6000),
                               timeout_ms=100)
            fb = [s for s in c.spans if s.name == "rag.fallback"]
            self.assertEqual(len(fb), 1)
            self.assertEqual(fb[0].attributes.get("rag.fallback_reason"), "timeout")
        finally:
            otel.set_collector(None)

    def test_rag_attributes_no_sensitive_query(self):
        """rag.query span must NOT contain the raw query string."""
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            query_for_reviewer(
                "ghp_secret_12345 sk-live-abc sql", "r", "t",
                adapter=FakeRetrievalAdapter(SAMPLE_CASES))
            for s in c.spans:
                for k, v in s.attributes.items():
                    if isinstance(v, str):
                        self.assertNotIn("ghp_secret", v)
                        self.assertNotIn("sk-live", v)
        finally:
            otel.set_collector(None)


class TestPgVectorBridge(unittest.TestCase):

    def test_bridge_delegates_to_inner_adapter(self):
        inner = FakeRetrievalAdapter(SAMPLE_CASES)
        bridge = PgVectorBridge(adapter=inner, timeout_ms=5000)
        results = bridge.retrieve("sql", top_k=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["case_id"], "case-001")

    def test_bridge_timeout_raises(self):
        inner = FakeRetrievalAdapter(SAMPLE_CASES, latency_ms=6000)
        bridge = PgVectorBridge(adapter=inner, timeout_ms=100)
        with self.assertRaises(TimeoutError):
            bridge.retrieve("sql")

    def test_bridge_no_adapter_raises(self):
        bridge = PgVectorBridge(adapter=None)
        with self.assertRaises(RuntimeError):
            bridge.retrieve("sql")


class TestResponseStructure(unittest.TestCase):

    def test_to_dict(self):
        resp = query_for_reviewer("sql", "run-15", "trace-15",
                                  adapter=FakeRetrievalAdapter(SAMPLE_CASES),
                                  top_k=2)
        d = resp.to_dict()
        self.assertIn("status", d)
        self.assertIn("results", d)
        self.assertIn("stats", d)
        self.assertEqual(d["run_id"], "run-15")
        self.assertEqual(d["trace_id"], "trace-15")

    def test_latency_positive(self):
        resp = query_for_reviewer("x", "r", "t",
                                  adapter=FakeRetrievalAdapter(SAMPLE_CASES,
                                                              latency_ms=50))
        self.assertGreater(resp.latency_ms, 0)


if __name__ == "__main__":
    unittest.main()
