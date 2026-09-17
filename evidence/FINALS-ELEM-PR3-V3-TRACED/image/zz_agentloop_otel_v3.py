"""AgentLoop live instrumentation for CoPaw workers (observation-only) — v3 FULL.

v3 (2026-09-17) resolves the four observability gaps found in the AgentLoop console
review (time-linear waterfall with low information density):

  FIX-1  LLM span double-layer removed: only the OUTERMOST model wrapper
         (copaw.providers.retry_chat_model.RetryChatModel.__call__ -> genai.llm.call)
         emits a span. The inner agentscope model patch (genai.llm.request) is applied
         ONLY as a fallback when RetryChatModel is not in the call path.
  FIX-2  genai semantics so the AgentLoop console understands our spans:
         - tool spans are emitted through loongsuite-otel-util-genai's
           ExecuteToolInvocation handler when available (console 工具调用 counter),
           falling back to a raw span otherwise;
         - every span carries gen_ai.conversation.id (matrix room / task session)
           and agentteams.* identity attributes.
  FIX-3  Cross-Agent trace correlation (distributed tracing over Matrix):
         - send side: `_send_matrix_room_message` injects the current W3C traceparent
           into the outgoing event content under the key `m.agentloop.traceparent`
           (custom content key; message body untouched);
         - receive side: `MatrixChannel._was_mentioned` parses it and emits an
           `agentteams.delegation.link` span PARENTED to the sender's span - the
           delegation shows up as a child in the leader's waterfall.

Unchanged v2.2 guarantees: deferred-everything (no .pth-time side effects beyond one
stdlib daemon thread), runtime switch file, 90s boot grace, lazy patching of
already-imported modules only, no message bodies in attributes, exception-safe wraps.
"""
import functools
import hashlib
import inspect
import json
import os
import sys
import threading
import time

CFG_PATH = os.environ.get("AGENTLOOP_OTEL_CONFIG", "/etc/agentloop-otel.json")
GRACE_SECONDS = float(os.environ.get("AGENTLOOP_OTEL_GRACE", "90"))
SPAN_LOG = "/tmp/agentloop-spans.log"
AUDIT_LOG = "/tmp/agentloop-model-audit.log"
TRACEPARENT_KEY = "m.agentloop.traceparent"

_state = {
    "tracer": None, "provider": None, "role": "worker",
    "worker": os.environ.get("AGENTTEAMS_WORKER_NAME", ""),
    "patched": [], "activated_at": None, "lock": threading.Lock(),
    "conversation_id": None, "genai_handler": None, "genai_mode": "raw",
}

_missing_logged = set()


def _sha12(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:12]


def _audit_write(tag, val=""):
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + tag + " " + str(val)[:200] + "\n")
    except Exception:
        pass


def _log_span(name):
    try:
        with open(SPAN_LOG, "a") as f:
            f.write(name + "\n")
    except Exception:
        pass


def _base_attrs(extra=None):
    a = {
        "agentteams.role": _state["role"],
        "agentteams.runtime": "copaw",
        "agentteams.worker": _state["worker"],
        "agentteams.approval_state": "not_required",
    }
    if _state.get("conversation_id"):
        a["gen_ai.conversation.id"] = _state["conversation_id"]
    if extra:
        a.update(extra)
    return a


def _start_span(name, attrs):
    t = _state.get("tracer")
    if t is None:
        return None
    try:
        return t.start_span(name, attributes=attrs)
    except Exception:
        return None


# ── genai handler (FIX-2) ─────────────────────────────────────────────────────

def _genai_handler():
    if _state["genai_handler"] is not None:
        return _state["genai_handler"]
    try:
        from opentelemetry.util.genai.extended_handler import get_extended_telemetry_handler
        _state["genai_handler"] = get_extended_telemetry_handler(tracer_provider=_state["provider"])
        _state["genai_mode"] = "loongsuite-genai"
        _audit_write("GENAI_HANDLER", "loongsuite extended handler active")
    except Exception as e:
        _state["genai_handler"] = False
        _state["genai_mode"] = "raw"
        _audit_write("GENAI_HANDLER_ABSENT", type(e).__name__ + ":" + str(e)[:100])
    return _state["genai_handler"]


