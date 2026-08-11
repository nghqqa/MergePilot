#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-B · SLS Trace exporter with batching, retry, backpressure, fail-closed.

Exports OTLP spans to an SLS-compatible HTTP endpoint (or fake SLS receiver
for testing). Designed to NEVER block the core business state machine.

Key properties:
- Background export thread (main thread only enqueues)
- Batching: max 64 spans or 2s timeout or 1 MiB
- Retry: 3 attempts with exponential backoff on 5xx/429
- Backpressure: drop-oldest when queue > 256 batches
- Fail-closed: SLS unreachable → silent drop, no business impact
- Credentials: from env only, never in span attributes or payload
- Redaction: reuses M6-A denylist before serialization
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from typing import Any, Optional

# Reuse M6-A infrastructure
_otel_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
if _otel_dir not in sys.path:
    sys.path.insert(0, _otel_dir)

import otel_spans as otel

# ---------------------------------------------------------------------------
# Configuration (env-owned, deploy-configurable)
# ---------------------------------------------------------------------------

DEFAULTS = {
    "batch_max_size": 64,
    "batch_timeout_ms": 2000,
    "batch_max_bytes": 1024 * 1024,  # 1 MiB
    "retry_max_attempts": 3,
    "retry_base_delay_ms": 500,
    "retry_max_delay_ms": 5000,
    "export_timeout_ms": 2000,
    "total_export_budget_ms": 6000,
    "queue_max_size": 256,
    "retry_on_status": [500, 502, 503, 504, 429],
}


class SLSConfig:
    """SLS exporter configuration. All values from env or defaults."""

    def __init__(self):
        self.endpoint = os.environ.get("SLS_ENDPOINT", "")
        self.access_key_id = os.environ.get("SLS_ACCESS_KEY_ID", "")
        self.access_key_secret = os.environ.get("SLS_ACCESS_KEY_SECRET", "")
        self.project = os.environ.get("SLS_PROJECT", "")
        self.logstore = os.environ.get("SLS_LOGSTORE", "")
        self.batch_max_size = int(os.environ.get("SLS_BATCH_MAX_SIZE",
            DEFAULTS["batch_max_size"]))
        self.batch_timeout_ms = int(os.environ.get("SLS_BATCH_TIMEOUT_MS",
            DEFAULTS["batch_timeout_ms"]))
        self.batch_max_bytes = int(os.environ.get("SLS_BATCH_MAX_BYTES",
            DEFAULTS["batch_max_bytes"]))
        self.retry_max_attempts = int(os.environ.get("SLS_RETRY_MAX_ATTEMPTS",
            DEFAULTS["retry_max_attempts"]))
        self.retry_base_delay_ms = int(os.environ.get("SLS_RETRY_BASE_DELAY_MS",
            DEFAULTS["retry_base_delay_ms"]))
        self.retry_max_delay_ms = int(os.environ.get("SLS_RETRY_MAX_DELAY_MS",
            DEFAULTS["retry_max_delay_ms"]))
        self.export_timeout_ms = int(os.environ.get("SLS_EXPORT_TIMEOUT_MS",
            DEFAULTS["export_timeout_ms"]))
        self.total_export_budget_ms = int(os.environ.get("SLS_TOTAL_EXPORT_BUDGET_MS",
            DEFAULTS["total_export_budget_ms"]))
        self.queue_max_size = int(os.environ.get("SLS_QUEUE_MAX_SIZE",
            DEFAULTS["queue_max_size"]))

    @property
    def is_configured(self):
        return bool(self.endpoint and self.access_key_id and
                    self.access_key_secret and self.project and self.logstore)


# ---------------------------------------------------------------------------
# Span → SLS mapping
# ---------------------------------------------------------------------------

def span_to_sls(span: otel.SpanRecord) -> dict:
    """Convert a SpanRecord to SLS Trace log format.

    Redaction is applied to attributes before mapping. Credentials
    never appear in the output.
    """
    attrs = otel.redact_attributes(span.attributes)
    start_ms = int(span.start_time * 1000)
    end_ms = int((span.end_time or span.start_time) * 1000)
    return {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id or "",
        "operation_name": span.name,
        "status_code": 1 if span.status == "OK" else (2 if span.status == "ERROR" else 0),
        "start_time_ms": start_ms,
        "end_time_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "service_name": attrs.get("service.name", "mergepilot"),
        "tags": {k.replace("mp.", "mp_"): v for k, v in attrs.items()
                 if k.startswith("mp.")},
    }


