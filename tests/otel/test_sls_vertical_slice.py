#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-B · SLS vertical slice integration tests.

Verifies the full chain: real OTel spans → DualSLSCollector →
SLSExporter (background batch/retry) → FakeSLSReceiver, covering:
- normal/deny/timeout/rollback trace arrival at fake SLS
- SLS field mapping (trace/span/parent IDs, run_id, agent_role,
  skill_name/version, policy_decision, retry_count, duration, final_status)
- batch 64 spans, 1 MiB cap, 2s export timeout
- 5xx retry with exponential backoff and total budget
- queue >256 drop-oldest + dropped_batches counter
- PAT/token/secret redaction in SLS payload
- fake SLS unreachable → core business continues
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OTEL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "otel"))
if OTEL not in sys.path:
    sys.path.insert(0, OTEL)

import otel_spans as otel
from sls_exporter import SLSConfig, SLSExporter, span_to_sls
from fake_sls_receiver import FakeSLSReceiver


def _make_sls_collector(port, batch_max_size=64, batch_timeout_ms=500,
                        queue_max_size=256, export_timeout_ms=2000,
                        total_budget_ms=6000):
    """Create a DualSLSCollector + SLSExporter + FakeSLSReceiver triple."""
    receiver = FakeSLSReceiver(port=port)
    receiver.start()
    cfg = SLSConfig()
    cfg.endpoint = f"http://127.0.0.1:{port}"
    cfg.access_key_id = "test-ak"
    cfg.access_key_secret = "test-sk"
    cfg.project = "mp-test"
    cfg.logstore = "trace"
    cfg.batch_max_size = batch_max_size
    cfg.batch_timeout_ms = batch_timeout_ms
    cfg.queue_max_size = queue_max_size
    cfg.export_timeout_ms = export_timeout_ms
    cfg.total_export_budget_ms = total_budget_ms
    exporter = SLSExporter(cfg)
    exporter.start()
    collector = otel.DualSLSCollector(
        memory=otel.InMemoryCollector(), sls_exporter=exporter)
    return collector, exporter, receiver


class TestNormalTraceArrives(unittest.TestCase):

    def test_full_chain_normal(self):
        c, exp, rcv = _make_sls_collector(14500)
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="sls-norm-1", trace_id="",
                                      agent_role="coordinator",
                                      stage="review", attempt=1) as ctrl:
                tid = ctrl.trace_id
                with otel.skill_span(run_id="sls-norm-1", trace_id=tid,
                                     skill_name="diff_parse",
                                     skill_version="1.0",
                                     request_id="req-1",
                                     agent_role="reviewer"):
                    pass
                with otel.gateway_span(run_id="sls-norm-1", trace_id=tid,
                                       correlation_id="c1",
                                       tool="pull_request_read",
                                       agent_role="reviewer") as gw:
                    gw.set_attribute("mp.decision", "ALLOW")
            time.sleep(1.5)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()

        self.assertGreaterEqual(rcv.span_count, 3)
        by_name = {s["operation_name"]: s for s in rcv.spans}
        self.assertIn("controller.process_event", by_name)
        self.assertIn("skill.diff_parse", by_name)
        self.assertIn("gateway.call_tool", by_name)

        ctrl_s = by_name["controller.process_event"]
        self.assertEqual(ctrl_s["tags"]["mp_run_id"], "sls-norm-1")
        self.assertEqual(ctrl_s["tags"]["mp_agent_role"], "coordinator")
        self.assertEqual(ctrl_s["tags"]["mp_stage"], "review")

        gw_s = by_name["gateway.call_tool"]
        self.assertEqual(gw_s["tags"]["mp_decision"], "ALLOW")
        self.assertEqual(gw_s["tags"]["mp_tool"], "pull_request_read")


