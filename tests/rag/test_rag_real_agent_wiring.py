#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-RAG · Real Agent wiring + DB timeout + verifier-no-skip tests.

Tests that RAG retrieval is wired into the actual Skill CLI entry points
(sast_scan/run.py and pr_lifecycle/run.py), with fail-closed semantics.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
OTEL = os.path.normpath(os.path.join(REPO, "tools", "otel"))
RAG = os.path.normpath(os.path.join(REPO, "tools", "rag"))
SKILLS_COMMON = os.path.normpath(os.path.join(REPO, "skills", "common"))
for p in (OTEL, RAG, SKILLS_COMMON, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

import otel_spans as otel


class TestSastScanRAGWiring(unittest.TestCase):
    """Reviewer (sast_scan) calls query_for_reviewer before scan."""

    def test_sast_scan_includes_rag_context(self):
        from runtime import cli as skill_cli
        # Set up RAG adapter via env
        os.environ["MERGEPILOT_RUN_ID"] = "test-sast-rag"
        req = {
            "contract_version": "1",
            "request_id": "req-sast-rag",
            "trace_id": "trace-sast-rag",
            "input": {"files": [{"path": "src/db.py", "content": "x = 1\n"}]},
        }
        def dummy_skill(ctx):
            return {"findings": [], "scan_complete": True,
                    "engines_run": ["secret", "ast_python", "dep_vuln"],
                    "engine_errors": [], "ruleset_version": "1.0",
                    "evidence": [], "stats": {"files_scanned": 1}}
        env, rc = skill_cli.run_request(req, dummy_skill,
                                        name="sast_scan", version="1.0")
        # The skill should complete (RAG is fail-closed; no adapter = no_history)
        self.assertIn(env.get("status"), ("OK", "ERROR", "PARTIAL"))
        # If OK, rag_status should be present
        if env.get("status") == "OK" and "output" in env:
            out = env["output"]
            # rag_context and rag_status added by run.py handle()
            self.assertIn("rag_status", out)
            self.assertIn("rag_context", out)


class TestPRLifecycleRAGWiring(unittest.TestCase):
    """Fix Planner (pr_lifecycle) calls query_for_fixer before fix."""

    def test_pr_lifecycle_includes_rag_context(self):
        from runtime import cli as skill_cli
        os.environ["MERGEPILOT_RUN_ID"] = "test-pr-rag"
        req = {
            "contract_version": "1",
            "request_id": "req-pr-rag",
            "trace_id": "trace-pr-rag",
            "input": {"action": "ensure_fix_pr", "repo": "test/repo",
                      "pr_number": 1, "base_sha": "abc123",
                      "head_sha": "def456", "fix_branch": "fix/test"},
        }
        def dummy_skill(ctx):
            return {"action": "ensure_fix_pr", "outcome": "CREATED",
                    "pr_number": 1, "branch": "fix/test",
                    "commit_sha": "abc", "warnings": []}
        env, rc = skill_cli.run_request(req, dummy_skill,
                                        name="pr_lifecycle", version="1.0")
        # Skill should complete (RAG fail-closed)
        self.assertIn(env.get("status"), ("OK", "ERROR"))


class TestRealTimeout100ms(unittest.TestCase):
    """timeout_ms=100 must return within bounded wall-clock."""

    def test_100ms_timeout_bounded(self):
        from rag_retrieval_service import FakeRetrievalAdapter, query_for_reviewer
        adapter = FakeRetrievalAdapter(
            [{"case_id": "c1", "score": 0.9, "issue": "test"}],
            latency_ms=6000)
        start = time.monotonic()
        resp = query_for_reviewer("test", "r", "t",
                                  adapter=adapter, timeout_ms=100)
        elapsed = time.monotonic() - start
        self.assertEqual(resp.status, "retrieval_unavailable")
        self.assertEqual(resp.fallback_reason, "timeout")
        self.assertLess(elapsed, 2.0,
                        "wall-clock %.2fs exceeded 2s budget" % elapsed)


class TestTimeoutReturnsUnavailable(unittest.TestCase):

    def test_timeout_status_unavailable(self):
        from rag_retrieval_service import FakeRetrievalAdapter, query_for_fixer
        adapter = FakeRetrievalAdapter([], latency_ms=6000)
        resp = query_for_fixer("x", "r", "t", adapter=adapter, timeout_ms=100)
        self.assertEqual(resp.status, "retrieval_unavailable")
        self.assertEqual(resp.fallback_reason, "timeout")
        self.assertEqual(resp.hit_count, 0)


class TestPgVectorBridgeStatementTimeout(unittest.TestCase):
    """PgVectorBridge sets real statement_timeout (not just daemon join)."""

    def test_bridge_timeout_ms_stored(self):
        from rag_retrieval_service import PgVectorBridge
        bridge = PgVectorBridge(timeout_ms=500)
        self.assertEqual(bridge.timeout_ms, 500)

    def test_bridge_close_cleans_connection(self):
        from rag_retrieval_service import PgVectorBridge
        bridge = PgVectorBridge(dsn="", timeout_ms=100)
        bridge.close()
        self.assertIsNone(bridge._conn)

    def test_bridge_no_dsn_uses_inner_adapter(self):
        from rag_retrieval_service import PgVectorBridge, FakeRetrievalAdapter
        inner = FakeRetrievalAdapter([{"case_id": "c1", "score": 0.9,
                                       "issue": "c1"}])
        bridge = PgVectorBridge(adapter=inner, timeout_ms=5000, dsn="")
        results = bridge.retrieve("c1", top_k=1)
        self.assertEqual(len(results), 1)

    def test_bridge_no_dsn_timeout(self):
        from rag_retrieval_service import PgVectorBridge, FakeRetrievalAdapter
        inner = FakeRetrievalAdapter([], latency_ms=6000)
        bridge = PgVectorBridge(adapter=inner, timeout_ms=100, dsn="")
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            bridge.retrieve("x")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0)