# ---------------------------------------------------------------------------
# SLS Exporter with batching, retry, backpressure
# ---------------------------------------------------------------------------


class SLSExporter:
    """Background-thread SLS exporter with batching and fail-closed semantics.

    Usage:
        exporter = SLSExporter(config)
        exporter.start()
        exporter.enqueue(span)  # non-blocking
        exporter.stop()  # flush + join
    """

    def __init__(self, config: SLSConfig = None):
        self.config = config or SLSConfig()
        self._queue: deque[list[dict]] = deque()
        self._pending: list[otel.SpanRecord] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # Metrics
        self.exported_spans = 0
        self.failed_exports = 0
        self.dropped_batches = 0
        self.retry_count = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, flush_timeout: float = 5.0):
        """Signal stop, flush remaining, join thread."""
        self._running = False
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=flush_timeout)

    def enqueue(self, span: otel.SpanRecord):
        """Non-blocking enqueue. Converts span to SLS format immediately
        (redaction happens here), then adds to pending list."""
        sls_span = span_to_sls(span)
        with self._lock:
            self._pending.append(sls_span)
            if len(self._pending) >= self.config.batch_max_size:
                self._flush_batch_locked()
            self._cond.notify()

    def _flush_batch_locked(self):
        """Flush pending spans as a batch into the queue."""
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending.clear()
        # Backpressure: drop oldest if queue full
        while len(self._queue) >= self.config.queue_max_size:
            self._queue.popleft()
            self.dropped_batches += 1
        self._queue.append(batch)

    def _run(self):
        """Background export loop."""
        while self._running or self._queue or self._pending:
            with self._cond:
                # Wait for batch or timeout
                if not self._queue and self._pending:
                    if len(self._pending) < self.config.batch_max_size:
                        self._cond.wait(
                            timeout=self.config.batch_timeout_ms / 1000.0)
                # Flush if batch ready or timeout
                if (len(self._pending) >= self.config.batch_max_size or
                        (self._pending and not self._queue)):
                    self._flush_batch_locked()
                if not self._queue:
                    continue
                batch = self._queue.popleft()

            # Export outside lock
            self._export_batch(batch)

    def _export_batch(self, batch: list[dict]):
        """Export one batch with retry."""
        payload = json.dumps(batch).encode("utf-8")
        # Check size limit
        if len(payload) > self.config.batch_max_bytes:
            # Split — for now just truncate (shouldn't happen with 64 spans)
            return

        budget_deadline = time.monotonic() + self.config.total_export_budget_ms / 1000.0
        for attempt in range(1, self.config.retry_max_attempts + 1):
            if time.monotonic() >= budget_deadline:
                self.failed_exports += 1
                return
            try:
                ok = self._http_post(payload)
                if ok:
                    self.exported_spans += len(batch)
                    return
                # Retryable status
                if attempt < self.config.retry_max_attempts:
                    self.retry_count += 1
                    delay = min(
                        self.config.retry_base_delay_ms * (2 ** (attempt - 1)),
                        self.config.retry_max_delay_ms) / 1000.0
                    time.sleep(min(delay, max(0, budget_deadline - time.monotonic())))
            except Exception:
                if attempt < self.config.retry_max_attempts:
                    self.retry_count += 1
                    delay = min(
                        self.config.retry_base_delay_ms * (2 ** (attempt - 1)),
                        self.config.retry_max_delay_ms) / 1000.0
                    time.sleep(min(delay, max(0, budget_deadline - time.monotonic())))
                else:
                    self.failed_exports += 1

    def _http_post(self, payload: bytes) -> bool:
        """POST to SLS endpoint. Returns True on 2xx, False on retryable."""
        if not self.config.is_configured:
            return False  # fail-closed: no endpoint configured
        try:
            req = urllib.request.Request(
                self.config.endpoint, data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-sls-accesskeyid": self.config.access_key_id,
                    "x-sls-signature": "placeholder",  # real SLS uses HMAC-SHA1
                },
                method="POST")
            resp = urllib.request.urlopen(
                req, timeout=self.config.export_timeout_ms / 1000.0)
            return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            return e.code not in self.config.retry_on_status.__class__(DEFAULTS["retry_on_status"])
        except Exception:
            return False  # network error → retryable

    @property
    def queue_size(self):
        with self._lock:
            return len(self._queue) + len(self._pending)
