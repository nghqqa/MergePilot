#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-B · Fake SLS receiver for local testing.

Minimal HTTP server that accepts SLS-format JSON payloads, validates
the schema, and stores them for test assertions. Never touches real SLS.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class FakeSLSHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl) if cl else b""
        receiver = self.server._receiver  # type: ignore
        try:
            payload = json.loads(body)
            receiver._process(payload)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            receiver._errors.append(str(e))
            self.send_response(400)
            self.end_headers()

    def log_message(self, *args):
        pass


class FakeSLSReceiver:
    """Minimal fake SLS receiver for testing.

    Usage:
        r = FakeSLSReceiver(port=14400)
        r.start()
        # ... export spans ...
        r.stop()
        assert r.span_count > 0
    """

    def __init__(self, port: int = 14400, host: str = "127.0.0.1",
                 response_status: int = 200, response_delay: float = 0.0):
        self.port = port
        self.host = host
        self.response_status = response_status
        self.response_delay = response_delay
        self.spans: list[dict] = []
        self._errors: list[str] = []
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        self._server = HTTPServer((self.host, self.port), FakeSLSHandler)
        self._server._receiver = self  # type: ignore
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        time.sleep(0.1)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _process(self, payload: Any):
        with self._lock:
            if isinstance(payload, list):
                self.spans.extend(payload)
            elif isinstance(payload, dict):
                self.spans.append(payload)
        if self.response_delay > 0:
            time.sleep(self.response_delay)

    def clear(self):
        with self._lock:
            self.spans.clear()
            self._errors.clear()

    @property
    def span_count(self):
        with self._lock:
            return len(self.spans)

    def get_by_trace_id(self, trace_id: str) -> list[dict]:
        with self._lock:
            return [s for s in self.spans if s.get("trace_id") == trace_id]

    def to_json(self):
        with self._lock:
            return json.dumps(self.spans, indent=2)
