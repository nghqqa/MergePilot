#!/usr/bin/env python3
"""Pure docker-run argv builder for hardened HiClaw worker/manager recreation.

This module contains NO Docker calls -- it only builds argv and prepares
artifacts. Fully unit-testable on the host (no WSL/Docker).

Key invariants (P1/P2 fixes):

  * FULL CONTRACT: ``build_run_argv_from_inspect`` preserves the complete
    container contract from ``docker inspect`` -- Entrypoint, Cmd, User,
    WorkingDir, Hostname, Labels, Healthcheck, Mounts/Binds, Networks +
    aliases, CapAdd/CapDrop, SecurityOpt, Privileged, ShmSize, Memory,
    PidsLimit, ExposedPorts, StopSignal/StopTimeout, Tty. Only
    RestartPolicy (-> no) and AutoRemove (-> false) are overridden.

  * SECRET-SAFE: the authoritative ``Config.Env`` is NEVER placed in argv
    as ``-e KEY=VALUE`` (visible via ps/proc) and NEVER written to a
    regular temp file on disk. It goes to a ``--env-file`` whose path is
    on tmpfs (``/dev/shm``), mode 0600, deleted by the caller after use.
    The argv contains only ``--env-file <path>``, never inline values.

  * ROLLBACK: ``save_rollback_artifact`` persists the full inspect JSON
    to ``/dev/shm`` (0600) BEFORE the original container is removed, so
    ``rollback_worker.py`` can faithfully rebuild the original container.

  * NO HOME REDIRECT: only specific temp paths get size-limited tmpfs
    (worker: .codex/tmp + /tmp; manager: npm cache + node compile cache
    redirected via NPM_CONFIG_CACHE/NODE_COMPILE_CACHE env into /tmp
    tmpfs -- these are non-secret path strings, not HOME overrides).
"""
from __future__ import annotations

import json
import os
import sys

SCOPE_PROD = "prod"
MANAGER_NPM_CACHE_PATH = "/tmp/mp-npm-cache"
MANAGER_NODE_COMPILE_PATH = "/tmp/mp-node-compile"
DEFAULT_SHM_DIR = "/dev/shm"

DEFAULT_CODEX_TMP_MIB = 512
DEFAULT_TMP_MIB = 256
DEFAULT_NPM_CACHE_MIB = 512
DEFAULT_NODE_COMPILE_MIB = 256


def _default_nonce():
    import secrets
    return secrets.token_hex(8)


def build_tmpfs_spec(path, size_mib, mode="1777"):
    """Build one ``--tmpfs`` spec: ``path:rw,size=<N>m,mode=1777``."""
    if not path or not isinstance(path, str):
        return None
    if not isinstance(size_mib, int) or size_mib <= 0:
        return None
    opts = ["rw", "size=%dm" % size_mib]
    if mode:
        opts.append("mode=%s" % mode)
    return "%s:%s" % (path, ",".join(opts))


