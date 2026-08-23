"""M8-GH-4B3-W3B-R2: E2E lifecycle orchestration (production-reachable).

Sole orchestrator for E2E-mode start/stop/status/cleanup. The CLI's
`--github-e2e` path calls into this module through injected executors
(no CLI↔lifecycle circular import; everything is callable injection).

§3   prerequisite gate (20-key config + read-only probes, zero side
     effects on failure).
§4   runtime validate → per-file atomic create → sanitized journal →
     immediate session persistence → six persisted → first network.
§6   startup DAG below (frozen; health-gated, bounded polling).
§10  receipt dual-check (pre-start + pre-complete).
§11  Matrix membership dual-check (pre-start + pre-complete).
§12  full-stage rollback: containers → firewall → networks → runtime.
§13  sanitized status for 11 services.
§14  stop (owned-only, ordered, idempotent) + cleanup (scan/report).
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import e2e_foundation as e2f
import e2e_probes as ep
import e2e_executors as ex
import e2e_runtime_specs as rs
import e2e_gateway_health as gwh

# Frozen 11-service startup DAG (planner.E2E_SERVICE_ORDER mirror).
_DAG_ORDER = (
    "postgres",           # database must be ready first
    "gh-proxy-r",         # reporter egress
    "gh-proxy-b",         # bridge egress
    "mcp-bridge",         # MCP upstream for gateway
    "policy-gateway",     # needs bridge MCP ready
    "controller",         # needs gateway + tuwunel
    "gh-webhook",         # webhook receiver
    "demo-console",       # console backend
    "console-edge",       # console proxy
    "gh-reporter",        # needs postgres + proxy-r
    "preflight",          # final semantic check
)

#: The six CLI-owned multi-homed services with authoritative runtime
#: specs (env-file + single-file :ro mounts). The remaining five DAG
#: services are created via the CLI-injected default-service plan.
_SPEC_SERVICES = (
    "controller", "policy-gateway", "mcp-bridge",
    "gh-reporter", "gh-proxy-r", "gh-proxy-b",
)

#: Operator-owned HiClaw agents — READ-ONLY readiness verification,
#: never created/modified/restarted by the CLI.
AGENT_ROLES = ("manager", "reviewer", "fixer", "verifier")

#: Bounded health polling (no fixed-sleep health assumptions).
HEALTH_POLL_SECONDS = 0.5
HEALTH_TIMEOUT_SECONDS = 120

#: Frozen E2E endpoint URLs (health targets). The gateway target is
#: the REVIEWER role endpoint (Bearer-authenticated /{role}/sse per
#: e2e_executors.hiclaw_role_gateway_url); the reviewer role's
#: policy exposure is EXACTLY the frozen read-only set, which makes
#: it the natural health-probe role. The gateway listens on its
#: gw-egress IP 172.31.0.18:8083 — 172.31.0.34 is the BRIDGE (8082
#: only), and a "manager" role does not exist in the fixture
#: policy (roles: reviewer/verifier/fixer/coordinator).
GATEWAY_SSE_URL = "http://172.31.0.18:8083/reviewer/sse"
BRIDGE_SSE_URL = "http://172.31.0.34:8082/sse"

_PREREQ_CONFIG_NAME = "github-e2e.json"


class E2ELifecycleError(Exception):
    def __init__(self, code: str, detail: str,
                 diagnostics: Optional[list] = None):
        self.code = code
        self.detail = detail
        self.diagnostics = list(diagnostics or [])
        super().__init__("%s: %s" % (code, detail))


# ── §3: prerequisite config loading + gate (zero side effects) ────────────

_WIN_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _to_wsl_source(path: str) -> str:
    r"""Mount sources must be WSL-visible: docker runs INSIDE the
    distro, so a Windows drive path (D:\x\y) crosses as /mnt/d/x/y —
    the same mapping _to_wsl_path applies to --env-file arguments in
    the CLI. Unconverted backslash paths reach the daemon mangled
    (D:x y) and every container create fails (the first real E2E
    start failed on exactly this)."""
    m = _WIN_PATH_RE.match(str(path))
    if not m:
        return str(path).replace("\\", "/")
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return "/mnt/%s/%s" % (drive, rest)


def _wsl_mounts(mounts: list) -> list:
    """Convert ['-v', '<src>:<dst>:<mode>', ...] sources."""
    out = []
    for i, frag in enumerate(mounts):
        if i % 2 == 0:
            out.append(frag)
            continue
        src, _sep, tail = frag.rpartition(":")
        out.append("%s:%s" % (_to_wsl_source(src), tail))
    return out


def load_e2e_prerequisite_config(path) -> dict:
    """Strictly load + validate the 20-key prerequisite config.

    A missing/unparseable file IS a real prerequisite failure (the
    external resources were never provisioned) — never an unconditional
    fake error. The detail carries only the config basename and a safe
    code; never path values beyond the state dir, never secret content.
    """
    path = Path(path)
    if not path.is_file():
        raise E2ELifecycleError(
            "GITHUB_E2E_PREREQUISITES_INCOMPLETE",
            "prerequisite config absent: %s (provision external "
            "resources first)" % _PREREQ_CONFIG_NAME)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise E2ELifecycleError(
            "GITHUB_E2E_PREREQUISITES_INCOMPLETE",
            "prerequisite config unparseable: %s"
            % _PREREQ_CONFIG_NAME) from None
    try:
        return ep.validate_prereq_config(raw)
    except ep.PrereqConfigError as exc:
        raise E2ELifecycleError(
            "GITHUB_E2E_PREREQUISITES_INCOMPLETE",
            "prerequisite config invalid: %s" % exc.code) from None


def run_prerequisite_gate(config: dict, *,
                          docker_executor: Callable,
                          host_executor: Callable,
                          matrix_joined_mxids=None,
                          docker_gw_priority_supported=None,
                          existing_network_cidrs=None,
                          firewall_scan_text: str = "") -> dict:
    """§3: validate config + run all read-only probes. Raises
    E2ELifecycleError(GITHUB_E2E_PREREQUISITES_INCOMPLETE, failed
    probe names) on any failure — before ANY side effect."""
    try:
        return ep.run_e2e_prerequisite_gate(
            config,
            matrix_joined_mxids=matrix_joined_mxids,
            docker_gw_priority_supported=docker_gw_priority_supported,
            existing_network_cidrs=existing_network_cidrs,
            firewall_scan_text=firewall_scan_text)
    except e2f.E2EConfigError as exc:
        raise E2ELifecycleError(exc.code, exc.detail) from None


# ── §3 R3: PRODUCTION read-only probe adapters ────────────────────────────
# These produce the four probe inputs from REAL environment state.
# All are strictly read-only; any failure yields a None/False/empty
# value so the prerequisite gate fails CLOSED (PROBE_NOT_INJECTED /
# probe failure) instead of silently passing.

def fetch_firewall_scan_text(host_executor: Callable) -> str:
    """Read-only iptables-save text for conflict/residue scanning."""
    try:
        cp = host_executor(["iptables-save"], check=False)
    except Exception:
        return ""
    if cp.returncode != 0:
        return ""
    return (cp.stdout or b"").decode("utf-8", "replace")


def fetch_existing_network_cidrs(docker_executor: Callable) -> list:
    """Read-only subnet inventory of every existing docker network."""
    cidrs = []
    try:
        cp = docker_executor(
            ["network", "ls", "--format", "{{.Name}}"], check=False)
        if cp.returncode != 0:
            return []
        names = (cp.stdout or b"").decode(
            "utf-8", "replace").split()
        for name in names:
            try:
                icp = docker_executor(
                    ["network", "inspect", name, "--format",
                     "{{range .IPAM.Config}}{{.Subnet}} {{end}}"],
                    check=False)
                if icp.returncode == 0:
                    cidrs.extend(
                        (icp.stdout or b"").decode(
                            "utf-8", "replace").split())
            except Exception:
                continue
    except Exception:
        return []
    return cidrs


def fetch_docker_gw_priority_supported(docker_executor) -> bool:
    """Read-only capability probe: docker network connect --gw-priority
    (same check cmd_doctor performs)."""
    try:
        cp = docker_executor(
            ["network", "connect", "--help"], check=False, timeout=30)
    except Exception:
        return False
    if cp.returncode != 0:
        return False
    return "--gw-priority" in (cp.stdout or b"").decode(
        "utf-8", "replace")


def fetch_matrix_joined_mxids(config: dict,
                              transport: Callable = None) -> Optional[list]:
    """§11: REAL read-only Matrix membership provider.

    Queries the homeserver's joined_members API using the controller
    credential named by the prerequisite config. Strictly read-only;
    the token is never logged or echoed. Any failure returns None so
    the membership probe fails closed (never a faked member list)."""
    try:
        creds = json.loads(Path(
            config["matrix_credentials_path"]).read_text(
                encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    token = (creds.get("access_token") or creds.get("token")
             or creds.get("matrix_token") or "")
    if not token:
        return None
    homeserver = config["matrix_homeserver"].rstrip("/")
    import urllib.parse
    room = urllib.parse.quote(config["matrix_room_id"], safe="")
    url = "%s/_matrix/client/v3/rooms/%s/joined_members" % (
        homeserver, room)
    request = urllib.request.Request(url)
    request.add_header("Authorization", "Bearer %s" % token)
    try:
        if transport is not None:
            status, _headers, body = transport(
                "GET", url,
                headers={"Authorization": "Bearer %s" % token},
                body=None)
        else:
            with urllib.request.urlopen(request, timeout=10) as resp:
                body = json.loads(resp.read().decode(
                    "utf-8", "replace"))
        if transport is not None:
            if status != 200 or not isinstance(body, dict):
                return None
        joined = body.get("joined")
        if not isinstance(joined, dict):
            return None
        return sorted(joined)
    except Exception:
        return None


def _matrix_membership_ok(joined_mxids) -> bool:
    """§11: all five frozen room members present in the joined set."""
    if joined_mxids is None:
        return False
    joined = set(joined_mxids)
    return all(m in joined for m in e2f.E2E_EXPECTED_ROOM_MEMBERS)


# ── production health probes (argv-level; injectable executors) ───────────

def _container_id(docker_executor, name) -> str:
    try:
        cp = docker_executor(
            ["inspect", name, "--format", "{{.Id}}"], check=False)
    except Exception:
        return ""
    if cp.returncode != 0:
        return ""
    return (cp.stdout or b"").decode("utf-8", "replace").strip()


def _container_running(docker_executor, name) -> bool:
    try:
        cp = docker_executor(
            ["inspect", name, "--format", "{{.State.Running}}"],
            check=False)
    except Exception:
        return False
    if cp.returncode != 0:
        return False
    return (cp.stdout or b"").decode().strip().lower() == "true"


def _probe_exec(docker_executor, name: str, probe_argv: list) -> bool:
    """One in-container probe attempt (docker exec; rc==0 = healthy)."""
    try:
        cp = docker_executor(
            ["exec", name] + probe_argv, check=False)
    except Exception:
        return False
    return cp.returncode == 0


def _wait_probe(docker_executor, name: str, probe_argv: list,
                *, what: str) -> None:
    """Bounded poll loop around an exec probe; stable error code."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _probe_exec(docker_executor, name, probe_argv):
            return
        time.sleep(HEALTH_POLL_SECONDS)
    raise E2ELifecycleError(
        "E2E_%s_UNREADY" % what.upper().replace("-", "_"), name)


