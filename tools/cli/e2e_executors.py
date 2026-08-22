"""M8-GH-4B3-W3A: production-capable executors for firewall, route-probe,
and HiClaw receipt validation. All are executor-injected (transport/
docker/host callables); production uses real WslDocker/wsl_exec, tests
use fakes. The component gate (§2) still fires BEFORE any executor.

§3 Firewall executor: install/verify/teardown/residue-scan.
§4 Route probe executor: six one-shot secret-free probes.
§5 HiClaw receipt validator: four-role read-only real-time check.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import e2e_foundation as e2f


# ── §3: session-owned firewall executor ────────────────────────────────────

class FirewallExecutorError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


def _normalize_rule_for_compare(rule_text: str) -> str:
    """Normalize an iptables-save rule line for comparison with a
    restore-blob rule. Handles the -I CH 1 vs -A CH serialization
    difference: '-I CH 1 ...' in the blob installs at position 1 but
    iptables-save emits it as '-A CH ...' (no rulumen). Also
    normalizes bare-IPv4 vs /32 serialization (iptables-save
    appends /32 to host addresses; the restore blob omits it)."""
    text = rule_text.strip()
    # strip ownership comment (compared separately via tag)
    text = re.sub(r'\s*-m comment --comment "[^"]*"', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # normalize '-I CH 1 ' -> '-A CH ' (rulenum is positional, not semantic)
    text = re.sub(r"^-I (\S+) \d+ ", r"-A \1 ", text)
    # bare IPv4 host address -> explicit /32 (both sides normalized)
    text = re.sub(
        r"(?<![\w/.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\w/.])",
        r"\1/32", text)
    # iptables-save serializes '-p tcp --dport N' as '-p tcp -m tcp
    # --dport N' (implicit match module expansion); normalize both
    # sides by dropping the redundant module re-declaration
    text = re.sub(r" -m (tcp|udp|icmp)(?= --dport| --sport)",
                  "", text)
    return text


def install_firewall(plan: dict, *, host_executor: Callable,
                     journal: dict) -> str:
    """§3: install the session-owned firewall plan atomically.

    Sequence: scan → conflict check → idempotent check →
    restore --test → restore --noflush → re-scan verify.

    The restore blob rides STDIN (never argv, never heredoc).
    On commit-then-verify failure: exact-SID rollback.
    Returns 'installed' or 'idempotent'."""
    sid = plan["sid"]
    scan_cp = host_executor(["iptables-save"], check=True)
    current = (scan_cp.stdout or b"").decode("utf-8", "replace")

    # conflict check
    conflict = e2f.firewall_conflict(current, plan)
    if conflict:
        raise FirewallExecutorError(conflict,
                                    "pre-install scan: %s" % conflict)

    # idempotent check
    if e2f.plan_is_installed(current, plan):
        journal["firewall_sid"] = sid
        journal["firewall_teardown"] = plan["teardown_argv"]
        journal["firewall_state"] = "installed-idempotent"
        return "idempotent"

    # test
    blob = plan["restore_blob"].encode("utf-8")
    test_cp = host_executor(["iptables-restore", "--test"],
                            check=True, input_bytes=blob)
    if test_cp.returncode != 0:
        raise FirewallExecutorError("FIREWALL_TEST_FAILED",
                                    "iptables-restore --test rc=%d"
                                    % test_cp.returncode)

    # commit (atomic, --noflush, blob on stdin)
    commit_cp = host_executor(["iptables-restore", "--noflush"],
                              check=True, input_bytes=blob)
    if commit_cp.returncode != 0:
        raise FirewallExecutorError("FIREWALL_COMMIT_FAILED",
                                    "rc=%d" % commit_cp.returncode)

    # verify: re-scan and compare normalized rules
    verify_cp = host_executor(["iptables-save"], check=True)
    after = (verify_cp.stdout or b"").decode("utf-8", "replace")
    if not _verify_installed(after, plan):
        # exact-SID rollback on verify failure; the PRIMARY error is
        # FIREWALL_VERIFY_FAILED — a rollback failure is recorded as a
        # diagnostic cause but NEVER replaces the primary error.
        primary = FirewallExecutorError("FIREWALL_VERIFY_FAILED",
                                        "post-commit scan mismatch")
        try:
            teardown_firewall(plan, host_executor=host_executor)
            # only mark "rolled-back" when teardown confirmed no residue
            journal["firewall_state"] = "verify-failed-rolled-back"
        except FirewallExecutorError as rollback_exc:
            # safe diagnostic: code only, no blob/stderr content
            journal["firewall_rollback_error"] = rollback_exc.code
            journal["firewall_state"] = "verify-failed-rollback-failed"
        raise primary

    journal["firewall_sid"] = sid
    journal["firewall_teardown"] = plan["teardown_argv"]
    journal["firewall_state"] = "installed"
    return "installed"


def _verify_installed(current_text: str, plan: dict) -> bool:
    """Verify every action rule in the blob (normalized) is present in
    the current iptables-save output (normalized)."""
    expected = set()
    for line in plan["restore_blob"].splitlines():
        line = line.strip()
        if not line or line.startswith(("*filter", ":", "COMMIT")):
            continue
        expected.add(_normalize_rule_for_compare(line))
    if not expected:
        return False
    actual = {_normalize_rule_for_compare(l)
              for l in current_text.splitlines() if l.strip()}
    return expected.issubset(actual)


def teardown_firewall(plan: dict, *, host_executor: Callable) -> list:
    """§3: teardown the current SID's rules only (reverse order:
    rules then chains). Returns the argvs executed."""
    executed = []
    for argv in plan["teardown_argv"]:
        host_executor(argv, check=False)
        executed.append(argv)
    # residue scan
    scan_cp = host_executor(["iptables-save"], check=True)
    after = (scan_cp.stdout or b"").decode("utf-8", "replace")
    residue = e2f.parse_owned_rules(after, plan["sid"])
    if residue["own"] or residue["chains"]:
        raise FirewallExecutorError("FIREWALL_TEARDOWN_RESIDUE",
                                    "%d rules, %d chains remain"
                                    % (len(residue["own"]),
                                       len(residue["chains"])))
    return executed


def scan_firewall_residue(*, host_executor: Callable) -> list:
    """§3: scan for ANY mp-e2e rules/chains (no SID filter)."""
    scan_cp = host_executor(["iptables-save"], check=True)
    text = (scan_cp.stdout or b"").decode("utf-8", "replace")
    return e2f.residue_scan(text)


# ── §4: one-shot route probe executor ──────────────────────────────────────

#: Frozen probe exit-code protocol (§3 R1.2): the probe's in-container
#: Python exits with these codes so the executor can classify WITHOUT
#: parsing OS/Python/Docker error text or reading stderr.
PROBE_EXIT_TCP_CONNECT_FAILED = 42     #: target unreachable (connection refused/timeout)
PROBE_EXIT_INTERNAL_ERROR = 43         #: other probe-internal error

#: Six probe specifications: service -> (dst_ip, dst_port, expected_src)
ROUTE_PROBE_SPECS = {
    "controller":     ("tuwunel", 6167, "172.31.0.2"),
    "policy-gateway": ("172.31.0.34", 8082, "172.31.0.18"),
    "mcp-bridge":     ("172.31.0.114", 18090, "172.31.0.82"),
    "gh-reporter":    ("172.31.0.98", 18090, "172.31.0.66"),
    "gh-proxy-r":     ("winproxy", 17890, "172.31.0.130"),
    "gh-proxy-b":     ("winproxy", 17890, "172.31.0.131"),
}


class RouteProbeError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


def run_route_probes(*, docker_executor: Callable,
                     host_executor: Callable,
                     image_ref: str,
                     tuwunel_ip: str,
                     windows_proxy_ip: str,
                     probe_journal: dict) -> dict:
    """§4: create/connect/start six one-shot probe containers.
    Each probe uses the SAME attachment pattern as the real service
    (E2E_CONTAINER_ATTACHMENTS), verifies kernel source-IP via
    Python socket getsockname(), then removes itself.

    Returns {service: {"source_ip": str, "verified": bool}}."""
    results = {}
    for service, (dst_template, dst_port, expected_src) in \
            ROUTE_PROBE_SPECS.items():
        dst_ip = (tuwunel_ip if dst_template == "tuwunel"
                  else windows_proxy_ip if dst_template == "winproxy"
                  else dst_template)
        probe_name = "mp-e2e-route-probe-%s" % service
        try:
            results[service] = _run_single_probe(
                docker_executor=docker_executor,
                image_ref=image_ref, probe_name=probe_name,
                service=service, dst_ip=dst_ip, dst_port=dst_port,
                expected_src=expected_src, probe_journal=probe_journal)
        except RouteProbeError as exc:
            results[service] = {"error": exc.code,
                                "detail": exc.detail,
                                "verified": False}
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
            results[service] = {"error": "PROBE_TIMEOUT",
                                "detail": type(exc).__name__,
                                "verified": False}
        except Exception as exc:
            # safe catch-all: continue collecting remaining probes;
            # never leak argv/stderr/exception body
            results[service] = {"error": "PROBE_INTERNAL_ERROR",
                                "detail": type(exc).__name__,
                                "verified": False}
        finally:
            # ALWAYS clean up the probe (even on success)
            _cleanup_probe(docker_executor, probe_name, probe_journal)
    return results


def _run_single_probe(*, docker_executor, image_ref, probe_name,
                      service, dst_ip, dst_port, expected_src,
                      probe_journal) -> dict:
    # create (network none, no env-file, no mounts)
    create_argv = ["create", "--name", probe_name,
                   "--network", "none", "--restart", "no", image_ref]
    cp = docker_executor(create_argv, check=True)
    if cp.returncode != 0:
        raise RouteProbeError("PROBE_CREATE_FAILED",
                              "%s: create rc=%d" % (service,
                                                    cp.returncode))
    cp = docker_executor(["inspect", probe_name, "--format",
                          "{{.Id}}"], check=True)
    cid = (cp.stdout or b"").decode().strip()
    if not cid:
        raise RouteProbeError("PROBE_ID_MISSING", service)
    probe_journal[probe_name] = cid

    # connect using the SAME attachments as the real service
    import e2e_probes
    for network, ip, priority in e2e_probes.E2E_CONTAINER_ATTACHMENTS[
            service]:
        argv = ["network", "connect"]
        if ip:
            argv.extend(["--ip", ip])
        argv.extend(["--gw-priority", str(priority), network, probe_name])
        cp = docker_executor(argv, check=True)
        if cp.returncode != 0:
            raise RouteProbeError("PROBE_CONNECT_FAILED",
                                  "%s: connect %s" % (service, network))

    # start
    cp = docker_executor(["start", probe_name], check=True)
    if cp.returncode != 0:
        raise RouteProbeError("PROBE_START_FAILED", service)

    # verify kernel source-IP via Python socket; the in-container code
    # uses FROZEN exit codes (not stderr text) so the executor can
    # classify without parsing OS/Python/Docker error strings.
    code = ("import socket,sys\n"
            "try:\n"
            "    s=socket.create_connection(('%s',%d),timeout=10)\n"
            "    print(s.getsockname()[0]);s.close()\n"
            "except (ConnectionRefusedError,ConnectionResetError,"
            "socket.timeout,OSError):\n"
            "    sys.exit(%d)\n"
            "except Exception:\n"
            "    sys.exit(%d)\n"
            % (dst_ip, dst_port,
               PROBE_EXIT_TCP_CONNECT_FAILED,
               PROBE_EXIT_INTERNAL_ERROR))
    cp = docker_executor(["exec", probe_name, "python", "-c", code],
                         check=True, timeout=30)
    exit_code = cp.returncode
    if exit_code == PROBE_EXIT_TCP_CONNECT_FAILED:
        raise RouteProbeError("PROBE_TARGET_UNREACHABLE",
                              "%s: TCP connect to %s:%d failed"
                              % (service, dst_ip, dst_port))
    if exit_code == PROBE_EXIT_INTERNAL_ERROR:
        raise RouteProbeError("PROBE_INTERNAL_ERROR",
                              "%s: probe internal error (exit %d)"
                              % (service, exit_code))
    if exit_code != 0:
        raise RouteProbeError("PROBE_INTERNAL_ERROR",
                              "%s: unexpected exit %d"
                              % (service, exit_code))
    output = (cp.stdout or b"").decode().strip()
    if not output:
        raise RouteProbeError("PROBE_OUTPUT_INVALID",
                              "%s: empty stdout from %s:%d"
                              % (service, dst_ip, dst_port))
    lines = [l.strip() for l in output.split("\n") if l.strip()]
    if len(lines) != 1:
        raise RouteProbeError("PROBE_OUTPUT_INVALID",
                              "%s: multi-line output (%d lines)"
                              % (service, len(lines)))
    source_ip = lines[0]
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", source_ip) \
            and not re.match(r"^\[?[0-9a-fA-F:]+\]?$", source_ip):
        raise RouteProbeError("PROBE_OUTPUT_INVALID",
                              "%s: non-IP output" % service)
    if source_ip != expected_src:
        raise RouteProbeError(
            "ROUTE_GATE_FAILED",
            "%s: source %s != expected %s"
            % (service, source_ip, expected_src))
    return {"source_ip": source_ip, "verified": True}


def _cleanup_probe(docker_executor, probe_name, probe_journal):
    try:
        docker_executor(["rm", "-f", probe_name], check=False,
                        timeout=30)
    except Exception:
        pass
    probe_journal.pop(probe_name, None)


# ── §5: HiClaw receipt real-time read-only validator ──────────────────────

class ReceiptValidationError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


#: Docker State.Status values that satisfy a "stopped" expectation
#: (docker never reports the literal "stopped" for State.Status).
#: EXACTLY the legal stopped family: container exists and is not
#: running. "dead" (removal-failure broken state) is deliberately
#: EXCLUDED — a broken old github-mcp must MISMATCH, not pass.
STOPPED_STATE_FAMILY = frozenset((
    "stopped",
    "exited",
    "created",
))


#: Direction-aware sync contract (M8-GH-4B4). The HiClaw runtime
#: propagates mcporter.json differently per role family — this is
#: the SINGLE production authority; the harness must never keep a
#: second copy of the mapping.
#:
#: - manager: live->canonical. The manager's change-triggered push
#:   (start-manager-agent.sh, ~10s, excludes .openclaw/.cache/.npm/
#:   .local/.mc but NOT config/mcporter.json) copies the live
#:   workspace file to MinIO; the ONLY canonical->live path is the
#:   container-startup mirror, so a running manager converges from
#:   live writes, never from canonical writes.
#: - workers: canonical->live. worker-entrypoint.sh explicitly
#:   EXCLUDES config/mcporter.json from its change-triggered push
#:   and unconditionally refreshes it from MinIO every ~300s
#:   (`mc cp ... || true`, no newer-than guard).
HICLAW_SYNC_MODES = {
    "manager": "live_to_canonical",
    "reviewer": "canonical_to_live",
    "fixer": "canonical_to_live",
    "verifier": "canonical_to_live",
}

#: Live mcporter.json path inside each role's container.
HICLAW_LIVE_CONFIG_PATHS = {
    "manager": "/root/manager-workspace/config/mcporter.json",
    "reviewer": "/root/hiclaw-fs/agents/reviewer/config/mcporter.json",
    "fixer": "/root/hiclaw-fs/agents/fixer/config/mcporter.json",
    "verifier": "/root/hiclaw-fs/agents/verifier/config/mcporter.json",
}

#: MinIO canonical object key (relative to the storage prefix).
HICLAW_CANONICAL_KEYS = {
    "manager": "manager/config/mcporter.json",
    "reviewer": "agents/reviewer/config/mcporter.json",
    "fixer": "agents/fixer/config/mcporter.json",
    "verifier": "agents/verifier/config/mcporter.json",
}

#: Convergence contract: bounded poll cadence and budget per family.
HICLAW_CONVERGENCE = {
    "manager": {"poll_seconds": 5, "timeout_seconds": 120,
                "stability_checks": 2},     # >= 2 push cycles (~10s)
    "worker": {"poll_seconds": 15, "timeout_seconds": 420,
               "stability_checks": 1},      # 300s pull + margin
}

#: Dedicated MinIO transaction prefix — MUST stay outside every
#: production mirror/push/pull prefix so backups are never synced.
HICLAW_TX_PREFIX = "mp-gh4-tx"

#: Stable fingerprint of the deployed sync scripts. The harness
#: computes and records the ACTUAL fingerprint at runtime; this
#: frozen expectation makes contract drift fail closed.
HICLAW_SYNC_FINGERPRINT_EXPECTED = {
    "manager_push_excludes_mcporter": False,   # NOT excluded
    "worker_push_excludes_mcporter": True,     # excluded
    "worker_pull_period_seconds": 300,
}


def hiclaw_role_canonical_key(role: str) -> str:
    """Single authority: MinIO object key for a role's mcporter."""
    return HICLAW_CANONICAL_KEYS[role]