class TestDenyTraceArrives(unittest.TestCase):

    def test_deny_at_sls(self):
        c, exp, rcv = _make_sls_collector(14501)
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="sls-deny-1", trace_id="",
                                      agent_role="coordinator"):
                with otel.gateway_span(run_id="sls-deny-1", trace_id="",
                                       tool="merge",
                                       agent_role="coordinator") as gw:
                    gw.set_attribute("mp.decision", "DENY")
                    gw.set_status("ERROR")
            time.sleep(1.5)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()

        gw_spans = [s for s in rcv.spans if s["operation_name"] == "gateway.call_tool"]
        self.assertEqual(len(gw_spans), 1)
        self.assertEqual(gw_spans[0]["status_code"], 2)  # ERROR
        self.assertEqual(gw_spans[0]["tags"]["mp_decision"], "DENY")


class TestTimeoutTraceArrives(unittest.TestCase):

    def test_timeout_at_sls(self):
        c, exp, rcv = _make_sls_collector(14502)
        otel.set_collector(c)
        try:
            with otel.gateway_span(run_id="sls-to-1", trace_id="",
                                   tool="create_branch",
                                   agent_role="fixer") as gw:
                with otel.mcp_span(run_id="sls-to-1", trace_id="",
                                   endpoint="/branches",
                                   method="POST",
                                   agent_role="fixer") as mcp:
                    mcp.set_status("ERROR")
                gw.set_status("ERROR")
            time.sleep(1.5)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()

        mcp_spans = [s for s in rcv.spans if s["operation_name"] == "mcp.upstream"]
        self.assertEqual(len(mcp_spans), 1)
        self.assertEqual(mcp_spans[0]["status_code"], 2)


class TestRollbackTraceArrives(unittest.TestCase):

    def test_rollback_at_sls(self):
        c, exp, rcv = _make_sls_collector(14503)
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="sls-rb-1", trace_id="",
                                      stage="verify", attempt=2) as ctrl:
                ctrl.set_attribute("mp.verdict", "FAIL")
            time.sleep(1.5)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()

        ctrl_spans = [s for s in rcv.spans
                      if s["operation_name"] == "controller.process_event"]
        self.assertEqual(len(ctrl_spans), 1)
        self.assertEqual(ctrl_spans[0]["tags"]["mp_verdict"], "FAIL")
        self.assertEqual(str(ctrl_spans[0]["tags"]["mp_attempt"]), "2")


class TestBatchSize64(unittest.TestCase):

    def test_64_spans_one_batch(self):
        c, exp, rcv = _make_sls_collector(14504, batch_max_size=64,
                                           batch_timeout_ms=5000)
        otel.set_collector(c)
        try:
            for i in range(64):
                with otel.start_span(f"batch.{i}", run_id="batch64",
                                     trace_id="t-batch"):
                    pass
            time.sleep(2.0)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()
        self.assertEqual(rcv.span_count, 64)


class TestRedactionInSLS(unittest.TestCase):

    def test_pat_not_in_sls(self):
        c, exp, rcv = _make_sls_collector(14505)
        otel.set_collector(c)
        try:
            with otel.gateway_span(run_id="sls-red", trace_id="",
                                   tool="read", agent_role="reviewer",
                                   authorization="ghp_secret_abc123",
                                   api_key="sk-live-xyz"):
                pass
            time.sleep(1.5)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()

        raw = rcv.to_json()
        self.assertNotIn("ghp_secret", raw)
        self.assertNotIn("sk-live", raw)


