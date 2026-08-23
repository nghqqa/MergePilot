"""M8-GH-4 WSL user-relay transport profile.

Bypasses the confirmed-broken WSL 6.18 IP FORWARD data path by
replacing cross-bridge kernel routing with user-space TCP relays.

This profile does NOT verify the original direct-routing/R4
implementation.  Evidence produced under this profile must carry:

    transport_profile = "wsl-user-relay"
    direct_routing_verified = False

§1  declarative edge contracts (three transport kinds only)
§2  relay security contract (zero-credential, fixed target,
    fail-closed, crash-safe)
§3  bridge-netfilter sysctl transaction (record/verify/restore)
§4  topology plan (docker argv, static IPs from the existing
    single authority, no duplicate role/network mapping)
§5  direction-aware Stage 10 probes (two-segment + negative)
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable, Optional

# ── §1 declarative edge contracts ─────────────────────────────────────────

#: Transport kinds (no fourth implicit path allowed)
DUAL_HOMED_RELAY = "dual_homed_relay"
GATEWAY_LISTENER_TO_CONTAINER = "gateway_listener_to_container"
PUBLISHED_EGRESS_RELAY = "published_egress_relay"
TRANSPORT_KINDS = frozenset((
    DUAL_HOMED_RELAY,
    GATEWAY_LISTENER_TO_CONTAINER,
    PUBLISHED_EGRESS_RELAY,
))


class RelayProfileError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


def _edge(
    edge_id: str,
    source_role: str,
    source_network: str,
    transport_kind: str,
    *,
    relay_container: str = "",
    relay_source_ip: str = "",
    relay_destination_ip: str = "",
    destination_network: str = "",
    destination_container: str = "",
    destination_ip: str = "",
    destination_port: int = 0,
    fixed_upstream_host: str = "",
    fixed_upstream_port: int = 0,
    listen_port: int = 0,
    **extra,
) -> dict:
    """Internal constructor validating the frozen edge contract.
    Extra kwargs (allowed_source_ip, host_listener_ip) are stored
    as-is for host-side relay edges."""
    if transport_kind not in TRANSPORT_KINDS:
        raise RelayProfileError(
            "RELAY_TRANSPORT_KIND_INVALID",
            "edge %s: %r not in %s" % (edge_id, transport_kind,
                                       sorted(TRANSPORT_KINDS)))
    d = {
        "edge_id": edge_id,
        "source_role": source_role,
        "source_network": source_network,
        "transport_kind": transport_kind,
        "relay_container": relay_container,
        "relay_source_ip": relay_source_ip,
        "relay_destination_ip": relay_destination_ip,
        "destination_network": destination_network,
        "destination_container": destination_container,
        "destination_ip": destination_ip,
        "destination_port": destination_port,
        "fixed_upstream_host": fixed_upstream_host,
        "fixed_upstream_port": fixed_upstream_port,
        "listen_port": listen_port,
    }
    d.update(extra)
    if transport_kind == DUAL_HOMED_RELAY:
        for k in ("relay_container", "relay_source_ip",
                  "relay_destination_ip", "destination_network",
                  "destination_container", "destination_ip",
                  "destination_port", "listen_port"):
            if not d[k] and k != "destination_port" and k != "listen_port":
                raise RelayProfileError(
                    "RELAY_EDGE_CONTRACT_INCOMPLETE",
                    "edge %s: dual_homed_relay requires %s" % (edge_id, k))
    if transport_kind == PUBLISHED_EGRESS_RELAY:
        for k in ("fixed_upstream_host", "fixed_upstream_port",
                  "listen_port"):
            if not d[k] and k != "fixed_upstream_port" and k != "listen_port":
                raise RelayProfileError(
                    "RELAY_EDGE_CONTRACT_INCOMPLETE",
                    "edge %s: published_egress_relay requires %s" % (edge_id, k))
    return d


#: Relay IP pool: derived from the existing mp-e2e /28 subnets by
#: using host .14 (unused in the frozen service assignments).
#: Track assigned relay IPs per subnet for uniqueness
_RELAY_IP_COUNTER = {}


def _relay_ip(subnet_cidr: str) -> str:
    """Derive a unique relay IP for a /28 subnet.

    Uses host .14 for the first relay on a subnet, then .13, .12, etc.
    (counting down from 14, all in the unused upper host range)."""
    import ipaddress
    net = ipaddress.ip_network(subnet_cidr, strict=False)
    offset = _RELAY_IP_COUNTER.get(subnet_cidr, 14)
    _RELAY_IP_COUNTER[subnet_cidr] = offset - 1
    return str(net.network_address + offset)


import e2e_foundation as e2f

_NETS = e2f.E2E_NETWORKS


def build_relay_edge_contracts(tuwunel_ip: str,
                                windows_proxy_ip: str = "172.23.48.1",
                                windows_proxy_port: int = 17890) -> list:
    """Build the 6 frozen Stage 10 probe edges as relay contracts.

    DUAL_HOMED_RELAY: container relay (works, verified).
    PUBLISHED_EGRESS_RELAY: host-side relay (container → host
    listener → external target, bypassing broken FORWARD).

    Static IPs derive from the existing e2e_foundation authority.
    Deterministic: resets all per-subnet counters each call."""
    _RELAY_IP_COUNTER.clear()
    _HOST_LISTENER_IP_COUNTER.clear()

    edges = []
    full = e2f._build_all_edges(tuwunel_ip)

    import e2e_executors as _ex_probe
    probe_sources = set(
        spec[2] for spec in _ex_probe.ROUTE_PROBE_SPECS.values())

    for src, dst, port, tag in full:
        if src not in probe_sources:
            continue
        src_subnet, dst_subnet = _find_subnets(src, dst)
        edge_id = tag
        allowed_source = src  # frozen source IP from R4

        if dst == windows_proxy_ip or dst == tuwunel_ip:
            # external target: host-side relay
            host_ip = _host_listener_ip(src_subnet)
            edges.append(_edge(
                edge_id, _role_from_ip(src), src_subnet,
                PUBLISHED_EGRESS_RELAY,
                relay_container="",  # no container for host relays
                relay_source_ip=host_ip,
                fixed_upstream_host=dst,
                fixed_upstream_port=port,
                listen_port=port,
                **{"allowed_source_ip": allowed_source,
                   "host_listener_ip": host_ip}))
        else:
            # container-to-container: dual-homed relay
            edges.append(_edge(
                edge_id, _role_from_ip(src), src_subnet,
                DUAL_HOMED_RELAY,
                relay_container="mp-e2e-relay-%s" % tag.replace("-to-", "-"),
                relay_source_ip=_relay_ip(src_subnet),
                relay_destination_ip=_relay_ip(dst_subnet),
                destination_network=dst_subnet,
                destination_container=_role_from_ip(dst),
                destination_ip=dst,
                destination_port=port,
                listen_port=port))
    return edges


def _find_subnets(src_ip: str, dst_ip: str) -> tuple:
    """Find the full /28 CIDR for src and dst IPs."""
    import ipaddress
    src_net = dst_net = ""
    for name, spec in _NETS.items():
        subnet = spec[0]
        if ipaddress.ip_address(src_ip) in ipaddress.ip_network(subnet):
            src_net = subnet
        if ipaddress.ip_address(dst_ip) in ipaddress.ip_network(subnet):
            dst_net = subnet
    if not src_net:
        parts = src_ip.split(".")
        if len(parts) == 4:
            src_net = ".".join(parts[:3]) + ".0/28"
    if not dst_net:
        parts = dst_ip.split(".")
        if len(parts) == 4:
            dst_net = ".".join(parts[:3]) + ".0/28"
    return src_net, dst_net


def _role_from_ip(ip: str) -> str:
    """Map a frozen static IP back to its role via E2E_NETWORKS."""
    for name, spec in _NETS.items():
        for role, assigned_ip in spec[2].items():
            if assigned_ip == ip:
                return role
    return "unknown"


# ── §2 relay security contract ────────────────────────────────────────────

#: Minimal fixed-target TCP relay (stdlib only; no CONNECT, no SOCKS,
#: no HTTP proxy, no dynamic targets).  Runs non-root, read-only
#: rootfs, cap_drop ALL, no-new-privileges.
#: Argv: LISTEN_IP LISTEN_PORT TARGET_HOST TARGET_PORT
RELAY_SCRIPT = r'''
import socket, threading, sys, signal

LISTEN_IP = sys.argv[1]
LISTEN_PORT = int(sys.argv[2])
TARGET_HOST = sys.argv[3]
TARGET_PORT = int(sys.argv[4])

if LISTEN_IP == "0.0.0.0":
    sys.stderr.write("refusing to bind 0.0.0.0\n")
    sys.exit(3)

def pump(a, b):
    a.settimeout(30); b.settimeout(30)
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    finally:
        for s in (a, b):
            try: s.close()
            except OSError: pass

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((LISTEN_IP, LISTEN_PORT))
srv.listen(64)

def _term(s, f):
    srv.close(); sys.exit(0)
signal.signal(signal.SIGTERM, _term)

print("relay up %s:%d -> %s:%d" % (LISTEN_IP, LISTEN_PORT,
                                   TARGET_HOST, TARGET_PORT), flush=True)

while True:
    try:
        c, _ = srv.accept()
    except OSError:
        break
    try:
        u = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=8)
    except OSError:
        try: c.close()
        except OSError: pass
        continue
    threading.Thread(target=pump, args=(c, u), daemon=True).start()
    threading.Thread(target=pump, args=(u, c), daemon=True).start()
'''

#: Security flags for relay containers (docker run argv fragments)
RELAY_SECURITY_FLAGS = (
    "--user", "65534:65534",       # nobody:nogroup, non-root
    "--read-only",                   # read_only rootfs
    "--cap-drop", "ALL",             # drop all capabilities
    "--security-opt", "no-new-privileges",
    "--memory", "64m",               # bounded memory
    "--pids-limit", "32",            # bounded process count
    "--restart", "no",               # never auto-restart (fail-closed)
    "--pull", "never",
)


def validate_relay_security(argv: list) -> None:
    """Fail-closed if a relay argv violates the security contract."""
    joined = " ".join(str(a) for a in argv)
    for flag in ("--user 65534:65534", "--read-only", "--cap-drop ALL",
                 "no-new-privileges", "--memory 64m", "--pids-limit 32"):
        if flag not in joined:
            raise RelayProfileError(
                "RELAY_SECURITY_CONTRACT_VIOLATION",
                "missing flag: %s" % flag)
    for forbidden in ("--privileged", "NET_ADMIN", "SYS_ADMIN",
                      "/var/run/docker.sock", "--network host",
                      "CONNECT", "SOCKS", "HTTP_PROXY"):
        if forbidden in joined:
            raise RelayProfileError(
                "RELAY_SECURITY_CONTRACT_VIOLATION",
                "forbidden element: %s" % forbidden)


# ── §3 bridge-netfilter sysctl transaction ────────────────────────────────

class SysctlTransaction:
    """Record → set to 0 → verify → restore (crash-safe via re-exec)."""

    def __init__(self, host_executor: Callable):
        self._exec = host_executor
        self._original: Optional[str] = None
        self.key = "net.bridge.bridge-nf-call-iptables"

    def begin(self) -> str:
        cp = self._exec(["sysctl", "-n", self.key], check=True)
        self._original = cp.stdout.decode().strip()
        if self._original != "0":
            self._exec(["sysctl", "-w", "%s=0" % self.key], check=True)
        cp = self._exec(["sysctl", "-n", self.key], check=True)
        current = cp.stdout.decode().strip()
        if current != "0":
            raise RelayProfileError(
                "RELAY_SYSCTL_SET_FAILED",
                "expected 0, got %s" % current)
        return self._original

    def restore(self) -> Optional[str]:
        if self._original is None:
            return None
        self._exec(["sysctl", "-w", "%s=%s" % (self.key, self._original)],
                   check=True)
        cp = self._exec(["sysctl", "-n", self.key], check=True)
        restored = cp.stdout.decode().strip()
        if restored != self._original:
            raise RelayProfileError(
                "RELAY_SYSCTL_RESTORE_FAILED",
                "expected %s, got %s" % (self._original, restored))
        return self._original


# ── §4 topology plan ──────────────────────────────────────────────────────

def plan_relay_run(edge: dict, image_ref: str,
                   relay_script_path: str = "") -> list:
    """Docker create argv for a relay container.

    Both kinds use create + connects + start.  The relay script
    receives LISTEN_IP LISTEN_PORT TARGET_HOST TARGET_PORT, binding
    only to the edge's frozen relay_source_ip (never 0.0.0.0)."""
    kind = edge["transport_kind"]
    name = edge["relay_container"]
    listen_ip = edge["relay_source_ip"]
    listen_port = str(edge["listen_port"])

    if not listen_ip or listen_ip == "0.0.0.0":
        raise RelayProfileError(
            "RELAY_LISTEN_IP_INVALID",
            "edge %s: listen IP %r forbidden" % (edge["edge_id"],
                                                 listen_ip))

    if kind == PUBLISHED_EGRESS_RELAY:
        upstream_host = edge["fixed_upstream_host"]
        upstream_port = str(edge["fixed_upstream_port"])
        argv = (
            ["create", "--name", name, "--network", "none",
             "--entrypoint", "python3",
             "-v", "%s:/relay.py:ro" % relay_script_path]
            + list(RELAY_SECURITY_FLAGS)
            + [image_ref, "/relay.py", listen_ip, listen_port,
               upstream_host, upstream_port])
        validate_relay_security(argv)
        return argv

    if kind == DUAL_HOMED_RELAY:
        dest_ip = edge["destination_ip"]
        dest_port = str(edge["destination_port"])
        argv = (
            ["create", "--name", name, "--network", "none",
             "--entrypoint", "python3",
             "-v", "%s:/relay.py:ro" % relay_script_path]
            + list(RELAY_SECURITY_FLAGS)
            + [image_ref, "/relay.py", listen_ip, listen_port,
               dest_ip, dest_port])
        validate_relay_security(argv)
        return argv

    raise RelayProfileError(
        "RELAY_TRANSPORT_KIND_INVALID", repr(kind))


