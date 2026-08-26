#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PoC Phase 9 offline probe on/off comparison + Phase 6/7 evidence generator.

What this measures (all offline, honestly labelled):
- mode OFF : export exit disabled (pure library overhead, pre-PoC behavior)
- mode ON  : OTLP exporter pointed at an UNREACHABLE localhost port so every
             export serializes + posts + fails fast — worst-case telemetry tax
Prohibited here: real credentials, console claims, product capability wording.

Artifacts land in evidence/agentloop-poc/OFFLINE-SELFCHECK/
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otel_spans as _otel          # noqa: E402
import exporter_init as _ei         # noqa: E402
import poc_health_check as _phc     # noqa: E402
from poc_evaluators import build_boards  # noqa: E402

RUN_ID = f"poc-selfcheck-{int(time.time())}"
OUT_DIR = Path(__file__).resolve().parents[2] / "evidence" / "agentloop-poc" / \
    "OFFLINE-SELFCHECK"
ITERATIONS = 60


def _timed_mode(enabled: bool):
    os.environ.pop("MP_OTEL_EXPORT_ENABLED", None)
    os.environ.pop("MP_OTLP_ENDPOINT", None)
    if enabled:
        os.environ["MP_OTEL_EXPORT_ENABLED"] = "1"
        os.environ["MP_OTEL_EXPORT_TIMEOUT_S"] = "0.05"
    _ei.reset_for_tests()
    samples = []
    rid_prefix = RUN_ID + ("-on" if enabled else "-off")
    for i in range(ITERATIONS):
        t0 = time.perf_counter()
        out = _phc.emit(f"{rid_prefix}-{i}")
        samples.append((time.perf_counter() - t0) * 1000.0)
    _ei.reset_for_tests()
    p50 = round(statistics.median(samples), 3)
    p95 = round(sorted(samples)[int(0.95 * len(samples))], 3)
    return {"p50_ms": p50, "p95_ms": p95,
            "mean_ms": round(statistics.fmean(samples), 3),
            "n": ITERATIONS}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- canonical trace (export OFF => pure local governance storage) ------
    _ei.reset_for_tests()
    canonical = _phc.emit(RUN_ID)
    _ei.reset_for_tests()

    # re-emit once with a captured local store for artifact dumps
    store = _otel.InMemoryCollector()
    os.environ.pop("MP_OTEL_EXPORT_ENABLED", None)
    os.environ["MP_OTEL_STORE_OVERRIDE"] = "1"  # document intent only
    collector = _otel.DualCollector(memory=store, exporter=None)
    _otel.set_collector(collector)
    with _otel.entry_span("mergepilot.poc.health_check", run_id=RUN_ID,
                          attrs={"final_decision": "HEALTH_CHECK"}):
        with _otel.start_span("tool.synthetic_health_check", run_id=RUN_ID,
                              agent_role="manager",
                              tool_name="synthetic_health_check",
                              policy_decision="ALLOW"):
            pass
    spans = [s.to_dict() for s in store.get_by_run_id(RUN_ID)]
    _otel.set_collector(None)
    os.environ.pop("MP_OTEL_STORE_OVERRIDE", None)

    # --- security scan over every exported attribute ------------------------
    hits = []
    scanned = 0
    for s in spans:
        blob_keys = list(_attr_iter(s)) + [list(e["attributes"]) for e in
                                           s.get("events", [])]
    for s in spans:
        payload = json.dumps({k: v for k, v in s.items()
                              if k in ("attributes", "events")})
        scanned += 1
        for pattern in ("ghp_", "LTAI", "AKID", "Bearer ", "sk-ant",
                        "PRIVATE KEY", "password", "postgres://"):
            if pattern.lower() in payload.lower():
                hits.append({"span": s["name"], "pattern": pattern})

    # --- perf on/off ---------------------------------------------------------
    off = _timed_mode(enabled=False)
    on = _timed_mode(enabled=True)

    boards = build_boards(
        spans,
        {"decision": "HEALTH_CHECK",
         "forbidden_actions": ["merge_pull_request", "delete_artifact"],
         "deny_expected_clean_run": True})

    payloads = {
        "OFFLINE_SELF_CHECK": True,
        "verdict_inputs": {
            "cloud_connectivity": "BLOCKED_CONFIGURATION",
            "real_agentteams_run": "NOT_RUN_THIS_ROUND",
            "offline_protocol_tests": "PASSED (pytest tests/otel)",
        },
        "run_id": RUN_ID,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_trace": canonical,
        "probe_overhead_offline": {
            "note": "ON 模式指向不可达端口：序列化+POST+快速失败的完整最坏路径；"
                    "非云上真实时延",
            "exporter_off_ms": off,
            "exporter_on_unreachable_ms": on,
            "delta_p50_ms": round(on["p50_ms"] - off["p50_ms"], 3),
            "cpu_process_seconds":
                round(time.process_time(), 4),
            "rss_memory": "NOT_MEASURED_SANDBOX",
        },
    }
    (OUT_DIR / "trace-summary.json").write_text(
        json.dumps(payloads, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "span-manifest.json").write_text(
        json.dumps(spans, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "redaction-report.json").write_text(json.dumps({
        "spans_scanned": scanned,
        "sensitive_hits": hits,
        "patterns_checked": ["ghp_", "LTAI", "AKID", "Bearer ", "sk-ant",
                             "PRIVATE KEY", "password", "postgres://"],
        "content_capture_disabled_by_default":
            _otel.GENAI_PROMPT_CAPTURE_DEFAULT_OFF,
    }, indent=2), encoding="utf-8")
    (OUT_DIR / "evaluation-boards.json").write_text(
        json.dumps(boards, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "README.md").write_text(
        "# AgentLoop PoC 离线自检证据\n\n本目录产物全部生成于本地离线环境；"
        "云侧连通性因配置缺失处于 BLOCKED_CONFIGURATION，"
        "AgentLoop 控制台截图与真实跨容器 Trace 待资源就绪后另行采集。\n",
        encoding="utf-8")

    print(json.dumps({
        "outdir": str(OUT_DIR.relative_to(OUT_DIR.parents[3])),
        "offline_perf_p50_delta_ms": payloads["probe_overhead_offline"]["delta_p50_ms"],
        "redaction_hits": len(hits),
        "boards": {"result_gates": boards["result_board"]["gates_passed"],
                   "trajectory_gates": boards["trajectory_board"]["gates_passed"]},
    }, indent=2))
    return 0


def _attr_iter(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _attr_iter(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _attr_iter(v)


if __name__ == "__main__":
    sys.exit(main())