def hiclaw_role_live_config_path(role: str) -> str:
    """Single authority: live mcporter path inside the container."""
    return HICLAW_LIVE_CONFIG_PATHS[role]


def hiclaw_role_sync_mode(role: str) -> str:
    """Single authority: 'live_to_canonical' | 'canonical_to_live'."""
    return HICLAW_SYNC_MODES[role]


def hiclaw_role_convergence(role: str) -> dict:
    """Single authority: bounded convergence budget for a role."""
    if hiclaw_role_sync_mode(role) == "live_to_canonical":
        return dict(HICLAW_CONVERGENCE["manager"])
    return dict(HICLAW_CONVERGENCE["worker"])


def hiclaw_role_gateway_url(role: str) -> str:
    """Single-authority E2E Gateway URL for a HiClaw role (harness
    and receipt validator MUST both derive from this; no second
    hardcopy of the value is allowed anywhere)."""
    return "http://172.31.0.18:8083%s" % HICLAW_ROLE_FREEZE[role][3]


#: Frozen role->(container_name, mxid, hiclaw_net_ip, gateway_role_path)
HICLAW_ROLE_FREEZE = {
    "manager": ("hiclaw-manager",
                "@manager:" + e2f.E2E_MATRIX_SERVER_NAME,
                "172.21.0.2", "/manager/sse"),
    "reviewer": ("hiclaw-worker-reviewer",
                 "@reviewer:" + e2f.E2E_MATRIX_SERVER_NAME,
                 "172.21.0.5", "/reviewer/sse"),
    "fixer": ("hiclaw-worker-fixer",
              "@fixer:" + e2f.E2E_MATRIX_SERVER_NAME,
              "172.21.0.4", "/fixer/sse"),
    "verifier": ("hiclaw-worker-verifier",
                 "@verifier:" + e2f.E2E_MATRIX_SERVER_NAME,
                 "172.21.0.6", "/verifier/sse"),
}


