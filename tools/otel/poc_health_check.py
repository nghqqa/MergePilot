#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PoC Phase 6/9 minimal trace emitter.

Emits exactly one Entry + one Tool span (offline synthetic work), through the
single env-configured exit point. Used for:
- offline tests / local OTLP receiver round-trips;
- Phase 6 minimal AgentLoop connectivity check (MP_OTEL_EXPORT_ENABLED=1 +
  MP_OTLP_ENDPOINT=<agentloop receiver>);
- Phase 9 probe on/off overhead comparison (the only knob is the env flag).

Prints ONLY opaque hex IDs and counters to stdout — no configuration values.
Exit code 0 always (telemetry must never fail the caller).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otel_spans as _otel                      # noqa: E402
from exporter_init import init_from_env         # noqa: E402


def emit(run_id: str) -> dict:
    collector = init_from_env()
    stats_before = _otel.get_export_stats()
    # Governance baseline: an offline process still keeps a local store even
    # if the external exit point is disabled.
    local_owned = False
    if collector is None or getattr(collector, "memory", None) is None:
        _otel.set_collector(_otel.InMemoryCollector())
        local_owned = True

    with _otel.entry_span("mergepilot.poc.health_check", run_id=run_id,
                          attrs={"final_decision": "HEALTH_CHECK"}) as entry:
        # Synthetic in-process tool work — no network, no upstream dependency.
        with _otel.start_span("tool.synthetic_health_check", run_id=run_id,
                              agent_role="manager",
                              tool_name="synthetic_health_check") as tool:
            time.sleep(0.01)
            tool.set_attribute("tool_status", "OK")
        entry.set_attribute("mp.policy_decision", "ALLOW")

    spans = []
    if local_owned:
        spans = [s.to_dict() for s in
                 (_otel.get_collector() or _otel.InMemoryCollector())
                 .get_by_run_id(run_id)]
        _otel.set_collector(None)
    else:
        spans = [s.to_dict() for s in collector.memory.get_by_run_id(run_id)]
    return {
        "run_id": run_id,
        "trace_id": spans[0]["trace_id"] if spans else "",
        "span_names": [s["name"] for s in spans],
        "parent_links_ok": bool(spans) and all(
            s["parent_span_id"] or s["name"].endswith("health_check")
            for s in spans),
        "export_stats": {
            k: v - stats_before[k] for k, v in _otel.get_export_stats().items()
        },
        "collector_enabled": collector is not None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True,
                    help="opaque run identifier (hex/word chars)")
    ns = ap.parse_args()
    try:
        out = emit(ns.run_id)
    except Exception as exc:  # never break the caller on telemetry issues
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:120]}))
        return 0
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