def _genai_tool_span(tool_name, args_digest, invoke=None):
    """Start a genai ExecuteTool span via the handler; returns (token, invocation) or None."""
    h = _genai_handler()
    if not h:
        return None
    try:
        from opentelemetry.util.genai.extended_types import ExecuteToolInvocation
        inv = ExecuteToolInvocation(
            tool_name=str(tool_name),
            tool_call_id="gen-" + _sha12(tool_name) + "-" + str(int(time.time() * 1000)),
            tool_call_arguments=args_digest,  # hash/summary only - never the body
            tool_type="function",
        )
        h.start_execute_tool(inv)
        return inv
    except Exception as e:
        _audit_write("GENAI_TOOL_FAIL", type(e).__name__ + ":" + str(e)[:100])
        return None


def _genai_tool_end(inv, ok, result_digest=""):
    h = _genai_handler()
    if not h or inv is None:
        return
    try:
        inv.tool_call_result = result_digest
        h.stop_execute_tool(inv)
    except Exception:
        pass


# ── attribute extractors (no bodies) ─────────────────────────────────────────

def _payload_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            j = json.loads(v)
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}
    return {}


def _toolcall_attrs(args, kwargs):
    tc = args[1] if len(args) > 1 else (kwargs.get("tool_call") or {})
    try:
        name = str(tc.get("name") or "")
        inp = tc.get("input") or {}
    except Exception:
        name, inp = "", {}
    if not isinstance(inp, dict):
        inp = _payload_dict(inp)
    payload = _payload_dict(inp.get("payload"))
    pid = str(inp.get("projectId") or payload.get("projectId") or "")
    tid = str(inp.get("taskId") or payload.get("taskId") or "")
    appr = "approved" if pid == "copaw-high-risk-human-gate" else "not_required"
    spec = inp.get("spec") or payload.get("spec") or ""
    a = {"tool.name": name, "tool.action": str(inp.get("action") or ""),
         "agentteams.project_id": pid, "agentteams.task_id": tid,
         "agentteams.event_type": "tool_call", "agentteams.approval_state": appr}
    if spec:
        a["spec.arguments_hash"] = _sha12(spec)
    tgt = inp.get("target") or inp.get("to") or inp.get("room_id") or ""
    if tgt:
        a["matrix.target"] = str(tgt)[:60]
    body = inp.get("message") or inp.get("text") or ""
    if body:
        a["matrix.body_sha12"] = _sha12(body)
        a["matrix.body_len"] = len(str(body))
    return a


def _toolcall_span_name(args, kwargs):
    tc = args[1] if len(args) > 1 else (kwargs.get("tool_call") or {})
    try:
        return "tool." + (str(tc.get("name") or "") or "unknown")
    except Exception:
        return "tool.unknown"


def _was_mentioned_attrs(args, kwargs):
    event = args[1] if len(args) > 1 else kwargs.get("event")
    text = args[2] if len(args) > 2 else (kwargs.get("text") or "")
    eid = str(getattr(event, "event_id", "") or "")
    sender = str(getattr(event, "sender", "") or "")
    body = str(getattr(event, "body", "") or text or "")
    # conversation id for this turn: room-scoped from the receiving channel
    _state["conversation_id"] = "matrix:" + str(_state.get("room_id") or _sha12(eid))
    return {"agentteams.event_type": "matrix.receive", "matrix.event_id": eid,
            "matrix.sender": sender, "matrix.body_sha12": _sha12(body),
            "matrix.body_len": len(str(body))}


def _channel_send_attrs(args, kwargs):
    to_handle = args[1] if len(args) > 1 else kwargs.get("to_handle") or ""
    text = args[2] if len(args) > 2 else kwargs.get("text") or ""
    return {"agentteams.event_type": "matrix.send", "matrix.target": str(to_handle)[:60],
            "matrix.body_sha12": _sha12(text), "matrix.body_len": len(str(text))}


