#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-A · Exporter timeout hardening tests.

Verifies that OTLPExporter timeout is bounded to ~2s (not 4s), using:
1. A dead port (connection refused — fast fail)
2. A slow receiver that accepts the connection but never responds
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OTEL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "otel"))
if OTEL not in sys.path:
    sys.path.insert(0, OTEL)

import otel_spans as otel


class _SlowReceiver:
    """Accepts a TCP connection but never responds (forces timeout)."""

    def __init__(self, port):
        self.port = port
        self._sock = None
        self._thread = None
        self._running = False
        self._accepted = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self._sock.settimeout(5.0)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        time.sleep(0.1)

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
                self._accepted.set()
                # Hold the connection open but never respond
                time.sleep(10)
                conn.close()
            except OSError:
                break

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)


class TestExporterTimeoutDeadPort(unittest.TestCase):
    """Connection refused → fast fail, well under 2s."""

    def test_dead_port_timeout(self):
        exporter = otel.OTLPExporter(
            endpoint="http://127.0.0.1:19998/v1/traces", timeout=2.0)
        span = otel.SpanRecord("d" * 32, "e" * 16, None,
                               "test.dead", "r")
        span.end()
        start = time.monotonic()
        exporter.export(span)
        elapsed = time.monotonic() - start
        self.assertEqual(exporter._failed, 1)
        self.assertEqual(exporter._sent, 0)
        # Connection refused may take up to the full timeout on Windows
        # (WinSock doesn't immediately return ECONNREFUSED for localhost).
        # Assert bounded to ~2s timeout + platform scheduling overhead.
        self.assertLess(elapsed, 3.0,
                        "dead-port export took %.2fs (expected < 3.0s)" % elapsed)


class TestExporterTimeoutSlowReceiver(unittest.TestCase):
    """Receiver accepts but never responds → bounded to ~2s."""

    def test_slow_receiver_timeout_bounded(self):
        slow = _SlowReceiver(port=14390)
        slow.start()
        try:
            self.assertTrue(slow._accepted.wait(timeout=1.0) is not None
                            or True)  # settle
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14390/v1/traces", timeout=2.0)
            span = otel.SpanRecord("a" * 32, "b" * 16, None,
                                   "test.slow", "r")
            span.end()
            start = time.monotonic()
            exporter.export(span)
            elapsed = time.monotonic() - start
            self.assertEqual(exporter._failed, 1)
            self.assertEqual(exporter._sent, 0)
            # Timeout must be bounded to ~2s + small scheduling overhead
            self.assertLess(elapsed, 3.0,
                            "slow-receiver export took %.2fs (expected < 3.0s)" % elapsed)
            self.assertGreaterEqual(elapsed, 1.5,
                                    "slow-receiver export was too fast (%.2fs); "
                                    "timeout may not be working" % elapsed)
        finally:
            slow.stop()

    def test_business_continues_after_timeout(self):
        """Business logic returns normally after exporter timeout."""
        slow = _SlowReceiver(port=14391)
        slow.start()
        try:
            exporter = otel.OTLPExporter(
                endpoint="http://127.0.0.1:14391/v1/traces", timeout=2.0)
            dual = otel.DualCollector(
                memory=otel.InMemoryCollector(), exporter=exporter)
            otel.set_collector(dual)
            try:
                # Business logic must complete normally
                with otel.start_span("biz", run_id="r", trace_id="t") as s:
                    s.set_attribute("result", "ok")
                # Memory collector received the span despite exporter failure
                self.assertEqual(dual.memory.count, 1)
                self.assertEqual(exporter._failed, 1)
            finally:
                otel.set_collector(None)
        finally:
            slow.stop()


if __name__ == "__main__":
    unittest.main()
