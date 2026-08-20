"""M8-GH-4B1: GitHub E2E foundation — default-off planning layer.

Implements the R4-frozen contracts as PURE, testable structures:

- the strict E2E controller env schema (15 keys; ``M4F_ALLOWED_SENDERS``
  carries Matrix LOCALPARTS only — full MXIDs are rejected);
- the B1 network slice (``ctrl-egress``) expressed inside the extensible
  R4 eight-network table with static addresses;
- the session-owned firewall rule model (``MP-EG-<sid>`` / ``MP-IN-<sid>``
  chains, ownership comments, per-edge bidirectional conntrack rules,
  per-subnet NEW default-deny, INPUT LOCAL bypass deny) rendered as an
  atomic ``iptables-restore`` blob plus an exact-match teardown;
- the Matrix five-identity membership preflight (transport-injected);
- the B1 activation gate: a REAL ``start --github-e2e`` fails closed with
  ``GITHUB_E2E_COMPONENTS_INCOMPLETE`` until B2 (reporter token provider)
  and B3 (MCP bridge) land.

Nothing in this module touches real iptables, WSL, Matrix or GitHub —
executors are injected by the CLI and exercised only with fakes in tests.
Frozen identities below are the R2/R3 live-verified facts (full MXIDs are
used ONLY for membership preflight and audit display, never as env keys).
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

# ── frozen identities (R2/R3 实证) ────────────────────────────────────────────

E2E_MATRIX_SERVER_NAME = "matrix-local.hiclaw.io:18080"
E2E_PLATFORM_SENDER_LOCALPARTS = ("manager", "reviewer", "fixer", "verifier")
E2E_CONTROLLER_LOCALPART = "m8gh4-controller"

E2E_PLATFORM_MXIDS = tuple(
    "@%s:%s" % (lp, E2E_MATRIX_SERVER_NAME)
    for lp in E2E_PLATFORM_SENDER_LOCALPARTS)
E2E_CONTROLLER_MXID = "@%s:%s" % (E2E_CONTROLLER_LOCALPART,
                                  E2E_MATRIX_SERVER_NAME)
#: the five identities the membership preflight requires in the test room
E2E_EXPECTED_ROOM_MEMBERS = E2E_PLATFORM_MXIDS + (E2E_CONTROLLER_MXID,)

#: Activation gate — the components still not shipped. B2 (reporter token
#: provider) merged; only the B3 MCP bridge remains.
E2E_PENDING_COMPONENTS = ("mcp_bridge(B3)",)


class E2EConfigError(Exception):
    """Fail-closed foundation error; ``detail`` never echoes secret values."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


# ── §3 strict E2E controller env schema ───────────────────────────────────────

E2E_CONTROLLER_ENV_KEYS = frozenset((
    "GITHUB_INGRESS_ENABLED",
    "GITHUB_ROOM_MAP_PATH",
    "GITHUB_POLICY_PATH",
    "GITHUB_DELIVERY_LEASE_SECONDS",
    "GITHUB_DELIVERY_MAX_ATTEMPTS",
    "MATRIX_HS",
    "MATRIX_SERVER_NAME",
    "MATRIX_USER",
    "CONTROLLER_CONSUMER_NAME",
    "M4F_ALLOWED_ROOMS",
    "M4F_ALLOWED_SENDERS",
    "M4F_RUN_PREFIX",
    "RESERVED_RUN_PREFIXES",
    "GATEWAY_URL",
    "COORDINATOR_TOKEN",
))

E2E_INGRESS_ENV_FILE = "github_ingress.env"

_LOCALPART_RE = re.compile(r"^[A-Za-z0-9._=/+\-]+$")
_ROOM_ID_RE = re.compile(r"^![^:\s]+:[A-Za-z0-9._\-]+(:\d+)?$")
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+(:\d+)?$")


def _bad(key: str, reason: str):
    raise E2EConfigError("CONFIG_INVALID", "E2E env key %s: %s" % (key, reason))


def _validate_int(key: str, value: str, low: int, high: int) -> int:
    if not value.isdigit() or not (low <= int(value) <= high):
        _bad(key, "must be an integer in [%d, %d]" % (low, high))
    return int(value)


def _validate_http_url(key: str, value: str) -> None:
    if not re.fullmatch(r"http://[A-Za-z0-9._\-]+(:\d{1,5})?/?",
                        value or ""):
        _bad(key, "must be http://host[:port] without userinfo or path")


def _validate_ro_path(key: str, value: str) -> None:
    if not value.startswith("/run/mergepilot/") or not value.endswith(".yaml"):
        _bad(key, "must be an absolute /run/mergepilot/*.yaml container path")
    if ".." in value or "\r" in value or "\n" in value or "\0" in value:
        _bad(key, "contains a rejected traversal/control sequence")


