#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-A · OTel observability deterministic tests.

Covers the 4 required paths (normal/deny/timeout/rollback) plus
context propagation, redaction, and fail-closed behavior.
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


class TestContextPropagation(unittest.TestCase):
    """Parent→child trace_id/run_id propagation."""

    def test_child_inherits_trace_id(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.start_span("parent", run_id="r1", trace_id="t1",
                                 agent_role="coordinator") as p:
                with otel.start_span("child", run_id="r1") as child:
                    child.set_attribute("x", 1)
            self.assertEqual(len(c.spans), 2)
            # spans are added on END, so child appears before parent
            by_name = {s.name: s for s in c.spans}
            parent = by_name["parent"]
            child_span = by_name["child"]
            self.assertEqual(parent.trace_id, child_span.trace_id)
            self.assertEqual(child_span.parent_span_id, parent.span_id)
            self.assertIsNone(parent.parent_span_id)
        finally:
            otel.set_collector(None)

    def test_run_id_propagates_to_children(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.start_span("root", run_id="run-abc", trace_id="trace-1"):
                with otel.start_span("child1"):
                    pass
                with otel.start_span("child2"):
                    pass
            for s in c.spans:
                self.assertEqual(s.run_id, "run-abc")
                self.assertEqual(s.attributes.get("mp.trace_id"), "trace-1")
        finally:
            otel.set_collector(None)


class TestRedaction(unittest.TestCase):
    """Sensitive fields are never written to span attributes."""

    def test_sensitive_key_redacted(self):
        for key in ("token", "api_key", "PASSWORD", "Authorization",
                    "secret_data", "auth_token"):
            self.assertTrue(otel._is_sensitive_key(key),
                            "%s should be sensitive" % key)

    def test_sensitive_value_redacted(self):
        for val in ("ghp_abc123", "sk-live-xxx", "AKIAIOSFODNN7EXAMPLE",
                     "xoxb-12345"):
            self.assertTrue(otel._is_sensitive_value(val),
                            "%r should be sensitive" % val[:10])

    def test_span_redacts_sensitive_attrs(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.start_span("test", run_id="r", trace_id="t",
                                 api_key="sk-secret-12345",
                                 normal_field="ok") as span:
                span.set_attribute("auth_token", "ghp_secret")
                span.set_attribute("safe_attr", "safe-value")
            s = c.spans[0]
            self.assertEqual(s.attributes["api_key"], "<redacted>")
            self.assertEqual(s.attributes["auth_token"], "<redacted>")
            self.assertEqual(s.attributes["normal_field"], "ok")
            self.assertEqual(s.attributes["safe_attr"], "safe-value")
        finally:
            otel.set_collector(None)

    def test_redact_attributes_function(self):
        attrs = {"token": "secret", "name": "ok", "api_key": "k"}
        r = otel.redact_attributes(attrs)
        self.assertEqual(r["token"], "<redacted>")
        self.assertEqual(r["api_key"], "<redacted>")
        self.assertEqual(r["name"], "ok")


class TestNormalPath(unittest.TestCase):
    """Controller → Skill → Gateway → MCP all succeed."""

    def test_normal_vertical_slice(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="run-1", trace_id="trace-1",
                                      agent_role="coordinator",
                                      stage="review", attempt=1):
                # skill 1
                with otel.skill_span(run_id="run-1", trace_id="trace-1",
                                     skill_name="diff_parse",
                                     request_id="req-1"):
                    time.sleep(0.01)

                # gateway call
                with otel.gateway_span(run_id="run-1", trace_id="trace-1",
                                       correlation_id="corr-1",
                                       tool="pull_request_read"):
                    with otel.mcp_span(run_id="run-1", trace_id="trace-1",
                                       correlation_id="corr-1",
                                       endpoint="/pulls/1", method="GET"):
                        time.sleep(0.01)
                    # set decision after MCP returns
                # set decision on gateway span
                for s in c.spans:
                    if s.name == "gateway.call_tool":
                        s.set_attribute("mp.decision", "ALLOW")

                # skill 2
                with otel.skill_span(run_id="run-1", trace_id="trace-1",
                                     skill_name="sast_scan",
                                     request_id="req-2"):
                    time.sleep(0.01)

            # verify
            self.assertEqual(len(c.spans), 5)
            names = [s.name for s in c.spans]
            self.assertIn("controller.process_event", names)
            self.assertIn("skill.diff_parse", names)
            self.assertIn("gateway.call_tool", names)
            self.assertIn("mcp.upstream", names)
            self.assertIn("skill.sast_scan", names)

            # all OK
            for s in c.spans:
                self.assertEqual(s.status, "OK")

            # all same run_id + trace_id
            for s in c.spans:
                self.assertEqual(s.run_id, "run-1")

            # controller is root (no parent)
            ctrl = [s for s in c.spans if s.name == "controller.process_event"][0]
            self.assertIsNone(ctrl.parent_span_id)

            # mcp is child of gateway
            gw = [s for s in c.spans if s.name == "gateway.call_tool"][0]
            mcp = [s for s in c.spans if s.name == "mcp.upstream"][0]
            self.assertEqual(mcp.parent_span_id, gw.span_id)

            # gateway has decision
            self.assertEqual(gw.attributes.get("mp.decision"), "ALLOW")

            # all have duration
            for s in c.spans:
                self.assertIsNotNone(s.duration_ms)
                self.assertGreater(s.duration_ms, 0)
        finally:
            otel.set_collector(None)


class TestDenyPath(unittest.TestCase):
    """Gateway returns DENY — skill span not created."""

    def test_deny_sets_error_status(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="run-2", trace_id="trace-2",
                                      stage="review"):
                with otel.gateway_span(run_id="run-2", trace_id="trace-2",
                                       correlation_id="corr-2",
                                       tool="merge_pull_request") as gw:
                    gw.set_attribute("mp.decision", "DENY")
                    gw.set_status("ERROR")
                    gw.add_event("policy.denied", {"reason": "L2 unauthorized"})
                    # skill NOT started (denied)

            gw_span = [s for s in c.spans if s.name == "gateway.call_tool"][0]
            self.assertEqual(gw_span.status, "ERROR")
            self.assertEqual(gw_span.attributes["mp.decision"], "DENY")
            self.assertTrue(len(gw_span.events) > 0)
            self.assertEqual(gw_span.events[0]["name"], "policy.denied")

            # no skill spans created
            skill_spans = [s for s in c.spans if s.name.startswith("skill.")]
            self.assertEqual(len(skill_spans), 0)
        finally:
            otel.set_collector(None)


class TestTimeoutPath(unittest.TestCase):
    """MCP call times out."""

    def test_timeout_sets_error_with_event(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="run-3", trace_id="trace-3",
                                      stage="fix"):
                with otel.gateway_span(run_id="run-3", trace_id="trace-3",
                                       correlation_id="corr-3",
                                       tool="create_branch"):
                    with otel.mcp_span(run_id="run-3", trace_id="trace-3",
                                       correlation_id="corr-3",
                                       endpoint="/branches",
                                       method="POST") as mcp:
                        mcp.set_status("ERROR")
                        mcp.add_event("timeout", {"deadline_ms": 5000})

            mcp_span = [s for s in c.spans if s.name == "mcp.upstream"][0]
            self.assertEqual(mcp_span.status, "ERROR")
            self.assertTrue(any(e["name"] == "timeout" for e in mcp_span.events))
        finally:
            otel.set_collector(None)


class TestRollbackPath(unittest.TestCase):
    """Controller verify-fail → rollback."""

    def test_rollback_records_event_on_controller(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="run-4", trace_id="trace-4",
                                      stage="verify", attempt=1) as ctrl:
                # verify skill
                with otel.skill_span(run_id="run-4", trace_id="trace-4",
                                     skill_name="test_runner",
                                     request_id="req-4"):
                    pass  # skill runs

                # verify FAIL → rollback
                ctrl.set_attribute("mp.verdict", "FAIL")
                ctrl.add_event("rollback.initiated", {
                    "reason": "verify_failed",
                    "parent_run_id": "run-4",
                })
                ctrl.add_event("rollback.completed", {
                    "revert_commit": "abc123",
                })

            ctrl_span = [s for s in c.spans if s.name == "controller.process_event"][0]
            self.assertEqual(ctrl_span.attributes.get("mp.verdict"), "FAIL")
            events = [e["name"] for e in ctrl_span.events]
            self.assertIn("rollback.initiated", events)
            self.assertIn("rollback.completed", events)
            # controller span itself is OK (rollback succeeded)
            self.assertEqual(ctrl_span.status, "OK")
        finally:
            otel.set_collector(None)


class TestExceptionHandling(unittest.TestCase):
    """Exception in span sets ERROR status and records event."""

    def test_exception_sets_error(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with self.assertRaises(ValueError):
                with otel.start_span("failing", run_id="r", trace_id="t"):
                    raise ValueError("test error")
            s = c.spans[0]
            self.assertEqual(s.status, "ERROR")
            self.assertTrue(len(s.events) > 0)
            self.assertEqual(s.events[0]["name"], "exception")
            self.assertEqual(s.events[0]["attributes"]["type"], "ValueError")
            # message is truncated but present
            self.assertIn("test error", s.events[0]["attributes"]["message"])
        finally:
            otel.set_collector(None)

    def test_exception_does_not_leak_traceback(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with self.assertRaises(RuntimeError):
                with otel.start_span("crash", run_id="r", trace_id="t"):
                    raise RuntimeError("crash with detail")
            s = c.spans[0]
            # verify no traceback field in events
            for ev in s.events:
                self.assertNotIn("traceback", ev["attributes"])
                self.assertNotIn("tb", ev["attributes"])
        finally:
            otel.set_collector(None)


class TestFailClosed(unittest.TestCase):
    """Collector failure does not break business logic."""

    def test_broken_collector_does_not_crash(self):
        class BrokenCollector:
            def add_span(self, span):
                raise RuntimeError("collector broken")

        otel.set_collector(BrokenCollector())
        try:
            # business logic must succeed despite broken collector
            with otel.start_span("test", run_id="r", trace_id="t") as span:
                span.set_attribute("x", 1)
            # if we reach here, fail-closed worked
            self.assertTrue(True)
        finally:
            otel.set_collector(None)


class TestSpanHierarchy(unittest.TestCase):
    """Verify proper parent-child relationships."""

    def test_full_hierarchy(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="r", trace_id="t",
                                      agent_role="coordinator"):
                with otel.gateway_span(run_id="r", trace_id="t",
                                       tool="read"):
                    with otel.mcp_span(run_id="r", trace_id="t",
                                       endpoint="/x"):
                        pass

            # find by name (start_time may tie on fast execution)
            by_name = {s.name: s for s in c.spans}
            ctrl = by_name["controller.process_event"]
            gw = by_name["gateway.call_tool"]
            mcp = by_name["mcp.upstream"]
            # controller is root
            self.assertIsNone(ctrl.parent_span_id)
            # gateway is child of controller
            self.assertEqual(gw.parent_span_id, ctrl.span_id)
            # mcp is child of gateway
            self.assertEqual(mcp.parent_span_id, gw.span_id)
            # all share trace_id
            self.assertEqual(ctrl.trace_id, gw.trace_id)
            self.assertEqual(gw.trace_id, mcp.trace_id)
        finally:
            otel.set_collector(None)


class TestRequiredAttributes(unittest.TestCase):
    """All spans must carry mp.run_id, mp.trace_id, mp.agent_role."""

    def test_controller_span_has_required(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.controller_span(run_id="r", trace_id="t",
                                      agent_role="coordinator",
                                      stage="review"):
                pass
            s = c.spans[0]
            self.assertEqual(s.attributes["mp.run_id"], "r")
            self.assertEqual(s.attributes["mp.trace_id"], "t")
            self.assertEqual(s.attributes["mp.agent_role"], "coordinator")
            self.assertEqual(s.attributes["mp.stage"], "review")
        finally:
            otel.set_collector(None)

    def test_skill_span_has_required(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.skill_span(run_id="r", trace_id="t",
                                 skill_name="sast_scan",
                                 skill_version="1.0",
                                 request_id="req-1",
                                 agent_role="reviewer"):
                pass
            s = c.spans[0]
            self.assertEqual(s.name, "skill.sast_scan")
            self.assertEqual(s.attributes["mp.skill_name"], "sast_scan")
            self.assertEqual(s.attributes["mp.skill_version"], "1.0")
            self.assertEqual(s.attributes["mp.request_id"], "req-1")
            self.assertEqual(s.attributes["mp.agent_role"], "reviewer")
        finally:
            otel.set_collector(None)

    def test_gateway_span_has_required(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            with otel.gateway_span(run_id="r", trace_id="t",
                                   correlation_id="c1",
                                   tool="merge_pull_request",
                                   agent_role="coordinator"):
                pass
            s = c.spans[0]
            self.assertEqual(s.attributes["mp.correlation_id"], "c1")
            self.assertEqual(s.attributes["mp.tool"], "merge_pull_request")
        finally:
            otel.set_collector(None)


class TestRetryCount(unittest.TestCase):
    """Span carries retry count for controller attempts."""

    def test_attempt_tracked(self):
        c = otel.InMemoryCollector()
        otel.set_collector(c)
        try:
            for attempt in range(1, 4):
                with otel.controller_span(run_id="r", trace_id="t",
                                          stage="verify",
                                          attempt=attempt):
                    if attempt < 3:
                        pass  # retry
                    else:
                        break  # success
            verify_spans = [s for s in c.spans
                           if s.attributes.get("mp.stage") == "verify"]
            self.assertEqual(len(verify_spans), 3)
            self.assertEqual(verify_spans[0].attributes["mp.attempt"], 1)
            self.assertEqual(verify_spans[2].attributes["mp.attempt"], 3)
        finally:
            otel.set_collector(None)


if __name__ == "__main__":
    unittest.main()