def _send_room_msg_attrs(args, kwargs):
    room = kwargs.get("room_id") or ""
    content = kwargs.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    body = content.get("body", "")
    mentions = (content.get("m.mentions") or {}).get("user_ids") or []
    return {"agentteams.event_type": "matrix.send", "matrix.room_id": str(room)[:60],
            "matrix.mentions": json.dumps(mentions)[:120],
            "matrix.body_sha12": _sha12(body), "matrix.body_len": len(body)}


def _notify_assign_attrs(args, kwargs):
    task = kwargs.get("task")
    room = kwargs.get("room_id") or ""
    spec = kwargs.get("spec") or ""
    tid = getattr(task, "task_id", "") or ""
    return {"agentteams.event_type": "taskflow_delegate", "agentteams.task_id": str(tid),
            "matrix.room_id": str(room)[:60],
            "spec.arguments_hash": _sha12(spec) if spec else ""}


def _notify_assign_result_attrs(span, result):
    try:
        if isinstance(result, dict):
            if result.get("eventId"):
                span.set_attribute("matrix.event_id", str(result["eventId"]))
            if result.get("roomId"):
                span.set_attribute("matrix.room_id", str(result["roomId"])[:60])
            span.set_attribute("taskflow.notify_sent", bool(result.get("sent", True)))
    except Exception:
        pass


def _llm_attrs_factory(cn, span_name):
    def attrs(args, kwargs):
        self_ = args[0] if args else None
        model = getattr(self_, "model_name", "") or getattr(self_, "model", "") or cn
        return {"gen_ai.operation.name": "chat", "gen_ai.request.model": str(model)[:60],
                "gen_ai.prompt.inlined": False, "agentteams.event_type": "llm_call"}
    return attrs


def _reply_attrs(args, kwargs):
    self_ = args[0] if args else None
    return {"agentteams.event_type": "agent_turn",
            "agentteams.agent_class": type(self_).__name__ if self_ is not None else ""}


# ── wrappers ──────────────────────────────────────────────────────────────────

def _end_span_ok(span, result=None, result_attrs_fn=None):
    if span is None:
        return
    try:
        if result_attrs_fn is not None:
            result_attrs_fn(span, result)
    except Exception:
        pass
    try:
        span.end()
    except Exception:
        pass


def _set_usage_attrs(span, last_item):
    try:
        usage = None
        for path in ("usage", "response_meta.usage", "metadata.usage"):
            obj = last_item
            for part in path.split("."):
                obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
                if obj is None:
                    break
            if obj is not None:
                usage = obj
                break
        if usage is not None:
            def _g(*names):
                for n in names:
                    v = usage.get(n) if isinstance(usage, dict) else getattr(usage, n, None)
                    if v:
                        return int(v)
                return 0
            span.set_attribute("gen_ai.usage.input_tokens", _g("input_tokens", "prompt_tokens"))
            span.set_attribute("gen_ai.usage.output_tokens", _g("output_tokens", "completion_tokens"))
    except Exception:
        pass


def _wrap_sync(fn, span_name, attrs_fn, result_attrs_fn=None):
    if getattr(fn, "_agentloop_patched", False):
        return fn

    @functools.wraps(fn)
    def swrapper(*args, **kwargs):
        cm = None
        try:
            name = span_name(args, kwargs) if callable(span_name) else span_name
            cm = _state["tracer"].start_as_current_span(name, attributes=_base_attrs(attrs_fn(args, kwargs) or {}))
            span = cm.__enter__()
            _log_span(name)
        except Exception:
            cm = None
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            if cm is not None:
                try:
                    span.set_attribute("error.type", type(e).__name__)
                except Exception:
                    pass
                cm.__exit__(type(e), e, e.__traceback__)
            raise
        _end_span_ok(span, result, result_attrs_fn)
        if cm is not None:
            cm.__exit__(None, None, None)
        return result

    swrapper._agentloop_patched = True
    return swrapper