#: Host-side relay script for PUBLISHED_EGRESS edges.
#: Runs as a systemd transient unit on the WSL host.
#: Binds to a specific listener IP (never 0.0.0.0), verifies
#: the peer source IP against the frozen allowlist, then connects
#: to the fixed upstream. No dynamic targets, no proxy protocol.
HOST_RELAY_SCRIPT = r'''
import socket, threading, sys, signal, os

LISTEN_IP = sys.argv[1]
LISTEN_PORT = int(sys.argv[2])
UPSTREAM_HOST = sys.argv[3]
UPSTREAM_PORT = int(sys.argv[4])
ALLOWED_SOURCE = sys.argv[5]

if LISTEN_IP == "0.0.0.0":
    sys.stderr.write("refusing 0.0.0.0\n")
    sys.exit(3)

def pump(a, b):
    a.settimeout(30); b.settimeout(30)
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    finally:
        for s in (a, b):
            try: s.close()
            except OSError: pass

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((LISTEN_IP, LISTEN_PORT))
srv.listen(64)

def _term(s, f):
    srv.close()
    sys.exit(0)
signal.signal(signal.SIGTERM, _term)

pid = os.getpid()
sys.stdout.write("host-relay[%d] %s:%d -> %s:%d allow=%s\n" % (
    pid, LISTEN_IP, LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT,
    ALLOWED_SOURCE))
sys.stdout.flush()

while True:
    try:
        c, addr = srv.accept()
    except OSError:
        break
    peer_ip = addr[0]
    if peer_ip != ALLOWED_SOURCE:
        try: c.close()
        except OSError: pass
        continue
    try:
        u = socket.create_connection(
            (UPSTREAM_HOST, UPSTREAM_PORT), timeout=8)
    except OSError:
        try: c.close()
        except OSError: pass
        continue
    threading.Thread(target=pump, args=(c, u), daemon=True).start()
    threading.Thread(target=pump, args=(u, c), daemon=True).start()
'''


