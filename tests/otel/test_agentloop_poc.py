#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PoC Phase 5 offline tests: AgentLoop OTel GenAI trace protocol.

Coverage map (task mandate §5, items 1-20):
 1  Entry→Agent→Tool hierarchy          test_entry_agent_tool_hierarchy
 2  four agent role mapping             test_four_agent_roles
 3  HTTP traceparent round-trip         test_http_traceparent_roundtrip
 4  Matrix carrier serialize/restore    test_matrix_carrier_roundtrip (+idempotent)
 5  async Span Link                     test_async_handoff_link
 6  illegal traceparent fail-closed     test_illegal_traceparent_failclosed / carrier variant
 7  controller stage event              test_stage_transition_event
 8  Gateway ALLOW span                  test_gateway_allow
 9  Gateway DENY span                   test_gateway_deny_marks_error
 10 Gateway HOLD span                   test_gateway_hold
 11 exporter unreachable not blocking   test_exporter_unreachable_never_raises
 12 exporter failure counters           test_export_failure_counters
 13 prompt capture off by default       test_prompt_response_not_captured
 14 response capture off (same guard)   └ covered jointly
 15 PAT/DSN/Authorization redaction     test_sensitive_value_forms_redacted
 16 redaction failure drops export      test_redaction_failure_drops_export
 17 single provider/exporter            test_exporter_init_singleton_and_default_off
 18 no credential file reads            test_exporter_init_reads_no_files
 19 zero-network offline                test_health_check_emitter_offline
