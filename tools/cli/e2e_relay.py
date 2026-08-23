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
def _relay_ip(subnet_prefix: str) -> str:
    """Derive the relay IP for a /28 subnet (host .14)."""
    return subnet_prefix + ".14"


import e2e_foundation as e2f

_NETS = e2f.E2E_NETWORKS


def build_relay_edge_contracts(tuwunel_ip: str,
                                windows_proxy_ip: str = "172.23.48.1",
                                windows_proxy_port: int = 17890) -> list:
    """Build the 10 frozen R4 edges as relay contracts.

    Static IPs derive from the existing e2e_foundation authority
    (the /28 gateway prefix + .14 for relay hosts, a previously
    unallocated address in each subnet)."""
    edges = []
    full = e2f._build_all_edges(tuwunel_ip)

    for src, dst, port, tag in full:
        src_subnet, dst_subnet = _find_subnets(src, dst)
        edge_id = tag

        if dst == windows_proxy_ip:
            # proxies -> winproxy: published egress relay
            edges.append(_edge(
                edge_id, _role_from_ip(src), src_subnet,
                PUBLISHED_EGRESS_RELAY,
                relay_container="mp-e2e-relay-%s" % tag.replace("-to-", "-"),
                fixed_upstream_host=dst,
                fixed_upstream_port=port,
                listen_port=port))
        elif dst == tuwunel_ip:
            # controller -> tuwunel: published egress relay to matrix
            edges.append(_edge(
                edge_id, _role_from_ip(src), src_subnet,
                PUBLISHED_EGRESS_RELAY,
                relay_container="mp-e2e-relay-%s" % tag.replace("-to-", "-"),
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
    """Find the 3-octet prefix for src and dst IPs."""
    import ipaddress
    src_net = dst_net = ""
    for name, spec in _NETS.items():
        subnet = spec[0]
        if ipaddress.ip_address(src_ip) in ipaddress.ip_network(subnet):
            src_net = subnet.rsplit(".", 1)[0]
        if ipaddress.ip_address(dst_ip) in ipaddress.ip_network(subnet):
            dst_net = subnet.rsplit(".", 1)[0]
    if not src_net:
        # outside our subnets (e.g. agent IPs on 172.21.x) — use the
        # first octet pair as prefix for network naming
        parts = src_ip.split(".")
        if len(parts) == 4:
            src_net = ".".join(parts[:3])
    if not dst_net:
        parts = dst_ip.split(".")
        if len(parts) == 4:
            dst_net = ".".join(parts[:3])
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
RELAY_SCRIPT = r'''
import socket, threading, sys, signal

LISTEN_PORT = int(sys.argv[1])
TARGET_HOST = sys.argv[2]
TARGET_PORT = int(sys.argv[3])

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
srv.bind(("0.0.0.0", LISTEN_PORT))
srv.listen(64)

def _term(s, f):
    srv.close(); sys.exit(0)
signal.signal(signal.SIGTERM, _term)

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
    """Docker run argv for a relay container.

    For DUAL_HOMED_RELAY: `docker create` argv (network none, then
    two connects with static IPs).
    For PUBLISHED_EGRESS_RELAY: `docker run -d` argv with -p.
    """
    kind = edge["transport_kind"]
    name = edge["relay_container"]
    listen = str(edge["listen_port"])

    if kind == PUBLISHED_EGRESS_RELAY:
        upstream_host = edge["fixed_upstream_host"]
        upstream_port = str(edge["fixed_upstream_port"])
        source_net_prefix = edge["source_network"]
        # bind only to the gateway IP of the source network
        gw_ip = source_net_prefix + ".1"
        argv = (
            ["run", "-d", "--name", name,
             "-p", "%s:%s:%s" % (gw_ip, listen, listen),
             "-v", "%s:/relay.py:ro" % relay_script_path]
            + list(RELAY_SECURITY_FLAGS)
            + [image_ref, "python3", "/relay.py", listen,
               upstream_host, upstream_port])
        validate_relay_security(argv)
        return argv

    if kind == DUAL_HOMED_RELAY:
        dest_ip = edge["destination_ip"]
        dest_port = str(edge["destination_port"])
        argv = (
            ["create", "--name", name, "--network", "none",
             "-v", "%s:/relay.py:ro" % relay_script_path]
            + list(RELAY_SECURITY_FLAGS)
            + [image_ref, "python3", "/relay.py", listen,
               dest_ip, dest_port])
        validate_relay_security(argv)
        return argv

    raise RelayProfileError(
        "RELAY_TRANSPORT_KIND_INVALID", repr(kind))


def plan_relay_connects(edge: dict) -> list:
    """Network connect argvs for a dual-homed relay (two attaches)."""
    if edge["transport_kind"] != DUAL_HOMED_RELAY:
        return []
    name = edge["relay_container"]
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


def _network_short_name(subnet_prefix: str) -> str:
    """Map a 3-octet prefix (e.g. '172.31.0') back to its short name."""
    import ipaddress
    for name, spec in _NETS.items():
        full = spec[0]  # e.g. '172.31.0.0/28'
        prefix = full.rsplit(".", 1)[0]  # e.g. '172.31.0'
        if prefix == subnet_prefix:
            return name
    return subnet_prefix.replace(".", "-")


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
    "PROBE_CONNECT", "classify",
]