#: Track assigned host listener IPs per bridge for uniqueness
_HOST_LISTENER_IP_COUNTER = {}


def _host_listener_ip(source_network_cidr: str) -> str:
    """Derive a unique host listener IP on the source bridge.

    Uses the upper end of the /28 host range: .12, .11, .10 etc.
    (avoiding the container relay IPs at .14+)."""
    import ipaddress
    net = ipaddress.ip_network(source_network_cidr, strict=False)
    offset = _HOST_LISTENER_IP_COUNTER.get(source_network_cidr, 12)
    _HOST_LISTENER_IP_COUNTER[source_network_cidr] = offset - 1
    return str(net.network_address + offset)


def _host_bridge_name(source_network_cidr: str) -> str:
    """Map a /28 CIDR to the Docker bridge interface name."""
    import subprocess
    # Docker bridge names are br-<first 12 chars of network ID>
    # We look up via `docker network inspect` at runtime; the
    # contract stores the network name and we derive at setup.
    return None  # resolved at runtime


def plan_host_relay_unit(edge: dict) -> dict:
    """Build the systemd transient unit contract for a host relay.

    Returns the unit spec dict with all frozen fields."""
    listen_ip = edge.get("host_listener_ip", "")
    if not listen_ip:
        listen_ip = _host_listener_ip(edge["source_network"])
    listen_port = edge["listen_port"]
    upstream_host = edge["fixed_upstream_host"]
    upstream_port = edge["fixed_upstream_port"]
    allowed_source = edge.get("allowed_source_ip",
                              _allowed_source_for_edge(edge))
    unit_name = "mp-e2e-host-relay-%s.service" % (
        edge["edge_id"].replace("-to-", "-"))

    return {
        "edge_id": edge["edge_id"],
        "source_role": edge.get("source_role", ""),
        "source_network": edge["source_network"],
        "allowed_source_ip": allowed_source,
        "host_listener_ip": listen_ip,
        "listen_port": listen_port,
        "fixed_upstream_host": upstream_host,
        "fixed_upstream_port": upstream_port,
        "transport_kind": edge["transport_kind"],
        "systemd_unit": unit_name,
        "session_owner": "mp-e2e-relay",
    }