def _ns_to_duration(ns):
    """Convert nanoseconds (docker inspect int) to a Docker duration string."""
    if not ns or not isinstance(ns, (int, float)):
        return None
    secs = int(ns / 1e9)
    if secs > 0 and secs % 60 == 0:
        return "%dm" % (secs // 60)
    return "%ds" % secs


def prepare_env_file(env_pairs, shm_dir=DEFAULT_SHM_DIR, name_prefix="mp-env",
                     rng_fn=None, writer=None):
    """Write ``env_pairs`` (KEY=VALUE strings) to a 0600 file in ``shm_dir``.

    Secret-safe: file lives on tmpfs, mode 0600, caller deletes after use.
    ``writer(path, content_bytes, mode)`` callback enables host-side testing
    without touching the real filesystem. Returns the file path.
    """
    rng_fn = rng_fn or _default_nonce
    nonce = rng_fn()
    path = "%s/%s-%s" % (shm_dir, name_prefix, nonce)
    content = ("\n".join(env_pairs) + "\n").encode("utf-8")
    if writer:
        writer(path, content, 0o600)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    return path


def save_rollback_artifact(container_name, inspect, shm_dir=DEFAULT_SHM_DIR,
                           rng_fn=None, writer=None):
    """Persist full inspect JSON to ``/dev/shm`` (0600) for rollback.

    Must be called BEFORE the original container is removed. Returns path.
    """
    rng_fn = rng_fn or _default_nonce
    nonce = rng_fn()
    path = "%s/mp-rollback-%s-%s.json" % (shm_dir, container_name, nonce)
    content = json.dumps(inspect, sort_keys=True).encode("utf-8")
    if writer:
        writer(path, content, 0o600)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    return path


def make_hardening(kind, agent_name, run_id, codex_tmp_mib=DEFAULT_CODEX_TMP_MIB,
                   tmp_mib=DEFAULT_TMP_MIB, npm_cache_mib=DEFAULT_NPM_CACHE_MIB,
                   node_compile_mib=DEFAULT_NODE_COMPILE_MIB,
                   storage_opt_gib=None, scope=SCOPE_PROD):
    """Return a hardening dict: tmpfs_mounts, storage_opt, extra_labels,
    env_additions (non-secret path strings only)."""
    if kind not in ("worker", "manager"):
        raise ValueError("kind must be 'worker' or 'manager', got %r" % kind)
    extra_labels = {
        "com.mergepilot.scope": scope,
        "com.mergepilot.run_id": run_id,
        "com.mergepilot.agent": agent_name,
        "com.mergepilot.hardened": "1",
    }
    env_additions = []
    tmpfs_mounts = []
    if kind == "worker":
        s = build_tmpfs_spec(
            "/root/hiclaw-fs/agents/%s/.codex/tmp" % agent_name, codex_tmp_mib)
        if s:
            tmpfs_mounts.append(s)
        s = build_tmpfs_spec("/tmp", tmp_mib)
        if s:
            tmpfs_mounts.append(s)
    else:
        s = build_tmpfs_spec(MANAGER_NPM_CACHE_PATH, npm_cache_mib)
        if s:
            tmpfs_mounts.append(s)
            env_additions.append("NPM_CONFIG_CACHE=" + MANAGER_NPM_CACHE_PATH)
        s = build_tmpfs_spec(MANAGER_NODE_COMPILE_PATH, node_compile_mib)
        if s:
            tmpfs_mounts.append(s)
            env_additions.append(
                "NODE_COMPILE_CACHE=" + MANAGER_NODE_COMPILE_PATH)
    storage_opt = ("%dg" % storage_opt_gib) if storage_opt_gib else None
    return {
        "kind": kind,
        "tmpfs_mounts": tmpfs_mounts,
        "storage_opt": storage_opt,
        "extra_labels": extra_labels,
        "env_additions": env_additions,
    }


def build_run_argv_from_inspect(container_name, inspect, env_file_path,
                                hardening, force_restart_no=True):
    """Build the hardened ``docker run`` argv from a full ``docker inspect``
    object (single dict, not the list wrapper).

    Preserves the COMPLETE container contract. When ``force_restart_no`` is
    True (hardening path), RestartPolicy is overridden to ``no`` and
    AutoRemove to false. When False (rollback path), the original
    RestartPolicy is preserved for faithful restoration.

    Adds --env-file, --tmpfs, --storage-opt, labels. The argv NEVER contains
    ``-e KEY=VALUE`` (secrets stay in the env-file).
    """
    cfg = inspect.get("Config", {}) or {}
    hc = inspect.get("HostConfig", {}) or {}
    nets = (inspect.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}

    argv = ["run", "-d", "--name", str(container_name)]
    if force_restart_no:
        argv += ["--restart=no"]
    else:
        rp = hc.get("RestartPolicy") or {}
        rp_name = rp.get("Name", "no") or "no"
        if rp_name == "on-failure" and rp.get("MaximumRetryCount"):
            argv += ["--restart", "on-failure:%d" % rp["MaximumRetryCount"]]
        else:
            argv += ["--restart", rp_name]

    # AutoRemove forced off (we manage lifecycle explicitly)
    if hc.get("AutoRemove"):
        argv += ["--rm=false"]

    # Network mode + additional networks + aliases
    net_mode = hc.get("NetworkMode") or ""
    primary_net = net_mode if net_mode and net_mode not in ("default",) else None
    if primary_net:
        argv += ["--network", primary_net]
    for net_name, net_cfg in nets.items():
        if primary_net and net_name == primary_net:
            for alias in (net_cfg.get("Aliases") or []):
                if alias:
                    argv += ["--network-alias", str(alias)]
        elif net_name not in ("none", "host", "bridge", "default"):
            argv += ["--network", net_name]
            for alias in (net_cfg.get("Aliases") or []):
                if alias:
                    argv += ["--network-alias", str(alias)]

    # User / WorkingDir / Hostname / Tty
    if cfg.get("User"):
        argv += ["--user", str(cfg["User"])]
    if cfg.get("WorkingDir"):
        argv += ["--workdir", str(cfg["WorkingDir"])]
    if cfg.get("Hostname"):
        argv += ["--hostname", str(cfg["Hostname"])]
    if cfg.get("Tty"):
        argv += ["--tty"]

    # Labels (original merged with hardening labels)
    labels = dict(cfg.get("Labels") or {})
    labels.update(hardening.get("extra_labels", {}))
    for k in sorted(labels):
        argv += ["--label", "%s=%s" % (k, labels[k])]

    # Healthcheck
    hck = cfg.get("Healthcheck")
    if hck and hck.get("Test"):
        test = hck["Test"]
        if isinstance(test, list) and test:
            tag = test[0] if test[0] in ("CMD", "CMD-SHELL", "NONE") else None
            rest = test[1:] if tag else test
            if rest:
                argv += ["--health-cmd", " ".join(str(x) for x in rest)]
        d = _ns_to_duration(hck.get("Interval"))
        if d:
            argv += ["--health-interval", d]
        d = _ns_to_duration(hck.get("Timeout"))
        if d:
            argv += ["--health-timeout", d]
        d = _ns_to_duration(hck.get("StartPeriod"))
        if d:
            argv += ["--health-start-period", d]
        if hck.get("Retries") is not None:
            argv += ["--health-retries", str(hck["Retries"])]

    # Mounts (top-level resolved mounts) and HostConfig.Binds
    for m in (inspect.get("Mounts") or []):
        mtype = m.get("Type", "bind")
        dst = m.get("Destination")
        if not dst:
            continue
        parts = ["type=%s" % mtype, "target=%s" % dst]
        src = m.get("Source") or m.get("Name")
        if src:
            parts.append("source=%s" % src)
        if m.get("ReadOnly") or m.get("RW") is False:
            parts.append("readonly")
        if m.get("Propagation"):
            parts.append("propagation=%s" % m["Propagation"])
        argv += ["--mount", ",".join(parts)]
    for b in (hc.get("Binds") or []):
        argv += ["-v", str(b)]

    # Capabilities / security / resources
    for cap in (hc.get("CapAdd") or []):
        argv += ["--cap-add", str(cap)]
    for cap in (hc.get("CapDrop") or []):
        argv += ["--cap-drop", str(cap)]
    for opt in (hc.get("SecurityOpt") or []):
        argv += ["--security-opt", str(opt)]
    if hc.get("Privileged"):
        argv += ["--privileged"]
    if hc.get("ShmSize") and hc["ShmSize"] > 0:
        argv += ["--shm-size", str(hc["ShmSize"])]
    if hc.get("Memory") and hc["Memory"] > 0:
        argv += ["--memory", str(hc["Memory"])]
    if hc.get("PidsLimit") and hc["PidsLimit"] > 0:
        argv += ["--pids-limit", str(hc["PidsLimit"])]
    nano = hc.get("NanoCpus")
    if nano and nano > 0:
        argv += ["--cpus", str(nano / 1e9)]

    # Exposed ports
    for port in (cfg.get("ExposedPorts") or {}):
        argv += ["--expose", str(port)]

    # Stop signal / timeout
    if cfg.get("StopSignal"):
        argv += ["--stop-signal", str(cfg["StopSignal"])]
    if cfg.get("StopTimeout") is not None:
        argv += ["--stop-timeout", str(cfg["StopTimeout"])]

    # Env: ONLY via --env-file (secret-safe); NEVER -e KEY=VALUE
    if env_file_path:
        argv += ["--env-file", str(env_file_path)]

    # Hardening: tmpfs + storage-opt
    for spec in hardening.get("tmpfs_mounts", []):
        argv += ["--tmpfs", spec]
    so = hardening.get("storage_opt")
    if so:
        argv += ["--storage-opt", "size=%s" % so]

    # Entrypoint + Cmd reconstruction (faithful)
    cmd_args = []
    entrypoint = cfg.get("Entrypoint")
    if entrypoint:
        argv += ["--entrypoint", str(entrypoint[0])]
        cmd_args.extend(str(x) for x in entrypoint[1:])
    cmd = cfg.get("Cmd")
    if cmd:
        cmd_args.extend(str(x) for x in cmd)

    # Image MUST be present
    image = cfg.get("Image")
    if not image:
        raise ValueError("inspect Config.Image is missing")
    argv += [str(image)]
    argv += cmd_args
    return argv


def argv_has_inline_secret(argv, secret_values):
    """Return True if any secret value appears as a bare argv element.

    Used by tests to prove the argv carries no inline secrets. ``secret_values``
    is the list of original env values that must NOT appear in argv.
    """
    for sv in secret_values:
        if sv is None:
            continue
        for elem in argv:
            if str(sv) == str(elem):
                return True
    return False


def _env_main():
    """Read env (set by shell wrapper) and emit NUL-delimited argv."""
    inspect = json.loads(os.environ.get("MP_INSPECT_JSON", "{}"))
    env_file = os.environ.get("MP_ENV_FILE", "")
    kind = os.environ.get("MP_CONTAINER_KIND", "worker")
    agent = os.environ.get("MP_AGENT_NAME", "")
    run_id = os.environ.get("MP_RUN_ID", "")
    codex_tmp = int(os.environ.get("MP_CODEX_TMP_MIB", DEFAULT_CODEX_TMP_MIB))
    tmp_mib = int(os.environ.get("MP_TMP_MIB", DEFAULT_TMP_MIB))
    npm_cache = int(os.environ.get("MP_NPM_CACHE_MIB", DEFAULT_NPM_CACHE_MIB))
    node_compile = int(os.environ.get(
        "MP_NODE_COMPILE_MIB", DEFAULT_NODE_COMPILE_MIB))
    storage_gib = os.environ.get("MP_STORAGE_OPT_GIB", "")
    storage_gib = int(storage_gib) if storage_gib else None
    container = os.environ.get("MP_CONTAINER_NAME", "")
    scope = os.environ.get("MP_SCOPE", SCOPE_PROD)

    hardening = make_hardening(
        kind, agent, run_id, codex_tmp_mib=codex_tmp, tmp_mib=tmp_mib,
        npm_cache_mib=npm_cache, node_compile_mib=node_compile,
        storage_opt_gib=storage_gib, scope=scope)
    argv = build_run_argv_from_inspect(container, inspect, env_file, hardening)
    sys.stdout.buffer.write(("\0".join(argv) + "\0").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(_env_main())