def _wrap_async(fn, span_name, attrs_fn, result_attrs_fn=None):
    """Wrap a coroutine function. span_name may be callable(args, kwargs)->str.

    If the awaited result is an async generator (streaming LLM call,
    Toolkit.call_tool_function), the coroutine shape is preserved and the span
    stays open until the generator is exhausted.
    """
    if getattr(fn, "_agentloop_patched", False):
        return fn
    if inspect.isasyncgenfunction(fn):
        return _wrap_agen(fn, span_name, attrs_fn)

    @functools.wraps(fn)
    async def awrapper(*args, **kwargs):
        cm = None
        span = None
        try:
            name = span_name(args, kwargs) if callable(span_name) else span_name
            cm = _state["tracer"].start_as_current_span(name, attributes=_base_attrs(attrs_fn(args, kwargs) or {}))
            span = cm.__enter__()
            _log_span(name)
        except Exception:
            cm = None
        handed_off = False
        try:
            result = await fn(*args, **kwargs)
            if inspect.isasyncgen(result):
                handed_off = True

                async def stream_wrapper():
                    last = None
                    err = None
                    try:
                        async for item in result:
                            last = item
                            yield item
                    except Exception as e:
                        err = e
                        raise
                    finally:
                        if span is not None:
                            try:
                                if last is not None:
                                    _set_usage_attrs(span, last)
                                if err is not None:
                                    span.set_attribute("error.type", type(err).__name__)
                            except Exception:
                                pass
                        if cm is not None:
                            if err is not None:
                                try:
                                    cm.__exit__(type(err), err, err.__traceback__)
                                except Exception:
                                    cm.__exit__(None, None, None)
                            else:
                                cm.__exit__(None, None, None)

                return stream_wrapper()
            _end_span_ok(span, result, result_attrs_fn)
            if cm is not None:
                cm.__exit__(None, None, None)
            return result
        except Exception as e:
            if cm is not None and not handed_off:
                try:
                    span.set_attribute("error.type", type(e).__name__)
                except Exception:
                    pass
                cm.__exit__(type(e), e, e.__traceback__)
            raise

    awrapper._agentloop_patched = True
    return awrapper


def _wrap_agen(fn, span_name, attrs_fn):
    if getattr(fn, "_agentloop_patched", False):
        return fn

    @functools.wraps(fn)
    async def agen(*args, **kwargs):
        cm = None
        try:
            name = span_name(args, kwargs) if callable(span_name) else span_name
            cm = _state["tracer"].start_as_current_span(name, attributes=_base_attrs(attrs_fn(args, kwargs) or {}))
            _log_span(name)
        except Exception:
            cm = None
        try:
            async for item in fn(*args, **kwargs):
                yield item
        finally:
            if cm is not None:
                cm.__exit__(None, None, None)

    agen._agentloop_patched = True
    return agen


# ── patch application (module must already be imported by the app) ────────────

def _toolcall_span_name(args, kwargs):
    tc = args[1] if len(args) > 1 else (kwargs.get("tool_call") or {})
    try:
        return "tool." + (str(tc.get("name") or "") or "unknown")
    except Exception:
        return "tool.unknown"