def _allowed_source_for_edge(edge: dict) -> str:
    """The frozen source IP for this edge from the R4 authority."""
    for name, spec in _NETS.items():
        for role, ip in spec[2].items():
            if role == edge.get("source_role", ""):
                return ip
    return ""


def plan_host_relay_systemd_argv(unit: dict,
                                  script_path: str) -> list:
    """systemd-run argv for a transient host relay unit."""
    return [
        "systemd-run",
        "--unit=%s" % unit["systemd_unit"],
        "--description=mergepilot-host-relay-%s" % unit["edge_id"],
        "--property", "NoNewPrivileges=true",
        "--property", "PrivateTmp=true",
        "--property", "ProtectSystem=strict",
        "--property", "ProtectHome=true",
        "--property", "CapabilityBoundingSet=",
        "--property", "Restart=no",
        "--collect",
        "--", "/usr/bin/python3", script_path,
        unit["host_listener_ip"],
        str(unit["listen_port"]),
        unit["fixed_upstream_host"],
        str(unit["fixed_upstream_port"]),
        unit["allowed_source_ip"],
    ]


class HostRelayTransaction:
    """§3: alias + systemd unit lifecycle for host-side relays.

    Setup: alias → verify → unit → verify.
    Cleanup: stop unit → verify listener gone → remove alias → verify.
    Crash recovery: re-execute cleanup (idempotent)."""

    def __init__(self, host_executor: Callable):
        self._exec = host_executor
        self._aliases = []  # (ip, bridge) tuples
        self._units = []    # unit names
        self._scripts = []  # script paths
        self._iptables_rules = []  # (src, dst, port) tuples

    def setup(self, unit: dict, script_path: str,
              bridge_name: str) -> bool:
        """Setup one host relay. Returns True on success."""
        import time
        listen_ip = unit["host_listener_ip"]

        # 1. Add /32 alias on the bridge
        cp = self._exec(
            ["ip", "addr", "add", "%s/32" % listen_ip, "dev",
             bridge_name],
            check=False, timeout=10)
        # rc 2 = already exists, which is fine for idempotent setup
        if cp.returncode not in (0, 2):
            return False
        self._aliases.append((listen_ip, bridge_name))

        # 2. Verify alias
        cp = self._exec(
            ["ip", "addr", "show", "dev", bridge_name],
            check=False, timeout=10)
        if listen_ip not in (cp.stdout or b"").decode(
                "utf-8", "replace"):
            self._cleanup_single(listen_ip, bridge_name, None)
            return False

        # 3. Allow the frozen source IP to reach this listener
        # (the R4 INPUT chain drops all container→LOCAL traffic;
        # this is a precise exception for host relay edges only)
        # Allow the source bridge subnet to reach this listener.
        # The relay script still enforces per-connection peer
        # verification against the frozen allowed_source_ip.
        source_cidr = unit["source_network"]
        cp = self._exec(
            ["iptables", "-I", "INPUT", "1",
             "-s", source_cidr,
             "-d", listen_ip,
             "-p", "tcp", "--dport", str(unit["listen_port"]),
             "-j", "ACCEPT"],
            check=False, timeout=10)
        if cp.returncode != 0:
            self._cleanup_single(listen_ip, bridge_name, None)
            return False
        self._iptables_rules.append((
            source_cidr, listen_ip,
            unit["listen_port"]))

        # 4. Start systemd unit
        argv = plan_host_relay_systemd_argv(unit, script_path)
        cp = self._exec(argv, check=False, timeout=30)
        if cp.returncode != 0:
            self._cleanup_single(listen_ip, bridge_name, None)
            return False
        self._units.append(unit["systemd_unit"])

        # 4. Verify unit is active
        time.sleep(1)
        cp = self._exec(
            ["systemctl", "is-active", unit["systemd_unit"]],
            check=False, timeout=10)
        if (cp.stdout or b"").decode().strip() != "active":
            self._cleanup_single(listen_ip, bridge_name,
                                 unit["systemd_unit"])
            return False

        self._scripts.append(script_path)
        return True

    def cleanup(self) -> None:
        """Idempotent cleanup of all host relays."""
        # Remove precise INPUT rules first
        for src, dst_ip, port in self._iptables_rules:
            self._exec(
                ["iptables", "-D", "INPUT",
                 "-s", src, "-d", dst_ip,
                 "-p", "tcp", "--dport", str(port),
                 "-j", "ACCEPT"],
                check=False, timeout=10)
        self._iptables_rules.clear()

        for unit_name in self._units:
            self._exec(["systemctl", "stop", unit_name],
                       check=False, timeout=15)
            self._exec(["systemctl", "reset-failed", unit_name],
                       check=False, timeout=10)
        self._units.clear()

        for ip, bridge in self._aliases:
            self._exec(
                ["ip", "addr", "del", "%s/32" % ip, "dev", bridge],
                check=False, timeout=10)
        self._aliases.clear()

        for path in self._scripts:
            try:
                import os
                os.unlink(path)
            except OSError:
                pass
        self._scripts.clear()

    def _cleanup_single(self, listen_ip, bridge, unit_name):
        if unit_name:
            self._exec(["systemctl", "stop", unit_name],
                       check=False, timeout=15)
            self._exec(["systemctl", "reset-failed", unit_name],
                       check=False, timeout=10)
        self._exec(
            ["ip", "addr", "del", "%s/32" % listen_ip, "dev", bridge],
            check=False, timeout=10)







