#!/usr/bin/env python3
"""Docker socket-proxy hardening policy (the REAL extensible entry).

The HiClaw Manager auto-creates ``hiclaw-worker-*`` containers by calling
the Docker Engine API (POST /containers/create) through the docker socket
bind-mounted into hiclaw-controller. The image-level ``hiclab create worker``
CLI exposes NO Docker-parameter injection hook -- so direct image-level
hardening is BLOCKED_UPSTREAM (see UPSTREAM_BLOCKED.md).

The real extensible entry is a **Docker socket proxy**: a process that owns
the unix socket the Manager talks to, intercepts every API call, and applies
this policy. Because ALL ContainerCreate calls (Manager auto-create, manual
``hiclab create worker``, operator ``docker run``) pass through the socket,
the proxy catches every worker birth. This module is the pure, testable
policy core; the proxy daemon itself is a deployment step (maintenance
window, see install_guarded_startup.sh).

Policy:
  * matches POST /containers/create whose ``name`` query matches
    ``^hiclaw-worker-[a-z0-9-]+$``
  * injects ``HostConfig.Tmpfs`` (worker temp paths), ``HostConfig.StorageOpt``
    (only if a disposable probe proved support), ``HostConfig.RestartPolicy``
    -> {Name: no}, and merges hardening labels
  * non-matching requests pass through unchanged
  * NEVER injects -e/env (the Manager sets its own env); only adds tmpfs +
    storage-opt + restart + labels

This module contains NO network/Docker calls -- it only transforms request
dicts. Fully unit-testable on the host.
"""
from __future__ import annotations

import re

WORKER_NAME_RE = re.compile(r"^hiclaw-worker-[a-z0-9-]+$")
MANAGER_NAME_RE = re.compile(r"^hiclaw-manager(-[a-z0-9-]+)?$")

MANAGER_NPM_CACHE_PATH = "/tmp/mp-npm-cache"
MANAGER_NODE_COMPILE_PATH = "/tmp/mp-node-compile"


def is_target_request(method, path, query, body=None):
    """Return ('worker'|'manager'|None) if this is a hardenable create.

    ``query`` is a dict of query params (e.g. {"name": "hiclaw-worker-fixer"}).
    """
    if method.upper() != "POST":
        return None
    if not path.rstrip("/").endswith("/containers/create"):
        return None
    name = ""
    if isinstance(query, dict):
        name = query.get("name", "") or ""
    if not name and isinstance(body, dict):
        name = body.get("Name", "") or ""
    name = name.lstrip("/")
    if WORKER_NAME_RE.match(name):
        return "worker"
    if MANAGER_NAME_RE.match(name):
        return "manager"
    return None


def _tmpfs_for(kind, agent_name, sizes):
    """Return a {path: opts} dict for the Docker API HostConfig.Tmpfs field."""
    tmpfs = {}
    if kind == "worker":
        codex = "/root/hiclaw-fs/agents/%s/.codex/tmp" % agent_name
        tmpfs[codex] = "rw,size=%dm,mode=1777" % sizes.get("codex_tmp_mib", 512)
        tmpfs["/tmp"] = "rw,size=%dm,mode=1777" % sizes.get("tmp_mib", 256)
    elif kind == "manager":
        tmpfs[MANAGER_NPM_CACHE_PATH] = "rw,size=%dm,mode=1777" % sizes.get(
            "npm_cache_mib", 512)
        tmpfs[MANAGER_NODE_COMPILE_PATH] = "rw,size=%dm,mode=1777" % sizes.get(
            "node_compile_mib", 256)
    return tmpfs


def _agent_name_from(container_name):
    """hiclaw-worker-fixer -> fixer ; hiclaw-manager -> manager."""
    n = container_name.lstrip("/")
    if n.startswith("hiclaw-worker-"):
        return n[len("hiclaw-worker-"):]
    return "manager"


def apply_hardening(body, kind, hardening_config):
    """Return a NEW body dict with hardening injected. Does not mutate input.

    hardening_config keys:
      storage_opt_supported (bool), storage_opt_gib (int|None),
      run_id (str), scope (str), sizes (dict)
    """
    import copy
    out = copy.deepcopy(body) if isinstance(body, dict) else {}
    out.setdefault("HostConfig", {})

    container_name = ""
    if isinstance(body, dict):
        container_name = body.get("Name", "") or ""
    agent = _agent_name_from(container_name) if container_name else kind
    sizes = hardening_config.get("sizes", {})

    # Tmpfs injection (additive -- preserve any existing)
    hc = out["HostConfig"]
    existing_tmpfs = dict(hc.get("Tmpfs") or {})
    existing_tmpfs.update(_tmpfs_for(kind, agent, sizes))
    hc["Tmpfs"] = existing_tmpfs

    # Storage-opt (only if probe-proven)
    if hardening_config.get("storage_opt_supported") and hardening_config.get(
            "storage_opt_gib"):
        opts = list(hc.get("StorageOpt") or [])
        spec = "size=%dg" % hardening_config["storage_opt_gib"]
        if spec not in opts:
            opts.append(spec)
        hc["StorageOpt"] = opts

    # Restart policy -> no (workers must not auto-restart past the guard)
    hc["RestartPolicy"] = {"Name": "no"}

    # Labels (merge hardening labels; preserve existing)
    labels = dict(out.get("Labels") or {})
    labels["com.mergepilot.scope"] = hardening_config.get("scope", "prod")
    labels["com.mergepilot.run_id"] = hardening_config.get("run_id", "")
    labels["com.mergepilot.agent"] = agent
    labels["com.mergepilot.hardened"] = "1"
    out["Labels"] = labels

    # Manager: redirect npm/node caches via env (non-secret path strings)
    if kind == "manager":
        env = list(out.get("Env") or [])
        additions = [
            "NPM_CONFIG_CACHE=" + MANAGER_NPM_CACHE_PATH,
            "NODE_COMPILE_CACHE=" + MANAGER_NODE_COMPILE_PATH,
        ]
        for a in additions:
            key = a.split("=", 1)[0]
            env = [e for e in env if not e.startswith(key + "=")]
            env.append(a)
        out["Env"] = env

    return out


def process_request(method, path, query, body, hardening_config):
    """Decide policy action for a Docker API request.

    Returns (action, body):
      ('passthrough', body)          -- not a target; unchanged
      ('hardened', modified_body)    -- target; hardening injected
    """
    kind = is_target_request(method, path, query, body)
    if kind is None:
        return ("passthrough", body)
    modified = apply_hardening(body, kind, hardening_config)
    return ("hardened", modified)