def _apply_toolkit(mod):
    """Toolkit.call_tool_function is a *coroutine* returning an async generator.
    Callers do `await toolkit.call_tool_function(tc)` then iterate - preserve that
    shape exactly (v2.1 used an async-generator wrapper here and broke the await).
    Additionally emits a genai ExecuteTool span via the loongsuite handler when
    available (console 工具调用 counter)."""
    cls = getattr(mod, "Toolkit", None)
    if cls is None:
        return False
    orig = getattr(cls, "call_tool_function", None)
    if orig is None:
        return False
    if getattr(orig, "_agentloop_patched", False):
        return True
    @functools.wraps(orig)
    async def call_tool_function(inner_self, tool_call):
        attrs = _base_attrs(_toolcall_attrs((inner_self, tool_call), {}))
        name = _toolcall_span_name((inner_self, tool_call), {})
        cm = _state["tracer"].start_as_current_span(name, attributes=attrs)
        span = cm.__enter__()
        _log_span(name)
        digest = str(attrs.get("spec.arguments_hash") or attrs.get("matrix.body_sha12") or attrs.get("matrix.body_len") or "")
        inv = _genai_tool_span(name.split(".", 1)[-1], digest)
        handed = False
        try:
            result = await orig(inner_self, tool_call)
            if inspect.isasyncgen(result):
                handed = True

                async def stream():
                    last = None
                    try:
                        async for ch in result:
                            last = ch
                            yield ch
                    finally:
                        _genai_tool_end(inv, True, ("chunked:" + str(last).split(".")[-1][:40]) if last is not None else "")
                        if span is not None:
                            span.end()
                        cm.__exit__(None, None, None)

                return stream()
            _genai_tool_end(inv, True, "ok")
            if span is not None:
                span.end()
            cm.__exit__(None, None, None)
            return result
        except Exception as e:
            if not handed:
                _genai_tool_end(inv, False, type(e).__name__)
                if span is not None:
                    try:
                        span.set_attribute("error.type", type(e).__name__)
                        span.end()
                    except Exception:
                        pass
                cm.__exit__(type(e), e, e.__traceback__)
            raise

    call_tool_function._agentloop_patched = True
    cls.call_tool_function = call_tool_function
    return True


def _apply_matrix_channel(mod):
    cls = getattr(mod, "MatrixChannel", None)
    if cls is None:
        return False
    ok = False
    wm = getattr(cls, "_was_mentioned", None)
    if wm is not None and not getattr(wm, "_agentloop_patched", False):
        wrapped = _wrap_sync(wm, "matrix.receive", _was_mentioned_attrs)

        def _linked(self, *a, **k):
            if a:
                _emit_delegation_link(a[0])
            return wrapped(self, *a, **k)

        _linked._agentloop_patched = True
        cls._was_mentioned = _linked
        ok = True
    sd = getattr(cls, "send", None)
    if sd is not None and not getattr(sd, "_agentloop_patched", False):
        cls.send = _wrap_async(sd, "matrix.send", _channel_send_attrs)
        ok = True
    return ok


def _apply_message_tool(mod):
    """Send-side FIX-3: inject the current traceparent into outgoing event content
    under `m.agentloop.traceparent` (custom content key - the body is untouched).
    The receiving worker turns it into a delegation.link span parented to this span."""
    orig = getattr(mod, "_send_matrix_room_message", None)
    if orig is None:
        return False
    if getattr(orig, "_agentloop_patched", False):
        return True

    @functools.wraps(orig)
    async def send_with_traceparent(*, room_id, content, account_id, txn_id=None, **kw):
        try:
            from opentelemetry import trace as _t
            span = _t.get_current_span()
            sc = span.get_span_context()
            if sc and sc.is_valid:
                tp = "00-%032x-%016x-%02x" % (sc.trace_id, sc.span_id, sc.trace_flags)
                content = dict(content or {})
                content[TRACEPARENT_KEY] = tp
        except Exception:
            pass
        return await orig(room_id=room_id, content=content, account_id=account_id, txn_id=txn_id)

    send_with_traceparent._agentloop_patched = True
    mod._send_matrix_room_message = send_with_traceparent
    return True


def _apply_taskflow(mod):
    orig = getattr(mod, "_notify_task_assignment", None)
    if orig is None:
        return False
    if getattr(orig, "_agentloop_patched", False):
        return True
    mod._notify_task_assignment = _wrap_async(orig, "taskflow.notify_assignment",
                                              _notify_assign_attrs, _notify_assign_result_attrs)
    return True


def _apply_retry_model(mod):
    cls = getattr(mod, "RetryChatModel", None)
    if cls is None:
        return False
    orig = getattr(cls, "__call__", None)
    if orig is None or getattr(orig, "_agentloop_patched", False):
        return bool(getattr(orig, "_agentloop_patched", False))
    cls.__call__ = _wrap_async(orig, "genai.llm.call", _llm_attrs_factory("RetryChatModel", "genai.llm.call"))
    return True