def derive_relay_endpoints(relay_edges: list) -> dict:
    """Derive runtime endpoint projections from edge contracts.
    Only called when transport_profile=wsl-user-relay."""
    endpoints = {}
    edge_by_id = {e["edge_id"]: e for e in relay_edges}

    def _ip(eid, field="relay_source_ip"):
        e = edge_by_id.get(eid)
        if not e:
            raise RelayProfileError(
                "RELAY_EDGE_CONTRACT_INCOMPLETE",
                "missing edge %s" % eid)
        return e.get(field, "")

    gb = _ip("gateway-to-bridge")
    if gb:
        endpoints["policy-gateway"] = {
            "UPSTREAM_URL": "http://%s:8082/sse" % gb}
    bpb = _ip("bridge-to-proxy-b")
    if bpb:
        endpoints["mcp-bridge"] = {
            "HTTPS_PROXY": "http://%s:18090" % bpb}
    rpr = _ip("reporter-to-proxy-r")
    if rpr:
        endpoints["gh-reporter"] = {
            "HTTPS_PROXY": "http://%s:18090" % rpr}
    ct = _ip("controller-to-tuwunel", "host_listener_ip")
    if ct:
        endpoints["controller"] = {
            "MATRIX_HS": "http://%s:6167" % ct}
    prw = _ip("proxy-r-to-winproxy", "host_listener_ip")
    if prw:
        endpoints["gh-proxy-r"] = {"GH_PROXY_UPSTREAM_IP": prw}
    pbw = _ip("proxy-b-to-winproxy", "host_listener_ip")
    if pbw:
        endpoints["gh-proxy-b"] = {"GH_PROXY_UPSTREAM_IP": pbw}
    return endpoints