def validate_e2e_controller_env(mapping) -> dict:
    """Strict validation of the 15-key E2E controller env schema.

    Unknown keys, missing keys and wrong value shapes raise
    ``E2EConfigError`` naming ONLY the key and the reason — values
    (COORDINATOR_TOKEN is a secret) never appear in errors. The sender
    contract is LOCALPART-only: a full MXID (contains ``@`` or ``:``) is
    rejected explicitly.
    """
    if not isinstance(mapping, dict):
        raise E2EConfigError("CONFIG_INVALID", "E2E env must be a mapping")
    unknown = sorted(set(mapping) - E2E_CONTROLLER_ENV_KEYS)
    if unknown:
        raise E2EConfigError(
            "CONFIG_INVALID", "unknown E2E env key(s): %s" % unknown)
    missing = sorted(E2E_CONTROLLER_ENV_KEYS - set(mapping))
    if missing:
        raise E2EConfigError(
            "CONFIG_INVALID", "missing E2E env key(s): %s" % missing)

    if mapping["GITHUB_INGRESS_ENABLED"] != "1":
        _bad("GITHUB_INGRESS_ENABLED", "must be exactly '1' in E2E mode")
    _validate_ro_path("GITHUB_ROOM_MAP_PATH", mapping["GITHUB_ROOM_MAP_PATH"])
    _validate_ro_path("GITHUB_POLICY_PATH", mapping["GITHUB_POLICY_PATH"])
    _validate_int("GITHUB_DELIVERY_LEASE_SECONDS",
                  mapping["GITHUB_DELIVERY_LEASE_SECONDS"], 1, 600)
    _validate_int("GITHUB_DELIVERY_MAX_ATTEMPTS",
                  mapping["GITHUB_DELIVERY_MAX_ATTEMPTS"], 1, 20)
    _validate_http_url("MATRIX_HS", mapping["MATRIX_HS"])
    if mapping["MATRIX_SERVER_NAME"] != E2E_MATRIX_SERVER_NAME:
        _bad("MATRIX_SERVER_NAME", "does not match the frozen E2E homeserver")
    if mapping["MATRIX_USER"] != E2E_CONTROLLER_LOCALPART:
        _bad("MATRIX_USER", "must be the frozen controller localpart")
    if mapping["CONTROLLER_CONSUMER_NAME"] != E2E_CONTROLLER_LOCALPART:
        _bad("CONTROLLER_CONSUMER_NAME", "must be the frozen consumer name")

    rooms = [r for r in mapping["M4F_ALLOWED_ROOMS"].split(",") if r]
    if len(rooms) != 1 or not _ROOM_ID_RE.fullmatch(rooms[0]):
        _bad("M4F_ALLOWED_ROOMS", "must be exactly one !room:server id")
    if not rooms[0].endswith(":" + mapping["MATRIX_SERVER_NAME"]):
        _bad("M4F_ALLOWED_ROOMS", "room server part != MATRIX_SERVER_NAME")

    senders = [s for s in mapping["M4F_ALLOWED_SENDERS"].split(",") if s]
    for s in senders:
        if "@" in s or ":" in s:
            _bad("M4F_ALLOWED_SENDERS",
                 "LOCALPARTS only — full MXID values are rejected "
                 "(the homeserver is enforced separately)")
        if not _LOCALPART_RE.fullmatch(s):
            _bad("M4F_ALLOWED_SENDERS", "invalid localpart charset")
    if sorted(senders) != sorted(E2E_PLATFORM_SENDER_LOCALPARTS):
        _bad("M4F_ALLOWED_SENDERS",
             "must be exactly the four platform agent localparts")

    if mapping["M4F_RUN_PREFIX"] != "gh-":
        _bad("M4F_RUN_PREFIX", "must be 'gh-' (matches the drain derivation)")
    if mapping["RESERVED_RUN_PREFIXES"] != "":
        _bad("RESERVED_RUN_PREFIXES", "must be empty (never exclude gh- runs)")
    _validate_http_url("GATEWAY_URL", mapping["GATEWAY_URL"])
    tok = mapping["COORDINATOR_TOKEN"]
    if not tok or re.search(r"[\s\"'\\\r\n\0]", tok):
        _bad("COORDINATOR_TOKEN", "non-empty, no whitespace/quote/backslash")
    return dict(mapping)


