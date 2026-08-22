"""M8-GH-4B3-W2: E2E prerequisite probes + 8-network executor.

Implements §3 (prerequisite config schema), §4 (read-only probes with
16 checks), §5 (prerequisite gate executor), §6 (8-network create/
remove/inspect executor), and §7 (multi-homed container create/connect
argv structures).

All probes and executors are INJECTABLE (transport/executor callable
parameters); the production CLI calls them with real WslDocker, tests
call them with fakes. The component gate (§2) still fires BEFORE any
probe or executor is reached — this module's code paths are reachable
only after the gate is cleared (W3 scope).
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# ── §3: E2E prerequisite config schema ─────────────────────────────────────

E2E_PREREQ_CONFIG_KEYS = frozenset((
    "room_map_path",           # runtime room-map (single file)
    "policy_path",             # fixture policy (single file)
    "matrix_homeserver",       # non-secret URL
    "matrix_room_id",          # !room:server
    "matrix_credentials_path", # controller creds file (token, not read)
    "app_pem_path",            # GitHub App PEM (existence check only)
    "webhook_secret_path",     # webhook secret (existence check only)
    "mcp_pat_path",            # fine-grained PAT (existence check only)
    "hiclaw_receipt_path",     # rewiring receipt JSON
    "callback_url_path",       # callback URL file
    "windows_proxy_ip",        # IP literal
    "windows_proxy_port",      # 17890
    "tuwunel_ip",              # IP literal
    "tuwunel_port",            # 6167
    "fixture_repo",            # owner/name
    "installation_id",         # numeric
    "repository_id",           # numeric
    "app_id",                  # numeric
    "expected_old_mcp_state",  # "stopped" or "running"
    "expected_8090_state",     # "free" or "occupied"
))

_IP_RE = re.compile(
    r"^(?:\d{1,3}\.){3}\d{1,3}$"
    r"|^\[(?:[0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}\]$")


class PrereqConfigError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


def _bad(key: str, reason: str):
    raise PrereqConfigError("PREREQ_CONFIG_INVALID",
                            "%s: %s" % (key, reason))


def validate_prereq_config(mapping) -> dict:
    """Strict validation: unknown/missing/blank keys fail-closed; paths
    must be single files (no directories); no traversal/control chars;
    IPs must be literals; ports exact. Errors name the key only."""
    if not isinstance(mapping, dict):
        raise PrereqConfigError("PREREQ_CONFIG_INVALID",
                                "config must be a mapping")
    unknown = sorted(set(mapping) - E2E_PREREQ_CONFIG_KEYS)
    if unknown:
        raise PrereqConfigError("PREREQ_CONFIG_INVALID",
                                "unknown key(s): %s" % unknown)
    missing = sorted(E2E_PREREQ_CONFIG_KEYS - set(mapping))
    if missing:
        raise PrereqConfigError("PREREQ_CONFIG_INVALID",
                                "missing key(s): %s" % missing)
    for key in sorted(E2E_PREREQ_CONFIG_KEYS):
        value = mapping[key]
        if not isinstance(value, str) or not value.strip():
            _bad(key, "must be a non-empty string")
        if ".." in value or "\r" in value or "\n" in value or "\0" in value:
            _bad(key, "contains traversal/control characters")
    # path keys: single file, not directory
    for key in ("room_map_path", "policy_path", "matrix_credentials_path",
                "app_pem_path", "webhook_secret_path", "mcp_pat_path",
                "hiclaw_receipt_path", "callback_url_path"):
        if mapping[key].endswith(("/", "\\")) or "/../" in mapping[key]:
            _bad(key, "must be a single file path, not a directory")
    # IP keys
    for key in ("windows_proxy_ip", "tuwunel_ip"):
        if not _IP_RE.match(mapping[key]):
            _bad(key, "must be an IP literal")
    # port keys
    if mapping["windows_proxy_port"] != "17890":
        _bad("windows_proxy_port", "must be exactly 17890")
    if mapping["tuwunel_port"] != "6167":
        _bad("tuwunel_port", "must be exactly 6167")
    # fixture repo format
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
                        mapping["fixture_repo"]):
        _bad("fixture_repo", "must be owner/name")
    # numeric IDs
    for key in ("installation_id", "repository_id", "app_id"):
        if not mapping[key].isdigit() or int(mapping[key]) <= 0:
            _bad(key, "must be a positive numeric string")
    # expected states
    if mapping["expected_old_mcp_state"] not in ("stopped", "running"):
        _bad("expected_old_mcp_state", "must be 'stopped' or 'running'")
    if mapping["expected_8090_state"] not in ("free", "occupied"):
        _bad("expected_8090_state", "must be 'free' or 'occupied'")
    return dict(mapping)


# ── §4: probe infrastructure ────────────────────────────────────────────────

def _result(verified: bool, code: str, detail: str) -> dict:
    return {"verified": verified, "code": code, "detail": detail}


def _file_probe(path: str) -> dict:
    """Check a file exists, is a regular file, and is non-empty.
    Never reads or prints the content."""
    try:
        p = Path(path)
        if not p.exists():
            return _result(False, "FILE_NOT_FOUND", path)
        if not p.is_file():
            return _result(False, "NOT_A_FILE", path)
        if p.stat().st_size == 0:
            return _result(False, "FILE_EMPTY", path)
        return _result(True, "OK", path)
    except OSError:
        return _result(False, "FILE_STAT_ERROR", path)


def _acl_probe(path: str) -> dict:
    """Check file permissions (0600 preferred; at minimum not
    world-readable). On Windows/DrvFs the POSIX mode is metadata-only
    (chmod has no enforcement effect) — the probe reports advisory."""
    if sys.platform == "win32":
        return _result(True, "OK", "advisory (Windows DrvFs)")
    try:
        mode = Path(path).stat().st_mode
        if mode & 0o004:  # world-readable
            return _result(False, "ACL_TOO_PERMISSIVE",
                           "world-readable (mode %o)" % (mode & 0o777))
        return _result(True, "OK", "mode=%o" % (mode & 0o777))
    except OSError:
        return _result(False, "ACL_STAT_ERROR", path)


def _room_map_policy_probe(room_map_path: str, policy_path: str) -> dict:
    """Parse room-map and fixture policy; verify strict 1:1."""
    import e2e_foundation as e2f
    try:
        room_map_text = Path(room_map_path).read_text(encoding="utf-8")
        repos = e2f.parse_room_map_repos(room_map_text)
        # Extract allowlist from fixture policy (restricted parse)
        policy_text = Path(policy_path).read_text(encoding="utf-8")
        allowlist = []
        in_repos = in_allowlist = False
        for line in policy_text.splitlines():
            stripped = line.split("#", 1)[0].rstrip()
            if not stripped.strip():
                continue
            if not stripped.startswith(" ") and stripped.endswith(":"):
                in_repos = stripped == "repos:"
                in_allowlist = False
                continue
            if in_repos and stripped == "  allowlist:":
                in_allowlist = True
                continue
            if in_allowlist:
                m = re.fullmatch(r'    - "([^"]+)"', stripped)
                if m:
                    allowlist.append(m.group(1))
        e2f.validate_room_map_policy_pair(room_map_text, allowlist)
        return _result(True, "OK",
                       "repos=%s" % sorted(repos))
    except e2f.E2EConfigError as exc:
        return _result(False, exc.code, exc.detail)
    except OSError:
        return _result(False, "FILE_READ_ERROR", "room-map or policy")
    except Exception:
        return _result(False, "PARSE_ERROR", "room-map or policy")


def _matrix_membership_probe(joined_mxids) -> dict:
    """Check 5 expected MXIDs are in the joined set."""
    import e2e_foundation as e2f
    try:
        e2f.membership_gate(joined_mxids)
        return _result(True, "OK", "count=5")
    except e2f.E2EConfigError as exc:
        return _result(False, exc.code, exc.detail)


def _hiclaw_receipt_probe(receipt_path: str) -> dict:
    """Check receipt JSON schema: 4 agents, container IDs, MXIDs, IPs,
    hashes, gateway URLs, old github-mcp state, rollback ownership."""
    import json
    try:
        receipt = json.loads(
            Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _result(False, "RECEIPT_INVALID", "unreadable/invalid JSON")
    for key in ("schema_version", "agents", "old_github_mcp",
                "rollback_ownership"):
        if key not in receipt:
            return _result(False, "RECEIPT_MISSING_KEY", key)
    agents = receipt.get("agents")
    if not isinstance(agents, list) or len(agents) != 4:
        return _result(False, "RECEIPT_AGENT_COUNT",
                       "expected 4, got %s" % (len(agents)
                                               if isinstance(agents, list)
                                               else "not-a-list"))
    for agent in agents:
        for key in ("container_id", "mxid", "hiclaw_net_ip",
                    "gateway_url", "config_hash_before",
                    "config_hash_after", "token_hash"):
            if not agent.get(key):
                return _result(False, "RECEIPT_AGENT_FIELD",
                               "agent missing %s" % key)
    return _result(True, "OK", "schema=4-agents")


def _ip_port_probe(ip: str, port: str, expected_port: str,
                   name: str) -> dict:
    if not _IP_RE.match(ip):
        return _result(False, "NOT_IP_LITERAL", name)
    if port != expected_port:
        return _result(False, "PORT_MISMATCH",
                       "%s: expected %s" % (name, expected_port))
    return _result(True, "OK", "%s:%s" % (ip, port))


def _subnet_overlap_probe(existing_networks) -> dict:
    """Check 8 E2E subnets don't overlap with existing host/docker
    networks. existing_networks: list of CIDR strings."""
    import e2e_foundation as e2f
    e2e_nets = [ipaddress.ip_network(spec[0])
                for spec in e2f.E2E_NETWORKS.values()]
    for existing_str in (existing_networks or []):
        try:
            existing = ipaddress.ip_network(existing_str, strict=False)
        except ValueError:
            continue
        for e2e_net in e2e_nets:
            if e2e_net.overlaps(existing):
                return _result(False, "SUBNET_OVERLAP",
                               "%s overlaps %s" % (e2e_net, existing))
    return _result(True, "OK", "no overlaps with %d networks"
                   % len(existing_networks or []))


#: The 16 check names (§4)
PROBE_CHECK_NAMES = (
    "room_map_file", "room_map_acl", "policy_file", "policy_acl",
    "room_map_policy_pair", "matrix_membership",
    "credentials_file", "pem_file", "webhook_secret_file", "pat_file",
    "hiclaw_receipt", "proxy_target", "tuwunel_target",
    "docker_gw_priority", "subnet_overlap", "firewall_ownership",
)


def run_prerequisite_probes(config: dict, *,
                            matrix_joined_mxids=None,
                            docker_gw_priority_supported: Optional[bool] = None,
                            existing_network_cidrs=None,
                            firewall_scan_text: str = "",
                            ) -> dict:
    """§4: run all 16 probes; return {verified, checks{}}.
    Injectables: matrix_joined_mxids (set of MXIDs), docker capability
    bool, existing network CIDR list, iptables-save text."""
    checks = {}

    checks["room_map_file"] = _file_probe(config["room_map_path"])
    checks["room_map_acl"] = _acl_probe(config["room_map_path"])
    checks["policy_file"] = _file_probe(config["policy_path"])
    checks["policy_acl"] = _acl_probe(config["policy_path"])
    checks["room_map_policy_pair"] = _room_map_policy_probe(
        config["room_map_path"], config["policy_path"])

    if matrix_joined_mxids is not None:
        checks["matrix_membership"] = _matrix_membership_probe(
            matrix_joined_mxids)
    else:
        checks["matrix_membership"] = _result(
            False, "PROBE_NOT_INJECTED", "matrix_joined_mxids")

    for key, name in (("matrix_credentials_path", "credentials_file"),
                      ("app_pem_path", "pem_file"),
                      ("webhook_secret_path", "webhook_secret_file"),
                      ("mcp_pat_path", "pat_file")):
        checks[name] = _file_probe(config[key])

    checks["hiclaw_receipt"] = _hiclaw_receipt_probe(
        config["hiclaw_receipt_path"])
    checks["proxy_target"] = _ip_port_probe(
        config["windows_proxy_ip"], config["windows_proxy_port"],
        "17890", "windows-proxy")
    checks["tuwunel_target"] = _ip_port_probe(
        config["tuwunel_ip"], config["tuwunel_port"],
        "6167", "tuwunel")

    if docker_gw_priority_supported is not None:
        checks["docker_gw_priority"] = _result(
            docker_gw_priority_supported,
            "OK" if docker_gw_priority_supported
            else "GW_PRIORITY_MISSING",
            "docker network connect --gw-priority")
    else:
        checks["docker_gw_priority"] = _result(
            False, "PROBE_NOT_INJECTED", "docker capability")

    checks["subnet_overlap"] = _subnet_overlap_probe(existing_network_cidrs)

    import e2e_foundation as e2f
    scan = e2f.parse_owned_rules(firewall_scan_text)
    # No current session exists yet: ANY mp-e2e-tagged rule or chain is
    # a conflict (foreign session residue or unknown ownership).
    if scan["own"] or scan["foreign"] or scan["chains"]:
        checks["firewall_ownership"] = _result(
            False, "FIREWALL_CONFLICT",
            "existing mp-e2e rules/chains present (%d rules, %d chains)"
            % (len(scan["own"]) + len(scan["foreign"]),
               len(scan["chains"])))
    else:
        checks["firewall_ownership"] = _result(
            True, "OK", "no conflicts")

    all_ok = all(c["verified"] for c in checks.values())
    return {"verified": all_ok, "checks": checks}


# ── §5: prerequisite gate executor ─────────────────────────────────────────

def run_e2e_prerequisite_gate(config: dict, **probe_kwargs) -> dict:
    """§5: validate config + run all probes; raise on failure.
    Must be called BEFORE any side effect (network/container/etc)."""
    import e2e_foundation as e2f
    validated = validate_prereq_config(config)
    result = run_prerequisite_probes(validated, **probe_kwargs)
    if not result["verified"]:
        failed = sorted(name for name, check in result["checks"].items()
                        if not check["verified"])
        raise e2f.E2EConfigError(
            "GITHUB_E2E_PREREQUISITES_INCOMPLETE",
            "failed probes: %s" % failed)
    return result


# ── §6: 8-network real executor ────────────────────────────────────────────

def create_e2e_networks(docker_executor, *, journal: dict) -> list:
    """§6: create all 8 E2E networks with fixed subnets; journal each
    network ID. On failure, reverse-delete already-created networks.
    docker_executor: callable(argv, check=...) -> CompletedProcess."""
    import e2e_foundation as e2f
    created = []
    try:
        for name in sorted(e2f.E2E_NETWORKS):
            subnet = e2f.E2E_NETWORKS[name][0]
            full_name = e2f.E2E_NETWORK_PREFIX + name
            argv = ["network", "create", "--driver", "bridge",
                    "--subnet", subnet, full_name]
            docker_executor(argv, check=True)
            # inspect ID
            cp = docker_executor(
                ["network", "inspect", full_name,
                 "--format", "{{.Id}}"], check=True)
            net_id = (cp.stdout or b"").decode().strip()
            if not net_id:
                raise e2f.E2EConfigError(
                    "NETWORK_CREATE_VERIFY_FAILED", name)
            journal[full_name] = net_id
            created.append(full_name)
    except Exception:
        for name in reversed(created):
            try:
                docker_executor(["network", "rm", name], check=False)
            except Exception:
                pass
            journal.pop(name, None)
        raise
    return created


def remove_e2e_networks(docker_executor, *, journal: dict) -> list:
    """Remove all journaled E2E networks (reverse creation order)."""
    removed = []
    for name in reversed(list(journal)):
        try:
            docker_executor(["network", "rm", journal[name]], check=True)
            removed.append(name)
            del journal[name]
        except Exception:
            pass
    return removed


# ── §7: multi-homed container create/connect argv ──────────────────────────

#: §7 container network attachment table: container -> list of
#: (network, static_ip, gw_priority)
E2E_CONTAINER_ATTACHMENTS = {
    "controller": [
        ("mp-e2e-ctrl-egress", "172.31.0.2", 100),
        ("mergepilot-isolated-isolated", None, 0),
    ],
    "policy-gateway": [
        ("mp-e2e-gw-egress", "172.31.0.18", 100),
        ("mergepilot-isolated-isolated", None, 0),
    ],
    "mcp-bridge": [
        ("mp-e2e-br-up", "172.31.0.82", 100),
        ("mp-e2e-mcp-bridge-net", "172.31.0.34", 0),
    ],
    "gh-reporter": [
        ("mp-e2e-rpt-egress", "172.31.0.66", 100),
        ("mergepilot-isolated-isolated", None, 0),
    ],
    "gh-proxy-r": [
        ("mp-e2e-winpx", "172.31.0.130", 100),
        ("mp-e2e-pxr", "172.31.0.98", 0),
    ],
    "gh-proxy-b": [
        ("mp-e2e-winpx", "172.31.0.131", 100),
        ("mp-e2e-pxb", "172.31.0.114", 0),
    ],
}


def plan_e2e_container_create(service: str, *, image_ref: str,
                              env_file: str = "",
                              mounts: list = None) -> list:
    """§7: docker create argv for an E2E multi-homed container.
    Always --network none (attachments via explicit connect).

    mounts: PRE-BUILT argv fragments from the authoritative runtime
    spec (rs.plan_runtime_mounts) — e.g. ["-v", "src:dst:ro", ...].
    Sensitive values ride the --env-file, never this argv."""
    argv = ["create", "--name",
            "mergepilot-isolated-%s-1" % service,
            "--network", "none"]
    argv.extend(mounts or [])
    if env_file:
        argv.extend(["--env-file", env_file])
    argv.extend(["--pull", "never", "--restart", "no", image_ref])
    return argv


def plan_e2e_container_connects(service: str) -> list:
    """§7: network connect argvs with explicit --ip and --gw-priority."""
    result = []
    for network, ip, priority in E2E_CONTAINER_ATTACHMENTS.get(service, []):
        argv = ["network", "connect"]
        if ip:
            argv.extend(["--ip", ip])
        argv.extend(["--gw-priority", str(priority), network,
                     "mergepilot-isolated-%s-1" % service])
        result.append(argv)
    return result


def execute_e2e_container_setup(docker_executor, service: str, *,
                                image_ref: str, env_file: str = "",
                                mounts: list = None,
                                container_journal: dict) -> str:
    """§7: create + connect an E2E container (not started).
    Returns the container ID. On any failure, removes the container."""
    create_argv = plan_e2e_container_create(
        service, image_ref=image_ref, env_file=env_file, mounts=mounts)
    docker_executor(create_argv, check=True)
    name = "mergepilot-isolated-%s-1" % service
    cp = docker_executor(
        ["inspect", name, "--format", "{{.Id}}"], check=True)
    cid = (cp.stdout or b"").decode().strip()
    if not cid:
        docker_executor(["rm", "-f", name], check=False)
        raise PrereqConfigError("CONTAINER_CREATE_FAILED", service)
    container_journal[service] = cid
    try:
        # a container created with --network none cannot be joined to
        # further networks on this docker generation ("container
        # cannot be connected to multiple networks with one of the
        # networks in private (none) mode"): detach the private none
        # endpoint first — the production E2E start failed on the
        # controller's SECOND connect before this
        docker_executor(["network", "disconnect", "none", name],
                        check=True)
        for connect_argv in plan_e2e_container_connects(service):
            docker_executor(connect_argv, check=True)
    except Exception:
        docker_executor(["rm", "-f", cid], check=False)
        container_journal.pop(service, None)
        raise
    return cid


__all__ = [
    "E2E_PREREQ_CONFIG_KEYS", "PrereqConfigError",
    "validate_prereq_config", "run_prerequisite_probes",
    "run_e2e_prerequisite_gate", "create_e2e_networks",
    "remove_e2e_networks", "E2E_CONTAINER_ATTACHMENTS",
    "plan_e2e_container_create", "plan_e2e_container_connects",
    "execute_e2e_container_setup", "PROBE_CHECK_NAMES",
]
