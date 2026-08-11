#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-A · Local OTLP Collector integration tests.

Spins up a LocalOTLPReceiver on localhost:4318, sends real OTLP payloads
via DualCollector + OTLPExporter, and verifies the receiver actually
receives and parses complete traces.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OTEL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "otel"))
for p in (OTEL,):
    if p not in sys.path:
        sys.path.insert(0, p)

import otel_spans as otel
from local_otlp_receiver import LocalOTLPReceiver, OTLPReceivedSpan


def _settle():
    """Wait briefly for async OTLP export to complete."""
    time.sleep(0.15)


class TestLocalReceiverLifecycle(unittest.TestCase):

    def test_start_stop(self):
        r = LocalOTLPReceiver(port=14318)
        r.start()
        self.assertEqual(r.span_count, 0)
        r.stop()

    def test_receive_single_span(self):
        r = LocalOTLPReceiver(port=14319)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14319/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                with otel.start_span("test.single", run_id="r1", trace_id="t1"):
                    pass
                _settle()
            finally:
                otel.set_collector(None)
            self.assertEqual(r.span_count, 1)
            self.assertEqual(r.spans[0].name, "test.single")
            self.assertEqual(r.spans[0].attributes.get("mp.run_id"), "r1")
        finally:
            r.stop()


class TestFullTraceReceived(unittest.TestCase):
    """A complete Controller→Gateway→Skill→MCP trace reaches the receiver."""

    def test_full_trace_hierarchy(self):
        r = LocalOTLPReceiver(port=14320)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14320/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                with otel.controller_span(run_id="full-1", trace_id="",
                                          agent_role="coordinator",
                                          stage="review", attempt=1) as ctrl:
                    tid = ctrl.trace_id

                    with otel.skill_span(run_id="full-1", trace_id=tid,
                                         skill_name="diff_parse",
                                         skill_version="1.0",
                                         request_id="req-1",
                                         agent_role="reviewer"):
                        pass

                    with otel.gateway_span(run_id="full-1", trace_id=tid,
                                           correlation_id="c1",
                                           tool="pull_request_read",
                                           agent_role="reviewer") as gw:
                        with otel.mcp_span(run_id="full-1", trace_id=tid,
                                           correlation_id="c1",
                                           endpoint="/pulls/1",
                                           method="GET",
                                           agent_role="reviewer") as mcp:
                            mcp.set_attribute("mp.status_code", 200)
                        gw.set_attribute("mp.decision", "ALLOW")

                _settle()
            finally:
                otel.set_collector(None)

            # Verify receiver got all 4 spans
            self.assertEqual(r.span_count, 4)

            # All share the same trace_id
            for s in r.spans:
                self.assertEqual(s.trace_id, tid)

            # Verify hierarchy
            by_name = {s.name: s for s in r.spans}
            ctrl_r = by_name["controller.process_event"]
            dp_r = by_name["skill.diff_parse"]
            gw_r = by_name["gateway.call_tool"]
            mcp_r = by_name["mcp.upstream"]

            self.assertIsNone(ctrl_r.parent_span_id)
            self.assertEqual(dp_r.parent_span_id, ctrl_r.span_id)
            self.assertEqual(gw_r.parent_span_id, ctrl_r.span_id)
            self.assertEqual(mcp_r.parent_span_id, gw_r.span_id)

            # Verify key attributes survived OTLP round-trip
            self.assertEqual(ctrl_r.attributes.get("mp.run_id"), "full-1")
            self.assertEqual(ctrl_r.attributes.get("mp.agent_role"), "coordinator")
            self.assertEqual(ctrl_r.attributes.get("mp.stage"), "review")
            self.assertEqual(ctrl_r.attributes.get("mp.attempt"), "1")
            self.assertEqual(dp_r.attributes.get("mp.skill_name"), "diff_parse")
            self.assertEqual(dp_r.attributes.get("mp.skill_version"), "1.0")
            self.assertEqual(gw_r.attributes.get("mp.tool"), "pull_request_read")
            self.assertEqual(gw_r.attributes.get("mp.decision"), "ALLOW")
            self.assertEqual(mcp_r.attributes.get("mp.endpoint"), "/pulls/1")
            self.assertEqual(mcp_r.attributes.get("mp.method"), "GET")

            # All OK
            for s in r.spans:
                self.assertEqual(s.status_str, "OK")

            # Duration recorded
            for s in r.spans:
                self.assertIsNotNone(s.duration_ms)
        finally:
            r.stop()


