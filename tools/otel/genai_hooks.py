#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Official GenAI semantic mount points (PoC Phase 7.1.6).

Shape mirrors loongsuite-otel-util-genai invocations (InvokeAgent /
LLM / ExecuteTool) but is a SEPARATE minimal implementation: spans are
emitted ONLY when the caller supplies a real invocation payload — real
model/tool results with token usage. No fake token/Retrieval/Memory/Prompt/
Response spans can be produced through this module (guards below).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class RealInvocation:
    """Real-call evidence required before any GenAI span may be emitted."""
    provider: str                     # e.g. dashscope
    model: str                        # request model name
    input_digest: str                 # sha256 of prompt (never the text)
    output_digest: str                # sha256 of completion (never the text)
    input_tokens: int
    output_tokens: int
    tool_name: Optional[str] = None   # ExecuteTool only
    tool_status: str = "OK"
    tool_result_digest: str = ""
    latency_ms: int = 0


def _validate(p: RealInvocation) -> Optional[str]:
    if not p.provider or not p.model:
        return "provider/model missing"
    if p.input_tokens <= 0 or p.output_tokens <= 0:
        return "token usage must be a real positive measurement"
    for d in (p.input_digest, p.output_digest):
        if len(d) != 64:
            return "payload digests must be sha256 hex (text is forbidden)"
    return None


def _emit(otel_mod, name: str, run_id: str, trace_id: str,
          p: RealInvocation, extra: Dict[str, Any]):
    reason = _validate(p)
    if reason:
        return None, reason
    attrs = otel_mod.build_genai_attrs(
        model_provider=p.provider, model_name=p.model,
        token_usage=p.input_tokens + p.output_tokens,
        duration_ms=p.latency_ms, **(extra or {}))
    span = otel_mod.SpanRecord(
        trace_id=trace_id or otel_mod._gen_trace_id(),
        span_id=otel_mod._gen_span_id(),
        parent_span_id=None, name=name, run_id=run_id,
        attributes=attrs)
    return span, None


def emit_invoke_agent(otel_mod, run_id, trace_id, p: RealInvocation):
    """Agent invocation span — only for a completed real agent turn."""
    return _emit(otel_mod, "genai.invoke_agent", run_id, trace_id, p, {})


def emit_llm(otel_mod, run_id, trace_id, p: RealInvocation):
    return _emit(otel_mod, "genai.llm", run_id, trace_id, p, {})


def emit_execute_tool(otel_mod, run_id, trace_id, p: RealInvocation):
    return _emit(otel_mod, "genai.execute_tool", run_id, trace_id, p,
                 {"tool_name": p.tool_name or "", "tool_status": p.tool_status})