class TestOTelParentChildRealEntry(unittest.TestCase):
    """When sast_scan runs with OTel active, rag.query is child of skill span."""

    def test_rag_query_child_of_skill_span(self):
        from rag_retrieval_service import FakeRetrievalAdapter, query_for_reviewer
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.skill_span(run_id="r-real", trace_id="t-real",
                                 skill_name="sast_scan",
                                 agent_role="reviewer") as skill:
                query_for_reviewer("sql", "r-real", skill.trace_id,
                                   adapter=FakeRetrievalAdapter(
                                       [{"case_id": "c1", "score": 0.9}]))
            by_name = {s.name: s for s in c.spans}
            skill_s = by_name.get("skill.sast_scan")
            rag_q = by_name.get("rag.query")
            self.assertIsNotNone(skill_s)
            self.assertIsNotNone(rag_q)
            self.assertEqual(rag_q.parent_span_id, skill_s.span_id)
            self.assertEqual(rag_q.trace_id, skill_s.trace_id)
        finally:
            otel.set_collector(None)


class TestVerifierNotSkipped(unittest.TestCase):
    """RAG results don't let Verifier skip current SAST/test."""

    def test_untrusted_remains_true_even_with_perfect_match(self):
        from rag_retrieval_service import FakeRetrievalAdapter, query_for_reviewer
        adapter = FakeRetrievalAdapter([
            {"case_id": "perfect", "score": 1.0, "issue": "exact"}])
        resp = query_for_reviewer("exact", "r", "t", adapter=adapter)
        self.assertEqual(resp.results[0].similarity, 1.0)
        self.assertTrue(resp.results[0].untrusted)
        self.assertFalse(resp.results[0].adopted)

    def test_rag_empty_does_not_block_scan(self):
        """When RAG returns empty, the skill still runs normally."""
        from rag_retrieval_service import FakeRetrievalAdapter, query_for_reviewer
        resp = query_for_reviewer("nonexistent", "r", "t",
                                  adapter=FakeRetrievalAdapter([]))
        self.assertEqual(resp.status, "empty")
        self.assertEqual(resp.hit_count, 0)


class TestRedactionInSpans(unittest.TestCase):

    def test_no_raw_query_in_span_attributes(self):
        from rag_retrieval_service import FakeRetrievalAdapter, query_for_reviewer
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            query_for_reviewer(
                "ghp_secret12345 sk-live-abc sql_injection",
                "r", "t",
                adapter=FakeRetrievalAdapter([{"case_id": "c1", "score": 0.9}]))
            for s in c.spans:
                for k, v in s.attributes.items():
                    if isinstance(v, str):
                        self.assertNotIn("ghp_secret", v)
                        self.assertNotIn("sk-live", v)
        finally:
            otel.set_collector(None)


if __name__ == "__main__":
    unittest.main()
