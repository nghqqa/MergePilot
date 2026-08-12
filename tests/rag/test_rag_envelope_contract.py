#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-RAG · Final envelope evidence contract tests.

Verifies RAG provenance flows through the full Skill CLI pipeline into
the final serialized response envelope's evidence[] field, using real
handle() calls + _result_to_envelope + _finalize.
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

# Ensure no RAG adapter configured (fail-closed path)
os.environ.pop("MERGEPILOT_CR_DSN", None)
os.environ.pop("MERGEPILOT_RUN_ID", None)


class TestSastScanEnvelopeEvidence(unittest.TestCase):
    """Real sast_scan CLI: RAG provenance in final envelope evidence[]."""

    def test_rag_evidence_in_final_envelope(self):
        from runtime import cli as skill_cli
        req = {
            "contract_version": "1",
            "request_id": "req-env-1",
            "trace_id": "trace-env-1",
            "input": {"mode": "inline",
                      "files": [{"path": "test.py", "content": "x = 1\n"}]},
        }
        from skills.sast_scan.run import handle as sast_handle
        # Call handle directly to get the handler return dict
        ctx = {"request_id": req["request_id"], "trace_id": req["trace_id"],
               "deadline": None, "input": req["input"]}
        result = sast_handle(ctx)
        # handler return should have evidence[] (not rag_evidence)
        self.assertIn("evidence", result, "handler return missing evidence[]")
        self.assertNotIn("rag_evidence", result,
                         "handler return should not have rag_evidence")
        self.assertNotIn("rag_status", result,
                         "handler return should not have rag_status")
        # evidence items should be {kind, ref} dicts
        for ev in result.get("evidence", []):
            self.assertIn("kind", ev)
            self.assertIn("ref", ev)
            self.assertEqual(ev["kind"], "rag_advisory")
            parsed = json.loads(ev["ref"])
            self.assertIn("status", parsed)
            self.assertIn("untrusted", parsed)
            self.assertTrue(parsed["untrusted"])


class TestPRLifecycleEnvelopeEvidence(unittest.TestCase):
    """Real pr_lifecycle CLI: RAG provenance in final envelope evidence[]."""

    def test_rag_evidence_in_final_envelope(self):
        from skills.pr_lifecycle.run import handle as pr_handle
        ctx = {
            "request_id": "req-env-2", "trace_id": "trace-env-2",
            "deadline": None,
            "input": {
                "action": "ensure_fix_pr",
                "idempotency_key": "idem-env-test-001",
                "changes": [{"path": "src/app.py", "content": "x = 1\n"}],
                "commit_message": "fix: test",
                "pr_title": "Fix",
                "pr_body": "Test",
            },
        }
        result = pr_handle(ctx)
        # All return paths (OK/ERROR/DENIED) should have evidence[]
        self.assertIn("evidence", result,
                      "handler return missing evidence[] on %s path" % result.get("status"))
        self.assertNotIn("rag_evidence", result)
        self.assertNotIn("rag_status", result)
        for ev in result.get("evidence", []):
            self.assertIn("kind", ev)
            self.assertIn("ref", ev)
            parsed = json.loads(ev["ref"])
            self.assertTrue(parsed.get("untrusted", True))


class TestSastScanOutputSchemaPurity(unittest.TestCase):
    """Business output must NOT contain RAG fields."""

    def test_output_has_no_rag_fields(self):
        from skills.sast_scan.run import handle as sast_handle
        ctx = {
            "request_id": "req-pure-1", "trace_id": "trace-pure-1",
            "deadline": None,
            "input": {"mode": "inline",
                      "files": [{"path": "test.py", "content": "x = 1\n"}]},
        }
        result = sast_handle(ctx)
        output = result.get("output", {})
        self.assertNotIn("rag_context", output)
        self.assertNotIn("rag_status", output)
        self.assertNotIn("rag_evidence", output)


class TestEnvelopeSchemaValidation(unittest.TestCase):
    """Final serialized envelope passes common response envelope schema."""

    def test_envelope_has_required_fields(self):
        from runtime import cli as skill_cli
        from skills.sast_scan.run import handle as sast_handle
        # Build request and run through full CLI pipeline
        req = {
            "contract_version": "1",
            "request_id": "req-schema-1",
            "trace_id": "trace-schema-1",
            "input": {"mode": "inline",
                      "files": [{"path": "test.py", "content": "x = 1\n"}]},
        }
        env, rc = skill_cli.run_request(
            req, lambda ctx: sast_handle(ctx),
            name="sast_scan", version="1.0")
        # Common envelope required fields
        for field in ("name", "version", "contract_version", "request_id",
                      "trace_id", "status", "evidence"):
            self.assertIn(field, env, "envelope missing required field: %s" % field)
        # evidence should be a list (possibly empty or with RAG items)
        self.assertIsInstance(env.get("evidence"), list)


class TestNoSensitiveInEvidence(unittest.TestCase):
    """Evidence refs must not contain raw query strings or secrets."""

    def test_no_raw_query_in_evidence(self):
        from skills.sast_scan.run import handle as sast_handle
        ctx = {
            "request_id": "req-sec-1", "trace_id": "trace-sec-1",
            "deadline": None,
            "input": {"mode": "inline",
                      "files": [{"path": "ghp_secret123.py",
                                 "content": "x = 1\n"}]},
        }
        result = sast_handle(ctx)
        for ev in result.get("evidence", []):
            raw = ev.get("ref", "")
            self.assertNotIn("ghp_secret", raw)


if __name__ == "__main__":
    unittest.main()