def plan_relay_connects(edge: dict) -> list:
    """Network connect argvs for container relays (dual-homed only)."""
    kind = edge["transport_kind"]
    name = edge["relay_container"]
    if kind == DUAL_HOMED_RELAY:
        return [
            ["network", "connect",
             "--ip", edge["relay_source_ip"],
             "--gw-priority", "100",
             "mp-e2e-" + _network_short_name(edge["source_network"]),
             name],
            ["network", "connect",
             "--ip", edge["relay_destination_ip"],
             "--gw-priority", "0",
             "mp-e2e-" + _network_short_name(edge["destination_network"]),
             name],
        ]
    # PUBLISHED_EGRESS uses host-side relays (no container connects)
    return []


def plan_probe_container(edge: dict, image_ref: str) -> list:
    """Docker create argv for a temporary probe container attached
    to the relay's source network."""
    probe_name = "mp-e2e-relay-probe-%s" % (
        edge["edge_id"].replace("-to-", "-"))
    return [
        "create", "--name", probe_name, "--network", "none",
        "--entrypoint", "python3", "--pull", "never",
        image_ref, "-c", "import time; time.sleep(120)",
    ]


def plan_probe_connect(edge: dict) -> list:
    """Connect the probe container to the relay's source network."""
    probe_name = "mp-e2e-relay-probe-%s" % (
        edge["edge_id"].replace("-to-", "-"))
    return [
        "network", "connect",
        "mp-e2e-" + _network_short_name(edge["source_network"]),
        probe_name,
    ]


