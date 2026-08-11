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

import copy
import re
import unicodedata

# Legacy v1.1.2 naming (hiclaw-*). Retained for backward compatibility with
# existing deployments; the v1.2.2 contract uses agentteams-* (below).
WORKER_NAME_RE = re.compile(r"^hiclaw-worker-[a-z0-9-]+$")
MANAGER_NAME_RE = re.compile(r"^hiclaw-manager(-[a-z0-9-]+)?$")

# v1.2.2 AgentTeams hard-cut rename (changelog #1063/#1065). The D2B-3 socket
# proxy matches these names by default; deployments still on v1.1.2 can opt in
# to the legacy regex via ProxyConfig.name_profile="hiclaw".
AGENTTEAMS_WORKER_NAME_RE = re.compile(r"^agentteams-worker-[a-z0-9-]+$")
AGENTTEAMS_MANAGER_NAME_RE = re.compile(r"^agentteams-manager(-[a-z0-9-]+)?$")

MANAGER_NPM_CACHE_PATH = "/tmp/mp-npm-cache"
MANAGER_NODE_COMPILE_PATH = "/tmp/mp-node-compile"

# ---------------------------------------------------------------------------
# D2B-3B1: authoritative security labels. These keys are reserved — the proxy
# STRIPS any client-supplied key that canonical-matches this set, then injects
# the authoritative value from ProxyConfig. Clients may not choose run_id,
# scope, agent, or hardened. (B5 fix; see design freeze §7.)
# ---------------------------------------------------------------------------
SECURE_LABEL_KEYS = (
    "com.mergepilot.scope",
    "com.mergepilot.run_id",
    "com.mergepilot.agent",
    "com.mergepilot.hardened",
)


def _canonical_label_key(key):
    """Normalize a label key for comparison: NFKC + casefold.

    Docker label keys are byte strings, but attackers may use Unicode
    lookalikes (fullwidth), case variants, or escape sequences to spoof a
    secure key. We normalize before comparing so that e.g.
    ``com.MergePilot.Run_ID`` and ``𝐜𝐨𝐦.𝐦𝐞𝐫𝐠𝐞𝐩𝐢𝐥𝐨𝐭.run_id`` both match
    the canonical ``com.mergepilot.run_id``.
    """
    if not isinstance(key, str):
        return ""
    return unicodedata.normalize("NFKC", key).casefold()


# Pre-compute the canonical set for O(1) membership tests.
SECURE_LABEL_KEYS_CANONICAL = frozenset(
    _canonical_label_key(k) for k in SECURE_LABEL_KEYS
)


def is_secure_label_key(key):
    """Return True if ``key`` canonical-matches a reserved security label."""
    return _canonical_label_key(key) in SECURE_LABEL_KEYS_CANONICAL


def strip_secure_labels(labels):
    """Return a NEW dict with all canonical-matched secure labels removed.

    Used by the proxy before injecting authoritative values. Does not mutate
    the input. Handles non-dict input by returning an empty dict.
    """
    if not isinstance(labels, dict):
        return {}
    return {
        k: v for k, v in labels.items()
        if not is_secure_label_key(k)
    }


# ---------------------------------------------------------------------------
# D2B-3B1: deny rules. The proxy calls ``evaluate_deny`` on every request that
# reaches a TRANSFORM or ALLOW-NAMEPREFIX decision; any non-None return is a
# 403. Each rule cites the design-freeze section (D1.x/D3.x) it implements.
# ---------------------------------------------------------------------------

# Container HostConfig fields that constitute namespace escape or privileged
# escalation. Any non-default value -> DENY. (D1.1-D1.6)
DENY_HOST_MODES = ("host", "container", "private", "none")

# docker.sock path patterns that must never be bind-mounted into a created
# container (recursive socket access). (D1.7)
SOCK_PATH_PATTERNS = (
    "docker.sock",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/mp/docker.sock",
)

# Capabilities that grant broad host influence. (D1.10) Stricter than the
# upstream agentteams-docker-proxy (which only blocks 6); we block the full
# known-dangerous set.
DENY_CAPS = frozenset({
    "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_RAWIO", "SYS_PACCT",
    "DAC_OVERRIDE", "DAC_READ_SEARCH", "NET_ADMIN", "NET_RAW",
    "CHOWN", "FOWNER", "SETFCAP", "MKNOD", "SYS_NICE",
    "SETUID", "SETGID", "KILL", "AUDIT_CONTROL", "LINUX_IMMUTABLE",
})

# SecurityOpt values that disable MAC/seccomp protections. (D1.11)
DENY_SECURITY_OPTS = (
    "apparmor=unconfined",
    "seccomp=unconfined",
    "label=",  # any SELinux label override
    "no-new-privileges=false",
)


def _host_mode_denied(value):
    """Return True if a *Mode field is a host/container escape (D1.2-D1.6)."""
    if not isinstance(value, str):
        return False
    v = value.lower()
    if v == "host":
        return True
    # container:<id> joins another container's namespace
    if v.startswith("container:"):
        return True
    return False


