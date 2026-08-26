#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PoC single-exit-point OTel collector initialization (Phase 4, item 9/12).

Rules frozen in docs/agentloop/AgentLoop-OTel-GenAI-PoC设计.md §5:
- Configuration comes ONLY from environment variables;
- Disabled by default: MP_OTEL_EXPORT_ENABLED != "1" returns None and the
  process behaves exactly like pre-PoC (spans created, dropped locally);
- Idempotent singleton: repeated calls return the same instance, enforcing
  the "never two TracerProviders/Exporters in one process" invariant.
"""
from __future__ import annotations

import os
import threading

import otel_spans as _otel

_lock = threading.Lock()
_initialized = False
_collector = None


def init_from_env(memory=None):
    """Build the process-wide collector once, from env only. May return None."""
    global _initialized, _collector
    with _lock:
        if _initialized:
            return _collector
        _initialized = True
        if os.environ.get("MP_OTEL_EXPORT_ENABLED", "0") != "1":
            return None  # observability off == pre-PoC behavior
        endpoint = os.environ.get("MP_OTLP_ENDPOINT",
                                  "http://127.0.0.1:4318/v1/traces")
        timeout = float(os.environ.get("MP_OTEL_EXPORT_TIMEOUT_S", "2"))
        memory_collector = memory or _otel.InMemoryCollector()
        _collector = _otel.DualCollector(
            memory=memory_collector,
            exporter=_otel.OTLPExporter(endpoint=endpoint, timeout=timeout),
        )
        _otel.set_collector(_collector)
        return _collector


def is_initialized() -> bool:
    return _initialized


def reset_for_tests():
    """Test-only: clear the singleton (collectors are per-test fixtures)."""
    global _initialized, _collector
    with _lock:
        _initialized = False
        _collector = None
        _otel.set_collector(None)


if __name__ == "__main__":  # pragma: no cover - manual smoke helper
    c = init_from_env()
    print({"enabled": c is not None,
           "endpoint_configured": bool(os.environ.get("MP_OTLP_ENDPOINT"))})
