#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-A · Minimal local OTLP/HTTP trace receiver.

Listens on localhost:4318/v1/traces, parses OTLP JSON payloads, and stores
them in an in-memory list for test verification. This is NOT a production
otelcol — it is a minimal test fixture that proves the OTLP export pipeline
works end-to-end.

Design:
- Single-threaded HTTP server (sufficient for test throughput)
- Parses OTLP JSON (resourceSpans → scopeSpans → spans)
- Thread-safe span list (lock-protected)
- Start/stop lifecycle managed by test fixtures
- Never touches SLS, Nacos, or any external service
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class OTLPReceivedSpan:
    """A span parsed from an OTLP JSON payload."""
    __slots__ = ("trace_id", "span_id", "parent_span_id", "name",
                 "status_code", "start_time_ns", "end_time_ns",
                 "attributes", "service_name")

    def __init__(self, raw: dict, service_name: str = ""):
        self.trace_id = raw.get("traceId", "")
        self.span_id = raw.get("spanId", "")
        self.parent_span_id = raw.get("parentSpanId", "") or None
        self.name = raw.get("name", "")
        status = raw.get("status", {})
        self.status_code = status.get("code", 0)  # 0=UNSET, 1=OK, 2=ERROR
        self.start_time_ns = int(raw.get("startTimeUnixNano", "0"))
        self.end_time_ns = int(raw.get("endTimeUnixNano", "0"))
        self.service_name = service_name
        # Parse attributes into a flat dict
        self.attributes = {}
        for attr in raw.get("attributes", []):
            key = attr.get("key", "")
            val = attr.get("value", {})
            if "stringValue" in val:
                self.attributes[key] = val["stringValue"]
            elif "intValue" in val:
                self.attributes[key] = int(val["intValue"])
            elif "doubleValue" in val:
                self.attributes[key] = float(val["doubleValue"])
            elif "boolValue" in val:
                self.attributes[key] = val["boolValue"] == "true"

    @property
    def status_str(self):
        return {0: "UNSET", 1: "OK", 2: "ERROR"}.get(self.status_code, "UNKNOWN")

    @property
    def duration_ms(self):
        if self.end_time_ns and self.start_time_ns:
            return round((self.end_time_ns - self.start_time_ns) / 1e6, 2)
        return None

    def to_dict(self):
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "status": self.status_str,
            "duration_ms": self.duration_ms,
            "attributes": dict(self.attributes),
            "service_name": self.service_name,
        }


class _OTLPHandler(BaseHTTPRequestHandler):
    """HTTP handler that receives OTLP JSON and stores spans."""

    def do_POST(self):
        if self.path != "/v1/traces":
            self.send_response(404)
            self.end_headers()
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        try:
            payload = json.loads(body)
            receiver = self.server._receiver  # type: ignore
            receiver._process_payload(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *args):
        pass  # suppress access logs


class LocalOTLPReceiver:
    """Minimal local OTLP/HTTP receiver for testing.

    Usage:
        receiver = LocalOTLPReceiver(port=4318)
        receiver.start()
        # ... run instrumented code ...
        traces = receiver.get_traces()
        receiver.stop()
    """

    def __init__(self, port: int = 4318, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.spans: list[OTLPReceivedSpan] = []
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._payload_count = 0

    def start(self):
        self._server = HTTPServer((self.host, self.port), _OTLPHandler)
        self._server._receiver = self  # type: ignore
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        # Brief settle time
        time.sleep(0.1)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _process_payload(self, payload: dict):
        """Parse OTLP JSON payload and extract spans."""
        with self._lock:
            self._payload_count += 1
            for resource_span in payload.get("resourceSpans", []):
                # Extract service name from resource attributes
                service_name = ""
                for attr in resource_span.get("resource", {}).get("attributes", []):
                    if attr.get("key") == "service.name":
                        service_name = attr.get("value", {}).get("stringValue", "")
                for scope_span in resource_span.get("scopeSpans", []):
                    for raw_span in scope_span.get("spans", []):
                        self.spans.append(OTLPReceivedSpan(raw_span, service_name))

    def clear(self):
        with self._lock:
            self.spans.clear()
            self._payload_count = 0

    def get_by_trace_id(self, trace_id: str) -> list[OTLPReceivedSpan]:
        with self._lock:
            return [s for s in self.spans if s.trace_id == trace_id]

    def get_by_name(self, name: str) -> list[OTLPReceivedSpan]:
        with self._lock:
            return [s for s in self.spans if s.name == name]

    def get_by_run_id(self, run_id: str) -> list[OTLPReceivedSpan]:
        with self._lock:
            return [s for s in self.spans
                    if s.attributes.get("mp.run_id") == run_id]

    def get_traces(self) -> dict[str, list[OTLPReceivedSpan]]:
        """Group spans by trace_id."""
        with self._lock:
            traces: dict[str, list[OTLPReceivedSpan]] = {}
            for s in self.spans:
                traces.setdefault(s.trace_id, []).append(s)
            return traces

    @property
    def span_count(self):
        with self._lock:
            return len(self.spans)

    @property
    def payload_count(self):
        with self._lock:
            return self._payload_count

    def to_json(self):
        with self._lock:
            return json.dumps([s.to_dict() for s in self.spans], indent=2)