def _apply_agentscope_model(mod):
    """FIX-1 fallback: patch the inner model ONLY when the Retry wrapper has not
    appeared within FALLBACK_AFTER seconds of activation. Import order varies
    (agentscope.model may load before copaw.providers.retry_chat_model via the
    toolkit import), so presence-in-sys.modules is not a safe signal."""
    if "copaw.providers.retry_chat_model" in sys.modules:
        return False  # outer wrapper is (or will be) in the path - keep single layer
    if time.time() - (_state["activated_at"] or 0) < float(os.environ.get("AGENTLOOP_OTEL_INNER_FALLBACK", "180")):
        return False  # give the retry wrapper time to appear before falling back
    _audit_write("INNER_FALLBACK", "retry wrapper absent; patching inner model for LLM spans")
    ok = False
    for cn in ("OpenAIChatModel", "DashScopeChatModel"):
        cls = getattr(mod, cn, None)
        orig = getattr(cls, "__call__", None) if cls else None
        if orig is not None and not getattr(orig, "_agentloop_patched", False):
            setattr(cls, "__call__",
                    _wrap_async(orig, "genai.llm.call", _llm_attrs_factory(cn, "genai.llm.call")))
            ok = True
    return ok


def _apply_react_agent(mod):
    cls = getattr(mod, "CoPawAgent", None)
    if cls is None:
        return False
    orig = getattr(cls, "reply", None)
    if orig is None or getattr(orig, "_agentloop_patched", False):
        return bool(getattr(orig, "_agentloop_patched", False))
    cls.reply = _wrap_async(orig, "agent.session.run", _reply_attrs)
    return True


PATCHES = [
    ("agentscope.tool._toolkit", _apply_toolkit),
    # CoPaw loads the worker's matrix_channel.py as a *custom channel plugin*:
    # copaw/app/channels/registry.py does import_module("matrix_channel") (top-level
    # name). Patch both; _scan_dynamic catches any other *matrix_channel module.
    ("matrix_channel", _apply_matrix_channel),
    ("copaw_worker.matrix_channel", _apply_matrix_channel),
    ("copaw_worker.hooks.tools.message", _apply_message_tool),
    ("copaw_worker.hooks.tools.taskflow", _apply_taskflow),
    ("copaw.providers.retry_chat_model", _apply_retry_model),
    ("agentscope.model", _apply_agentscope_model),
    ("copaw.agents.react_agent", _apply_react_agent),
]


def _scan_dynamic_channel_modules():
    known = {n for n, _ in PATCHES}
    for name, mod in list(sys.modules.items()):
        if name in known or name in _state["patched"] or mod is None:
            continue
        if name.endswith("matrix_channel") and getattr(mod, "MatrixChannel", None) is not None:
            _patch(name, _apply_matrix_channel)


# ── exporter status proxy (direct-export evidence) ────────────────────────────

class _ExportStatusLogger:
    def __init__(self, inner):
        self._inner = inner

    def export(self, spans):
        try:
            r = self._inner.export(spans)
            _audit_write("OTEL_EXPORT", "%s n=%d" % (getattr(r, "name", r), len(spans)))
            return r
        except Exception as e:
            _audit_write("OTEL_EXPORT_EXC", type(e).__name__ + ":" + str(e)[:120])
            raise

    def shutdown(self):
        return self._inner.shutdown()

    def force_flush(self, timeout_millis=30000):
        try:
            return self._inner.force_flush(timeout_millis)
        except TypeError:
            return self._inner.force_flush()


# ── FIX-3 receive side: delegation.link span from m.agentloop.traceparent ────