def _wait_running(docker_executor, name: str, *, what: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _container_running(docker_executor, name):
            return
        time.sleep(HEALTH_POLL_SECONDS)
    raise E2ELifecycleError(
        "E2E_%s_UNREADY" % what.upper().replace("-", "_"), name)


# service -> (probe kind, probe argv or url)
_HEALTH_KINDS = {
    "postgres": ("exec", ["pg_isready", "-U", "postgres"]),
    "gh-proxy-r": ("exec", _PY_TCP_PROBE := [
        "python3", "-c",
        "import socket;s=socket.create_connection(('127.0.0.1',18090),3)"]),
    "gh-proxy-b": ("exec", _PY_TCP_PROBE),
}


def production_service_health(docker_executor, service: str,
                              mcp_health: Callable = None,
                              gateway_bearer: str = "") -> None:
    """§6 production readiness gate for one service (bounded poll).

    - postgres: docker exec pg_isready
    - proxies: docker exec python3 TCP connect
    - bridge: MCP initialize + tools/list (non-zero, read-only subset)
    - gateway: MCP initialize + tools/list (frozen exact set)
    - others: docker inspect State.Running

    The two MCP checks exec INSIDE the target service container and
    probe its own loopback port: no Windows-host position can reach
    the docker-network IPs at all (no L3 route into the WSL bridges;
    a system TUN proxy intercepts even no-proxy direct connects),
    and the distro-host position is blocked by the §8 firewall's
    total container→LOCAL deny (which deliberately drops even
    ESTABLISHED reply packets). In-container loopback is the same
    position pg_isready and the proxy TCP probes already use.

    gateway_bearer is the manager role token for the gateway probe;
    it travels to the probe process on STDIN (docker exec -i) and
    is never logged, journalled or persisted.
    Raises E2ELifecycleError with a stable code when unready.
    """
    name = "mergepilot-isolated-%s-1" % service
    kind = _HEALTH_KINDS.get(service)
    if kind:
        _wait_probe(docker_executor, name, kind[1], what=service)
        return
    if service == "mcp-bridge":
        check = mcp_health or _bridge_mcp_check(docker_executor)
        _wait_mcp(check, _LOOPBACK_BRIDGE_SSE_URL, service,
                  diagnose=lambda: _container_death_detail(
                      docker_executor, name))
        return
    if service == "policy-gateway":
        check = mcp_health or _gateway_mcp_check(docker_executor,
                                                 gateway_bearer)
        _wait_mcp(check, _LOOPBACK_GATEWAY_SSE_URL, service,
                  diagnose=lambda: _container_death_detail(
                      docker_executor, name))
        return
    _wait_running(docker_executor, name, what=service)


#: Loopback probe targets (in-container; the *_SSE_URL constants
#: above stay the network-facing contract for journals/status).
_LOOPBACK_BRIDGE_SSE_URL = "http://127.0.0.1:8082/sse"
_LOOPBACK_GATEWAY_SSE_URL = "http://127.0.0.1:8083/reviewer/sse"


# Real MCP SSE client (stdlib only), transported into the distro as a
# `python3 -c <script> <url>` argv (the same pattern as the harness's
# _S3_COND_SCRIPT bootstrap). Dialect: GET <url> as text/event-stream
# → first data line carrying an absolute path is the per-session POST
# endpoint (SDK servers send /messages/?session_id=…, mcp-proxy sends
# /message?sessionId=…) → POST initialize → POST notifications/
# initialized → POST tools/list; JSON-RPC responses arrive as SSE
# `data:` events on the GET stream. The optional bearer rides STDIN
# (never argv — it must not appear in distro ps or wsl_exe logs).
_MCP_SSE_PROBE_SCRIPT = r'''
import json, queue, sys, threading, time, urllib.request
from urllib.parse import urlparse

URL = sys.argv[1]
BEARER = ""
try:
    BEARER = sys.stdin.readline().strip()
except Exception:
    pass
DEADLINE = time.monotonic() + 15
EVENTS = queue.Queue()
STATE = {"error": None}
HEADERS = {"Accept": "text/event-stream",
           "Content-Type": "application/json"}
if BEARER:
    HEADERS["Authorization"] = "Bearer " + BEARER


def fail(code):
    sys.stdout.write(json.dumps({"error": code}))
    sys.exit(1)


def reader():
    try:
        req = urllib.request.Request(URL, headers=HEADERS, method="GET")
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status != 200:
            STATE["error"] = "PROBE_HTTP_%d" % resp.status
            return
        buf = []
        for raw in resp:
            if time.monotonic() > DEADLINE:
                STATE["error"] = "PROBE_TIMEOUT"
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                buf.append(line)
                continue
            if not buf:
                continue
            ev = {}
            for ln in buf:
                if ln.startswith("event:"):
                    ev["event"] = ln[6:].strip()
                elif ln.startswith("data:"):
                    ev["data"] = ev.get("data", "") + ln[5:].strip()
            EVENTS.put(ev)
            buf = []
    except Exception:
        STATE["error"] = STATE["error"] or "PROBE_CONNECT_FAILED"


def post(endpoint, payload):
    req = urllib.request.Request(endpoint,
                                 data=json.dumps(payload).encode(),
                                 headers=HEADERS, method="POST")
    urllib.request.urlopen(req, timeout=10)


def wait_response(rid):
    while time.monotonic() < DEADLINE:
        try:
            ev = EVENTS.get(timeout=1)
        except queue.Empty:
            if STATE["error"]:
                fail(STATE["error"])
            continue
        try:
            data = json.loads(ev.get("data", ""))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("id") == rid:
            return data
    fail("PROBE_TIMEOUT")


try:
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    endpoint_path = None
    while time.monotonic() < DEADLINE and endpoint_path is None:
        try:
            ev = EVENTS.get(timeout=1)
        except queue.Empty:
            if STATE["error"]:
                fail(STATE["error"])
            continue
        data = ev.get("data", "")
        if data.startswith("/"):
            endpoint_path = data
    if endpoint_path is None:
        fail("PROBE_NO_ENDPOINT")
    base = "%s://%s" % (urlparse(URL).scheme, urlparse(URL).netloc)
    endpoint = base + endpoint_path
    post(endpoint, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "mp-e2e-health",
                                              "version": "1.0"}}})
    init = wait_response(1)
    if init.get("error"):
        fail("PROBE_INIT_ERROR")
    post(endpoint, {"jsonrpc": "2.0",
                    "method": "notifications/initialized"})
    post(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listing = wait_response(2)
    if listing.get("error"):
        fail("PROBE_TOOLS_ERROR")
    tools = sorted(t.get("name", "")
                   for t in (listing.get("result") or {}).get("tools", [])
                   if isinstance(t, dict))
    sys.stdout.write(json.dumps({"tools": tools}))
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    fail("PROBE_UNEXPECTED")
'''


def exec_mcp_sse_probe(runner: Callable, url: str,
                       bearer: str = "", timeout: int = 25) -> dict:
    """Run the real MCP SSE probe via an exec runner bound to the
    target service container (`docker exec -i <container> python3
    -c <script> <url>`, bearer on STDIN only).

    Returns the same {"healthy", "tools", "error"} shape as the
    gateway-health module; any transport-level failure collapses to
    GATEWAY_UPSTREAM_UNREACHABLE (the historical code for an
    unreachable upstream, preserved for status continuity)."""
    argv = ["python3", "-c", _MCP_SSE_PROBE_SCRIPT, url]
    try:
        cp = runner(argv, check=False, timeout=timeout,
                    input_bytes=((bearer or "") + "\n").encode("utf-8"))
    except Exception:
        return {"healthy": False, "tools": [],
                "error": "GATEWAY_UPSTREAM_UNREACHABLE"}
    out = (cp.stdout or b"").decode("utf-8", "replace").strip()
    # the probe script reports its precise failure code as JSON on
    # stdout even when exiting non-zero — prefer it over the generic
    # unreachable collapse
    if out:
        try:
            data = json.loads(out)
            if isinstance(data, dict) and data.get("error"):
                return {"healthy": False, "tools": [],
                        "error": data["error"]}
        except ValueError:
            pass
    if getattr(cp, "returncode", 1) != 0 or not out:
        return {"healthy": False, "tools": [],
                "error": "GATEWAY_UPSTREAM_UNREACHABLE"}
    try:
        data = json.loads(out)
    except ValueError:
        return {"healthy": False, "tools": [],
                "error": "GATEWAY_TOOLS_PARSE_ERROR"}
    if data.get("error"):
        return {"healthy": False, "tools": [], "error": data["error"]}
    return {"healthy": True, "tools": list(data.get("tools") or []),
            "error": None}


def _container_exec_runner(docker_executor: Callable,
                            container: str) -> Callable:
    """docker-exec runner for the MCP probe (interactive so the
    bearer can ride STDIN; same channel the firewall blob uses)."""
    def runner(argv, check=False, timeout=25, input_bytes=None):
        return docker_executor(["exec", "-i", container] + list(argv),
                               check=check, timeout=timeout,
                               input_bytes=input_bytes)
    return runner


def _health_with_tool_contract(result: dict, exact: bool) -> dict:
    """Apply the frozen tool contract to a probe result (subset for
    the bridge, exact set for the gateway) using the same stable
    error codes as e2e_gateway_health."""
    if not result.get("healthy"):
        return {"healthy": False, "tools": result.get("tools") or [],
                "error": result.get("error") or "GATEWAY_UPSTREAM_UNREACHABLE"}
    tools = frozenset(result.get("tools") or [])
    if not tools:
        return {"healthy": False, "tools": [],
                "error": "GATEWAY_ZERO_TOOLS"}
    missing = frozenset(gwh.FROZEN_READ_ONLY_TOOLS) - tools
    if missing:
        return {"healthy": False, "tools": sorted(tools),
                "error": "GATEWAY_MISSING_TOOLS"}
    if exact:
        extra = tools - frozenset(gwh.FROZEN_READ_ONLY_TOOLS)
        if extra:
            return {"healthy": False, "tools": sorted(tools),
                    "error": "GATEWAY_EXTRA_TOOLS"}
    return {"healthy": True, "tools": sorted(tools), "error": None}


def _bridge_mcp_check(docker_executor: Callable) -> Callable:
    def check(url: str) -> dict:
        if docker_executor is None:
            return {"healthy": False, "tools": [],
                    "error": "GATEWAY_UPSTREAM_UNREACHABLE"}
        return _health_with_tool_contract(
            exec_mcp_sse_probe(
                _container_exec_runner(
                    docker_executor,
                    "mergepilot-isolated-mcp-bridge-1"),
                url), exact=False)
    return check


def _gateway_mcp_check(docker_executor: Callable,
                       gateway_bearer: str) -> Callable:
    def check(url: str) -> dict:
        if docker_executor is None:
            return {"healthy": False, "tools": [],
                    "error": "GATEWAY_UPSTREAM_UNREACHABLE"}
        return _health_with_tool_contract(
            exec_mcp_sse_probe(
                _container_exec_runner(
                    docker_executor,
                    "mergepilot-isolated-policy-gateway-1"),
                url, bearer=gateway_bearer), exact=True)
    return check


def _container_death_detail(docker_executor: Callable,
                            name: str) -> str:
    """Sanitized post-mortem for a service that failed its MCP
    health gate: State/exit/oom/error plus the last log lines
    (bounded, newline-flattened; env/token values are never logged
    by these images, and the text is truncated hard anyway)."""
    try:
        cp = docker_executor(
            ["inspect", name, "--format",
             "{{.State.Status}} exit={{.State.ExitCode}}"
             " oom={{.State.OOMKilled}} err={{.State.Error}}"],
            check=False)
        state = (cp.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        state = "inspect-failed"
    tail = ""
    try:
        cp = docker_executor(["logs", "--tail", "10", name],
                             check=False, timeout=20)
        tail = ((cp.stderr or b"") + (cp.stdout or b"")).decode(
            "utf-8", "replace")
    except Exception:
        tail = ""
    flat = " ".join(tail.split())[-320:]
    return "container[%s] %s | logs: %s" % (name.split("-")[-2],
                                             state, flat)


def _wait_mcp(check: Callable, url: str, service: str,
              timeout: float = HEALTH_TIMEOUT_SECONDS,
              diagnose: Callable = None) -> None:
    deadline = time.monotonic() + timeout
    last = "GATEWAY_UPSTREAM_UNREACHABLE"
    while time.monotonic() < deadline:
        result = check(url)
        if result.get("healthy"):
            return
        last = result.get("error") or last
        time.sleep(HEALTH_POLL_SECONDS)
    if diagnose is not None:
        try:
            last = "%s | %s" % (last, diagnose())
        except Exception:
            pass
    raise E2ELifecycleError(
        "E2E_%s_MCP_UNHEALTHY" % service.upper().replace("-", "_"),
        last)


# ── §6: full startup DAG ──────────────────────────────────────────────────

def run_e2e_start(*, config: dict,
                  runtime_configs: dict = None,
                  runtime_directory: str = "",
                  docker_executor: Callable,
                  host_executor: Callable,
                  image_refs: dict,
                  default_service_plan: Callable = None,
                  db_bootstrap: Callable = None,
                  matrix_members_provider: Callable = None,
                  service_health: Callable = None,
                  receipt_validator: Callable = None,
                  persist_callback: Callable = None,
                  env_file_resolver: Callable = None,
                  matrix_joined_mxids=None,
                  docker_gw_priority_supported=None,
                  existing_network_cidrs=None,
                  firewall_scan_text: str = "",
                  gateway_bearer: str = "",
                  agents_docker_executor: Callable = None,
                  session: dict = None) -> dict:
    """Full E2E startup DAG. Returns the session dict (journal).

    DAG (frozen): prerequisites → runtime validate/create/persist →
    8 networks → 11 containers create/connect → firewall → postgres
    ready → DB bootstrap → proxies ready → bridge start + MCP health →
    route probes → gateway start + MCP health → controller + reporter
    → four agents (read-only readiness) → receipt recheck → Matrix
    recheck → final semantic preflight → complete.

    ANY failure → reverse-order rollback of owned resources; the
    primary error survives (rollback issues become diagnostics).
    """
    session = session or {}
    session.setdefault("e2e_network_ids", {})
    session.setdefault("e2e_container_ids", {})
    session.setdefault("e2e_stage", "init")
    session.setdefault("e2e_runtime_journal", {})
    session.setdefault("e2e_started", [])

    def _persist():
        if persist_callback:
            persist_callback(session)

    def _fail(code, detail):
        diagnostics = _rollback_all(
            docker_executor, session, host_executor,
            runtime_directory)
        raise E2ELifecycleError(code, detail, diagnostics)

    # ── Stage 1: prerequisite gate (§3; zero side effects on failure)
    session["e2e_stage"] = "prerequisites"
    probe_result = run_prerequisite_gate(
        config,
        docker_executor=docker_executor,
        host_executor=host_executor,
        matrix_joined_mxids=matrix_joined_mxids,
        docker_gw_priority_supported=docker_gw_priority_supported,
        existing_network_cidrs=existing_network_cidrs,
        firewall_scan_text=firewall_scan_text)
    session["prerequisite_summary"] = {
        "verified": True,
        "checks_passed": len(probe_result["checks"]),
    }
    _persist()

    # ── Stage 2: runtime validate → atomic create → journal → persist
    session["e2e_stage"] = "runtime_files"
    if runtime_configs is not None:
        try:
            validated = rs.validate_runtime_configs(runtime_configs)
        except rs.RuntimeSpecError as exc:
            # Nothing written yet — nothing to roll back.
            raise E2ELifecycleError(exc.code, exc.detail) from None

        def _journal_persist(journal):
            # §4: persist the SESSION (which carries the sanitized
            # runtime journal) immediately after every file creation.
            _persist()

        try:
            rs.create_runtime_files(
                validated, directory=runtime_directory,
                journal=session["e2e_runtime_journal"],
                persist_callback=_journal_persist)
        except rs.RuntimeSpecError as exc:
            _fail(exc.code, exc.detail)
        except Exception as exc:
            _fail("E2E_RUNTIME_FILE_CREATE_FAILED",
                  type(exc).__name__)
    _persist()

    # ── Stage 3: 8 E2E networks (§6) ──
    session["e2e_stage"] = "networks"
    try:
        ep.create_e2e_networks(docker_executor,
                               journal=session["e2e_network_ids"])
    except Exception as exc:
        _fail("E2E_NETWORK_CREATE_FAILED", type(exc).__name__)
    _persist()

    # ── Stage 4: 11 containers create+connect (§6) ──
    session["e2e_stage"] = "containers"
    for service in _SPEC_SERVICES:
        image = image_refs.get(service, "")
        if not image:
            _fail("E2E_IMAGE_MISSING", service)
        if env_file_resolver is not None:
            env_file = env_file_resolver(service)
        else:
            env_file = _absolute_env_file(runtime_directory, service)
        mounts = _wsl_mounts(
            rs.plan_runtime_mounts(service, config=config))
        try:
            ep.execute_e2e_container_setup(
                docker_executor, service,
                image_ref=image, env_file=env_file,
                mounts=mounts,
                container_journal=session["e2e_container_ids"])
        except Exception:
            _fail("E2E_CONTAINER_SETUP_FAILED", service)
        _persist()
    if default_service_plan is not None:
        for service in ("postgres", "gh-webhook", "demo-console",
                        "console-edge", "preflight"):
            argv = default_service_plan(service)
            if not argv:
                _fail("E2E_SERVICE_PLAN_MISSING", service)
            name = "mergepilot-isolated-%s-1" % service
            try:
                # reap a stale OWNED never-started container from
                # an earlier failed run (_fail fired before the cid
                # reached the journal). ONLY when not running: a
                # running holder means foreign ownership -> leave it
                # and let the run below fail closed on the conflict
                try:
                    probe = docker_executor(
                        ["inspect", name, "--format",
                         "{{.State.Status}}"], check=False)
                    if getattr(probe, "returncode", 1) == 0 and                             (probe.stdout or b"").strip() == b"created":
                        docker_executor(["rm", "-f", name],
                                        check=False)
                except Exception:
                    pass
                docker_executor(list(argv), check=True)
                cid = _container_id(docker_executor, name)
                if not cid:
                    raise RuntimeError("container id missing")
                session["e2e_container_ids"][service] = cid
            except Exception:
                _fail("E2E_CONTAINER_SETUP_FAILED", service)
            _persist()

    # ── Stage 5: firewall install (§8) ──
    session["e2e_stage"] = "firewall"
    edges = e2f._build_all_edges(config["tuwunel_ip"])
    plan = e2f.build_firewall_plan(
        _sid_from_session(session),
        edges=edges, own_subnets=e2f.R4_ALL_SUBNETS)
    try:
        state = ex.install_firewall(plan, host_executor=host_executor,
                                    journal=session)
        session["firewall_sid"] = plan["sid"]
        session["firewall_state"] = state
    except ex.FirewallExecutorError as exc:
        _fail(exc.code, exc.detail)
    except Exception as exc:
        _fail("E2E_FIREWALL_INSTALL_FAILED", type(exc).__name__)
    _persist()

    health = service_health or (
        lambda svc: production_service_health(
            docker_executor, svc, gateway_bearer=gateway_bearer))

    # ── Stage 6: postgres start + ready (bounded pg_isready poll) ──
    session["e2e_stage"] = "postgres_ready"
    _start_service(docker_executor, session, "postgres", _fail)
    try:
        health("postgres")
    except E2ELifecycleError as exc:
        _fail(exc.code, exc.detail)

    # ── Stage 7: DB bootstrap (injected; CLI owns the SQL) ──
    session["e2e_stage"] = "db_bootstrap"
    if db_bootstrap is not None:
        try:
            db_bootstrap()
        except Exception as exc:
            _fail("E2E_DB_BOOTSTRAP_FAILED", type(exc).__name__)
    _persist()

    # ── Stage 8: proxies start + ready ──
    session["e2e_stage"] = "proxies_ready"
    for svc in ("gh-proxy-r", "gh-proxy-b"):
        _start_service(docker_executor, session, svc, _fail)
        try:
            health(svc)
        except E2ELifecycleError as exc:
            _fail(exc.code, exc.detail)

    # ── Stage 9: bridge start + MCP semantic health ──
    session["e2e_stage"] = "bridge_start"
    _start_service(docker_executor, session, "mcp-bridge", _fail)
    session["e2e_stage"] = "bridge_health"
    try:
        health("mcp-bridge")
    except E2ELifecycleError as exc:
        _fail(exc.code, exc.detail)

    # ── Stage 10: route probes (§7) ──
    session["e2e_stage"] = "route_probes"
    probe_journal = {}
    try:
        route_result = ex.run_route_probes(
            docker_executor=docker_executor,
            host_executor=host_executor,
            image_ref=image_refs.get(
                "gh-proxy-r", image_refs.get("mcp-bridge", "")),
            tuwunel_ip=config["tuwunel_ip"],
            windows_proxy_ip=config["windows_proxy_ip"],
            probe_journal=probe_journal)
    except Exception as exc:
        _fail("E2E_ROUTE_PROBE_FAILED", type(exc).__name__)
    session["route_probe_results"] = {
        svc: {"verified": r.get("verified", False),
              "error": r.get("error", ""),
              "detail": r.get("detail", ""),
              "vantage": r.get("vantage", "probe-container")}
        for svc, r in route_result.items()}
    # persist BEFORE the gate: a failing probe must leave its code
    # in the journal (the rollback keeps the last persisted state)
    _persist()
    if not all(r.get("verified") for r in route_result.values()):
        failed = [s for s, r in route_result.items()
                  if not r.get("verified")]
        _fail("E2E_ROUTE_PROBE_FAILED", "failed: %s" % failed)
    _persist()

    # ── Stage 11: gateway start + MCP semantic health (frozen set) ──
    session["e2e_stage"] = "gateway_start"
    _start_service(docker_executor, session, "policy-gateway", _fail)
    session["e2e_stage"] = "gateway_health"
    try:
        health("policy-gateway")
    except E2ELifecycleError as exc:
        _fail(exc.code, exc.detail)

    # ── Stage 12: remaining services in the frozen DAG order
    # (controller → webhook → console → edge → reporter), each
    # dependency-gated with a bounded health poll before the next.
    for svc in _DAG_ORDER[5:10]:
        session["e2e_stage"] = "%s_start" % svc.replace("-", "_")
        _start_service(docker_executor, session, svc, _fail)
        try:
            health(svc)
        except E2ELifecycleError as exc:
            _fail(exc.code, exc.detail)
    _persist()

    # ── Stage 13: four agents — READ-ONLY readiness (§6; never
    # created/modified/restarted by the CLI; HiClaw untouched). The
    # HiClaw stack lives in the HARNESS distro (Ubuntu-22.04 — the
    # same authority the rewiring harness execs against), not the
    # E2E distro; agents_docker_executor carries that binding. ──
    session["e2e_stage"] = "agents_ready"
    agents_exec = agents_docker_executor or docker_executor
    not_ready = [role for role in AGENT_ROLES
                 if not _container_running(
                     agents_exec,
                     ex.HICLAW_ROLE_FREEZE[role][0])]
    if not_ready:
        _fail("E2E_AGENTS_NOT_READY",
              "not running: %s" % sorted(not_ready))
    _persist()

    # ── Stage 14: receipt recheck (§10; drift → no complete) ──
    session["e2e_stage"] = "receipt_recheck"
    validate_receipt = receipt_validator or _default_receipt_validator
    receipt_path = config.get("hiclaw_receipt_path", "")
    if receipt_path:
        try:
            receipt_result = validate_receipt(receipt_path)
        except Exception:
            _fail("E2E_RECEIPT_RECHECK_FAILED", "receipt drift detected")
        if not receipt_result.get("verified", False):
            _fail("E2E_RECEIPT_RECHECK_FAILED", "receipt drift detected")
        session["receipt_verified"] = True

    # ── Stage 15: Matrix membership recheck (§11; drift → no complete) ──
    session["e2e_stage"] = "matrix_recheck"
    if matrix_members_provider is not None:
        joined = matrix_members_provider()
        if not _matrix_membership_ok(joined):
            missing = sorted(
                m for m in e2f.E2E_EXPECTED_ROOM_MEMBERS
                if m not in set(joined or ()))
            _fail("E2E_MATRIX_RECHECK_FAILED",
                  "missing members: %s" % missing)
        session["matrix_verified"] = True

    # ── Stage 16: final semantic preflight ──
    session["e2e_stage"] = "final_preflight"
    _start_service(docker_executor, session, "preflight", _fail)
    exit_code = _wait_preflight_exit(docker_executor)
    if exit_code != 0:
        _fail("E2E_PREFLIGHT_FAILED", "preflight exit=%d" % exit_code)
    _persist()

    # ── Stage 17: complete ──
    session["e2e_stage"] = "complete"
    session["e2e_pending_components"] = ()
    _persist()
    return session


def _start_service(docker_executor, session, service, _fail) -> None:
    try:
        docker_executor(
            ["start", "mergepilot-isolated-%s-1" % service],
            check=True)
        session.setdefault("e2e_started", []).append(service)
    except Exception:
        _fail("E2E_SERVICE_START_FAILED", service)


def _wait_preflight_exit(docker_executor,
                         timeout: float = HEALTH_TIMEOUT_SECONDS * 2):
    """Bounded poll for the one-shot preflight container exit code."""
    name = "mergepilot-isolated-preflight-1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cp = docker_executor(
                ["inspect", name, "--format",
                 "{{.State.Running}} {{.State.ExitCode}}"], check=False)
        except Exception:
            return -1
        if cp.returncode == 0:
            parts = (cp.stdout or b"").decode().strip().split()
            if parts and parts[0].lower() == "false":
                try:
                    return int(parts[1])
                except (IndexError, ValueError):
                    return -1
        time.sleep(HEALTH_POLL_SECONDS)
    return -1


def _default_receipt_validator(receipt_path: str) -> dict:
    raise E2ELifecycleError(
        "E2E_RECEIPT_RECHECK_FAILED",
        "receipt validation requires a docker executor")


# ── §14: stop (owned-only, ordered, idempotent) ───────────────────────────

def run_e2e_stop(*, docker_executor: Callable,
                 host_executor: Callable, session: dict,
                 runtime_directory: str = "",
                 persist_callback: Callable = None) -> dict:
    """Stop an E2E session in the frozen order:

    owned containers → owned firewall → owned 8 networks → owned
    runtime files → residue verification.

    - Journal-ID mismatch → the container is NOT deleted (diagnostic).
    - Foreign resources are never touched.
    - A single failure never aborts the remaining owned cleanup.
    - Idempotent: a second stop on the same journal is a no-op.
    """
    actions: list = []
    diagnostics: list = []
    removed_by_journal: set = set()

    # 1. owned containers (journal IDs; verify before remove)
    for service, cid in list(session.get("e2e_container_ids", {})
                             .items()):
        name = "mergepilot-isolated-%s-1" % service
        live = _container_id(docker_executor, name)
        if live and cid and live != cid:
            diagnostics.append("CONTAINER_ID_MISMATCH:%s" % service)
            continue
        try:
            docker_executor(["rm", "-f", cid], check=False)
            actions.append("container:%s" % service)
            removed_by_journal.add(service)
            session["e2e_container_ids"].pop(service, None)
        except Exception as exc:
            diagnostics.append("CONTAINER_RM_FAILED:%s(%s)"
                               % (service, type(exc).__name__))
    for service in reversed(list(session.get("e2e_started", []))):
        if service in removed_by_journal:
            continue  # already removed (and actioned) by journal ID
        name = "mergepilot-isolated-%s-1" % service
        try:
            docker_executor(["rm", "-f", name], check=False)
            actions.append("container:%s" % service)
        except Exception as exc:
            diagnostics.append("CONTAINER_RM_FAILED:%s(%s)"
                               % (service, type(exc).__name__))
    session["e2e_started"] = []
    if persist_callback:
        persist_callback(session)

    # 2. owned firewall (journaled SID only)
    if session.get("firewall_sid"):
        sid = session["firewall_sid"]
        edges = e2f._build_all_edges("172.22.0.2")
        plan = e2f.build_firewall_plan(
            sid, edges=edges, own_subnets=e2f.R4_ALL_SUBNETS)
        try:
            ex.teardown_firewall(plan, host_executor=host_executor)
            actions.append("firewall:%s" % sid)
            session.pop("firewall_sid", None)
            session.pop("firewall_state", None)
        except Exception as exc:
            diagnostics.append("FIREWALL_TEARDOWN_FAILED:(%s)"
                               % type(exc).__name__)
        if persist_callback:
            persist_callback(session)

    # 3. owned networks (journal only; includes the default-mode
    # networks the github-e2e start path created and journaled)
    for net, nid in list(session.get("default_network_ids",
                                     {}).items()):
        try:
            docker_executor(["network", "rm", nid], check=False)
            actions.append("network:%s" % net)
        except Exception as exc:
            diagnostics.append("NETWORK_RM_FAILED:%s(%s)"
                               % (net, type(exc).__name__))
    try:
        removed = ep.remove_e2e_networks(
            docker_executor,
            journal=session.get("e2e_network_ids", {}))
        actions.extend("network:%s" % n for n in removed)
    except Exception as exc:
        diagnostics.append("NETWORK_RM_FAILED:(%s)" % type(exc).__name__)
        removed = []
    if persist_callback:
        persist_callback(session)

    # 4. owned runtime files (journal ownership only)
    if session.get("e2e_runtime_journal"):
        try:
            rs.remove_runtime_files(
                directory=runtime_directory,
                journal=session.get("e2e_runtime_journal", {}))
            actions.append("runtime_files")
        except rs.RuntimeSpecError as exc:
            diagnostics.append("RUNTIME_REMOVE_REFUSED:%s" % exc.code)
        except Exception as exc:
            diagnostics.append("RUNTIME_REMOVE_FAILED:(%s)"
                               % type(exc).__name__)
    if persist_callback:
        persist_callback(session)

    # 5. residue verification (stable report; never a guess-delete)
    residue = _scan_residue(docker_executor, host_executor,
                            runtime_directory)
    return {"actions": actions, "residue": residue,
            "diagnostics": diagnostics}


# ── §14: cleanup (scan + report; ownership never guessed) ────────────────

def run_e2e_cleanup(*, docker_executor: Callable,
                    host_executor: Callable,
                    runtime_directory: str = "") -> dict:
    """E2E residue scan: 11 containers, 8 networks, firewall chains,
    runtime env files, route-probe containers. Reports everything;
    deletes NOTHING (unowned resources are reported, never guessed).
    The caller maps a non-empty residue list to a stable non-zero
    exit code; the DEFAULT cleanup behavior is unchanged.
    """
    residue = _scan_residue(docker_executor, host_executor,
                            runtime_directory)
    report = {
        "scanned": {
            "containers": len(_DAG_ORDER),
            "networks": len(e2f.E2E_NETWORKS),
            "firewall": True,
            "runtime_files": True,
            "route_probes": True,
        },
        "residue": residue,
    }
    return report


def _scan_residue(docker_executor, host_executor,
                  runtime_directory: str) -> list:
    residue = []
    for service in _DAG_ORDER:
        name = "mergepilot-isolated-%s-1" % service
        if _container_id(docker_executor, name):
            residue.append("container:%s" % service)
    for net in e2f.E2E_NETWORKS:
        full = e2f.E2E_NETWORK_PREFIX + net
        try:
            cp = docker_executor(
                ["network", "inspect", full, "--format", "{{.Id}}"],
                check=False)
            present = cp.returncode == 0 and bool(
                (cp.stdout or b"").decode().strip())
        except Exception:
            present = False
        if present:
            residue.append("network:%s" % full)
    # firewall chains: read-only iptables-save scan (E2E prefix only)
    try:
        cp = host_executor(["iptables-save"], check=False)
        text = (cp.stdout or b"").decode("utf-8", "replace")
        if ":mp-e2e-" in text or "-E2E-" in text:
            residue.append("firewall:e2e-chains")
    except Exception:
        residue.append("firewall:scan-failed")
    # runtime files (spec env-file basenames present on disk)
    if runtime_directory:
        directory = Path(runtime_directory)
        for spec in rs.SERVICE_RUNTIME_SPECS.values():
            if (directory / spec["env_file"]).exists():
                residue.append("runtime_file:%s" % spec["env_file"])
    # route-probe one-shot containers (probe naming contract)
    for probe in ("probe-tuwunel", "probe-winproxy", "probe-bridge",
                  "probe-gateway", "probe-reporter", "probe-controller"):
        if _container_id(docker_executor, "mp-e2e-%s" % probe):
            residue.append("route_probe:%s" % probe)
    return residue


# ── internal helpers ──────────────────────────────────────────────────────

def _service_env_file(service: str) -> str:
    """Delegate to the S1 authoritative runtime spec (basename)."""
    spec = rs.SERVICE_RUNTIME_SPECS.get(service)
    return spec["env_file"] if spec else ""


def _absolute_env_file(runtime_directory: str, service: str) -> str:
    """Absolute path of the created runtime env file (the argv
    --env-file must point at the REAL generated file, never a bare
    basename)."""
    if not runtime_directory:
        return _service_env_file(service)
    return str(Path(runtime_directory)
               / _service_env_file(service))


def _sid_from_session(session: dict) -> str:
    """Derive a stable 8-hex SID from the session run_id."""
    import hashlib
    run_id = session.get("run_id", "default")
    return hashlib.sha256(
        run_id.encode()).hexdigest()[:8]


def _rollback_runtime(session, diagnostics: list = None) -> None:
    """Best-effort runtime file cleanup via the S1 API. Errors become
    safe diagnostics and NEVER replace the primary error."""
    journal = session.get("e2e_runtime_journal", {})
    if not journal:
        return
    try:
        rs.remove_runtime_files(directory="", journal=journal)
    except rs.RuntimeSpecError as exc:
        if diagnostics is not None:
            diagnostics.append("ROLLBACK_RUNTIME_REFUSED:%s" % exc.code)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(
                "ROLLBACK_RUNTIME_FAILED:(%s)" % type(exc).__name__)


def _rollback_networks(docker_executor, session,
                       diagnostics: list = None) -> None:
    try:
        ep.remove_e2e_networks(
            docker_executor,
            journal=session.get("e2e_network_ids", {}))
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.append(
                "ROLLBACK_NETWORK_FAILED:(%s)" % type(exc).__name__)


def _rollback_all(docker_executor, session,
                  host_executor=None, runtime_directory="") -> list:
    """Full reverse-order rollback of OWNED resources only:
    containers → firewall → networks → runtime files. Idempotent;
    every failure becomes a safe diagnostic. Returns diagnostics."""
    diagnostics: list = []
    # Containers
    for service, cid in list(session.get(
            "e2e_container_ids", {}).items()):
        try:
            docker_executor(["rm", "-f", cid], check=False)
        except Exception as exc:
            diagnostics.append("ROLLBACK_CONTAINER_RM_FAILED:%s(%s)"
                               % (service, type(exc).__name__))
        session["e2e_container_ids"].pop(service, None)
    for service in reversed(list(session.get("e2e_started", []))):
        try:
            docker_executor(
                ["rm", "-f",
                 "mergepilot-isolated-%s-1" % service],
                check=False)
        except Exception as exc:
            diagnostics.append("ROLLBACK_CONTAINER_RM_FAILED:%s(%s)"
                               % (service, type(exc).__name__))
    session["e2e_started"] = []

    # Firewall
    if host_executor and session.get("firewall_sid"):
        sid = session["firewall_sid"]
        edges = e2f._build_all_edges("172.22.0.2")
        plan = e2f.build_firewall_plan(
            sid, edges=edges, own_subnets=e2f.R4_ALL_SUBNETS)
        try:
            ex.teardown_firewall(plan, host_executor=host_executor)
        except Exception as exc:
            diagnostics.append("ROLLBACK_FIREWALL_FAILED:(%s)"
                               % type(exc).__name__)
        session.pop("firewall_sid", None)
        session.pop("firewall_state", None)

    # Networks
    _rollback_networks(docker_executor, session, diagnostics)

    # Runtime files (S1 §10: unified remove API)
    _rollback_runtime(session, diagnostics)
    return diagnostics


# ── §13: E2E-aware Status (read-only, sanitized) ─────────────────────────

def run_e2e_status(*, docker_executor: Callable,
                   session: dict,
                   mcp_health: Callable = None,
                   receipt_validator: Callable = None,
                   gateway_bearer: str = "") -> dict:
    """§7/§13: read-only sanitized status for 11 E2E services.

    Reports: expected, journal ID match, exists, running, semantic
    health (bridge/gateway via the MCP protocol adapter), network
    attachments, firewall state, runtime ownership, receipt verified,
    session stage.

    Never reports: env values, PAT/PEM/token, actual hashes, DSN,
    Authorization, restore blob, receipt/config bodies.
    """
    services = _DAG_ORDER
    result = {}
    journal_nets = session.get("e2e_network_ids", {})
    for service in services:
        name = "mergepilot-isolated-%s-1" % service
        entry = {"expected": True}

        try:
            cp = docker_executor(
                ["inspect", name, "--format",
                 "{{.Id}} {{.State.Status}}"],
                check=False)
            if cp.returncode == 0:
                parts = (cp.stdout or b"").decode().strip().split()
                live_id = parts[0] if parts else ""
                live_state = parts[1] if len(parts) > 1 else ""
                entry["exists"] = bool(live_id)
                entry["running"] = live_state == "running"
                recorded = session.get(
                    "e2e_container_ids", {}).get(service, "")
                entry["id_match"] = (recorded == live_id
                                     if recorded else None)
            else:
                entry["exists"] = False
                entry["running"] = False
                entry["id_match"] = None
        except Exception:
            entry["exists"] = False
            entry["running"] = False
            entry["id_match"] = None

        # network attachment match (owned networks the service joins)
        attached = 0
        try:
            cp = docker_executor(
                ["inspect", name, "--format",
                 "{{range $k, $v := .NetworkSettings.Networks}}"
                 "{{$k}} {{end}}"], check=False)
            if cp.returncode == 0:
                live_nets = (cp.stdout or b"").decode().split()
                attached = sum(
                    1 for n in journal_nets if n in live_nets)
        except Exception:
            attached = 0
        expected_nets = len(
            ep.E2E_CONTAINER_ATTACHMENTS.get(service, ()))
        entry["network_attachments_match"] = (
            attached == expected_nets if expected_nets else None)

        # semantic health — MCP protocol adapter (bridge subset,
        # gateway frozen-exact); never a container-Running shortcut.
        if service in ("mcp-bridge", "policy-gateway") \
                and entry["running"]:
            url = (_LOOPBACK_BRIDGE_SSE_URL if service == "mcp-bridge"
                   else _LOOPBACK_GATEWAY_SSE_URL)
            check = mcp_health or (
                _bridge_mcp_check(docker_executor)
                if service == "mcp-bridge"
                else _gateway_mcp_check(docker_executor,
                                        gateway_bearer))
            try:
                health = check(url)
            except Exception:
                health = {"healthy": False,
                          "error": "GATEWAY_UPSTREAM_UNREACHABLE"}
            entry["semantic_health"] = bool(health.get("healthy"))
            entry["health_code"] = health.get("error")
        else:
            entry["semantic_health"] = None
            entry["health_code"] = None

        result[service] = entry

    # Firewall state (sanitized; no restore blob / rule bodies)
    result["_firewall"] = {
        "installed": bool(session.get("firewall_sid")),
        "state": session.get("firewall_state", "none"),
    }
    # Runtime ownership (count + ownership kind only; no paths' env)
    runtime_journal = session.get("e2e_runtime_journal", {})
    result["_runtime_files"] = {
        "count": len(runtime_journal),
        "ownership": sorted({str(info.get("ownership", "unknown"))
                             for info in runtime_journal.values()
                             if isinstance(info, dict)})
        or ["none"],
    }
    # Receipt verified (boolean only; never the receipt body)
    if receipt_validator is not None and session.get(
            "hiclaw_receipt_path"):
        try:
            rr = receipt_validator(session["hiclaw_receipt_path"])
            result["_receipt_verified"] = bool(
                rr.get("verified", False))
        except Exception:
            result["_receipt_verified"] = False
    else:
        result["_receipt_verified"] = bool(
            session.get("receipt_verified", False))
    result["_stage"] = session.get("e2e_stage", "unknown")
    result["_prerequisite_verified"] = session.get(
        "prerequisite_summary", {}).get("verified", False)
    return result


__all__ = [
    "E2ELifecycleError", "AGENT_ROLES", "load_e2e_prerequisite_config",
    "run_prerequisite_gate", "run_e2e_start", "run_e2e_stop",
    "run_e2e_status", "run_e2e_cleanup", "production_service_health",
    "fetch_firewall_scan_text", "fetch_existing_network_cidrs",
    "fetch_docker_gw_priority_supported",
    "fetch_matrix_joined_mxids", "_DAG_ORDER",
]
