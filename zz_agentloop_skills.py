"""AgentLoop Skills-MCP integration hook for CoPaw workers — DEFERRED (v1).

Injects the deterministic Skills MCP client (stdio: node /usr/local/bin/skill-mcp-server.mjs
-> skill_diff_parse / skill_risk_classify / skill_test_runner / skill_case_retrieval) into
every CoPawAgent, APPENDING to whatever MCP clients already exist (the rag hook may have
injected rag_mcp first — both are wanted).

Same deferred discipline as zz_agentloop_otel / zz_agentloop_rag:
  .pth import = stdlib only + one daemon watcher thread; patching happens only after
  /etc/agentloop-skills.json enables it AND the boot grace period elapsed; only modules
  already in sys.modules are touched. StdIOStatefulClient is connect()ed before injection
  (toolkit validates connection state; empty cwd breaks the node spawn).
"""
import json
import os
import sys
import threading
import time

CFG_PATH = os.environ.get("AGENTLOOP_SKILLS_CONFIG", "/etc/agentloop-skills.json")
GRACE_SECONDS = float(os.environ.get("AGENTLOOP_SKILLS_GRACE", "0"))  # 与 rag(4s)错峰:同时打补丁会互覆
LOG = "/tmp/agentloop-skills-hook.log"

_state = {"patched": False, "activated_at": None, "lock": threading.Lock()}


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

    if getattr(CoPawAgent, "_skills_hook_installed", False):
        _state["patched"] = True
        return True

    client_kwargs = {
        "name": "skills_mcp",
        "command": "node",
        "args": [cfg.get("mcp_server_path", "/usr/local/bin/skill-mcp-server.mjs")],
        "env": {
            "SKILL_REPO_ROOT": cfg.get("skill_repo_root", "/opt/mergepilot"),
            "PYTHONPATH": cfg.get("skill_repo_root", "/opt/mergepilot"),
            "SKILL_AUDIT_ENDPOINT": cfg.get("audit_endpoint", "http://host.docker.internal:4184/api/rag/toolspan-audit"),
            **(cfg.get("extra_env") or {}),
        },
    }

    orig = CoPawAgent.register_mcp_clients

    async def patched(self, *args, **kwargs):
        already = [c for c in (getattr(self, "_mcp_clients", None) or [])
                   if getattr(c, "name", "") == "skills_mcp"]
        if already:
            _log("skills_mcp already present; skipping injection")
        else:
            try:
                client = StdIOStatefulClient(**client_kwargs)
                await client.connect(timeout=float(cfg.get("connect_timeout", 30)))
                self._mcp_clients = list(getattr(self, "_mcp_clients", None) or []) + [client]
                _log("skills_mcp connected and appended (total=%d)" % len(self._mcp_clients))
            except Exception as e:
                _log("inject/connect failed: " + type(e).__name__ + ":" + str(e)[:160])
        return await orig(self, *args, **kwargs)

    CoPawAgent.register_mcp_clients = patched
    CoPawAgent._skills_hook_installed = True
    _state["patched"] = True
    _log("CoPawAgent.register_mcp_clients patched (deterministic Skills MCP, append mode)")
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
    threading.Thread(target=_watch, name="agentloop-skills-watch", daemon=True).start()
except Exception:
    pass
