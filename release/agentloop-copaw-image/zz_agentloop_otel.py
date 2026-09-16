"""AgentLoop live instrumentation for CoPaw workers (observation-only) — v2.2 DEFERRED.

Why v2 exists (2026-09-16, R3 fix):
  v1 ran its whole setup at .pth import time, including ``__import__`` of the copaw
  modules it wanted to patch. Importing copaw internals that early (before the copaw
  app bootstraps its own environment) left the matrix bridge unable to dispatch
  inbound @mentions (A/B verified). v2 defers *everything*:

  * .pth import time: stdlib only. One daemon watcher thread. No third-party imports,
    no provider, nothing touched.
  * watcher (every 2s): reads the switch file; only when ``enabled`` is true it
      (1) builds the OTel provider/tracer (third-party imports happen here, inside a
          running app, never at interpreter start),
      (2) after a grace period (copaw fully booted) applies patches to modules that
          are ALREADY in ``sys.modules``. This module never imports a copaw module.
  * the switch can be flipped at runtime (rewrite /etc/agentloop-otel.json) —
    activation within one poll, no container restart.

Patch choke points (v2.2) — every one is resolved by CLASS or MODULE-GLOBAL name
lookup at call time, so patching AFTER the app registered its callbacks still works
(this is why ``taskflow``/``projectflow``/``message`` tool functions and nio-bound
``_on_room_event`` are NOT patched here: their object references are captured at
registration time and post-hoc patching would silently miss them):

  agentscope.tool._toolkit.Toolkit.call_tool_function   -> tool.<name>          (all tool calls)
  matrix_channel.MatrixChannel._was_mentioned           -> matrix.receive        (inbound event + mention verdict;
                                                           module is the custom-channel plugin copy, see PATCHES)
  matrix_channel.MatrixChannel.send                     -> matrix.send           (agent replies)
  copaw_worker.hooks.tools.message._send_matrix_room_message -> matrix.send     (tool-driven sends)
  copaw_worker.hooks.tools.taskflow._notify_task_assignment -> taskflow.notify_assignment (delegation event id)
  copaw.providers.retry_chat_model.RetryChatModel.__call__   -> genai.llm.call
  agentscope.model.{OpenAI,DashScope}ChatModel.__call__      -> genai.llm.request
  copaw.agents.react_agent.CoPawAgent.reply                  -> agent.session.run

Content policy: no message bodies / prompts — hashes, lengths, ids, status only.
Every wrapper is exception-safe and never alters the wrapped call's result.
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

_state = {
    "tracer": None,
    "provider": None,
    "role": "worker",
    "worker": os.environ.get("AGENTTEAMS_WORKER_NAME", ""),
    "patched": [],
    "activated_at": None,
    "lock": threading.Lock(),
}


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
    """Attrs from a Toolkit.call_tool_function(self, tool_call) invocation."""
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


def _was_mentioned_attrs(args, kwargs):
    event = args[1] if len(args) > 1 else kwargs.get("event")
    text = args[2] if len(args) > 2 else (kwargs.get("text") or "")
    eid = getattr(event, "event_id", "") or ""
    sender = getattr(event, "sender", "") or ""
    body = getattr(event, "body", "") or text or ""
    return {"agentteams.event_type": "matrix.receive", "matrix.event_id": str(eid),
            "matrix.sender": str(sender), "matrix.body_sha12": _sha12(body),
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
        span = None
        try:
            span = _start_span(span_name, _base_attrs(attrs_fn(args, kwargs) or {}))
            _log_span(span_name)
        except Exception:
            pass
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            if span is not None:
                try:
                    span.set_attribute("error.type", type(e).__name__)
                    span.end()
                except Exception:
                    pass
            raise
        _end_span_ok(span, result, result_attrs_fn)
        return result

    swrapper._agentloop_patched = True
    return swrapper


def _wrap_async(fn, span_name, attrs_fn, result_attrs_fn=None):
    """Wrap a coroutine function. `span_name` may be a str or a callable(args, kwargs) -> str.

    If the awaited result is an async generator (streaming LLM call, Toolkit.call_tool_function),
    the coroutine shape is preserved: we still return an async generator, and the span stays open
    until that generator is exhausted.
    """
    if getattr(fn, "_agentloop_patched", False):
        return fn
    if inspect.isasyncgenfunction(fn):
        return _wrap_agen(fn, span_name, attrs_fn)

    @functools.wraps(fn)
    async def awrapper(*args, **kwargs):
        span = None
        try:
            name = span_name(args, kwargs) if callable(span_name) else span_name
            span = _start_span(name, _base_attrs(attrs_fn(args, kwargs) or {}))
            _log_span(name)
        except Exception:
            pass
        handed_off = False
        try:
            result = await fn(*args, **kwargs)
            # streaming call: keep the span open until the stream is consumed
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
                            try:
                                span.end()
                            except Exception:
                                pass

                return stream_wrapper()
            _end_span_ok(span, result, result_attrs_fn)
            return result
        except Exception as e:
            if span is not None and not handed_off:
                try:
                    span.set_attribute("error.type", type(e).__name__)
                    span.end()
                except Exception:
                    pass
            raise

    awrapper._agentloop_patched = True
    return awrapper


def _wrap_agen(fn, span_name, attrs_fn):
    if getattr(fn, "_agentloop_patched", False):
        return fn

    @functools.wraps(fn)
    async def agen(*args, **kwargs):
        span = None
        try:
            name = span_name(args, kwargs) if callable(span_name) else span_name
            span = _start_span(name, _base_attrs(attrs_fn(args, kwargs) or {}))
            _log_span(name)
        except Exception:
            pass
        try:
            async for item in fn(*args, **kwargs):
                yield item
        finally:
            if span is not None:
                try:
                    span.end()
                except Exception:
                    pass

    agen._agentloop_patched = True
    return agen


# ── patch application (module must already be in sys.modules) ────────────────

_missing_logged = set()


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


def _toolcall_span_name(args, kwargs):
    tc = args[1] if len(args) > 1 else (kwargs.get("tool_call") or {})
    try:
        return "tool." + (str(tc.get("name") or "") or "unknown")
    except Exception:
        return "tool.unknown"


def _apply_toolkit(mod):
    """Toolkit.call_tool_function is a *coroutine* that returns an async generator
    (it contains `return _object_wrapper(...)`); callers do
    `tool_res = await self.toolkit.call_tool_function(tool_call)` and then iterate.
    _wrap_async preserves exactly that shape (coroutine -> async generator) and keeps the
    span open until the generator is exhausted. (v2.1 wrongly used an async-generator
    wrapper here, which made the caller's `await` raise TypeError.)"""
    cls = getattr(mod, "Toolkit", None)
    if cls is None:
        return False
    orig = getattr(cls, "call_tool_function", None)
    if orig is None:
        return False
    if getattr(orig, "_agentloop_patched", False):
        return True
    cls.call_tool_function = _wrap_async(orig, _toolcall_span_name, _toolcall_attrs)
    return True


def _apply_matrix_channel(mod):
    cls = getattr(mod, "MatrixChannel", None)
    if cls is None:
        return False
    ok = False
    wm = getattr(cls, "_was_mentioned", None)
    if wm is not None and not getattr(wm, "_agentloop_patched", False):
        cls._was_mentioned = _wrap_sync(wm, "matrix.receive", _was_mentioned_attrs)
        ok = True
    sd = getattr(cls, "send", None)
    if sd is not None and not getattr(sd, "_agentloop_patched", False):
        cls.send = _wrap_async(sd, "matrix.send", _channel_send_attrs)
        ok = True
    return ok


def _apply_message_tool(mod):
    orig = getattr(mod, "_send_matrix_room_message", None)
    if orig is None:
        return False
    if getattr(orig, "_agentloop_patched", False):
        return True
    mod._send_matrix_room_message = _wrap_async(orig, "matrix.send", _send_room_msg_attrs)
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
    ok = False
    for cn, span_name in (("OpenAIChatModel", "genai.llm.request"),
                          ("DashScopeChatModel", "genai.llm.request")):
        cls = getattr(mod, cn, None)
        orig = getattr(cls, "__call__", None) if cls else None
        if orig is not None and not getattr(orig, "_agentloop_patched", False):
            setattr(cls, "__call__",
                    _wrap_async(orig, span_name, _llm_attrs_factory(cn, span_name)))
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
    # copaw/app/channels/registry.py adds CUSTOM_CHANNELS_DIR to sys.path and
    # import_module("matrix_channel") -> the live class lives in top-level module
    # "matrix_channel", not "copaw_worker.matrix_channel". Patch both; a dynamic
    # scan in _tick() also catches any other module name ending in matrix_channel.
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
        "service.version": "223ddc2-agentloop",
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
    # Our tracer comes from OUR provider directly: export is guaranteed even if the
    # app installed (or later installs) a different global provider.
    _state["tracer"] = provider.get_tracer("agentteams.agentloop", "2.2.0")
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass
    _state["activated_at"] = time.time()
    _audit_write("ACTIVATED", "worker=%s role=%s otlp=%s grace=%ss" % (w, role, str(cfg["otlp"])[:80], GRACE_SECONDS))


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
