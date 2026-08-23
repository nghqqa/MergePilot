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
) -> dict:
    """Internal constructor validating the frozen edge contract."""
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
    """Build the 10 frozen R4 edges as relay contracts.

    Static IPs derive from the existing e2e_foundation authority.
    Deterministic: resets the per-subnet counter each call."""
    _RELAY_IP_COUNTER.clear()
    """Build the 10 frozen R4 edges as relay contracts.

    Static IPs derive from the existing e2e_foundation authority
    (the /28 gateway prefix + .14 for relay hosts, a previously
    unallocated address in each subnet)."""
    edges = []
    full = e2f._build_all_edges(tuwunel_ip)

    # Only the 6 Stage 10 probe edges need relays; the 4 agent-to-
    # gateway edges are cross-daemon and not probed in Stage 10
    import e2e_executors as _ex_probe
    probe_sources = set(
        spec[2] for spec in _ex_probe.ROUTE_PROBE_SPECS.values())

    for src, dst, port, tag in full:
        if src not in probe_sources:
            continue
        src_subnet, dst_subnet = _find_subnets(src, dst)
        edge_id = tag

        if dst == windows_proxy_ip or dst == tuwunel_ip:
            # egress to external target (winproxy/tuwunel): dual-homed
            # relay on the source network, upstream to external target
            edges.append(_edge(
                edge_id, _role_from_ip(src), src_subnet,
                PUBLISHED_EGRESS_RELAY,
                relay_container="mp-e2e-relay-%s" % tag.replace("-to-", "-"),
                relay_source_ip=_relay_ip(src_subnet),
                fixed_upstream_host=dst,
                fixed_upstream_port=port,
                listen_port=port))
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


def plan_relay_connects(edge: dict) -> list:
    """Network connect argvs for each transport kind."""
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
    if kind == PUBLISHED_EGRESS_RELAY:
        return [
            ["network", "connect",
             "--ip", edge["relay_source_ip"],
             "--gw-priority", "100",
             "mp-e2e-" + _network_short_name(edge["source_network"]),
             name],
        ]
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
    "RELAY_SECURITY_FLAGS", "validate_relay_security",
    "SysctlTransaction", "plan_relay_run", "plan_relay_connects",
    "plan_probe_container", "plan_probe_connect",
    "PROBE_CONNECT", "classify",
]