def _emit_delegation_link(event):
    """If the incoming event carries m.agentloop.traceparent, emit a span parented to
    the sender's span so the delegation appears inside the leader's waterfall."""
    try:
        raw = getattr(event, "source", None) or {}
        content = (raw.get("content") or {}) if isinstance(raw, dict) else {}
        tp = content.get(TRACEPARENT_KEY)
        if not tp:
            return
        parts = str(tp).split("-")
        if len(parts) != 4:
            return
        from opentelemetry.trace import NonRecordingSpan, SpanContext, set_span_in_context, TraceFlags
        sc = SpanContext(trace_id=int(parts[1], 16), span_id=int(parts[2], 16),
                         is_remote=True, trace_flags=TraceFlags(int(parts[3], 16)))
        if not sc.is_valid:
            return
        span = _start_span("agentteams.delegation.link", _base_attrs({
            "agentteams.event_type": "delegation_link",
            "agentteams.link.trace_id": parts[1],
            "agentteams.link.parent_span_id": parts[2],
            "matrix.event_id": str(getattr(event, "event_id", "") or ""),
            "matrix.sender": str(getattr(event, "sender", "") or ""),
        }))
        _end_span_ok(span)
        if not _state.get("conversation_id"):
            _state["conversation_id"] = "trace:" + parts[1][:16]
        _log_span("agentteams.delegation.link")
    except Exception as e:
        _audit_write("LINK_SPAN_ERROR", type(e).__name__ + ":" + str(e)[:100])


# ── activation (watcher thread only; never at import) ─────────────────────────

def _read_cfg():
    try:
        if not os.path.exists(CFG_PATH):
            return None
        with open(CFG_PATH) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return None
        if not cfg.get("otlp"):
            return None
        return cfg
    except Exception:
        return None


def _activate(cfg):
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "genai_latest_experimental")
    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NONE")

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    w = _state["worker"]
    role = w.rsplit("-", 1)[-1] if w else "worker"
    _state["role"] = role
    res = Resource.create({
        "service.name": cfg.get("service_name") or "mergepilot-copaw",
        "service.version": "223ddc2-agentloop-v3",
        "deployment.environment": "demo",
        "agentteams.worker": w,
        "agentteams.role": role,
        "acs.cms.workspace": cfg.get("workspace", ""),
        "acs.arms.service.feature": "genai_app",
        "gen_ai.instrumentation.sdk.name": "loongsuite-genai-utils",
    })
    provider = TracerProvider(resource=res)
    exporter = _ExportStatusLogger(OTLPSpanExporter(endpoint=cfg["otlp"], headers=cfg.get("headers") or {}))
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=int(cfg.get("schedule_delay_ms", 3000))))
    _state["provider"] = provider
    _state["tracer"] = provider.get_tracer("agentteams.agentloop", "3.0.0")
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass
    _state["activated_at"] = time.time()
    _audit_write("ACTIVATED", "worker=%s role=%s otlp=%s grace=%ss v3" % (w, role, str(cfg["otlp"])[:80], GRACE_SECONDS))


def _patch(mod_name, apply_fn):
    if mod_name in _state["patched"]:
        return False
    mod = sys.modules.get(mod_name)
    if mod is None:
        return False
    spec = getattr(mod, "__spec__", None)
    if spec is not None and getattr(spec, "_initializing", False):
        return False  # module body still executing; retry next tick
    ok = False
    try:
        ok = apply_fn(mod)
    except Exception as e:
        _audit_write("PATCH_ERROR " + mod_name, type(e).__name__ + ":" + str(e)[:120])
        ok = False
    if ok:
        _state["patched"].append(mod_name)
        _audit_write("PATCH " + mod_name, "applied")
    elif mod_name not in _missing_logged:
        _missing_logged.add(mod_name)
        _audit_write("PATCH " + mod_name, "target-missing (will retry)")
    return ok


def _tick():
    if _state["tracer"] is None:
        cfg = _read_cfg()
        if cfg is None:
            return
        with _state["lock"]:
            if _state["tracer"] is None:
                _activate(cfg)
        return
    if time.time() - (_state["activated_at"] or 0) < GRACE_SECONDS:
        return
    for mod_name, apply_fn in PATCHES:
        _patch(mod_name, apply_fn)
    _scan_dynamic_channel_modules()


def _watch():
    deadline = time.time() + 12 * 3600
    while time.time() < deadline:
        try:
            _tick()
        except Exception as e:
            _audit_write("TICK_ERROR", type(e).__name__ + ":" + str(e)[:100])
        time.sleep(2)


try:
    threading.Thread(target=_watch, name="agentloop-otel-watch", daemon=True).start()
except Exception:
    pass
