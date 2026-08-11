#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-A · OTel integration tests: real vertical slice with trace propagation.

Tests the full Controller → Gateway → Skill → MCP chain with:
- W3C traceparent HTTP header propagation
- In-memory + dual-collector verification
- 4 paths (normal/deny/timeout/rollback)
- Redaction enforcement on real attribute flows
- Fail-closed when collector/exporter is unreachable
"""
from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OTEL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "otel"))
sys.path.insert(0, OTEL)

import otel_spans as otel


class TestTraceparentPropagation(unittest.TestCase):
    """W3C traceparent serialization + deserialization."""

    def test_roundtrip(self):
        ctx = otel.SpanContext("a" * 32, "b" * 16, "run-1")
        tp = otel.to_traceparent(ctx)
        parsed = otel.from_traceparent(tp, run_id="run-1")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.trace_id, "a" * 32)
        self.assertEqual(parsed.span_id, "b" * 16)
        self.assertEqual(parsed.run_id, "run-1")

    def test_malformed_traceparent_returns_none(self):
        for bad in ("", "invalid", "00-short-short-00",
                     "00-" + "x" * 32 + "-short-00",
                     "00-" + "g" * 32 + "-" + "b" * 16 + "-00"):
            self.assertIsNone(otel.from_traceparent(bad))

    def test_sampled_flag(self):
        ctx = otel.SpanContext("a" * 32, "b" * 16, "r")
        tp_sampled = otel.to_traceparent(ctx, sampled=True)
        tp_not = otel.to_traceparent(ctx, sampled=False)
        self.assertTrue(tp_sampled.endswith("-01"))
        self.assertTrue(tp_not.endswith("-00"))

    def test_inject_extract_roundtrip(self):
        headers = {}
        ctx = otel.SpanContext("a" * 32, "b" * 16, "run-x")
        otel.inject_headers(headers, ctx, run_id="run-x")
        self.assertIn("traceparent", headers)
        self.assertIn("X-MP-Run-Id", headers)
        extracted = otel.extract_context(headers)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted.trace_id, "a" * 32)
        self.assertEqual(extracted.run_id, "run-x")

    def test_extract_from_empty_headers(self):
        self.assertIsNone(otel.extract_context({}))

    def test_case_insensitive_header_lookup(self):
        headers = {"TRACEPARENT": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
                   "X-MP-RUN-ID": "run-ci"}
        extracted = otel.extract_context(headers)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted.run_id, "run-ci")


class TestVerticalSliceNormal(unittest.TestCase):
    """Full Controller→Gateway→Skill→MCP chain, all succeed.

    Verifies:
    - trace_id propagates from controller root to MCP leaf
    - parent_span_id chain is correct
    - all spans carry run_id, trace_id, agent_role
    - duration is recorded on every span
    """

    def test_normal_chain_with_traceparent(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            run_id = "vert-normal-001"
            trace_id = ""

            # Controller root span
            with otel.controller_span(run_id=run_id, trace_id="",
                                      agent_role="coordinator",
                                      stage="review", attempt=1) as ctrl:
                trace_id = ctrl.trace_id  # capture the generated trace_id

                # Skill: diff_parse
                with otel.skill_span(run_id=run_id, trace_id=trace_id,
                                     skill_name="diff_parse",
                                     skill_version="1.0",
                                     request_id="req-dp-1",
                                     agent_role="reviewer"):
                    time.sleep(0.005)

                # Gateway call (reviewer reads PR)
                with otel.gateway_span(run_id=run_id, trace_id=trace_id,
                                       correlation_id="corr-gw-1",
                                       tool="pull_request_read",
                                       agent_role="reviewer") as gw:
                    # Inject traceparent for MCP call
                    headers = {}
                    otel.inject_headers(headers)
                    self.assertIn("traceparent", headers)

                    # MCP upstream (simulated GitHub API)
                    extracted = otel.extract_context(headers)
                    self.assertIsNotNone(extracted)
                    self.assertEqual(extracted.trace_id, trace_id)

                    with otel.mcp_span(run_id=run_id, trace_id=trace_id,
                                       correlation_id="corr-gw-1",
                                       endpoint="/pulls/42",
                                       method="GET",
                                       agent_role="reviewer") as mcp:
                        mcp.set_attribute("mp.status_code", 200)

                    gw.set_attribute("mp.decision", "ALLOW")

                # Skill: sast_scan
                with otel.skill_span(run_id=run_id, trace_id=trace_id,
                                     skill_name="sast_scan",
                                     skill_version="1.0",
                                     request_id="req-sast-1",
                                     agent_role="reviewer"):
                    time.sleep(0.005)

            # Verify
            self.assertEqual(len(c.spans), 5)
            by_name = {s.name: s for s in c.spans}

            # All share the same trace_id
            for s in c.spans:
                self.assertEqual(s.trace_id, trace_id,
                                 "%s has wrong trace_id" % s.name)
                self.assertEqual(s.run_id, run_id)

            # Parent chain
            ctrl_s = by_name["controller.process_event"]
            dp = by_name["skill.diff_parse"]
            gw = by_name["gateway.call_tool"]
            mcp = by_name["mcp.upstream"]
            sast = by_name["skill.sast_scan"]

            self.assertIsNone(ctrl_s.parent_span_id)
            self.assertEqual(dp.parent_span_id, ctrl_s.span_id)
            self.assertEqual(gw.parent_span_id, ctrl_s.span_id)
            self.assertEqual(mcp.parent_span_id, gw.span_id)
            self.assertEqual(sast.parent_span_id, ctrl_s.span_id)

            # All OK
            for s in c.spans:
                self.assertEqual(s.status, "OK")

            # Gateway has decision
            self.assertEqual(gw.attributes["mp.decision"], "ALLOW")
            self.assertEqual(gw.attributes["mp.correlation_id"], "corr-gw-1")

            # MCP has status code
            self.assertEqual(mcp.attributes["mp.status_code"], 200)

            # All have duration (may be 0 on high-precision systems)
            for s in c.spans:
                self.assertIsNotNone(s.duration_ms)
                self.assertGreaterEqual(s.duration_ms, 0)

            # Agent roles propagated
            self.assertEqual(ctrl_s.attributes["mp.agent_role"], "coordinator")
            self.assertEqual(dp.attributes["mp.agent_role"], "reviewer")
            self.assertEqual(gw.attributes["mp.agent_role"], "reviewer")
        finally:
            otel.set_collector(None)


class TestVerticalSliceDeny(unittest.TestCase):
    """Gateway returns DENY → no downstream skill/mcp spans."""

    def test_deny_blocks_downstream(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="vert-deny-001", trace_id="",
                                      agent_role="coordinator",
                                      stage="review") as ctrl:
                trace_id = ctrl.trace_id
                with otel.gateway_span(run_id="vert-deny-001",
                                       trace_id=trace_id,
                                       correlation_id="corr-deny",
                                       tool="merge_pull_request",
                                       agent_role="coordinator") as gw:
                    gw.set_attribute("mp.decision", "DENY")
                    gw.set_status("ERROR")
                    gw.add_event("policy.denied", {"reason": "L2 unauthorized"})
                    # No skill or MCP call made (denied)

            by_name = {s.name: s for s in c.spans}
            gw = by_name["gateway.call_tool"]
            self.assertEqual(gw.status, "ERROR")
            self.assertEqual(gw.attributes["mp.decision"], "DENY")

            # No skill spans
            skill_spans = [s for s in c.spans if s.name.startswith("skill.")]
            self.assertEqual(len(skill_spans), 0)

            # No MCP spans
            mcp_spans = [s for s in c.spans if s.name == "mcp.upstream"]
            self.assertEqual(len(mcp_spans), 0)
        finally:
            otel.set_collector(None)


class TestVerticalSliceTimeout(unittest.TestCase):
    """MCP call times out → error propagates up."""

    def test_timeout_marks_mcp_error(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="vert-timeout-001", trace_id="",
                                      agent_role="fixer",
                                      stage="fix") as ctrl:
                trace_id = ctrl.trace_id
                with otel.gateway_span(run_id="vert-timeout-001",
                                       trace_id=trace_id,
                                       correlation_id="corr-to",
                                       tool="create_branch",
                                       agent_role="fixer") as gw:
                    with otel.mcp_span(run_id="vert-timeout-001",
                                       trace_id=trace_id,
                                       correlation_id="corr-to",
                                       endpoint="/branches",
                                       method="POST",
                                       agent_role="fixer") as mcp:
                        mcp.set_status("ERROR")
                        mcp.add_event("timeout", {"deadline_ms": 5000})
                    gw.set_attribute("mp.decision", "ERROR")
                    gw.set_status("ERROR")

            by_name = {s.name: s for s in c.spans}
            mcp = by_name["mcp.upstream"]
            self.assertEqual(mcp.status, "ERROR")
            self.assertTrue(any(e["name"] == "timeout" for e in mcp.events))
        finally:
            otel.set_collector(None)


class TestVerticalSliceRollback(unittest.TestCase):
    """Verify FAIL → rollback on controller span."""

    def test_rollback_events_on_controller(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="vert-rb-001", trace_id="",
                                      agent_role="coordinator",
                                      stage="verify", attempt=2) as ctrl:
                trace_id = ctrl.trace_id
                with otel.skill_span(run_id="vert-rb-001", trace_id=trace_id,
                                     skill_name="test_runner",
                                     agent_role="verifier"):
                    pass
                ctrl.set_attribute("mp.verdict", "FAIL")
                ctrl.add_event("rollback.initiated", {
                    "reason": "verify_failed",
                    "parent_run_id": "vert-rb-001",
                })
                ctrl.add_event("rollback.completed", {
                    "revert_commit": "abc1234",
                    "reverify_passed": True,
                })

            by_name = {s.name: s for s in c.spans}
            ctrl = by_name["controller.process_event"]
            self.assertEqual(ctrl.attributes["mp.verdict"], "FAIL")
            events = [e["name"] for e in ctrl.events]
            self.assertIn("rollback.initiated", events)
            self.assertIn("rollback.completed", events)
            self.assertEqual(ctrl.status, "OK")  # rollback itself succeeded
            self.assertEqual(ctrl.attributes["mp.attempt"], 2)
        finally:
            otel.set_collector(None)


class TestRedactionInChain(unittest.TestCase):
    """Sensitive data never enters span attributes in a real chain."""

    def test_pat_not_in_span(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="r", trace_id="t",
                                      agent_role="reviewer"):
                with otel.gateway_span(run_id="r", trace_id="t",
                                       tool="pull_request_read",
                                       agent_role="reviewer",
                                       authorization="Bearer ghp_secret123") as gw:
                    gw.set_attribute("x-github-token", "ghp_abc123def456")
                    gw.set_attribute("safe.attr", "ok-value")

            gw = {s.name: s for s in c.spans}["gateway.call_tool"]
            self.assertEqual(gw.attributes["authorization"], "<redacted>")
            self.assertEqual(gw.attributes["x-github-token"], "<redacted>")
            self.assertEqual(gw.attributes["safe.attr"], "ok-value")
        finally:
            otel.set_collector(None)

    def test_llm_api_key_not_in_span(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.skill_span(run_id="r", trace_id="t",
                                 skill_name="sast_scan",
                                 agent_role="reviewer",
                                 api_key="sk-live-abc123def456"):
                pass
            s = c.spans[0]
            self.assertEqual(s.attributes["api_key"], "<redacted>")
        finally:
            otel.set_collector(None)


class TestFailClosedCollector(unittest.TestCase):
    """Collector failure does not affect business logic."""

    def test_broken_collector_no_crash(self):
        class Broken:
            def add_span(self, span):
                raise RuntimeError("collector broken")
        otel.set_collector(Broken())
        try:
            with otel.start_span("test", run_id="r", trace_id="t"):
                pass
            self.assertTrue(True)  # reached here = pass
        finally:
            otel.set_collector(None)

    def test_otlp_exporter_unreachable(self):
        """OTLPExporter to nonexistent endpoint → fail-closed."""
        exporter = otel.OTLPExporter(
            endpoint="http://localhost:19999/v1/traces", timeout=0.5)
        span = otel.SpanRecord("a" * 32, "b" * 16, None,
                               "test.span", "r")
        span.end()
        # must not raise
        exporter.export(span)
        self.assertEqual(exporter._failed, 1)
        self.assertEqual(exporter._sent, 0)


class TestDualCollector(unittest.TestCase):
    """DualCollector sends to both memory + exporter."""

    def test_dual_works_when_exporter_fails(self):
        mem = otel.InMemoryCollector()
        exporter = otel.OTLPExporter(
            endpoint="http://localhost:19999/v1/traces", timeout=0.5)
        dual = otel.DualCollector(memory=mem, exporter=exporter)
        otel.set_collector(dual)
        try:
            with otel.start_span("test", run_id="r", trace_id="t"):
                pass
            # memory got the span despite exporter failure
            self.assertEqual(len(mem.spans), 1)
            self.assertEqual(exporter._failed, 1)
        finally:
            otel.set_collector(None)


class TestRetryCountInChain(unittest.TestCase):
    """Retry attempts are tracked across controller spans."""

    def test_verify_retries_then_success(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            trace_id = ""
            for attempt in range(1, 4):
                with otel.controller_span(run_id="retry-test",
                                          trace_id=trace_id or "",
                                          agent_role="coordinator",
                                          stage="verify",
                                          attempt=attempt) as ctrl:
                    if not trace_id:
                        trace_id = ctrl.trace_id
                    if attempt < 3:
                        ctrl.set_attribute("mp.verdict", "RETRY")
                    else:
                        ctrl.set_attribute("mp.verdict", "PASS")

            verify_spans = [s for s in c.spans
                           if s.attributes.get("mp.stage") == "verify"]
            self.assertEqual(len(verify_spans), 3)
            attempts = [s.attributes["mp.attempt"] for s in verify_spans]
            self.assertEqual(attempts, [1, 2, 3])
            self.assertEqual(verify_spans[2].attributes["mp.verdict"], "PASS")
        finally:
            otel.set_collector(None)


class TestTraceparentAcrossGateway(unittest.TestCase):
    """Traceparent is correctly injected at Gateway and extractable at MCP."""

    def test_gateway_injects_traceparent_for_mcp(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="tp-test", trace_id="",
                                      agent_role="coordinator") as ctrl:
                tid = ctrl.trace_id
                with otel.gateway_span(run_id="tp-test", trace_id=tid,
                                       tool="read",
                                       agent_role="reviewer"):
                    # Simulate the MCP side receiving the HTTP call
                    headers = {}
                    otel.inject_headers(headers)
                    extracted = otel.extract_context(headers)
                    self.assertIsNotNone(extracted)
                    self.assertEqual(extracted.trace_id, tid)
                    self.assertEqual(extracted.run_id, "tp-test")
        finally:
            otel.set_collector(None)


if __name__ == "__main__":
    unittest.main()
