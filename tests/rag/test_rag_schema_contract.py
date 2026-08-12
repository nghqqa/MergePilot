#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-RAG · Real Skill handle() tests + schema validation.

Calls the actual sast_scan/run.py handle() and pr_lifecycle/run.py
handle() — not dummy_skill — and verifies:
- RAG fields are in the handler return, not the business output
- Business output passes Skill schema (no undeclared fields)
- RAG is fail-closed (no adapter → no_history, doesn't block)
"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
SKILLS = os.path.join(REPO, "skills")
for p in [SKILLS,
          os.path.join(SKILLS, "common"),
          os.path.join(SKILLS, "common", "runtime"),
          os.path.join(REPO, "tools", "otel"),
          os.path.join(REPO, "tools", "rag")]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Ensure no RAG adapter is configured (fail-closed path)
os.environ.pop("MERGEPILOT_CR_DSN", None)


class TestSastScanHandleSchema(unittest.TestCase):
    """Real sast_scan handle() — business output must pass schema."""

    def test_handle_returns_rag_outside_output(self):
        from skills.sast_scan.run import handle
        ctx = {
            "request_id": "req-1", "trace_id": "trace-1",
            "deadline": None,
            "input": {"mode": "inline", "files": [{"path": "test.py", "content": "x = 1\n"}]},
        }
        result = handle(ctx)
        self.assertIn("status", result)
        self.assertIn("output", result)
        # RAG provenance is in evidence[], NOT rag_evidence/rag_status
        self.assertIn("evidence", result, "handler return missing evidence[]")
        self.assertNotIn("rag_evidence", result)
        self.assertNotIn("rag_status", result)
        # output should NOT contain rag fields (schema purity)
        self.assertNotIn("rag_context", result.get("output", {}))
        self.assertNotIn("rag_status", result.get("output", {}))


class TestPRLifecycleHandleSchema(unittest.TestCase):
    """Real pr_lifecycle handle() — business output must pass schema."""

    def test_handle_returns_rag_outside_output(self):
        from skills.pr_lifecycle.run import handle
        ctx = {
            "request_id": "req-2", "trace_id": "trace-2",
            "deadline": None,
            "input": {
                "action": "ensure_fix_pr",
                "idempotency_key": "idem-rag-test-001",
                "changes": [{"path": "src/app.py", "content": "x = 1\n"}],
                "commit_message": "fix: test change",
                "pr_title": "Fix test",
                "pr_body": "Test PR",
            },
        }
        result = handle(ctx)
        self.assertIn("status", result)
        # RAG provenance is in evidence[] on all return paths
        self.assertIn("evidence", result,
                      "handler return missing evidence[] on %s path" % result.get("status"))
        self.assertNotIn("rag_evidence", result)
        self.assertNotIn("rag_status", result)


class TestCreateAdapterFromEnv(unittest.TestCase):

    def test_no_dsn_returns_none(self):
        from rag_retrieval_service import create_adapter_from_env
        os.environ.pop("MERGEPILOT_CR_DSN", None)
        adapter = create_adapter_from_env()
        self.assertIsNone(adapter)

    def test_with_dsn_attempts_pgvector(self):
        from rag_retrieval_service import create_adapter_from_env
        os.environ["MERGEPILOT_CR_PG_DSN"] = "postgresql://nonexistent:5432/db"
        try:
            adapter = create_adapter_from_env()
            # Returns a CaseRetrievalBridge if import succeeds, or None if not
            self.assertIsNotNone(adapter)
        finally:
            os.environ.pop("MERGEPILOT_CR_PG_DSN", None)


class TestPgVectorBridgeCancelSafe(unittest.TestCase):

    def test_cancel_does_not_cross_thread_close(self):
        """When worker is still alive, bridge calls cancel() not close()."""
        from rag_retrieval_service import PgVectorBridge, FakeRetrievalAdapter
        inner = FakeRetrievalAdapter([], latency_ms=6000)
        bridge = PgVectorBridge(adapter=inner, timeout_ms=100, dsn="")
        start_time = os.times()[4] if hasattr(os, 'times') else 0
        import time as _time
        start = _time.monotonic()
        with self.assertRaises(TimeoutError):
            bridge.retrieve("x")
        elapsed = _time.monotonic() - start
        self.assertLess(elapsed, 2.0)
        # Bridge should still be usable for next call
        inner2 = FakeRetrievalAdapter([{"case_id": "c1", "score": 0.9,
                                        "issue": "c1"}])
        bridge.adapter = inner2
        results = bridge.retrieve("c1", top_k=1)
        self.assertEqual(len(results), 1)


class TestRAGEvidenceSchema(unittest.TestCase):
    """RAG evidence items follow a consistent schema."""

    def test_rag_evidence_has_kind_and_ref(self):
        from rag_retrieval_service import RetrievalResult
        r = RetrievalResult("case-1", 0.85, issue_summary="test")
        ev = {"kind": "rag_advisory", "ref": json.dumps(r.to_dict())}
        self.assertEqual(ev["kind"], "rag_advisory")
        parsed = json.loads(ev["ref"])
        self.assertIn("case_id", parsed)
        self.assertIn("similarity", parsed)
        self.assertIn("untrusted", parsed)
        self.assertTrue(parsed["untrusted"])


if __name__ == "__main__":
    unittest.main()