class GithubE2eSecretFile:
    """Secret env-file transport for the E2E controller ingress env
    (fixed name ``github_ingress.env``; same guarantees as the planner's
    SecretFile classes: 0600 where enforceable, refuses to overwrite,
    idempotent delete, values never logged)."""

    _NAME = E2E_INGRESS_ENV_FILE

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._path = self._dir / self._NAME

    @property
    def path(self) -> Path:
        return self._path

    def write(self, mapping: dict) -> None:
        validate_e2e_controller_env(mapping)
        if self._path.exists():
            raise E2EConfigError("SECRET_FILE_EXISTS",
                                 "refusing to overwrite an existing E2E "
                                 "secret env file")
        self._dir.mkdir(parents=True, exist_ok=True)
        lines = ["%s=%s\n" % (k, mapping[k])
                 for k in sorted(E2E_CONTROLLER_ENV_KEYS)]
        self._path.write_text("".join(lines), encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # Windows: recorded honestly in capability, not enforced

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()

    def exists(self) -> bool:
        return self._path.exists()


# ── M8-GH-4B2 reporter env contract (production E2E only) ───────────────────

E2E_REPORTER_ENV_KEYS = frozenset((
    "GITHUB_PUBLISHER_DSN",
    "GITHUB_API_BASE",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_REPOSITORY_ID",
    "GITHUB_PRIVATE_KEY_PATH",
    "GH_REPORTER_POLL_SECONDS",
    "GH_REPORTER_LEASE_SECONDS",
    "GH_REPORTER_MAX_ATTEMPTS",
))
E2E_REPORTER_ENV_FILE = "gh_reporter.env"
E2E_REPORTER_KEY_CONTAINER_PATH = "/run/secrets/github-app-private-key.pem"
E2E_REPORTER_API_BASE = "https://api.github.com"


def validate_e2e_reporter_env(mapping) -> dict:
    """Strict validation of the 9-key E2E reporter env schema (§6):
    unknown/missing/blank keys fail closed; numeric ranges enforced; the
    API base must be exactly the production endpoint (no implicit fake
    fallback); the PEM path must be the frozen single-file container path.
    Errors name only keys and reasons — never the DSN value."""
    if not isinstance(mapping, dict):
        raise E2EConfigError("CONFIG_INVALID", "reporter env must be a mapping")
    unknown = sorted(set(mapping) - E2E_REPORTER_ENV_KEYS)
    if unknown:
        raise E2EConfigError("CONFIG_INVALID",
                             "unknown reporter env key(s): %s" % unknown)
    missing = sorted(E2E_REPORTER_ENV_KEYS - set(mapping))
    if missing:
        raise E2EConfigError("CONFIG_INVALID",
                             "missing reporter env key(s): %s" % missing)
    for key in sorted(E2E_REPORTER_ENV_KEYS):
        value = mapping[key]
        if not isinstance(value, str) or not value.strip():
            if key != "":  # (no key may be blank)
                _bad(key, "must be a non-empty string")
    dsn = mapping["GITHUB_PUBLISHER_DSN"]
    if not dsn.startswith("postgresql://") or "connect_timeout=" not in dsn:
        _bad("GITHUB_PUBLISHER_DSN",
             "must be a postgresql:// DSN with a forced connect_timeout")
    if mapping["GITHUB_API_BASE"] != E2E_REPORTER_API_BASE:
        _bad("GITHUB_API_BASE",
             "E2E reporter must use exactly %s (no implicit fake fallback)"
             % E2E_REPORTER_API_BASE)
    for key in ("GITHUB_APP_ID", "GITHUB_INSTALLATION_ID",
                "GITHUB_REPOSITORY_ID"):
        if not mapping[key].isdigit() or int(mapping[key]) <= 0:
            _bad(key, "must be a positive numeric string")
    if mapping["GITHUB_PRIVATE_KEY_PATH"] != E2E_REPORTER_KEY_CONTAINER_PATH:
        _bad("GITHUB_PRIVATE_KEY_PATH",
             "must be the frozen single-file container path %s"
             % E2E_REPORTER_KEY_CONTAINER_PATH)
    _validate_int("GH_REPORTER_POLL_SECONDS",
                  mapping["GH_REPORTER_POLL_SECONDS"], 1, 3600)
    _validate_int("GH_REPORTER_LEASE_SECONDS",
                  mapping["GH_REPORTER_LEASE_SECONDS"], 30, 600)
    _validate_int("GH_REPORTER_MAX_ATTEMPTS",
                  mapping["GH_REPORTER_MAX_ATTEMPTS"], 1, 50)
    return dict(mapping)


class GithubReporterE2eSecretFile:
    """E2E-mode gh_reporter.env transport (same file NAME as the fake-stack
    one; the E2E key set is the strict 9-key schema above)."""

    _NAME = E2E_REPORTER_ENV_FILE

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._path = self._dir / self._NAME

    @property
    def path(self) -> Path:
        return self._path

    def write(self, mapping: dict) -> None:
        validate_e2e_reporter_env(mapping)
        if self._path.exists():
            raise E2EConfigError("SECRET_FILE_EXISTS",
                                 "refusing to overwrite an existing reporter "
                                 "E2E secret env file")
        self._dir.mkdir(parents=True, exist_ok=True)
        lines = ["%s=%s" % (k, mapping[k]) + chr(10)
                 for k in sorted(E2E_REPORTER_ENV_KEYS)]
        self._path.write_text("".join(lines), encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()

    def exists(self) -> bool:
        return self._path.exists()


# ── room-map / fixture-policy 1:1 (strict line parse; mirrors the
#    github_drain.parse_room_map shape contract) ─────────────────────────────

def parse_room_map_repos(text: str) -> dict:
    """Strict ``repos: / repo: / room_id:`` parse; any other shape,
    duplicate repo, missing room_id or a non-!room:server id raises
    ROOM_MAP_INVALID (line number named; room ids are not secrets)."""
    repos: dict = {}
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "repos:":
        raise E2EConfigError("ROOM_MAP_INVALID", "line 1 must be 'repos:'")
    current = None
    for lineno, raw in enumerate(lines[1:], start=2):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("    ") and current is not None:
            m = re.fullmatch(r'    room_id: "(.+)"', line)
            if not m:
                raise E2EConfigError(
                    "ROOM_MAP_INVALID",
                    "line %d: expected quoted room_id" % lineno)
            room_id = m.group(1)
            if not _ROOM_ID_RE.fullmatch(room_id):
                raise E2EConfigError(
                    "ROOM_MAP_INVALID", "line %d: invalid room id" % lineno)
            if repos.get(current) is not None:
                raise E2EConfigError(
                    "ROOM_MAP_INVALID", "line %d: duplicate room_id" % lineno)
            repos[current] = room_id
            continue
        if line.startswith("  ") and line.endswith(":"):
            repo = line.strip().rstrip(":")
            if len(repo) >= 2 and repo[0] == '"' and repo[-1] == '"':
                repo = repo[1:-1]
            if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
                raise E2EConfigError(
                    "ROOM_MAP_INVALID", "line %d: invalid repo key" % lineno)
            if repo in repos:
                raise E2EConfigError(
                    "ROOM_MAP_INVALID", "line %d: duplicate repo" % lineno)
            repos[repo] = None
            current = repo
            continue
        raise E2EConfigError(
            "ROOM_MAP_INVALID", "line %d: unexpected shape" % lineno)
    missing = sorted(r for r, v in repos.items() if v is None)
    if missing:
        raise E2EConfigError(
            "ROOM_MAP_INVALID", "repo(s) without room_id: %s" % missing)
    return repos


def validate_room_map_policy_pair(room_map_text: str, allowlist) -> None:
    """Exact 1:1 between the runtime room-map repos and the fixture policy
    allowlist (policy-only or map-only entries are both fatal)."""
    repos = parse_room_map_repos(room_map_text)
    allowed = sorted(set(allowlist))
    if sorted(repos) != allowed:
        raise E2EConfigError(
            "ROOM_MAP_MISMATCH",
            "room-map repos != policy allowlist (map-only=%s policy-only=%s)"
            % (sorted(set(repos) - set(allowed)),
               sorted(set(allowed) - set(repos))))


# ── §4 network slice (R4 eight-network table; B1 activates ctrl-egress) ─────

E2E_NETWORKS = {
    # name: (subnet, gateway, static assignments {container: ip})
    "ctrl-egress":     ("172.31.0.0/28",   "172.31.0.1",
                        {"controller": "172.31.0.2"}),
    "gw-egress":       ("172.31.0.16/28",  "172.31.0.17",
                        {"policy-gateway": "172.31.0.18"}),
    "mcp-bridge-net":  ("172.31.0.32/28",  "172.31.0.33",
                        {"mcp-bridge": "172.31.0.34"}),
    "rpt-egress":      ("172.31.0.64/28",  "172.31.0.65",
                        {"gh-reporter": "172.31.0.66"}),
    "br-up":           ("172.31.0.80/28",  "172.31.0.81",
                        {"mcp-bridge": "172.31.0.82"}),
    "pxr":             ("172.31.0.96/28",  "172.31.0.97",
                        {"gh-proxy-r": "172.31.0.98"}),
    "pxb":             ("172.31.0.112/28", "172.31.0.113",
                        {"gh-proxy-b": "172.31.0.114"}),
    "winpx":           ("172.31.0.128/28", "172.31.0.129",
                        {"gh-proxy-r": "172.31.0.130",
                         "gh-proxy-b": "172.31.0.131"}),
}
B1_ACTIVE_NETWORKS = ("ctrl-egress",)

#: R4-frozen external targets (the tuwunel IP is MEASURED at runtime; the
#: value here is the current live observation used only for plan previews).
E2E_TUWUNEL_DEFAULT_IP = "172.22.0.2"
E2E_TUWUNEL_PORT = 6167
E2E_ROUTE_GATE_EXPECTED_SRC = "172.31.0.2"

E2E_NETWORK_PREFIX = "mp-e2e-"


def plan_e2e_network_create(name: str) -> list:
    if name not in E2E_NETWORKS:
        raise E2EConfigError("CONFIG_INVALID", "unknown E2E network %r" % name)
    subnet = E2E_NETWORKS[name][0]
    return ["network", "create", "--driver", "bridge", "--subnet", subnet,
            E2E_NETWORK_PREFIX + name]


def plan_controller_e2e_create(*, image_ref: str, container: str,
                               room_map_host: str, policy_host: str) -> list:
    """docker create argv for the E2E controller slice.

    ``--network none`` at creation + the two ``network connect`` calls below
    make the default gateway a DECLARED property (--gw-priority), never an
    artifact of attachment order. The room-map/policy ride as SINGLE-FILE
    read-only mounts (never the secrets directory)."""
    for label, path in (("room-map", room_map_host), ("policy", policy_host)):
        if not path or ".." in path or "\r" in path or "\n" in path:
            raise E2EConfigError("CONFIG_INVALID",
                                 "%s host path invalid" % label)
    return ["create", "--name", container, "--network", "none",
            "-v", "%s:/run/mergepilot/room-map.yaml:ro" % room_map_host,
            "-v", "%s:/run/mergepilot/policy-fixture.yaml:ro" % policy_host,
            "--pull", "never", "--restart", "no", image_ref]


def plan_controller_e2e_connects(*, container: str,
                                 isolated_network: str) -> list:
    """The two attachment argvs: ctrl-egress with gw-priority 100 (the ONLY
    default route — the egress interface), then the isolated network at 0."""
    ip = E2E_NETWORKS["ctrl-egress"][2]["controller"]
    return [
        ["network", "connect", "--ip", ip, "--gw-priority", "100",
         E2E_NETWORK_PREFIX + "ctrl-egress", container],
        ["network", "connect", "--gw-priority", "0", isolated_network,
         container],
    ]


def plan_route_gate_argv(*, container: str, dst_ip: str,
                         dst_port: int) -> list:
    """Pure-python route gate: TCP connect from the container and read the
    kernel's source-address choice (getsockname IS the routing decision;
    no iproute2 needed in the slim image). The caller compares the printed
    source against E2E_ROUTE_GATE_EXPECTED_SRC — mismatch = ROUTE_GATE_FAILED.
    No secret rides this argv."""
    code = ("import socket;"
            "s=socket.create_connection(('%s',%d),timeout=5);"
            "print(s.getsockname()[0]);s.close()"
            % (dst_ip, int(dst_port)))
    return ["exec", container, "python", "-c", code]


# ── §5 session-owned firewall model ──────────────────────────────────────────

FIREWALL_EGRESS_CHAIN_PREFIX = "MP-EG-"
FIREWALL_INPUT_CHAIN_PREFIX = "MP-IN-"
FIREWALL_COMMENT_PREFIX = "mp-e2e:"
DOCKER_USER_CHAIN = "DOCKER-USER"
INPUT_CHAIN = "INPUT"

_CIDR_RE = re.compile(r"^[0-9a-fA-F.:]+(/\d{1,2})?$")


def _sid_ok(sid: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{8}", sid or ""):
        raise E2EConfigError("CONFIG_INVALID",
                             "sid must be exactly 8 lowercase hex chars")


def _cidr32(addr: str) -> str:
    if not _CIDR_RE.fullmatch(addr or ""):
        raise E2EConfigError("CONFIG_INVALID",
                             "invalid firewall address %r" % addr)
    return addr if "/" in addr else "%s/32" % addr


def _rule(sid: str, chain: str, parts: list, tag: str) -> dict:
    return {"chain": chain, "parts": parts,
            "comment": "%s%s:%s" % (FIREWALL_COMMENT_PREFIX, sid, tag)}


def _render_line(rule: dict) -> str:
    return " ".join(rule["parts"]) + ' -m comment --comment "%s"' \
        % rule["comment"]


def _delete_argv(rule: dict) -> list:
    """Exact-match deletion argv; ``-I CH 1`` becomes ``-D CH <rest>`` (the
    rulenum never appears in a delete)."""
    parts = rule["parts"]
    rest = parts[3:] if parts[0] == "-I" else parts[2:]
    return (["iptables", "-D", rule["chain"]] + rest
            + ["-m", "comment", "--comment", rule["comment"]])


def build_firewall_plan(sid: str, *, edges, own_subnets,
                        input_deny_subnets=None) -> dict:
    """Structured, session-owned firewall plan.

    ``edges``: iterable of ``(src, dst, dport, tag)``. For every edge the
    plan emits a forward NEW,ESTABLISHED rule and the EXACT reverse
    ESTABLISHED rule (src/dst swapped, --sport). Every own subnet then
    gets a terminal NEW default-deny DROP. DOCKER-USER receives one
    precise jump per own subnet plus one /32 jump per edge source OUTSIDE
    the own subnets (e.g. the four HiClaw agent IPs) — inserted at
    position 1, ahead of Docker's own RETURN/ACCEPT entries. The INPUT
    chain receives one precise jump per input-deny subnet into
    MP-IN-<sid>, whose single rule denies container->LOCAL entirely (the
    design has ZERO legitimate container->LOCAL flows; a global
    ESTABLISHED accept would let rule-window connections survive). The
    egress chain ends with RETURN so foreign FORWARD traffic continues
    through Docker's own chains untouched.

    Rendering is an atomic ``iptables-restore --noflush`` blob; teardown
    is the exact-match reverse plus chain deletion.
    """
    _sid_ok(sid)
    edges = [tuple(e) for e in edges]
    for src, dst, port, _etag in edges:
        _cidr32(src)
        _cidr32(dst)
        if not (isinstance(port, int) and 1 <= port <= 65535):
            raise E2EConfigError("CONFIG_INVALID", "invalid port in edges")
    own_subnets = list(own_subnets)
    for sub in own_subnets:
        if "/" not in sub:
            raise E2EConfigError("CONFIG_INVALID",
                                 "own subnet %r must be a CIDR" % sub)
    input_deny = list(input_deny_subnets if input_deny_subnets is not None
                      else own_subnets)
    eg_chain = FIREWALL_EGRESS_CHAIN_PREFIX + sid
    in_chain = FIREWALL_INPUT_CHAIN_PREFIX + sid

    def subnet_tag(sub: str) -> str:
        for name, spec in E2E_NETWORKS.items():
            if spec[0] == sub:
                return name
        return re.sub(r"[^a-z0-9]+", "-", sub.lower()).strip("-") or "unknown"

    rules: list[dict] = []
    own_nets = [ipaddress.ip_network(sub) for sub in own_subnets]

    def _covered(addr: str) -> bool:
        # a bare container IP inside one of our subnets is already steered
        # by that subnet's jump; only OUTSIDE sources (e.g. the four
        # HiClaw agent IPs) get their own precise /32 jump.
        try:
            ip = ipaddress.ip_address(addr.split("/")[0])
        except ValueError:
            return False
        return any(ip in net for net in own_nets)

    jump_sources = [(sub, subnet_tag(sub)) for sub in own_subnets]
    for src, _dst, _port, etag in edges:
        if not _covered(src):
            jump_sources.append((src, "edge-%s" % etag))

    for sub, jtag in jump_sources:
        rules.append(_rule(
            sid, DOCKER_USER_CHAIN,
            ["-I", DOCKER_USER_CHAIN, "1", "-s", sub, "-j", eg_chain],
            "jump:%s" % jtag))
    for src, dst, port, etag in edges:
        rules.append(_rule(
            sid, eg_chain,
            ["-A", eg_chain, "-s", _cidr32(src), "-d", _cidr32(dst),
             "-p", "tcp", "--dport", str(port), "-m", "conntrack",
             "--ctstate", "NEW,ESTABLISHED", "-j", "ACCEPT"],
            "fwd:%s" % etag))
        rules.append(_rule(
            sid, eg_chain,
            ["-A", eg_chain, "-s", _cidr32(dst), "-d", _cidr32(src),
             "-p", "tcp", "--sport", str(port), "-m", "conntrack",
             "--ctstate", "ESTABLISHED", "-j", "ACCEPT"],
            "rev:%s" % etag))
    for sub in own_subnets:
        rules.append(_rule(sid, eg_chain,
                           ["-A", eg_chain, "-s", sub, "-j", "DROP"],
                           "drop:%s" % subnet_tag(sub)))
    rules.append(_rule(sid, eg_chain, ["-A", eg_chain, "-j", "RETURN"], "return"))

    in_rules = [
        _rule(sid, INPUT_CHAIN,
              ["-I", INPUT_CHAIN, "1", "-s", sub, "-j", in_chain],
              "in-jump:%s" % subnet_tag(sub))
        for sub in input_deny]
    in_rules.append(_rule(sid, in_chain, ["-A", in_chain, "-j", "DROP"],
                          "in-deny"))

    blob_lines = ["*filter",
                  ":%s - [0:0]" % eg_chain,
                  ":%s - [0:0]" % in_chain]
    blob_lines += [_render_line(r) for r in rules]
    blob_lines += [_render_line(r) for r in in_rules]
    blob_lines.append("COMMIT")
    blob = "\n".join(blob_lines) + "\n"

    teardown = ([_delete_argv(r) for r in reversed(in_rules)]
                + [["iptables", "-X", in_chain]]
                + [_delete_argv(r) for r in reversed(rules)]
                + [["iptables", "-X", eg_chain]])

    counts = {
        "docker_user_jumps": len(jump_sources),
        "forward_accept": len(edges),
        "reverse_accept": len(edges),
        "subnet_drop": len(own_subnets),
        "return": 1,
        "input_jumps": len(input_deny),
        "input_deny": 1,
    }
    return {
        "sid": sid,
        "egress_chain": eg_chain,
        "input_chain": in_chain,
        "restore_blob": blob,
        "install_argv": [["iptables-restore", "--test"],
                         ["iptables-restore", "--noflush"]],
        "teardown_argv": teardown,
        "counts": counts,
        "comment_tag": "%s%s" % (FIREWALL_COMMENT_PREFIX, sid),
    }


def parse_owned_rules(iptables_save_text: str, sid: str = None) -> dict:
    """Ownership scan over ``iptables-save`` output: rules tagged with our
    comment prefix (optionally filtered to one sid) vs foreign-session
    tags vs our chain declarations present without rules."""
    own, foreign, chains = [], [], []
    for line in (iptables_save_text or "").splitlines():
        m = re.search(r'--comment "mp-e2e:([^:"\s]+)(:([^"]*))?"', line)
        if m:
            if sid is None or m.group(1) == sid:
                own.append(line.strip())
            else:
                foreign.append(line.strip())
            continue
        cm = re.match(r":(MP-(?:EG|IN)-[a-f0-9]{8}) ", line)
        if cm:
            chains.append(cm.group(1))
    return {"own": own, "foreign": foreign, "chains": chains}


def plan_is_installed(iptables_save_text: str, plan: dict) -> bool:
    """Idempotency check: every action line of the blob (non chain-decl)
    already present verbatim in the current iptables-save text."""
    text = iptables_save_text or ""
    for line in plan["restore_blob"].splitlines():
        if line.startswith(("*filter", ":", "COMMIT")):
            continue
        if line.strip() and line.strip() not in text:
            return False
    return True


def firewall_conflict(iptables_save_text: str, plan: dict):
    """Return a conflict code or None: our chain present without our rules
    (OWNERSHIP_UNKNOWN), same-sid rules that differ (PIN_TARGET_DRIFT), or
    a foreign mp-e2e session's rules (PIN_FOREIGN_SESSION)."""
    scan = parse_owned_rules(iptables_save_text, plan["sid"])
    if scan["foreign"]:
        return "PIN_FOREIGN_SESSION"
    if scan["chains"]:
        if not scan["own"]:
            return "OWNERSHIP_UNKNOWN"
        if not plan_is_installed(iptables_save_text, plan):
            return "PIN_TARGET_DRIFT"
    return None


def residue_scan(iptables_save_text: str, sid: str = None) -> list:
    """Cleanup residue: ANY mp-e2e-tagged rule or MP-EG/MP-IN chain left
    (``sid`` optionally splits ours vs a foreign session's leftovers)."""
    scan = parse_owned_rules(iptables_save_text, sid)
    codes = []
    codes += ["FIREWALL_RULE_RESIDUE"] * len(scan["own"])
    codes += ["FIREWALL_FOREIGN_SESSION_RESIDUE"] * len(scan["foreign"])
    codes += ["FIREWALL_CHAIN_RESIDUE:%s" % c for c in scan["chains"]]
    return codes


# ── §6 Matrix membership preflight (transport-injected) ──────────────────────

def verify_membership(joined, expected=E2E_EXPECTED_ROOM_MEMBERS):
    """(ok, sorted(missing)) over full MXIDs; the expected identities are
    frozen public facts, safe to name in errors."""
    joined = set(joined or [])
    missing = sorted(m for m in expected if m not in joined)
    return (not missing, missing)


def membership_gate(joined, expected=E2E_EXPECTED_ROOM_MEMBERS) -> None:
    ok, missing = verify_membership(joined, expected)
    if not ok:
        raise E2EConfigError(
            "MATRIX_MEMBERSHIP_INCOMPLETE",
            "missing from test room: %s (invited != joined; re-check via "
            "joined_members after the agents accept)" % missing)


def fetch_joined_members(homeserver_url: str, room_id: str,
                         access_token: str, transport) -> set:
    """GET /_matrix/client/v3/rooms/<room>/joined_members via the injected
    ``transport(url, headers) -> parsed-json`` callable. The token rides
    ONLY the header argument (never argv/log output of this module)."""
    url = ("%s/_matrix/client/v3/rooms/%s/joined_members"
           % (homeserver_url.rstrip("/"), room_id))
    data = transport(url, {"Authorization": "Bearer %s" % access_token})
    joined = (data or {}).get("joined") or {}
    return set(joined.keys())


def build_reporter_planning() -> dict:
    """B2 reporter planning: a standalone container reusing the gh-webhook
    image with an overridden entrypoint. The PEM rides as a SINGLE-FILE
    read-only mount into the reporter ONLY; the future rpt-egress network
    (and the constrained api.github.com proxy chain) is B3 scope and is
    shown as pending — no executable public egress exists in B2."""
    return {
        "container": "mergepilot-isolated-gh-reporter-1",
        "image": "mergepilot-isolated-gh-webhook:local (entrypoint override)",
        "entrypoint": ["python", "-u", "/app/gh_app/checks_reporter.py"],
        "env_file": E2E_REPORTER_ENV_FILE,
        "env_keys": sorted(E2E_REPORTER_ENV_KEYS),
        "pem_mount": "%s:%s:ro" % ("<host-pem-path>",
                                   E2E_REPORTER_KEY_CONTAINER_PATH),
        "pem_mount_policy": "single-file :ro into gh-reporter ONLY",
        "networks": {
            "current": ["isolated (PG via GITHUB_PUBLISHER_DSN)"],
            "pending_b3": ["rpt-egress (172.31.0.64/28, static .66)",
                           "restricted api.github.com proxy chain"],
        },
        "activation_gate": "GITHUB_E2E_COMPONENTS_INCOMPLETE "
                           "(mcp_bridge(B3) pending; no real egress)",
    }


# ── §2 B1 activation gate ────────────────────────────────────────────────────

def e2e_activation_gate() -> None:
    """The final B1 gate: a REAL `start --github-e2e` must fail closed
    because G3 (reporter token provider) and G4 (MCP bridge) are not
    implemented yet. Remove this gate only after B2/B3 merge."""
    raise E2EConfigError(
        "GITHUB_E2E_COMPONENTS_INCOMPLETE",
        "E2E activation incomplete — pending components: %s"
        % ", ".join(E2E_PENDING_COMPONENTS))


# ── §7 dry-run preview (pure; zero side effects) ─────────────────────────────

def build_b1_dry_run_preview(*, run_id: str, tuwunel_ip: str,
                             room_map_host: str, policy_host: str) -> dict:
    """Everything a future real E2E start WOULD do for the B1 slice, as
    plan data only. Includes the activation-gate marker so no consumer can
    mistake a preview for an executable mode."""
    seed = re.sub(r"[^0-9a-f]", "", (run_id or "").lower())[:8]
    sid = (seed.ljust(8, "0"))[:8]
    subnet = E2E_NETWORKS["ctrl-egress"][0]
    edges = [("172.31.0.2", tuwunel_ip, E2E_TUWUNEL_PORT,
              "controller-to-tuwunel")]
    plan = build_firewall_plan(sid, edges=edges, own_subnets=[subnet])
    return {
        "activation_gate": "GITHUB_E2E_COMPONENTS_INCOMPLETE (B1)",
        "networks_create": [plan_e2e_network_create(n)
                            for n in B1_ACTIVE_NETWORKS],
        "controller_create": plan_controller_e2e_create(
            image_ref="<sha256:image-id-from-install>",
            container="mergepilot-isolated-controller-1",
            room_map_host=room_map_host, policy_host=policy_host),
        "controller_connects": plan_controller_e2e_connects(
            container="mergepilot-isolated-controller-1",
            isolated_network="mergepilot-isolated-isolated"),
        "firewall": {"restore_blob": plan["restore_blob"],
                     "teardown_argv": plan["teardown_argv"],
                     "counts": plan["counts"],
                     "sid": sid},
        "route_gate": {
            "argv": plan_route_gate_argv(
                container="mergepilot-isolated-controller-1",
                dst_ip=tuwunel_ip, dst_port=E2E_TUWUNEL_PORT),
            "expected_src": E2E_ROUTE_GATE_EXPECTED_SRC,
            "failure_code": "ROUTE_GATE_FAILED",
        },
        "membership_preflight": {
            "required_members": list(E2E_EXPECTED_ROOM_MEMBERS),
            "failure_code": "MATRIX_MEMBERSHIP_INCOMPLETE",
        },
        # M8-GH-4B2: the standalone checks reporter (G3) planning slice.
        "reporter_planning": build_reporter_planning(),
    }
