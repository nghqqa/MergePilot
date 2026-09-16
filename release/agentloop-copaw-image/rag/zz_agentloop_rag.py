"""AgentLoop RAG-MCP integration hook for CoPaw workers — DEFERRED (v1, 2026-09-17).

Functional counterpart to zz_agentloop_otel (which stays observation-only). Injects the
P14-proven SYNTHETIC RAG MCP client (stdio: node /usr/local/bin/rag-mcp-server.mjs ->
rag_retrieve tool) into every CoPawAgent, using the SAME deferred discipline that fixed
the v1 dispatch incident:

  * .pth import time: stdlib only + one daemon watcher thread. No copaw imports.
  * watcher: waits for /etc/agentloop-rag.json {enabled:true}; then waits a grace period
    (copaw fully booted); then patches CoPawAgent.register_mcp_clients — a CLASS attribute
    looked up at agent-construction time, so post-hoc patching is effective. The hook
    module body never imports copaw at interpreter start; the copaw imports happen inside
    the patch application, inside the already-running app.

Injection semantics are verbatim from the P14-proven rag_agent_hook.py: if the agent has
no MCP clients of its own, prepend exactly one StdIOStatefulClient ("rag_mcp"); the
original register_mcp_clients always runs afterwards. Endpoints are config-driven so a
port change never needs an image rebuild.
"""
import json
import os
import sys
import threading
import time

CFG_PATH = os.environ.get("AGENTLOOP_RAG_CONFIG", "/etc/agentloop-rag.json")
GRACE_SECONDS = float(os.environ.get("AGENTLOOP_RAG_GRACE", "90"))
LOG = "/tmp/agentloop-rag-hook.log"

_state = {"patched": False, "activated_at": None, "cfg": None, "lock": threading.Lock()}


def _log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + str(msg)[:220] + "\n")
    except Exception:
        pass


def _read_cfg():
    try:
        if not os.path.exists(CFG_PATH):
            return None
        with open(CFG_PATH) as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and cfg.get("enabled"):
            return cfg
    except Exception as e:
        _log("cfg error: " + type(e).__name__)
    return None


def _apply(cfg):
    try:
        from copaw.agents.react_agent import CoPawAgent
        from copaw.app.mcp import StdIOStatefulClient
    except Exception as e:
        _log("import failed (will retry): " + type(e).__name__ + ":" + str(e)[:120])
        return False
    if getattr(CoPawAgent, "_rag_hook_installed", False):
        _state["patched"] = True
        return True
    client_kwargs = {
        "name": "rag_mcp",
        "command": "node",
        "args": [cfg.get("mcp_server_path", "/usr/local/bin/rag-mcp-server.mjs")],
        "env": {
            "RAG_ENDPOINT": cfg.get("rag_endpoint", "http://host.docker.internal:4184/api/rag/search"),
            "RAG_AUDIT_ENDPOINT": cfg.get("audit_endpoint", "http://host.docker.internal:4184/api/rag/toolspan-audit"),
        },
    }  # NOTE: no cwd — an empty cwd makes the stdio spawn fail with FileNotFoundError

    orig = CoPawAgent.register_mcp_clients

    async def patched(self, *args, **kwargs):
        had = list(getattr(self, "_mcp_clients", None) or [])
        _log("register_mcp_clients ENTRY pre_clients=%d" % len(had))
        try:
            if not had:
                client = StdIOStatefulClient(**client_kwargs)
                # toolkit.register_mcp_client validates the connection: connect first
                await client.connect(timeout=float(cfg.get("connect_timeout", 30)))
                self._mcp_clients = [client]
                _log("rag_mcp connected and injected (endpoint=%s)" % cfg.get("rag_endpoint", "")[:60])
        except Exception as e:
            _log("inject/connect failed: " + type(e).__name__ + ":" + str(e)[:160])
        try:
            return await orig(self, *args, **kwargs)
        except Exception as e:
            _log("orig register raised: " + type(e).__name__ + ":" + str(e)[:160])
            raise

    CoPawAgent.register_mcp_clients = patched
    CoPawAgent._rag_hook_installed = True
    _state["patched"] = True
    _log("CoPawAgent.register_mcp_clients patched (live-run SYNTHETIC RAG, endpoint=%s)" % cfg.get("rag_endpoint", "")[:60])
    return True


def _tick():
    if _state["patched"]:
        return
    cfg = _read_cfg()
    if cfg is None:
        return
    with _state["lock"]:
        if _state["patched"]:
            return
        if _state["activated_at"] is None:
            _state["activated_at"] = time.time()
            _log("config enabled; grace %ss before patching" % GRACE_SECONDS)
            return
        if time.time() - _state["activated_at"] < GRACE_SECONDS:
            return
        try:
            _apply(cfg)
        except Exception as e:
            _log("apply error: " + type(e).__name__ + ":" + str(e)[:120])


def _watch():
    deadline = time.time() + 12 * 3600
    while time.time() < deadline:
        try:
            _tick()
        except Exception as e:
            _log("tick error: " + type(e).__name__ + ":" + str(e)[:100])
        time.sleep(2)


try:
    threading.Thread(target=_watch, name="agentloop-rag-watch", daemon=True).start()
except Exception:
    pass