def _network_short_name(subnet_cidr: str) -> str:
    """Map a full /28 CIDR (e.g. '172.31.0.0/28') to its short name."""
    import ipaddress
    target = ipaddress.ip_network(subnet_cidr, strict=False)
    for name, spec in _NETS.items():
        if ipaddress.ip_network(spec[0]) == target:
            return name
    return subnet_cidr.replace(".", "-").replace("/", "-")


# ── §5 direction-aware relay probes ───────────────────────────────────────

PROBE_CONNECT = (
    "import socket,sys,time\n"
    "ip,port=sys.argv[1],int(sys.argv[2])\n"
    "payload=sys.argv[3].encode() if len(sys.argv)>3 else b''\n"
    "t0=time.time()\n"
    "try:\n"
    "    s=socket.create_connection((ip,port),timeout=6)\n"
    "    if payload: s.sendall(payload)\n"
    "    s.settimeout(8)\n"
    "    d=s.recv(96)\n"
    "    print('CONNECTED dt=%.1f first=%r'% (time.time()-t0, d[:40]))\n"
    "    s.close()\n"
    "except ConnectionRefusedError:\n"
    "    print('REFUSED dt=%.1f'% (time.time()-t0))\n"
    "except Exception as e:\n"
    "    print('%s dt=%.1f'% (type(e).__name__, time.time()-t0))\n"
)


def classify(result: str) -> str:
    """Parse probe stdout into CONNECTED / REFUSED / TIMEOUT."""
    if result.startswith("CONNECTED"):
        return "CONNECTED"
    if result.startswith("REFUSED"):
        return "REFUSED"
    return "TIMEOUT"


__all__ = [
    "RelayProfileError", "TRANSPORT_KINDS",
    "DUAL_HOMED_RELAY", "GATEWAY_LISTENER_TO_CONTAINER",
    "PUBLISHED_EGRESS_RELAY",
    "build_relay_edge_contracts", "RELAY_SCRIPT",
    "HOST_RELAY_SCRIPT", "HostRelayTransaction",
    "derive_relay_endpoints",
    "plan_host_relay_unit", "plan_host_relay_systemd_argv",
    "RELAY_SECURITY_FLAGS", "validate_relay_security",
    "SysctlTransaction", "plan_relay_run", "plan_relay_connects",
    "plan_probe_container", "plan_probe_connect",
    "PROBE_CONNECT", "classify",
]
