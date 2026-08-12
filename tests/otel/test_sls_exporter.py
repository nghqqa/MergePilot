#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-B · SLS exporter tests: schema, redaction, retry, timeout, backpressure, fail-closed."""
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


def _make_span(name="test.span", run_id="r1", trace_id="t1",
               agent_role="reviewer", **attrs):
    span = otel.SpanRecord(trace_id, "b" * 16, None, name, run_id)
    span.set_attribute("mp.run_id", run_id)
    span.set_attribute("mp.trace_id", trace_id)
    span.set_attribute("mp.agent_role", agent_role)
    for k, v in attrs.items():
        span.set_attribute(k, v)
    span.end()
    return span


class TestSpanToSLSMapping(unittest.TestCase):

    def test_basic_mapping(self):
        span = _make_span("controller.process_event", stage="review", attempt=1,
                          **{"mp.stage": "review", "mp.attempt": "1"})
        sls = span_to_sls(span)
        self.assertEqual(sls["trace_id"], "t1")
        self.assertEqual(sls["span_id"], "b" * 16)
        self.assertEqual(sls["operation_name"], "controller.process_event")
        self.assertEqual(sls["status_code"], 1)  # OK
        self.assertIn("duration_ms", sls)
        self.assertEqual(sls["tags"]["mp_run_id"], "r1")
        self.assertEqual(sls["tags"]["mp_agent_role"], "reviewer")
        self.assertEqual(sls["tags"]["mp_stage"], "review")
        self.assertEqual(sls["tags"]["mp_attempt"], "1")
        self.assertEqual(sls["service_name"], "mergepilot")

    def test_error_status_mapping(self):
        span = _make_span()
        span.set_status("ERROR")
        sls = span_to_sls(span)
        self.assertEqual(sls["status_code"], 2)

    def test_redaction_before_mapping(self):
        span = _make_span(authorization="Bearer ghp_secret123",
                          api_key="sk-live-abc")
        sls = span_to_sls(span)
        # Sensitive attrs should be redacted in tags
        raw = json.dumps(sls)
        self.assertNotIn("ghp_secret", raw)
        self.assertNotIn("sk-live", raw)


class TestFakeSLSReceiver(unittest.TestCase):

    def test_receive_spans(self):
        r = FakeSLSReceiver(port=14401)
        r.start()
        try:
            exporter = SLSExporter(SLSConfig())
            exporter.config.endpoint = "http://127.0.0.1:14401"
            exporter.config.access_key_id = "fake"
            exporter.config.access_key_secret = "fake"
            exporter.config.project = "test"
            exporter.config.logstore = "trace"
            exporter.start()
            try:
                for i in range(5):
                    exporter.enqueue(_make_span(f"span.{i}"))
                time.sleep(1.0)  # allow background export
            finally:
                exporter.stop()
            self.assertGreaterEqual(r.span_count, 5)
        finally:
            r.stop()


class TestBatching(unittest.TestCase):

    def test_batch_under_max_uses_timeout(self):
        """Fewer than batch_max_size spans should still export via timeout."""
        r = FakeSLSReceiver(port=14402)
        r.start()
        try:
            cfg = SLSConfig()
            cfg.endpoint = "http://127.0.0.1:14402"
            cfg.access_key_id = "fake"
            cfg.access_key_secret = "fake"
            cfg.project = "test"
            cfg.logstore = "trace"
            cfg.batch_max_size = 100  # larger than test count
            cfg.batch_timeout_ms = 500  # short timeout
            exporter = SLSExporter(cfg)
            exporter.start()
            try:
                for i in range(3):
                    exporter.enqueue(_make_span(f"batch.span.{i}"))
                time.sleep(1.5)  # wait for timeout flush
            finally:
                exporter.stop()
            self.assertGreaterEqual(r.span_count, 3)
        finally:
            r.stop()


class TestRedactionInSLS(unittest.TestCase):

    def test_pat_not_in_sls_payload(self):
        r = FakeSLSReceiver(port=14403)
        r.start()
        try:
            cfg = SLSConfig()
            cfg.endpoint = "http://127.0.0.1:14403"
            cfg.access_key_id = "fake"
            cfg.access_key_secret = "fake"
            cfg.project = "test"
            cfg.logstore = "trace"
            exporter = SLSExporter(cfg)
            exporter.start()
            try:
                span = _make_span(authorization="ghp_secret_token_12345",
                                  api_key="sk-live-key-12345")
                exporter.enqueue(span)
                time.sleep(1.0)
            finally:
                exporter.stop()
            # Verify no secret in received payload
            raw = r.to_json()
            self.assertNotIn("ghp_secret", raw)
            self.assertNotIn("sk-live", raw)
        finally:
            r.stop()


class TestFailClosedUnconfigured(unittest.TestCase):

    def test_unconfigured_exporter_silently_drops(self):
        cfg = SLSConfig()  # no endpoint configured
        exporter = SLSExporter(cfg)
        exporter.start()
        try:
            exporter.enqueue(_make_span())
            time.sleep(0.5)
            # Should not crash; spans silently dropped
            self.assertEqual(exporter.exported_spans, 0)
        finally:
            exporter.stop()


class TestFailClosedUnreachable(unittest.TestCase):

    def test_unreachable_does_not_block(self):
        cfg = SLSConfig()
        cfg.endpoint = "http://127.0.0.1:19997/v1/traces"  # dead port
        cfg.access_key_id = "fake"
        cfg.access_key_secret = "fake"
        cfg.project = "test"
        cfg.logstore = "trace"
        cfg.export_timeout_ms = 500
        cfg.total_export_budget_ms = 1500
        exporter = SLSExporter(cfg)
        exporter.start()
        try:
            start = time.monotonic()
            for i in range(10):
                exporter.enqueue(_make_span(f"unreachable.{i}"))
            # enqueue is non-blocking; verify it returned quickly
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.5,
                            "enqueue blocked too long")
            time.sleep(2.0)  # let background thread attempt+fail
            self.assertEqual(exporter.exported_spans, 0)
        finally:
            exporter.stop()


class TestBackpressureDropOldest(unittest.TestCase):

    def test_queue_overflow_drops_oldest(self):
        cfg = SLSConfig()
        cfg.endpoint = "http://127.0.0.1:19996"  # unreachable
        cfg.access_key_id = "fake"
        cfg.access_key_secret = "fake"
        cfg.project = "test"
        cfg.logstore = "trace"
        cfg.queue_max_size = 4  # very small
        cfg.batch_max_size = 2
        cfg.export_timeout_ms = 200
        cfg.total_export_budget_ms = 400
        exporter = SLSExporter(cfg)
        exporter.start()
        try:
            # Enqueue many spans faster than they can export
            for i in range(100):
                exporter.enqueue(_make_span(f"overflow.{i}"))
            time.sleep(2.0)
            self.assertGreater(exporter.dropped_batches, 0,
                              "expected dropped_batches > 0 from overflow")
        finally:
            exporter.stop()


class TestCredentialsNotInPayload(unittest.TestCase):

    def test_credentials_not_leaked(self):
        span = _make_span()
        sls = span_to_sls(span)
        raw = json.dumps(sls)
        # Credentials should never appear in the SLS payload
        self.assertNotIn("access_key", raw)
        self.assertNotIn("secret", raw)
        self.assertNotIn("AKID", raw)


if __name__ == "__main__":
    unittest.main()