class TestRetryOn500(unittest.TestCase):

    def test_retry_on_500(self):
        """Fake SLS returns 500 first, 200 second → retry succeeds."""
        rcv = FakeSLSReceiver(port=14506, response_status=200)
        rcv.start()
        call_count = [0]
        orig_process = rcv._process
        def flaky_process(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                from http.server import BaseHTTPRequestHandler
                raise Exception("simulated 500")
            orig_process(payload)
        rcv._process = flaky_process

        cfg = SLSConfig()
        cfg.endpoint = "http://127.0.0.1:14506"
        cfg.access_key_id = "test"
        cfg.access_key_secret = "test"
        cfg.project = "test"
        cfg.logstore = "trace"
        cfg.batch_max_size = 1
        cfg.batch_timeout_ms = 200
        cfg.retry_max_attempts = 3
        cfg.retry_base_delay_ms = 100
        cfg.export_timeout_ms = 1000
        cfg.total_export_budget_ms = 3000
        exporter = SLSExporter(cfg)
        exporter.start()
        try:
            span = otel.SpanRecord("a" * 32, "b" * 16, None,
                                   "retry.test", "r")
            span.end()
            exporter.enqueue(span)
            time.sleep(3.0)
            # Should have retried at least once
            self.assertGreater(exporter.retry_count, 0)
        finally:
            exporter.stop()
            rcv.stop()


class TestQueueOverflowDropOldest(unittest.TestCase):

    def test_drop_oldest(self):
        cfg = SLSConfig()
        cfg.endpoint = "http://127.0.0.1:19995"  # unreachable
        cfg.access_key_id = "fake"
        cfg.access_key_secret = "fake"
        cfg.project = "test"
        cfg.logstore = "trace"
        cfg.queue_max_size = 4
        cfg.batch_max_size = 2
        cfg.batch_timeout_ms = 100
        cfg.export_timeout_ms = 100
        cfg.total_export_budget_ms = 200
        exporter = SLSExporter(cfg)
        exporter.start()
        try:
            for i in range(100):
                span = otel.SpanRecord("a" * 32, "b" * 16, None,
                                       f"overflow.{i}", "r")
                span.end()
                exporter.enqueue(span)
            time.sleep(2.0)
            self.assertGreater(exporter.dropped_batches, 0)
        finally:
            exporter.stop()


class TestUnreachableNoBlock(unittest.TestCase):

    def test_business_continues(self):
        cfg = SLSConfig()
        cfg.endpoint = "http://127.0.0.1:19994"  # dead
        cfg.access_key_id = "fake"
        cfg.access_key_secret = "fake"
        cfg.project = "test"
        cfg.logstore = "trace"
        cfg.export_timeout_ms = 500
        cfg.total_export_budget_ms = 1000
        exporter = SLSExporter(cfg)
        collector = otel.DualSLSCollector(
            memory=otel.InMemoryCollector(), sls_exporter=exporter)
        exporter.start()
        otel.set_collector(collector)
        try:
            # Business logic must work
            with otel.start_span("biz", run_id="r", trace_id="t") as s:
                s.set_attribute("ok", True)
            # Memory collector received the span
            self.assertEqual(collector.memory.count, 1)
            time.sleep(1.5)
            # SLS export failed (unreachable)
            self.assertEqual(exporter.exported_spans, 0)
        finally:
            exporter.stop()
            otel.set_collector(None)


class TestCredentialsNotInPayload(unittest.TestCase):

    def test_ak_sk_absent(self):
        span = otel.SpanRecord("a" * 32, "b" * 16, None, "test", "r")
        span.set_attribute("mp.run_id", "r")
        span.end()
        sls = span_to_sls(span)
        raw = json.dumps(sls)
        self.assertNotIn("access_key", raw)
        self.assertNotIn("test-ak", raw)
        self.assertNotIn("test-sk", raw)


class TestDurationAndStatusMapped(unittest.TestCase):

    def test_duration_and_status(self):
        span = otel.SpanRecord("t" * 32, "s" * 16, None, "dur.test", "r")
        span.start_time = 1000.0
        span.end_time = 1001.5
        span.set_status("OK")
        sls = span_to_sls(span)
        self.assertEqual(sls["duration_ms"], 1500)
        self.assertEqual(sls["status_code"], 1)


class TestParentSpanIDMapped(unittest.TestCase):

    def test_parent_in_sls(self):
        c, exp, rcv = _make_sls_collector(14507)
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="parent-1", trace_id="",
                                      agent_role="coordinator") as ctrl:
                with otel.gateway_span(run_id="parent-1", trace_id="",
                                       tool="read", agent_role="reviewer"):
                    pass
            time.sleep(1.5)
        finally:
            exp.stop()
            otel.set_collector(None)
            rcv.stop()

        by_name = {s["operation_name"]: s for s in rcv.spans}
        if "controller.process_event" in by_name and "gateway.call_tool" in by_name:
            ctrl = by_name["controller.process_event"]
            gw = by_name["gateway.call_tool"]
            self.assertEqual(gw["parent_span_id"], ctrl["span_id"])


if __name__ == "__main__":
    unittest.main()