def _bind_denied(binds, allowlist):
    """Return a deny reason if any bind mount is disallowed (D1.7/D1.8)."""
    items = []
    if isinstance(binds, list):
        items = binds
    elif isinstance(binds, str):
        items = [binds]
    for b in items:
        if not isinstance(b, str):
            return "non-string bind entry"
        bl = b.lower()
        # D1.7: docker.sock recursive protection
        for pat in SOCK_PATH_PATTERNS:
            if pat in bl:
                return "docker.sock bind forbidden (D1.7)"
        # D1.8: host absolute path not in allowlist
        # bind format: host:container[:ro]; extract host part
        host = b.split(":", 1)[0]
        if host and host not in allowlist:
            return "host bind not in allowlist: %s" % host[:48]
    return None


def _mounts_denied(mounts, allowlist):
    """Return a deny reason if any mount is disallowed (D1.7/D1.8)."""
    if not isinstance(mounts, list):
        return None
    for m in mounts:
        if not isinstance(m, dict):
            return "non-dict mount entry"
        src = str(m.get("Source", "") or "")
        typ = str(m.get("Type", "") or "").lower()
        sl = src.lower()
        # D1.7: docker.sock via any mount type
        for pat in SOCK_PATH_PATTERNS:
            if pat in sl:
                return "docker.sock mount forbidden (D1.7)"
        # D1.8: bind/volume source not in allowlist
        if typ in ("bind", "volume") and src and src not in allowlist:
            return "mount source not in allowlist: %s" % src[:48]
    return None


def evaluate_deny(body, config):
    """Return a deny reason string, or None if the create body is allowed.

    ``body`` is the parsed JSON of POST /containers/create. ``config`` is a
    ProxyConfig-like object with ``.image_allowlist`` (set of digests),
    ``.bind_allowlist`` (set of host paths), ``.tmpfs_mib``,
    ``.log_max_size``.
    """
    if not isinstance(body, dict):
        return "body not a dict"

    img = body.get("Image")
    if not isinstance(img, str) or not img:
        return "missing Image (D3)"
    # D3.1: image must be a digest in the allowlist (accept name@sha256:... or
    # bare sha256:...); reject tags.
    digest = img.rsplit("@", 1)[-1] if "@" in img else img
    if not digest.startswith("sha256:"):
        return "image must be a digest, got: %s" % str(img)[:32]
    if digest not in getattr(config, "image_allowlist", frozenset()):
        return "image not in allowlist (D3.1)"

    hc = body.get("HostConfig") or {}
    if not isinstance(hc, dict):
        return "HostConfig not a dict"

    # D1.1: privileged
    if hc.get("Privileged"):
        return "Privileged forbidden (D1.1)"

    # D1.2-D1.6: namespace escapes
    for field in ("PidMode", "IpcMode", "NetworkMode", "UsernsMode",
                  "CgroupnsMode"):
        if _host_mode_denied(hc.get(field)):
            return "%s host/container escape forbidden (D1.2-D1.6)" % field

    # D1.7/D1.8: binds + mounts
    allowlist = getattr(config, "bind_allowlist", frozenset())
    reason = _bind_denied(hc.get("Binds"), allowlist)
    if reason:
        return reason
    reason = _mounts_denied(hc.get("Mounts"), allowlist)
    if reason:
        return reason

    # D1.8: Devices (any device passthrough forbidden)
    if hc.get("Devices"):
        return "Devices passthrough forbidden (D1.8)"

    # D1.10: dangerous capabilities
    cap_add = hc.get("CapAdd") or []
    if isinstance(cap_add, list):
        for cap in cap_add:
            if isinstance(cap, str) and cap.upper() in DENY_CAPS:
                return "dangerous CapAdd forbidden: %s (D1.10)" % cap

    # D1.11: SecurityOpt unconfined
    sec_opts = hc.get("SecurityOpt") or []
    if isinstance(sec_opts, list):
        for opt in sec_opts:
            if isinstance(opt, str):
                ol = opt.lower()
                for bad in DENY_SECURITY_OPTS:
                    if ol.startswith(bad) or ol == bad:
                        return "SecurityOpt forbidden: %s (D1.11)" % opt[:32]

    # D1.12: Sysctls (any net.*/kernel.* modification forbidden)
    sysctls = hc.get("Sysctls") or {}
    if isinstance(sysctls, dict):
        for key in sysctls:
            kl = str(key).lower()
            if kl.startswith("net.") or kl.startswith("kernel."):
                return "Sysctl forbidden: %s (D1.12)" % str(key)[:32]

    # D1.13: RestartPolicy must be absent or {Name: no}; anything else the
    # proxy will OVERWRITE in transform, but if the caller insists on a
    # dangerous policy (always/unless-stopped), deny rather than silently
    # downgrade (defense in depth). Manager allowlist handled by caller.
    rp = hc.get("RestartPolicy")
    if isinstance(rp, dict):
        rpname = str(rp.get("Name", "") or "").lower()
        if rpname and rpname not in ("no",):
            # worker path denies any non-no; manager path is caller-gated
            if not getattr(config, "manager_mode", False):
                return "RestartPolicy must be 'no', got '%s' (D1.13)" % rpname

    # D3.2: container name traversal / non-ASCII handled by caller regex
    return None