20 regression of existing suites        executed via pytest tests/otel in CI command
"""
import contextlib
import inspect
import json
import sys
from pathlib import Path

import pytest

OTEL_DIR = Path(__file__).resolve().parents[2] / "tools" / "otel"
sys.path.insert(0, str(OTEL_DIR))

import otel_spans as ot        # noqa: E402
import exporter_init as ei     # noqa: E402

sys.path.insert(0, str(OTEL_DIR))  # ensure poc emitter importable too


def _ids():
    return ot._gen_trace_id(), ot._gen_span_id()


@pytest.fixture()
def mem():
    collector = ot.InMemoryCollector()
    ot.set_collector(collector)
    yield collector
    ot.set_collector(None)


@pytest.fixture(autouse=True)
def clean_exporter_env(monkeypatch):
    for var in ("MP_OTEL_EXPORT_ENABLED", "MP_OTLP_ENDPOINT",
                "MP_SERVICE_NAME", "MP_OTEL_EXPORT_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    ei.reset_for_tests()
    yield
    ei.reset_for_tests()


def _by_name(mem, name):
    spans = [s for s in mem.spans if s.name == name]
    assert spans, f"span {name} not collected"
    return spans[-1]


# 1 ---------------------------------------------------------------------------
def test_entry_agent_tool_hierarchy(mem):
    with ot.entry_span("mergepilot.pr_review", run_id="r1") as entry:
        win = ot.AgentWindowSpan("mergepilot.agent.reviewer", run_id="r1",
                                 trace_id=entry.trace_id,
                                 agent_role="reviewer",
                                 stage="review", attempt=1)
        try:
            with ot.start_span("gateway.call_tool", run_id="r1",
                               tool_name="sast_scan",
                               policy_decision="ALLOW") as tool:
                tool.set_attribute("tool_status", "OK")
        finally:
            win.finish(final_decision="PROCEED")
    e = _by_name(mem, "mergepilot.pr_review")
    a = _by_name(mem, "mergepilot.agent.reviewer")
    t = _by_name(mem, "gateway.call_tool")
    assert a.parent_span_id == e.span_id and t.parent_span_id == a.span_id
    assert {e.trace_id, a.trace_id, t.trace_id} == {e.trace_id}


# 2 ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["manager", "reviewer", "fixer", "verifier"])
def test_four_agent_roles(mem, role):
    win = ot.AgentWindowSpan(f"mergepilot.agent.{role}", run_id="r-role",
                             trace_id=ot._gen_trace_id(), agent_role=role)
    win.finish()
    rec = _by_name(mem, f"mergepilot.agent.{role}")
    assert rec.attributes["mp.agent_role"] == role
    assert rec.attributes["mp.attempt"] == 0


# 3 ---------------------------------------------------------------------------
def test_http_traceparent_roundtrip():
    ctx = ot.SpanContext(*_ids(), run_id="r-http")
    headers = {}
    ot.inject_headers(headers, ctx=ctx)
    tp = headers["traceparent"]
    assert tp.startswith("00-") and len(tp.split("-")) == 4
    back = ot.extract_context(headers)
    assert (back.trace_id, back.span_id) == (ctx.trace_id, ctx.span_id)


# 4 ---------------------------------------------------------------------------
def test_matrix_carrier_roundtrip():
    ctx = ot.SpanContext(*_ids(), run_id="r-mx")
    text = "请用 gh-mcp-fix.sh 提修复 PR。完成写 TASK_COMPLETED: r-mx-fix。"
    once = ot.append_task_carrier(text, ctx)
    assert once.count("[MPTRACE] ") == 1 and "[MPTRACE]" not in text
    twice = ot.append_task_carrier(once, ctx)
    assert twice.count("[MPTRACE] v=1 ") == 1          # idempotent refresh
    parsed = ot.parse_task_carrier(twice)
    assert parsed is not None
    assert (parsed.trace_id, parsed.span_id, parsed.run_id) == \
        (ctx.trace_id, ctx.span_id, "r-mx")
    # 无上下文时原文返回；旧正文不受影响
    assert ot.append_task_carrier("plain body") == "plain body"


# 5 ---------------------------------------------------------------------------
def test_async_handoff_link(mem):
    producer = ot.SpanContext(*_ids(), run_id="r-link")
    with ot.start_span("agent.handoff_complete", run_id="r-link",
                       trace_id=producer.trace_id, agent_role="reviewer",
                       stage="review", links=[ot.make_link(producer)]):
        pass
    rec = _by_name(mem, "agent.handoff_complete")
    assert rec.parent_span_id is None                     # hop root, honest
    assert rec.links == [{"trace_id": producer.trace_id,
                          "span_id": producer.span_id}]
    otlp = ot.OTLPExporter()._span_to_otlp(rec)
    assert otlp["links"][0]["traceId"] == producer.trace_id


# 6 ---------------------------------------------------------------------------
def test_illegal_traceparent_failclosed():
    good_ctx = ot.SpanContext(*_ids(), run_id="r-bad")
    bad_headers = {"traceparent": "00-deadbeef-zz-" + "f" * 16 + "-01"}
    assert ot.extract_context(bad_headers) is None
    assert ot.from_traceparent("00-not-hex-not-hex-01") is None
    tampered = "[MPTRACE] v=1 tp=00-%s-%s-FF run=r%%20bad" % ("z" * 32, "y" * 16)
    assert ot.parse_task_carrier(tampered) is None
    # 合法格式伪造不了：解析只接受严格正则
    legit = ot.append_task_carrier("t", good_ctx)
    half = legit.replace(good_ctx.span_id[:8], "________")
    assert ot.parse_task_carrier(half) is None


# 7 ---------------------------------------------------------------------------
def test_stage_transition_event(mem):
    with ot.start_span("controller.process_event", run_id="r-ev",
                       stage="review", agent_role="coordinator") as sp:
        sp.add_event("mp.stage_transition",
                     {"stage": "review", "decision": "fix"})
    rec = _by_name(mem, "controller.process_event")
    assert rec.events and rec.events[0]["name"] == "mp.stage_transition"
    assert rec.events[0]["attributes"]["decision"] == "fix"


# 8-10 ------------------------------------------------------------------------
def _gw(mem, decision, boom=None):
    attrs = ot.build_genai_attrs(policy_decision=decision,
                                 tool_name="sast_scan")
    cm = pytest.raises(boom) if boom else contextlib.nullcontext()
    with cm:
        with ot.gateway_span(run_id="r-gw", trace_id=ot._gen_trace_id(),
                             agent_role="verifier", **attrs) as sp:
            if boom:
                raise boom("denied")


def test_gateway_allow(mem):
    _gw(mem, "ALLOW")
    rec = _by_name(mem, "gateway.call_tool")
    assert rec.attributes["mp.policy_decision"] == "ALLOW"
    assert rec.attributes["mp.tool_name"] == "sast_scan"


def test_gateway_deny_marks_error(mem):
    class GatewayDenied(RuntimeError):
        pass
    _gw(mem, "DENY", boom=GatewayDenied)
    rec = _by_name(mem, "gateway.call_tool")
    assert rec.status == "ERROR"
    assert rec.attributes["mp.policy_decision"] == "DENY"


def test_gateway_hold(mem):
    _gw(mem, "HOLD")
    assert _by_name(mem, "gateway.call_tool")\
        .attributes["mp.policy_decision"] == "HOLD"


# 11 --------------------------------------------------------------------------
def test_exporter_unreachable_never_raises(mem):
    before = dict(ot.get_export_stats())
    exp = ot.OTLPExporter(endpoint="http://127.0.0.1:9/v1/traces", timeout=0.2)
    dual = ot.DualCollector(memory=mem, exporter=exp)
    ot.set_collector(dual)                        # 真实走出口路径
    try:
        with ot.start_span("controller.process_event", run_id="r-exp"):
            pass                                  # business side effect: none
        after = ot.get_export_stats()
    finally:
        ot.set_collector(None)
    assert mem.get_by_run_id("r-exp"), "本地治理存储不得受出口故障影响"
    assert after["failed_export"] - before["failed_export"] >= 1


# 12 --------------------------------------------------------------------------
def test_export_failure_counters(mem):
    stats = ot.get_export_stats()
    assert set(stats) >= {"sent", "failed_export",
                          "dropped_export", "dropped_redaction"}


# 13/14 ----------------------------------------------------------------------
def test_prompt_response_not_captured():
    out = ot.build_genai_attrs(prompt="SECRET PROMPT TEXT",
                              response="SECRET OUTPUT",
                              code_blob="print('x')",
                              model_name="qwen-x")
    exported = json.dumps(out)
    assert "SECRET PROMPT" not in exported and "SECRET OUTPUT" not in exported
    assert "prompt" not in out and "response" not in out
    assert out["gen_ai.request.model"] == "qwen-x"
    assert ot.GENAI_PROMPT_CAPTURE_DEFAULT_OFF is True
    # allowlist 中不存在任何内容型键位
    assert all("prompt" not in k and "content" not in k
               for k in ot.GENAI_ATTR_ALLOWLIST.values())


# 15 --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "ghp_" + "a" * 36,
    "LTAI" + "b" * 16,
    "AKIDEXAMPLE0001",
    "Bearer mF_9.B5f-4.1JqM",
    "postgres://mergepilot:s3cr3t@audit-pg:5432/mergepilot_audit",
])
def test_sensitive_value_forms_redacted(value):
    assert ot._is_sensitive_value(value), value
    out = ot.redact_attributes({"payload": value})
    assert out["payload"] == "<redacted>"
    safe = "TASK_COMPLETED: run-a-fix | findings.md 已更新"
    assert ot.redact_attributes({"body": safe})["body"] == safe


# 16 --------------------------------------------------------------------------
def test_redaction_failure_drops_export(mem, monkeypatch):
    calls = []

    class FakeExp:
        def export(self, span):
            calls.append(span)

    def explode(attrs):
        raise ValueError("scrubber broken")

    before = dict(ot.get_export_stats())
    dual = ot.DualCollector(memory=mem, exporter=FakeExp())
    monkeypatch.setattr(ot, "redact_attributes", explode)
    rec = ot.SpanRecord("t" * 32, "f" * 16, None, "skill.test_runner",
                        run_id="r-drop", attributes={"k": "v"})
    dual.add_span(rec)
    monkeypatch.undo()

    assert rec.drop_reason == "redaction_failed"
    assert not calls, "脱敏失败的 span 绝不允许进入任何出口"
    assert mem.get_by_run_id("r-drop"), "本地治理存储保留现场"
    assert ot.get_export_stats()["dropped_redaction"] > \
        before["dropped_redaction"]
    # start_span 路径同样隔离：脱敏失败时该 run 不入任何收集器（含内存）
    monkeypatch.setattr(ot, "redact_attributes", explode)
    try:
        with ot.start_span("skill.test_runner", run_id="r-drop3"):
            pass
    finally:
        monkeypatch.undo()
    assert not mem.get_by_run_id("r-drop3")


# 17 --------------------------------------------------------------------------
def test_exporter_init_singleton_and_default_off(monkeypatch):
    assert ei.init_from_env() is None                 # 默认关闭 == 现状行为
    monkeypatch.setenv("MP_OTEL_EXPORT_ENABLED", "1")
    ei.reset_for_tests()                              # 切换场景：进程级决定重启
    first = ei.init_from_env()
    second = ei.init_from_env()
    assert first is second and first is not None      # 单一出口，幂等
    assert isinstance(first, ot.DualCollector)


# 18 --------------------------------------------------------------------------
def test_exporter_init_reads_no_files(monkeypatch):
    src = inspect.getsource(ei)
    for forbidden in ('open(', 'Path(', 'read_text', 'load('):
        assert forbidden not in src, forbidden
    cfg = ei.init_from_env()                          # 行为面：纯 env
    assert cfg is None                                # 无 env → 关闭，零异常


# 19 --------------------------------------------------------------------------
def test_health_check_emitter_offline():
    import poc_health_check as phc
    ei.reset_for_tests()
    out = phc.emit("poc-offline-run")
    assert out["collector_enabled"] is False
    assert set(out["span_names"]) == {"mergepilot.poc.health_check",
                                      "tool.synthetic_health_check"}
    assert len(out["trace_id"]) == 32 and out["parent_links_ok"] is True


# ---- 续篇：官方标准 OTLP 认证头通道（不臆造变量名，值不入日志） --------------
def test_standard_otlp_headers_parse(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS",
                       "Authorization=Bearer%20abc123,api-key=k%3Dv,,")
    out = ot.OTLPExporter.headers_from_standard_env()
    assert out == {"Authorization": "Bearer abc123", "api-key": "k=v"}
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS")
    assert ot.OTLPExporter.headers_from_standard_env() == {}


def test_headers_reach_export_request_but_not_logs(mem, monkeypatch):
    class CaptureOpener:
        def __init__(self):
            self.captured = None
        def open(self, req, timeout=None):
            self.captured = req

    cap = CaptureOpener()
    exp = ot.OTLPExporter(endpoint="http://127.0.0.1:9/v1/traces",
                          timeout=0.2,
                          headers={"x-license": "SECRET-VALUE"})
    exp._opener = cap
    dual = ot.DualCollector(memory=mem, exporter=exp)
    ot.set_collector(dual)
    try:
        with ot.start_span("controller.process_event", run_id="r-hdr"):
            pass
    finally:
        ot.set_collector(None)
    assert cap.captured is not None
    sent = {k.lower(): v for k, v in cap.captured.header_items()}
    assert sent.get("x-license") == "SECRET-VALUE"
    assert sent.get("content-type") == "application/json"
    # 值不得出现在任何统计/序列化旁路里
    blob = json.dumps(ot.get_export_stats())
    assert "SECRET-VALUE" not in blob


def test_init_wires_standard_headers(monkeypatch):
    monkeypatch.setenv("MP_OTEL_EXPORT_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-auth=token9")
    ei.reset_for_tests()
    col = ei.init_from_env()
    ei.reset_for_tests()
    assert col is not None and col.exporter._headers == {"x-auth": "token9"}