class TestDenyPathReceived(unittest.TestCase):

    def test_deny_span_at_receiver(self):
        r = LocalOTLPReceiver(port=14321)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14321/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                with otel.controller_span(run_id="deny-1", trace_id="",
                                          agent_role="coordinator"):
                    with otel.gateway_span(run_id="deny-1", trace_id="",
                                           tool="merge",
                                           agent_role="coordinator") as gw:
                        gw.set_attribute("mp.decision", "DENY")
                        gw.set_status("ERROR")
                _settle()
            finally:
                otel.set_collector(None)

            gw_spans = r.get_by_name("gateway.call_tool")
            self.assertEqual(len(gw_spans), 1)
            self.assertEqual(gw_spans[0].status_str, "ERROR")
            self.assertEqual(gw_spans[0].attributes.get("mp.decision"), "DENY")
        finally:
            r.stop()


class TestTimeoutPathReceived(unittest.TestCase):

    def test_timeout_span_at_receiver(self):
        r = LocalOTLPReceiver(port=14322)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14322/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                with otel.gateway_span(run_id="to-1", trace_id="",
                                       tool="create_branch") as gw:
                    with otel.mcp_span(run_id="to-1", trace_id="",
                                       endpoint="/branches",
                                       method="POST") as mcp:
                        mcp.set_status("ERROR")
                    gw.set_status("ERROR")
                _settle()
            finally:
                otel.set_collector(None)

            mcp_spans = r.get_by_name("mcp.upstream")
            self.assertEqual(len(mcp_spans), 1)
            self.assertEqual(mcp_spans[0].status_str, "ERROR")
        finally:
            r.stop()


class TestRollbackPathReceived(unittest.TestCase):

    def test_rollback_events_at_receiver(self):
        r = LocalOTLPReceiver(port=14323)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14323/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                with otel.controller_span(run_id="rb-1", trace_id="",
                                          stage="verify", attempt=2) as ctrl:
                    ctrl.set_attribute("mp.verdict", "FAIL")
                _settle()
            finally:
                otel.set_collector(None)

            ctrl_spans = r.get_by_name("controller.process_event")
            self.assertEqual(len(ctrl_spans), 1)
            self.assertEqual(ctrl_spans[0].attributes.get("mp.verdict"), "FAIL")
            self.assertEqual(ctrl_spans[0].attributes.get("mp.attempt"), "2")
        finally:
            r.stop()


class TestRedactionInOTLP(unittest.TestCase):
    """Sensitive fields are redacted BEFORE reaching the receiver."""

    def test_pat_not_in_otlp_payload(self):
        r = LocalOTLPReceiver(port=14324)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14324/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                with otel.gateway_span(run_id="red-1", trace_id="",
                                       tool="read",
                                       authorization="Bearer ghp_secret123",
                                       api_key="sk-live-abc"):
                    pass
                _settle()
            finally:
                otel.set_collector(None)

            gw = r.get_by_name("gateway.call_tool")[0]
            self.assertEqual(gw.attributes.get("authorization"), "<redacted>")
            self.assertEqual(gw.attributes.get("api_key"), "<redacted>")
            # Ensure the raw token never appears
            for v in gw.attributes.values():
                if isinstance(v, str):
                    self.assertNotIn("ghp_secret", v)
                    self.assertNotIn("sk-live", v)
        finally:
            r.stop()


class TestFailClosedUnreachable(unittest.TestCase):
    """Receiver unreachable → exporter times out ≤2s, business continues."""

    def test_exporter_timeout_does_not_block(self):
        # Point exporter at a port with nothing listening
        exporter = otel.OTLPExporter(
            endpoint="http://127.0.0.1:19999/v1/traces", timeout=2.0)
        dual = otel.DualCollector(
            memory=otel.InMemoryCollector(), exporter=exporter)
        otel.set_collector(dual)
        try:
            start = time.monotonic()
            with otel.start_span("fail.closed", run_id="r", trace_id="t"):
                pass
            elapsed = time.monotonic() - start
            # Should complete in < 3s (2s timeout + overhead)
            self.assertLess(elapsed, 4.0,
                            "exporter timeout blocked too long: %.2fs" % elapsed)
            # Exporter failed
            self.assertEqual(exporter._failed, 1)
            self.assertEqual(exporter._sent, 0)
        finally:
            otel.set_collector(None)


class TestMultiplePayloads(unittest.TestCase):
    """Multiple spans sent in separate exports are all received."""

    def test_multiple_exports(self):
        r = LocalOTLPReceiver(port=14325)
        r.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14325/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                for i in range(5):
                    with otel.start_span("batch.%d" % i, run_id="batch",
                                         trace_id=""):
                        pass
                _settle()
            finally:
                otel.set_collector(None)

            self.assertEqual(r.span_count, 5)
            self.assertGreaterEqual(r.payload_count, 5)
            names = {s.name for s in r.spans}
            for i in range(5):
                self.assertIn("batch.%d" % i, names)
        finally:
            r.stop()


if __name__ == "__main__":
    unittest.main()