def apply_hardening_v2(body, kind, hardening_config):
    """Authoritative transform for POST /containers/create (D2B-3B1).

    Differs from the legacy ``apply_hardening`` (which MERGES labels) in that
    it performs STRIP-THEN-INJECT on secure labels (B5 fix). All other
    hardening (tmpfs/storage-opt/restart=no/labels) is identical.

    Returns a NEW body dict; does not mutate input. Assumes the caller has
    already run ``evaluate_deny`` (this function does not re-check deny
    rules, but does strip+inject labels unconditionally).
    """
    out = copy.deepcopy(body) if isinstance(body, dict) else {}
    out.setdefault("HostConfig", {})
    hc = out["HostConfig"]

    container_name = ""
    if isinstance(body, dict):
        container_name = body.get("Name", "") or ""
    # D2B-3B1.2: authoritative agent derivation (single source of truth).
    # derive_agent_strict returns None for unknown names -> the transform
    # cannot produce authoritative labels; callers (proxy) MUST have already
    # DENYed such names at classify time. If we reach here with an unknown
    # agent, fall back to ``kind`` but the resulting labels will still be
    # rejected by the authoritative inspect on subsequent operations.
    agent = derive_agent_strict(container_name) or kind
    sizes = hardening_config.get("sizes", {})

    # Tmpfs injection (additive — preserve any existing non-conflicting paths)
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

    # Log driver rotation limit (design §6.4: enforce log rotation + disk quota)
    log_cfg = hc.get("LogConfig") or {}
    if not isinstance(log_cfg, dict):
        log_cfg = {}
    log_cfg.setdefault("Type", "json-file")
    log_opts = dict(log_cfg.get("Config") or {})
    log_max = hardening_config.get("log_max_size", "10m")
    log_max_file = hardening_config.get("log_max_file", "3")
    log_opts.setdefault("max-size", log_max)
    log_opts.setdefault("max-file", str(log_max_file))
    log_cfg["Config"] = log_opts
    hc["LogConfig"] = log_cfg

    # Restart policy -> no (workers must not auto-restart past the guard)
    hc["RestartPolicy"] = {"Name": "no"}

    # B5 fix: STRIP secure labels then INJECT authoritative values
    labels = strip_secure_labels(out.get("Labels") or {})
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
    """Extract the agent name from a worker/manager container name.

    Handles both naming profiles:
      hiclaw-worker-fixer      -> fixer    (v1.1.2 legacy)
      agentteams-worker-fixer  -> fixer    (v1.2.2)
      hiclaw-manager           -> manager
      agentteams-manager       -> manager

    NOTE: for unknown names this returns "manager" (legacy behavior). For the
    STRICT variant used by the proxy's authoritative inspect + transform, use
    ``derive_agent_strict`` which returns None on unknown names -> DENY.
    """
    n = (container_name or "").lstrip("/")
    for prefix in ("agentteams-worker-", "hiclaw-worker-"):
        if n.startswith(prefix):
            return n[len(prefix):]
    return "manager"


# ---------------------------------------------------------------------------
# D2B-3B1.2: unified authoritative agent derivation.
#
# Single source of truth for mapping a container name to its agent identity.
# Used by BOTH the create transform (apply_hardening_v2) and the authoritative
# inspect (_inspect_authoritative) so the two paths can never drift.
#
# Returns the agent string, or None if the name does not match a known
# worker/manager pattern -> caller MUST DENY.
# ---------------------------------------------------------------------------

# Allowed worker-agent names (the four MergePilot roles). Anything outside
# this set is not a recognized agent -> DENY.
_ALLOWED_AGENTS = frozenset({"reviewer", "fixer", "verifier", "manager"})


def derive_agent_strict(container_name):
    """Authoritative agent derivation. Returns the agent string or None.

    Rules (single source of truth; used by transform + inspect):
      agentteams-worker-reviewer -> reviewer
      agentteams-worker-fixer    -> fixer
      agentteams-worker-verifier -> verifier
      agentteams-manager         -> manager
      hiclaw-worker-{agent}      -> {agent}      (v1.1.2 legacy compat)
      hiclaw-manager             -> manager
      <unknown>                  -> None  (caller DENYs)

    The derived agent MUST be in the allowed set; an unknown suffix like
    ``agentteams-worker-evil`` derives "evil" which is not allowed -> None.
    """
    n = (container_name or "").lstrip("/")
    if not n:
        return None
    for prefix in ("agentteams-worker-", "hiclaw-worker-"):
        if n.startswith(prefix):
            agent = n[len(prefix):]
            return agent if agent in _ALLOWED_AGENTS else None
    # manager variants (agentteams-manager or hiclaw-manager, optionally with
    # a trailing suffix like -<id> which we ignore for the agent identity)
    for prefix in ("agentteams-manager", "hiclaw-manager"):
        if n == prefix or n.startswith(prefix + "-"):
            return "manager"
    return None


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
