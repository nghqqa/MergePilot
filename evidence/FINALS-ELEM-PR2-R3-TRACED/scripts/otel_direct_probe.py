#!/opt/venv/standard/bin/python
"""Direct-to-SLS OTLP export probe — runs INSIDE a worker container.

Proves the container can reach the AgentLoop endpoint directly (no relay) with the
configured headers: builds one real span with the same Resource the live
instrumentation uses, encodes it with the OTLP protobuf encoder and POSTs it with
`requests`, printing the HTTP status code and response body (this is the explicit
"200" evidence). Reads /etc/agentloop-otel.json; prints no secrets.
"""
import json
import sys
import time

CFG = "/etc/agentloop-otel.json"


def main():
    cfg = json.load(open(CFG))
    if not cfg.get("otlp"):
        print("PROBE_SKIP: otlp endpoint not configured")
        return 2
    import requests
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import os
    w = os.environ.get("AGENTTEAMS_WORKER_NAME", "probe")
    res = Resource.create({
        "service.name": cfg.get("service_name") or "mergepilot-copaw",
        "service.version": "223ddc2-agentloop",
        "deployment.environment": "demo",
        "agentteams.worker": w,
        "acs.cms.workspace": cfg.get("workspace", ""),
        "acs.arms.service.feature": "genai_app",
        "gen_ai.instrumentation.sdk.name": "loongsuite-genai-utils",
    })
    mem = InMemorySpanExporter()
    tp = TracerProvider(resource=res)
    tp.add_span_processor(SimpleSpanProcessor(mem))
    tr = tp.get_tracer("agentteams.agentloop.probe", "2.1.0")
    with tr.start_as_current_span("agentloop.direct_export_probe", attributes={
        "agentteams.event_type": "probe", "agentteams.worker": w,
        "probe.ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) as s:
        s.set_attribute("probe.note", "direct SLS export, no relay")
    spans = mem.get_finished_spans()
    body = encode_spans(spans).SerializeToString()
    headers = dict(cfg.get("headers") or {})
    headers["Content-Type"] = "application/x-protobuf"
    t0 = time.time()
    r = requests.post(cfg["otlp"], data=body, headers=headers, timeout=20)
    dt = round((time.time() - t0) * 1000)
    trace_id = format(spans[0].get_span_context().trace_id, "032x")
    print(json.dumps({
        "probe": "agentloop.direct_export_probe",
        "worker": w,
        "endpoint_host": cfg["otlp"].split("/")[2],
        "headers_sent": sorted(k for k in headers if k != "Content-Type"),
        "payload_bytes": len(body),
        "http_status": r.status_code,
        "latency_ms": dt,
        "response_body": r.text[:200],
        "trace_id": trace_id,
    }, ensure_ascii=False))
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