def _compute_receipt_sha256(receipt: dict) -> str:
    """§5.1: canonical SHA-256 of the receipt (receipt_sha256 field
    removed, sort_keys=True, separators=(',',':'), UTF-8)."""
    canonical = {k: v for k, v in receipt.items()
                 if k != "receipt_sha256"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True,
                   separators=(",", ":"),
                   ensure_ascii=True).encode("utf-8")).hexdigest()


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_hex64(value, *, role: str, field: str) -> None:
    """Fail-closed hash format check: the value must be a string of
    exactly 64 lowercase hex chars (a non-string fails with the same
    stable code, never a TypeError)."""
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise ReceiptValidationError(
            "RECEIPT_HASH_FORMAT",
            "agent %s field %s must be 64-char lowercase hex"
            % (role, field))


def validate_hiclaw_receipt(receipt_path: str, *,
                            docker_executor: Callable,
                            minio_executor: Callable,
                             expected_old_mcp_state: str = "stopped"
                             ) -> dict:
    """§5: read-only real-time validation of the HiClaw rewiring receipt.
    Checks four roles' container IDs, MXIDs, IPs, Gateway URLs,
    mcporter config hashes, and old github-mcp state against live
    Docker inspect. NEVER modifies, restarts, or reads config bodies."""
    try:
        receipt = json.loads(
            Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReceiptValidationError("RECEIPT_INVALID",
                                     "unreadable or invalid JSON")

    # receipt-level checks
    schema = receipt.get("schema_version")
    if schema == 2:
        # direction-aware receipt (M8-GH-4B4): strict v2 contract
        return _validate_direction_receipt(
            receipt, docker_executor=docker_executor,
            minio_executor=minio_executor,
            expected_old_mcp_state=expected_old_mcp_state)
    if schema == 1:
        # schema v1 predates direction-awareness (no sync_mode,
        # canonical/live hashes or convergence evidence). It is
        # rejected with a STABLE schema error — never silently
        # downgraded or validated against the weaker v1 rules.
        raise ReceiptValidationError(
            "RECEIPT_SCHEMA",
            "schema_version 1 receipts are retired; regenerate with "
            "the direction-aware harness (schema_version 2)")
    raise ReceiptValidationError("RECEIPT_SCHEMA",
                                 "schema_version must be 2")
def minio_readonly_via_docker(docker_exec: Callable) -> Callable:
    """Build the read-only MinIO canonical executor from a docker
    executor: mc argv in, CompletedProcess out (metadata and hashes
    only; bodies never parsed by the validator)."""
    def minio_exec(mc_argv, check=True, timeout=30, **_):
        return docker_exec(
            ["exec", "hiclaw-controller", "mc"] + list(mc_argv),
            check=check, timeout=timeout)
    return minio_exec


def _mc_stat(minio_executor: Callable, key: str) -> dict:
    cp = minio_executor(["stat", "hiclaw/hiclaw-storage/" + key],
                        check=True)
    if getattr(cp, "returncode", 0) != 0:
        raise ReceiptValidationError(
            "RECEIPT_CANONICAL_INACCESSIBLE",
            "mc stat rc!=0 for %s" % key.split("/")[-1])
    out = {}
    for line in (cp.stdout or b"").decode(
            "utf-8", "replace").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _mc_hash(minio_executor: Callable, key: str) -> str:
    cp = minio_executor(
        ["cat", "hiclaw/hiclaw-storage/" + key], check=True)
    if getattr(cp, "returncode", 0) != 0:
        raise ReceiptValidationError(
            "RECEIPT_CANONICAL_INACCESSIBLE",
            "mc cat rc!=0 for %s" % key.split("/")[-1])
    # hash WITHOUT exposing the body: the executor's stdout crosses
    # this boundary only into hashlib
    return hashlib.sha256(cp.stdout or b"").hexdigest()


def _validate_direction_receipt(receipt: dict, *,
                                 docker_executor: Callable,
                                 minio_executor: Callable,
                                 expected_old_mcp_state: str = "stopped"
                                 ) -> dict:
    """M8-GH-4B4 schema v2: direction-aware receipt validation.
    Verifies per-role live AND canonical state, sync_mode
    correctness, contract fingerprint, and old github-mcp. Old v1
    receipts missing direction fields are REJECTED by the caller
    (schema check upstream), never downgraded."""
    # strict v2 contract fields
    if receipt.get("rollback_ownership") != "mp-gh4-harness":
        raise ReceiptValidationError("RECEIPT_OWNERSHIP",
                                     "rollback_ownership mismatch")
    rewire_session = receipt.get("rewire_session")
    if not isinstance(rewire_session, str) \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                                rewire_session):
        raise ReceiptValidationError(
            "RECEIPT_SESSION_INVALID",
            "rewire_session must be a 1-128 char run-unique id")
    if rewire_session in ("mp-gh4-harness", "session", "default"):
        raise ReceiptValidationError(
            "RECEIPT_SESSION_INVALID",
            "rewire_session must be run-unique, not a fixed sentinel")
    if not receipt.get("sync_contract_fingerprint"):
        raise ReceiptValidationError(
            "RECEIPT_FINGERPRINT_MISSING",
            "sync_contract_fingerprint required for v2")
    stored_sha = receipt.get("receipt_sha256", "")
    if not hmac.compare_digest(stored_sha,
                               _compute_receipt_sha256(receipt)):
        raise ReceiptValidationError("RECEIPT_INTEGRITY_MISMATCH",
                                     "receipt_sha256 mismatch")

    agents = receipt.get("agents")
    if not isinstance(agents, list) or len(agents) != 4:
        raise ReceiptValidationError("RECEIPT_AGENT_COUNT",
                                     "expected 4 agents")
    role_list = [a.get("role") for a in agents]
    # duplicate check BEFORE the role-set check (same priority as
    # v1): 4 agents with a dup reports DUPLICATE, not MISMATCH
    if len(role_list) != len(set(role_list)):
        raise ReceiptValidationError("RECEIPT_DUPLICATE_ROLE",
                                     "duplicate role in agents list")
    if set(role_list) != set(HICLAW_ROLE_FREEZE):
        raise ReceiptValidationError("RECEIPT_ROLE_MISMATCH",
                                     "missing=%s extra=%s" % (
                                         sorted(set(HICLAW_ROLE_FREEZE)
                                                - set(role_list)),
                                         sorted(set(role_list)
                                                - set(HICLAW_ROLE_FREEZE))))

    seen_mxids = set()
    for agent in agents:
        mxid = agent.get("mxid", "")
        if mxid in seen_mxids:
            raise ReceiptValidationError("RECEIPT_DUPLICATE_MXID",
                                         "duplicate mxid")
        seen_mxids.add(mxid)

    results = {}
    for agent in agents:
        role = agent["role"]
        frozen = HICLAW_ROLE_FREEZE[role]
        checks = {}

        # direction-aware fields REQUIRED (no downgrade from v1)
        for f in ("sync_mode", "canonical_key", "live_path",
                  "live_hash_before", "live_hash_after",
                  "canonical_hash_before", "canonical_hash_after",
                  "canonical_etag_before", "canonical_etag_after",
                  "convergence_evidence"):
            if not agent.get(f):
                raise ReceiptValidationError(
                    "RECEIPT_DIRECTION_FIELD_MISSING",
                    "%s.%s" % (role, f))

        # hash format strictness (same bar as v1): every hash field
        # is exactly 64-char lowercase hex; anything else is a schema
        # defect, not drift
        for f in ("live_hash_before", "live_hash_after",
                  "canonical_hash_before", "canonical_hash_after",
                  "config_hash_before", "config_hash_after",
                  "token_hash"):
            _require_hex64(agent.get(f), role=role, field=f)
        _require_hex64(receipt.get("receipt_sha256"),
                       role=receipt.get("rewire_session", "?"),
                       field="receipt_sha256")

        # sync_mode must match the production authority
        expected_mode = hiclaw_role_sync_mode(role)
        checks["sync_mode"] = ("OK"
                               if agent["sync_mode"] == expected_mode
                               else "MISMATCH")

        # canonical/live hash agreement
        checks["hash_agreement"] = (
            "OK" if agent["live_hash_after"]
            == agent["canonical_hash_after"] else "DRIFT")

        # container identity via docker inspect
        cp = docker_executor(
            ["inspect", frozen[0], "--format", "{{.Id}}"],
            check=True)
        live_id = (cp.stdout or b"").decode().strip()
        checks["container_id"] = (
            "OK" if live_id and agent.get("container_id") == live_id
            else "DRIFT")

        # hiclaw-net live IP (present AND matching frozen + receipt)
        cp = docker_executor(
            ["inspect", frozen[0], "--format",
             "{{(index .NetworkSettings.Networks \"hiclaw-net\")"
             ".IPAddress}}"],
            check=True)
        live_ip = (cp.stdout or b"").decode().strip()
        checks["ip"] = (
            "OK" if live_ip == frozen[2]
            and agent.get("hiclaw_net_ip") == frozen[2]
            else "DRIFT")

        # mcporter config hash (live side)
        cp = docker_executor(
            ["exec", frozen[0], "sha256sum",
             hiclaw_role_live_config_path(role)],
            check=True, timeout=15)
        hash_output = (cp.stdout or b"").decode().strip()
        live_hash = hash_output.split()[0] if hash_output else ""
        checks["config_hash"] = (
            "OK" if live_hash
            and live_hash == agent.get("live_hash_after")
            else "DRIFT")

        # gateway URL from the frozen authority
        expected_url = hiclaw_role_gateway_url(role)
        checks["gateway_url"] = (
            "OK" if agent.get("gateway_url") == expected_url
            else "MISMATCH")

        # F8: MXID must match the frozen authority exactly; a
        # missing, unknown or cross-role MXID is a hard mismatch
        checks["mxid"] = (
            "OK" if agent.get("mxid") == frozen[1] else "MISMATCH")

        # F4: LIVE canonical verification — the MinIO object is
        # probed read-only (stat metadata + in-memory hash). The
        # receipt's internal hash_agreement can NEVER substitute for
        # this: a rehashed receipt whose canonical_hash_after merely
        # COPIES the live hash must still fail here when the real
        # canonical object never converged.
        ckey = agent.get("canonical_key")
        if ckey != hiclaw_role_canonical_key(role):
            checks["canonical_key"] = "MISMATCH"
        else:
            cinfo = _mc_stat(minio_executor, ckey)
            live_etag = cinfo.get("etag", "")
            checks["canonical_key"] = "OK"
            checks["canonical_hash"] = (
                "OK" if _mc_hash(minio_executor, ckey)
                == agent.get("canonical_hash_after")
                else "DRIFT")
            checks["canonical_etag"] = (
                "OK" if live_etag
                and live_etag == agent.get("canonical_etag_after")
                else "DRIFT")

        results[role] = checks

    # old github-mcp state (same normalization as v1)
    old_mcp = receipt.get("old_github_mcp", {})
    old_checks = {}
    cp = docker_executor(
        ["inspect", "github-mcp", "--format", "{{.Id}}"], check=True)
    live_old_id = (cp.stdout or b"").decode().strip()
    old_checks["container_id"] = (
        "OK" if old_mcp.get("container_id") == live_old_id
        else "DRIFT")
    cp = docker_executor(
        ["inspect", "github-mcp", "--format", "{{.State.Status}}"],
        check=True)
    live_state = (cp.stdout or b"").decode().strip()
    if expected_old_mcp_state == "stopped":
        state_ok = live_state in STOPPED_STATE_FAMILY
    else:
        state_ok = live_state == expected_old_mcp_state
    old_checks["state"] = "OK" if state_ok else "MISMATCH"
    cp = docker_executor(
        ["inspect", "github-mcp", "--format",
         "{{.HostConfig.RestartPolicy.Name}}"], check=True)
    live_rp = (cp.stdout or b"").decode().strip()
    old_checks["restart_policy"] = (
        "OK" if old_mcp.get("restart_policy") == live_rp else "DRIFT")
    cp = docker_executor(
        ["inspect", "github-mcp", "--format",
         "{{range $k, $v := .NetworkSettings.Networks}}"
         "{{$k}} {{end}}"], check=True)
    live_nets = set(n for n in
                    (cp.stdout or b"").decode().strip().split() if n)
    receipt_nets = set(old_mcp.get("network_attachments", []))
    old_checks["network_attachments"] = (
        "OK" if live_nets == receipt_nets else "DRIFT")
    results["old_github_mcp"] = old_checks

    all_ok = all(
        v == "OK"
        for checks in results.values() for v in checks.values())
    return {"verified": all_ok, "checks": results}


__all__ = [
    "FirewallExecutorError", "install_firewall", "teardown_firewall",
    "HICLAW_ROLE_FREEZE", "hiclaw_role_gateway_url",
    "HICLAW_SYNC_MODES", "HICLAW_LIVE_CONFIG_PATHS",
    "HICLAW_CANONICAL_KEYS", "HICLAW_CONVERGENCE",
    "HICLAW_TX_PREFIX", "HICLAW_SYNC_FINGERPRINT_EXPECTED",
    "hiclaw_role_canonical_key", "hiclaw_role_live_config_path",
    "hiclaw_role_sync_mode", "hiclaw_role_convergence",
    "STOPPED_STATE_FAMILY",
    "scan_firewall_residue", "RouteProbeError", "run_route_probes",
    "ROUTE_PROBE_SPECS", "ReceiptValidationError",
    "validate_hiclaw_receipt", "minio_readonly_via_docker",
    "HICLAW_ROLE_FREEZE",
]
