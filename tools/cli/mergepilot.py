"""MergePilot minimal local CLI — isolated-stack operator (development preview).

Six commands over the ISOLATED_LIVE one-click stack contract:

    mergepilot install    build the 5 local images, record real image IDs
    mergepilot doctor     read-only environment / stack checks
    mergepilot start      run the isolated stack end-to-end (preflight-gated)
    mergepilot status     absent / partial / healthy classification
    mergepilot stop       remove session containers+networks+secrets (keep images)
    mergepilot cleanup    stop + remove verified local images + install manifest

Platform boundary (v0.1, explicit): Windows 10/11 + WSL2 distro
``MergePilot-Test`` only. No Linux/macOS/native-Windows-Docker/remote-TCP/SSH
daemon or production support is claimed or implemented. This CLI is the local
operator entry for the M8 isolated stack — NOT a GitHub App, NOT production
verification, NOT a SaaS. The five repo truth boundaries stay unchanged:
database_verified=false, application_integration_verified=false,
production_verified=false, revision_producer_contract=NOT_VERIFIED,
audit_producer_contract=NOT_VERIFIED.

Execution contract:
  - reuses the versioned plan generators and SecretFile transports from
    ``tools/demo_console/one_click_startup.py`` (single source of truth);
    NEVER imports anything from ``tests/``.
  - every Docker command is routed through
    ``wsl.exe -u root -d MergePilot-Test -- docker`` with argv arrays only
    (shell execution is forbidden) and is checked by the planner's
    ``assert_argv_safe`` before execution.
  - the authorized distro must already be Running; a missing/Stopped distro is
    a hard failure (it is NEVER implicitly started). The ephemeral
    verification env gate belongs to the test harness only and is not part
    of the CLI contract.
  - secrets are generated per session (``secrets.token_urlsafe``), travel only
    via the planner's 0600 env-file transports, and never appear in argv,
    logs, manifests, or JSON output (collector-side ``redact()`` everywhere).
  - state lives under ``<project>/.mergepilot/`` (gitignored):
    ``install.json`` (version, project root, image tag -> real image ID) and
    ``session.json`` (run_id, stage, real container/network IDs, secret file
    basenames — never any secret value). Both are written atomically
    (temp file + ``os.replace``).

Journal / rollback contract:
  - ``session.json`` IS the write journal: it is created before the first
    Docker write and updated (atomically) after every successful creation
    with the resource's REAL inspected ID.
  - a failed ``start`` rolls back ONLY the resources this run created, in
    reverse journal order, then deletes the secret files and the journal;
    primary and rollback failures are both reported (neither swallows the
    other). Exit 5 = failed but rollback verified; exit 9 = rollback/residue
    verification failed.
  - ``stop``/``cleanup`` may discover resources by their fixed names, but a
    resource is only deleted after its name-resolved ID matches the manifest
    ID. Name-present-but-ID-different is fail-closed (exit 4, nothing
    deleted). Resources present without a session manifest are an ownership
    conflict (exit 4) — ownership is never guessed.

Exit codes: 0 success/explicit idempotent no-op; 2 CLI usage error;
3 environment/config/precheck failure with zero side effects; 4 existing
resource / state conflict; 5 execution failure with verified rollback;
9 rollback or residue-verification failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# M8-GH-4B1: GitHub E2E foundation (pure planning module, sibling file —
# the CLI script's own directory is on sys.path both as a script and under
# the test harness's path injection).
import e2e_foundation as e2f

# ── Exit codes (stable contract) ─────────────────────────────────────────────

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PRECHECK = 3
EXIT_CONFLICT = 4
EXIT_FAILED_CLEANED = 5
EXIT_RESIDUE = 9

# ── Platform constants (production-owned; mirrors of established contracts) ──

#: Single authoritative distro source (usability round §2): the
#: default stays MergePilot-Test; operators may override through the
#: MERGEPILOT_WSL_DISTRO environment variable (validated against the
#: registered distro set — a name that is not registered fails with
#: DISTRO_NOT_REGISTERED before any docker emission). The bootstrapper
#: passes its -Distro through this variable and nowhere else.
DISTRO_ENV_VAR = "MERGEPILOT_WSL_DISTRO"
AUTHORIZED_DISTRO = os.environ.get(DISTRO_ENV_VAR, "MergePilot-Test")

#: The HiClaw stack distro: agents (hiclaw-manager/workers), the
#: minio canonical store (mc via hiclaw-controller) and the rewired
#: mcporter configs all live in the docker daemon the rewiring
#: harness execs against (mp_gh4_harness: wsl -d Ubuntu-22.04).
#: The E2E distro cannot see those containers at all — agent
#: readiness, receipt revalidation and role-token extraction MUST
#: bind to THIS distro.
HICLAW_DISTRO = "Ubuntu-22.04"
APPROVED_ENDPOINT = "unix:///var/run/docker.sock"
CONSOLE_URL = "http://127.0.0.1:8600/api/live/status"
CONSOLE_PORT = 8600
BUILT_BASE_IMAGE = "python:3.12-slim"

STATE_DIR_NAME = ".mergepilot"
INSTALL_MANIFEST = "install.json"
SESSION_MANIFEST = "session.json"
SECRETS_DIR_NAME = "secrets"

# A documentation-only placeholder bridge IP (RFC 5737 TEST-NET-3) used to
# render start --dry-run / doctor plan previews. The REAL run always measures
# the postgres bridge IP after the container is healthy.
PLACEHOLDER_BRIDGE_IP = "203.0.113.1"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_WIN_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_CONTAINER_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NETWORK_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# Ordered audit-db migration chain (source of truth: the repository bootstrap
# order mirrored by tests/isolated_live/ephemeral_harness.py and
# tools/m4f1/run_schema_foundation.sh — production code must not import
# tests/, so the order is restated here; m4f1_state and m4f1_hotfix_1 are
# applied twice each on purpose (idempotency verification)).
AUDIT_DB_MIGRATION_CHAIN = (
    "init.sql",
    "m3_state.sql",
    "m3b_policy.sql",
    "m3b_b4.sql",
    "m3b_b4c.sql",
    "m3b_b4c1.sql",
    "m3b_b4c1_1.sql",
    "m3b_b4d1.sql",
    "m3c_state.sql",
    "m4f1_state.sql",
    "m4f1_state.sql",
    "m4f1_hotfix_1.sql",
    "m4f1_hotfix_1.sql",
    # M8-GH-3: the GitHub ingress migration (deliveries/outbox tables +
    # NOLOGIN capability roles + LOGIN runtime roles + minimal grants) is
    # part of the standard chain — installs create the ingress surface.
    "m8gh1_github_ingress.sql",
)
ISOLATED_LIVE_MIGRATIONS = (
    "001_environment_identity.sql",
    "002_mergepilot_reader_acl.sql",
)

# Phase-0 prerequisite roles (idempotent DO block; mirrors
# run_schema_foundation.sh:43 — the audit-db migrations reference these roles
# in triggers/ownership, so they must exist BEFORE the chain runs).
PREREQUISITE_ROLE_SQL = (
    "DO $d$ BEGIN "
    "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') "
    "THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF; "
    "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') "
    "THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF; "
    "END $d$;"
)


class Failure(Exception):
    """Stable-code CLI failure; ``exit_code`` maps to the contract table."""

    def __init__(self, code, detail="", exit_code=EXIT_PRECHECK):
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        super().__init__("%s%s" % (code, (" (%s)" % detail) if detail else ""))


# ── Logging / output (redacted, JSON-safe) ───────────────────────────────────

_JSON_MODE = False


def _redact(text):
    # Late-bound planner redaction (module loaded per project dir); falls back
    # to a minimal DSN/password scrub so even pre-planner errors stay safe.
    planner = _PLANNER
    if planner is not None:
        return planner.redact(text)
    out = re.sub(r"postgresql?://[^/\s@]+:[^/\s@]+@",
                 "postgresql://***:***@", text)
    out = re.sub(r"(password\s*=\s*)['\"]?[^\s;&'\"]+",
                 r"\1***REDACTED***", out, flags=re.IGNORECASE)
    return out


def log(text):
    """Progress logging. stderr in --json mode so stdout stays pure JSON."""
    if not text:
        return
    stream = sys.stderr if _JSON_MODE else sys.stdout
    try:
        stream.write(_redact(text) + "\n")
        stream.flush()
    except OSError:
        pass


def _status_line(result):
    """Human status line for a command result. A status that starts
    with 'failed' is a FAILURE and must never render as OK — the
    preview.2 blocker printed 'OK (failed_rolled_back)' (usability
    round §6)."""
    status = result.get("status") or "ok"
    if str(status).startswith("failed"):
        primary = result.get("primary_code") or result.get("error_code")
        hint = (" (%s)" % primary) if primary else ""
        return "FAILED (%s%s)" % (status, hint)
    return "OK (%s)" % status


# ── Project / planner resolution ─────────────────────────────────────────────

_PLANNER = None          # one_click_startup module (single source of truth)
_SHOWCASE = None         # showcase_cases module (deterministic seed SQL)


_PLANNER_ROOT = None


def resolve_project_dir(explicit):
    if explicit:
        path = Path(explicit)
    else:
        path = Path.cwd()
    path = path.resolve()
    if not path.is_dir():
        raise Failure("PROJECT_DIR_INVALID", "not a directory: %s" % path,
                      exit_code=EXIT_USAGE)
    if not (path / "tools" / "demo_console" / "one_click_startup.py").is_file():
        raise Failure(
            "PROJECT_DIR_INVALID",
            "%s is not a MergePilot checkout (missing "
            "tools/demo_console/one_click_startup.py)" % path,
            exit_code=EXIT_USAGE)
    return path


def _load_planner(project_dir):
    """Import the versioned planner + showcase seed generator from the
    checkout being operated on (never from tests/, never a second copy)."""
    global _PLANNER, _SHOWCASE, _PLANNER_ROOT
    project_dir = Path(project_dir).resolve()
    if _PLANNER is not None and _PLANNER_ROOT != project_dir:
        # Canonical module names otherwise keep the first checkout alive in a
        # long-running process that invokes main() with another --project-dir.
        sys.modules.pop("one_click_startup", None)
        sys.modules.pop("showcase_cases", None)
        _PLANNER = None
        _SHOWCASE = None
    if _PLANNER is not None:
        return _PLANNER, _SHOWCASE
    demo_console = str(project_dir / "tools" / "demo_console")
    while demo_console in sys.path:
        sys.path.remove(demo_console)
    sys.path.insert(0, demo_console)
    import one_click_startup as planner
    import showcase_cases as showcase
    _PLANNER = planner
    _SHOWCASE = showcase
    _PLANNER_ROOT = project_dir
    return planner, showcase


def container_name(planner, service):
    """The planner's fixed container-name contract (see plan_service_run)."""
    return "mergepilot-isolated-%s-1" % service


def image_tag(planner, service):
    """The planner's fixed local-image tag contract (see plan_build)."""
    return "mergepilot-isolated-%s:local" % service


def _to_wsl_path(path):
    """Map a Windows path to its WSL drvfs form (D:\\x\\y -> /mnt/d/x/y).

    Docker runs INSIDE the distro, so ``--env-file`` arguments must be
    WSL-visible paths; the secret bytes are written by this (Windows-side)
    process to the same file. Non-Windows-style paths pass through with
    normalized separators (native-WSL development layout).
    """
    text = str(path)
    m = _WIN_PATH_RE.match(text)
    if not m:
        return text.replace("\\", "/")
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return "/mnt/%s/%s" % (drive, rest)


# ── Atomic manifests (no secret fields, ever) ────────────────────────────────

def _atomic_write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def load_manifest(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        raise Failure("MANIFEST_INVALID", "unparseable: %s" % path.name,
                      exit_code=EXIT_RESIDUE) from None
    if not isinstance(obj, dict):
        raise Failure("MANIFEST_INVALID", "not an object: %s" % path.name,
                      exit_code=EXIT_RESIDUE)
    return obj


def state_paths(project_dir):
    state = project_dir / STATE_DIR_NAME
    return {
        "state": state,
        "install": state / INSTALL_MANIFEST,
        "session": state / SESSION_MANIFEST,
        "secrets": state / SECRETS_DIR_NAME,
    }


# ── WSL-routed Docker execution ──────────────────────────────────────────────

def _entry_wake(docker):
    """Command-entry bounded wake (§2). Test doubles that substitute
    WslDocker without the wake method simply pass through — the wake
    is an operator affordance, never a structural dependency of the
    command logic."""
    wake = getattr(docker, "wake_if_dormant", None)
    if callable(wake):
        docker.wake_if_dormant(soft=getattr(docker, "_soft_wake", False))


def _looks_argv_truncated(args, err) -> bool:
    """Detect the wsl.exe argv-truncation signature in a docker error:
    docker reports 'no such object: X' / 'not found: X' where X is a
    proper prefix of one of OUR arguments (and shorter than it) —
    i.e. docker never received the full resource name."""
    for marker in ("no such object: ", "No such object: ",
                   "not found: ", "No such image: "):
        idx = err.find(marker)
        while idx != -1:
            tail = err[idx + len(marker):]
            reported = tail.split()[0] if tail.split() else ""
            for arg in args:
                if (reported and len(reported) >= 3
                        and arg.startswith(reported)
                        and len(reported) < len(arg)):
                    return True
            idx = err.find(marker, idx + 1)
    return False


class WslDocker:
    """All Docker access: argv arrays via wsl.exe, redacted collection,
    planner-side argv-secret guard, explicit rc handling (a failed command is
    NEVER mistaken for an absent resource)."""

    # -- raw wsl.exe ---------------------------------------------------------

    def _run_wsl(self, argv, *, input_bytes=None, timeout=60):
        try:
            cp = subprocess.run(
                argv, input=input_bytes,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=False,
                cwd=str(self._project_dir))
        except FileNotFoundError:
            raise Failure("WSL_MISSING",
                          "wsl.exe not found (Windows/WSL2 required)") from None
        except subprocess.TimeoutExpired:
            raise Failure("COMMAND_TIMEOUT",
                          "wsl %s timed out after %ds" % (argv[0], timeout),
                          exit_code=EXIT_FAILED_CLEANED) from None
        return cp

    def distro_states(self):
        """{distro: state} from read-only `wsl -l -v` (never starts one)."""
        if self._distro_states is not None:
            return self._distro_states
        cp = self._run_wsl(["wsl.exe", "-l", "-v"], timeout=30)
        states = {}
        for line in cp.stdout.decode("utf-8", "replace").splitlines():
            clean = line.replace("\x00", "").strip()
            if not clean:
                continue
            if clean.startswith("*"):
                clean = clean.lstrip("*").strip()
            parts = clean.split()
            if len(parts) < 3:
                continue
            state = parts[-2]
            if state.lower() not in ("running", "stopped"):
                continue
            if not parts[-1].isdigit():
                continue
            name = " ".join(parts[:-2]).strip()
            if name:
                states[name] = state
        self._distro_states = states
        return states

    #: operator lifecycle commands (install/doctor/status/start/stop/
    #: cleanup) may BOUNDED-WAKE a registered-but-dormant distro;
    #: every other construction (E2E executor path) keeps the
    #: never-implicitly-started contract
    def __init__(self, planner, project_dir, allow_wake=False):
        self._planner = planner
        self._project_dir = Path(project_dir)
        self._distro_states = None
        self._allow_wake = bool(allow_wake)
        self._wake_attempted = False

    def registered_distros(self):
        """Names from `wsl -l -v` (read-only)."""
        return set(self.distro_states().keys())

    def wake_if_dormant(self, soft=False):
        """Bounded wake of a REGISTERED but dormant distro at operator
        command entry: boot with a trivial --exec and poll. An
        UNREGISTERED name fails fast with DISTRO_NOT_REGISTERED (never
        spins the wake loop); a wake that cannot reach Running within
        the bound (MERGEPILOT_WAKE_TIMEOUT_SECS, default 45) raises
        the stable DISTRO_WAKE_TIMEOUT (or returns False in soft mode
        so diagnostics continue and REPORT)."""
        states = self.distro_states()
        if AUTHORIZED_DISTRO not in states:
            raise Failure(
                "DISTRO_NOT_REGISTERED",
                "%s is not in `wsl -l -v` (set %s to a registered "
                "distro)" % (AUTHORIZED_DISTRO, DISTRO_ENV_VAR),
                exit_code=EXIT_PRECHECK)
        if states.get(AUTHORIZED_DISTRO) == "Running":
            return True
        if self._wake_attempted:
            return False
        self._wake_attempted = True
        deadline = time.monotonic() + float(
            os.environ.get("MERGEPILOT_WAKE_TIMEOUT_SECS", "45"))
        while time.monotonic() < deadline:
            try:
                subprocess.run(
                    ["wsl.exe", "-d", AUTHORIZED_DISTRO, "--exec",
                     "/bin/true"],
                    capture_output=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._distro_states = None
            if self.distro_states().get(AUTHORIZED_DISTRO) == "Running":
                return True
            time.sleep(2.0)
        if soft:
            return False  # diagnostics continue and REPORT the state
        raise Failure(
            "DISTRO_WAKE_TIMEOUT",
            "%s is registered but did not reach Running within the "
            "bounded wake window" % AUTHORIZED_DISTRO,
            exit_code=EXIT_PRECHECK)

    def _require_distro_running(self, *, refresh=False):
        """Fail-closed distro gate for every ``-d`` emission.

        ``refresh=True`` discards the cached ``wsl -l -v`` result and
        re-probes read-only — used by mid-execution emitters (psql_exec) so
        a distro shut down after the command started is still caught and
        never implicitly restarted. An UNREGISTERED name fails with
        DISTRO_NOT_REGISTERED; a registered-but-dormant distro fails with
        DISTRO_NOT_RUNNING. Bounded waking is NOT part of this gate:
        the six operator commands call wake_if_dormant() explicitly at
        entry (allow_wake construction); every internal gate — psql
        re-probes, rollback paths — keeps the never-restart contract.
        """
        if refresh:
            self._distro_states = None
        states = self.distro_states()
        if AUTHORIZED_DISTRO not in states:
            raise Failure(
                "DISTRO_NOT_REGISTERED",
                "%s is not in `wsl -l -v` (set %s to a registered "
                "distro)" % (AUTHORIZED_DISTRO, DISTRO_ENV_VAR),
                exit_code=EXIT_PRECHECK)
        if states.get(AUTHORIZED_DISTRO) != "Running":
            raise Failure(
                "DISTRO_NOT_RUNNING",
                "%s is %s; refusing to issue docker commands (never "
                "implicitly started)" % (AUTHORIZED_DISTRO,
                                         states.get(AUTHORIZED_DISTRO,
                                                    "absent")),
                exit_code=EXIT_PRECHECK)

    def bash_env(self, expr):
        """Run a fixed read-only shell expression INSIDE the distro (direct
        wsl bash, no docker prefix). Only used for the DOCKER_HOST probe."""
        self._require_distro_running()
        argv = ["wsl.exe", "-u", "root", "-d", AUTHORIZED_DISTRO, "--",
                "bash", "-c", expr]
        self._planner.assert_argv_safe(argv)
        cp = self._run_wsl(argv, timeout=30)
        out = _redact(cp.stdout.decode("utf-8", "replace") if cp.stdout
                      else "")
        err = _redact(cp.stderr.decode("utf-8", "replace") if cp.stderr
                      else "")
        log("bash -c rc=%d out=%s err=%s" % (cp.returncode, out[:80],
                                             err[:80]))
        return cp

    def wsl_exec(self, argv, *, input_bytes=None, timeout=60, check=True,
                 log_tag=None):
        """Run a HOST-side command inside the distro as root (iptables
        and friends — NOT docker). Same argv-safety / redaction / rc
        contract as docker(); a checked failure is a stable failure, never
        a silent absence."""
        self._require_distro_running()
        full = (["wsl.exe", "-u", "root", "-d", AUTHORIZED_DISTRO, "--"]
                + list(argv))
        self._planner.assert_argv_safe(full)
        cp = self._run_wsl(full, input_bytes=input_bytes, timeout=timeout)
        out = _redact(cp.stdout.decode("utf-8", "replace") if cp.stdout
                      else "")
        err = _redact(cp.stderr.decode("utf-8", "replace") if cp.stderr
                      else "")
        tag = log_tag or argv[0]
        log("wsl %s rc=%d out=%s err=%s" % (tag, cp.returncode,
                                             out[:120], err[:120]))
        if check and cp.returncode != 0:
            raise Failure(
                "WSL_EXEC_FAILED",
                "wsl %s rc=%d (detail redacted)" % (tag, cp.returncode),
                exit_code=EXIT_FAILED_CLEANED)
        return cp

    # -- docker --------------------------------------------------------------

    def docker(self, args, *, input_bytes=None, timeout=90, check=True,
               log_tag=None, distro=None, suppress_output_log=False):
        # Distro gate BEFORE any -d command: a missing/Stopped distro is
        # never implicitly started (wsl -d on a stopped distro would start it).
        target = distro or AUTHORIZED_DISTRO
        if target == AUTHORIZED_DISTRO:
            self._require_distro_running()
        else:
            # secondary distro (HiClaw side): same fail-closed contract,
            # read-only state probe, never implicitly started
            states = self.distro_states()
            if states.get(target) != "Running":
                raise Failure(
                    "DISTRO_NOT_RUNNING",
                    "%s is %s; refusing to issue docker commands (never "
                    "implicitly started)" % (target,
                                             states.get(target, "absent")))
        argv = ["wsl.exe", "-u", "root", "-d", target, "--",
                "docker"] + list(args)
        self._planner.assert_argv_safe(argv)
        cp = self._run_wsl(argv, input_bytes=input_bytes, timeout=timeout)
        out = _redact(cp.stdout.decode("utf-8", "replace") if cp.stdout else "")
        err = _redact(cp.stderr.decode("utf-8", "replace") if cp.stderr else "")
        tag = log_tag or args[0]
        if suppress_output_log:
            # canonical-store reads (mc cat) return SECRET bodies; the
            # log line keeps rc visibility without the body
            log("docker %s rc=%d out=<suppressed> err=%s"
                % (tag, cp.returncode, err[:160]))
        else:
            log("docker %s rc=%d out=%s err=%s"
                % (tag, cp.returncode, out[:160], err[:160]))
        if cp.returncode != 0 and _looks_argv_truncated(args, err):
            # §1.11: wsl.exe NON-DETERMINISTICALLY truncates an argv
            # token on reassembly (observed: 'mergepilot-isolated-
            # console-edge-1' reaching docker as 'mergepi'); a
            # truncated name must never be classified as absence.
            # Bounded retry, then the stable WSL_ARGV_TRUNCATION code.
            for _attempt in range(2):
                log("docker %s: argv truncation signature detected; "
                    "bounded retry" % tag)
                cp = self._run_wsl(argv, input_bytes=input_bytes,
                                   timeout=timeout)
                err = _redact(cp.stderr.decode("utf-8", "replace")
                              if cp.stderr else "")
                if cp.returncode == 0 or \
                        not _looks_argv_truncated(args, err):
                    break
            else:
                raise Failure(
                    "WSL_ARGV_TRUNCATION",
                    "docker %s: wsl.exe delivered a truncated argument "
                    "twice (err tail: %s)" % (tag, err[-80:]),
                    exit_code=EXIT_FAILED_CLEANED)
        if check and cp.returncode != 0:
            raise Failure(
                "DOCKER_FAILED",
                "docker %s rc=%d (detail redacted)" % (tag, cp.returncode),
                exit_code=EXIT_FAILED_CLEANED)
        return cp

    # -- read-only probes ----------------------------------------------------

    def inspect_id(self, kind, name):
        """Resolve (state, id) for a named container/network.

        state: 'absent' (clean no-such), 'present', or raises on daemon error
        — a probe failure is never conflated with absence.
        """
        cp = self.docker(["inspect", name, "--format", "{{.Id}}"],
                         check=False, log_tag="inspect-%s" % kind)
        if cp.returncode == 0:
            cid = cp.stdout.decode("utf-8", "replace").strip()
            if cid:
                return "present", cid
            raise Failure("DOCKER_INSPECT_FAILED",
                          "empty Id for %s %s" % (kind, name),
                          exit_code=EXIT_FAILED_CLEANED)
        err = (cp.stderr or b"").decode("utf-8", "replace").lower()
        if "no such" in err:
            return "absent", None
        raise Failure("DOCKER_INSPECT_FAILED",
                      "inspect %s %s rc=%d (not 'no such')"
                      % (kind, name, cp.returncode),
                      exit_code=EXIT_FAILED_CLEANED)

    def image_id(self, ref):
        """Local image ID for ref, or None when the image is not cached."""
        cp = self.docker(["image", "inspect", ref, "--format", "{{.Id}}"],
                         check=False, log_tag="image-inspect")
        if cp.returncode != 0:
            err = (cp.stderr or b"").decode("utf-8", "replace").lower()
            if "no such" in err or "not found" in err:
                return None
            raise Failure("DOCKER_IMAGE_INSPECT_FAILED",
                          "image inspect rc=%d for %s" % (cp.returncode, ref),
                          exit_code=EXIT_FAILED_CLEANED)
        img = cp.stdout.decode("utf-8", "replace").strip()
        if not img:
            return None
        return img

    def container_state(self, name):
        """(state, {id, status, health, exit_code}) — ONE inspect per
        container. Uses ``{{json .State}}`` — pipe characters in a
        ``--format`` template are re-interpreted as shell pipes by
        wsl.exe's argument reassembly (real-Docker E2E finding), so no
        literal ``|`` may appear anywhere in the format string."""
        cp = self.docker(
            ["inspect", name, "--format",
             "{{.Id}}@@{{json .State}}"],
            check=False, log_tag="inspect-state")
        if cp.returncode != 0:
            err = (cp.stderr or b"").decode("utf-8", "replace").lower()
            if "no such" in err:
                return "absent", {}
            raise Failure("DOCKER_INSPECT_FAILED",
                          "inspect %s rc=%d (not 'no such')"
                          % (name, cp.returncode),
                          exit_code=EXIT_FAILED_CLEANED)
        raw = cp.stdout.decode("utf-8", "replace").strip()
        parts = raw.split("@@", 1)
        if len(parts) != 2:
            raise Failure("DOCKER_INSPECT_FAILED",
                          "state probe unparseable for %s" % name,
                          exit_code=EXIT_FAILED_CLEANED)
        try:
            state = json.loads(parts[1])
        except ValueError:
            raise Failure("DOCKER_INSPECT_FAILED",
                          "state JSON unparseable for %s" % name,
                          exit_code=EXIT_FAILED_CLEANED) from None
        health = ""
        health_obj = state.get("Health")
        if isinstance(health_obj, dict):
            health = health_obj.get("Status") or ""
        return "present", {
            "id": parts[0],
            "status": state.get("Status") or "",
            "health": health,
            "exit_code": str(state.get("ExitCode", "")),
        }

    def network_ip(self, name):
        cp = self.docker(
            ["inspect", name, "--format",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            check=True, log_tag="inspect-ip")
        return cp.stdout.decode("utf-8", "replace").strip()

    # -- lifecycle waits -----------------------------------------------------

    def wait_healthy(self, name, timeout):
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            state, info = self.container_state(name)
            last = info.get("health") or info.get("status") or state
            if state == "present" and info.get("health") == "healthy":
                return
            if state == "present" and info.get("status") not in ("running",
                                                                 "restarting"):
                # m9 finding D: an early exit must carry the REAL
                # error — full logs are fetched and the first stable
                # error + stderr tail ride in the failure detail, not
                # just the preflight banner.
                tail = ""
                try:
                    tail = self.container_logs(name)
                except Exception:
                    pass
                raise Failure(
                    "CONTAINER_NOT_RUNNING",
                    "%s status=%s exit=%s before healthy; first_error=%s; "
                    "logs_tail=%s" % (
                        name, info.get("status"),
                        info.get("exit_code", "?"),
                        _first_stable_error(tail) or "(none in logs)",
                        _tail_lines(tail, 12)),
                    exit_code=EXIT_FAILED_CLEANED)
            time.sleep(2)
        raise Failure("HEALTH_TIMEOUT",
                      "%s not healthy within %ds (last=%s)"
                      % (name, timeout, last),
                      exit_code=EXIT_FAILED_CLEANED)

    def wait_exited(self, name, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, info = self.container_state(name)
            if state == "present" and info.get("status") == "exited":
                try:
                    return int(info.get("exit_code", "-1"))
                except (TypeError, ValueError):
                    return -1
            time.sleep(2)
        raise Failure("EXIT_TIMEOUT",
                      "%s did not exit within %ds" % (name, timeout),
                      exit_code=EXIT_FAILED_CLEANED)

    def container_logs(self, name):
        cp = self.docker(["logs", name], check=False, log_tag="logs")
        out = cp.stdout.decode("utf-8", "replace") if cp.stdout else ""
        return _redact(out)

    def psql_exec(self, container, sql, *, timeout=300):
        """Pipe SQL to psql INSIDE the postgres container (stdin, never argv).

        The reader-role SQL embeds a password — the SQL bytes are therefore
        never logged; only rc and a heavily truncated redacted stdout tail.
        The distro gate runs FIRST, with a fresh read-only re-probe, BEFORE
        the ``wsl ... -d ... docker exec`` argv is constructed: a
        missing/Stopped distro would otherwise be implicitly started.
        """
        self._require_distro_running(refresh=True)
        argv_args = ["exec", "-i", container, "psql", "-U", "mergepilot",
                     "-d", "mergepilot_audit", "-v", "ON_ERROR_STOP=1",
                     "-A", "-t", "-f", "-"]
        self._planner.assert_argv_safe(argv_args)
        full = ["wsl.exe", "-u", "root", "-d", AUTHORIZED_DISTRO, "--",
                "docker"] + argv_args
        cp = self._run_wsl(full, input_bytes=sql.encode("utf-8"),
                           timeout=timeout)
        out = _redact(cp.stdout.decode("utf-8", "replace")
                      if cp.stdout else "")
        log("psql rc=%d out=%s" % (cp.returncode, out[-160:]))
        if cp.returncode != 0:
            raise Failure("DB_PREPARE_FAILED",
                          "psql rc=%d (detail redacted)" % cp.returncode,
                          exit_code=EXIT_FAILED_CLEANED)
        return out


# ── Environment gate (shared by doctor / install / start) ────────────────────

def probe_environment(docker):
    """Ordered, read-only environment gates. Returns a list of check dicts;
    probing STOPS at the first failure (a Stopped distro is never probed
    further, never implicitly started)."""
    checks = []

    def add(name, code, ok, detail):
        checks.append({"name": name, "code": code, "ok": ok, "detail": detail})
        return ok

    if not add("wsl_present", "DOCTOR_WSL_MISSING", True, "wsl.exe probe"):
        return checks
    try:
        states = docker.distro_states()
    except Failure as exc:
        checks.append({"name": "wsl_present", "code": exc.code, "ok": False,
                       "detail": exc.detail})
        return checks
    if AUTHORIZED_DISTRO not in states:
        add("distro_state", "DOCTOR_DISTRO_MISSING", False,
            "%s not in `wsl -l -v`" % AUTHORIZED_DISTRO)
        return checks
    if states[AUTHORIZED_DISTRO] != "Running":
        add("distro_state", "DOCTOR_DISTRO_STOPPED", False,
            "%s is %s (never implicitly started)" % (AUTHORIZED_DISTRO,
                                                     states[AUTHORIZED_DISTRO]))
        return checks
    add("distro_state", "DOCTOR_DISTRO_RUNNING", True,
        "%s Running" % AUTHORIZED_DISTRO)

    cp = docker.docker(["context", "inspect", "--format",
                        "{{.Endpoints.docker.Host}}"], check=False,
                       log_tag="context")
    if cp.returncode != 0:
        add("daemon_endpoint", "DOCTOR_ENDPOINT_PROBE_FAILED", False,
            "docker context inspect rc=%d" % cp.returncode)
        return checks
    endpoint = cp.stdout.decode("utf-8", "replace").strip()
    if endpoint != APPROVED_ENDPOINT:
        add("daemon_endpoint", "DOCTOR_ENDPOINT_INVALID", False,
            "endpoint %s != %s (no TCP/SSH/remote)" % (endpoint,
                                                       APPROVED_ENDPOINT))
        return checks
    add("daemon_endpoint", "DOCTOR_ENDPOINT_OK", True, endpoint)

    bcp = docker.bash_env("echo \"${DOCKER_HOST:-}\"")
    if bcp.returncode != 0:
        add("docker_host", "DOCTOR_DOCKER_HOST_PROBE_FAILED", False,
            "rc=%d" % bcp.returncode)
        return checks
    docker_host = bcp.stdout.decode("utf-8", "replace").strip()
    if docker_host not in ("", APPROVED_ENDPOINT):
        add("docker_host", "DOCTOR_DOCKER_HOST_INVALID", False,
            "DOCKER_HOST=%r not empty/local-socket" % docker_host)
        return checks
    add("docker_host", "DOCTOR_DOCKER_HOST_OK", True,
        docker_host or "(empty)")

    icp = docker.docker(["info"], check=False, log_tag="info")
    if icp.returncode != 0:
        add("daemon_fingerprint", "DOCTOR_FINGERPRINT_PROBE_FAILED", False,
            "docker info rc=%d" % icp.returncode)
        return checks
    info_text = icp.stdout.decode("utf-8", "replace")
    fingerprint = {}
    for line in info_text.splitlines():
        s = line.strip()
        if ":" not in s:
            continue
        key, _, value = s.partition(":")
        fingerprint[key.strip().lower()] = value.strip()
    # Docker 29.x removed "Server ID" from `docker info`; the fingerprint
    # contract now requires Docker Root Dir + Server Version (both stable
    # across 24..29) — Server ID is recorded when present but optional.
    missing = [f for f in ("server version", "docker root dir")
               if not fingerprint.get(f)]
    if missing:
        add("daemon_fingerprint", "DOCTOR_FINGERPRINT_MISSING", False,
            "fields missing: %s" % missing)
        return checks
    add("daemon_fingerprint", "DOCTOR_FINGERPRINT_OK", True,
        "version=%s root=%s%s"
        % (fingerprint.get("server version", ""),
           fingerprint.get("docker root dir", ""),
           (" server_id=%s" % fingerprint["server id"][:16])
           if fingerprint.get("server id") else ""))

    base = docker.image_id(BUILT_BASE_IMAGE)
    if base is None:
        add("base_image", "DOCTOR_BASE_IMAGE_NOT_CACHED", False,
            "%s not cached (no pull is performed)" % BUILT_BASE_IMAGE)
        return checks
    add("base_image", "DOCTOR_BASE_IMAGE_CACHED", True, base)

    return checks


def pgvector_recorded_pins(planner) -> frozenset:
    """Every RECORDED pgvector identity, across storage backends.

    `docker inspect .Id` differs by backend (graph2: config digest;
    containerd image store: manifest digest), so the pin set carries
    the registry manifest digest, the classic-docker config Id, and
    the shipped-tar manifest digest. Anything outside the set still
    fails closed.
    """
    return frozenset((
        planner.PGVECTOR_IMAGE_DIGEST,
        planner.PGVECTOR_IMAGE_ID,
        planner.PGVECTOR_IMAGE_TAR_DIGEST,
    ))


def pgvector_cached_at_recorded_pin(docker, planner) -> bool:
    """True when any recorded ref resolves to a recorded identity."""
    pins = pgvector_recorded_pins(planner)
    for ref in (planner.PGVECTOR_IMAGE_REF,) + tuple(pins):
        img = docker.image_id(ref)
        if img is not None and img.strip() in pins:
            return True
    return False


def pgvector_runnable_ref(docker, planner) -> str:
    """A pgvector ref THIS daemon can actually `docker run`.

    The classic store runs the config-Id ref; the containerd image
    store runs manifest-digest refs (the config-Id ref does not
    resolve). Preference order is deterministic; byte-exactness is
    guaranteed by require_environment, which runs before any plan.
    """
    for ref in (planner.PGVECTOR_IMAGE_ID,
                planner.PGVECTOR_IMAGE_TAR_DIGEST,
                planner.PGVECTOR_IMAGE_DIGEST):
        if docker.image_id(ref) is not None:
            return ref
    img = docker.image_id(planner.PGVECTOR_IMAGE_REF)
    if img is not None and img.strip() in pgvector_recorded_pins(planner):
        # byte-exact identity of the tag-pinned image — never the
        # mutable tag itself
        return img.strip()
    raise Failure(
        "PGVECTOR_NOT_CACHED",
        "pgvector image not cached at any recorded pin (pull=never)",
        exit_code=EXIT_PRECHECK)


def require_environment(docker):
    """Install/start gate: probe_environment + pgvector digest cache."""
    checks = probe_environment(docker)
    if not all(c["ok"] for c in checks):
        bad = next(c for c in checks if not c["ok"])
        raise Failure(bad["code"], bad["detail"], exit_code=EXIT_PRECHECK)
    planner = _PLANNER
    # byte-exact offline pin, backend-stable: any RECORDED ref must
    # resolve to a RECORDED identity (registry manifest digest,
    # classic-docker config Id, or shipped-tar manifest digest).
    if not pgvector_cached_at_recorded_pin(docker, planner):
        raise Failure(
            "PGVECTOR_NOT_CACHED",
            "pgvector image not cached at any recorded pin (pull=never): "
            "%s must resolve to one of %s" % (
                planner.PGVECTOR_IMAGE_REF,
                ", ".join(sorted(pgvector_recorded_pins(planner)))),
            exit_code=EXIT_PRECHECK)
    # Every start path passes through this gate before planning —
    # record once which recorded ref THIS daemon can actually run so
    # all build_start_steps callers share the resolution (m9 B).
    planner.record_pgvector_run_ref(pgvector_runnable_ref(docker, planner))


# ── Stack discovery / classification ─────────────────────────────────────────

def discover_stack(docker, planner):
    """Read-only snapshot of the six fixed-name containers + two networks."""
    containers = {}
    for svc in planner.SERVICE_ORDER:
        state, info = docker.container_state(container_name(planner, svc))
        containers[svc] = {"state": state, **info}
    networks = {}
    for net in (planner.ORCHESTRATOR_NETWORK, planner.PUBLICATION_NETWORK):
        state, nid = docker.inspect_id("network", net)
        networks[net] = {"state": state, "id": nid}
    return {"containers": containers, "networks": networks}


def console_endpoint_ok():
    """Probe the loopback publication (the ONLY published port)."""
    try:
        with urllib.request.urlopen(CONSOLE_URL, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False, "endpoint unreachable"
    if body.get("source_read_only") is not True \
            or body.get("not_production") is not True \
            or body.get("production_resource_accessed") is not None:
        return False, "status contract mismatch"
    return True, "200 + read-only contract"


def classify_stack(docker, planner, snapshot):
    """absent | partial | healthy (+ per-detail). Read-only."""
    cons = snapshot["containers"]
    nets = snapshot["networks"]
    n_present = sum(1 for c in cons.values() if c["state"] == "present")
    n_net = sum(1 for n in nets.values() if n["state"] == "present")
    if n_present == 0 and n_net == 0:
        return "absent", "no stack resources found"
    if n_present != len(cons) or n_net != len(nets):
        return "partial", ("%d/%d containers, %d/%d networks"
                           % (n_present, len(cons), n_net, len(nets)))
    preflight = cons.get("preflight", {})
    preflight_ok = (preflight.get("status") == "exited"
                    and preflight.get("exit_code") == "0")
    running = all(cons[s]["status"] == "running"
                  for s in planner.SERVICE_ORDER if s != "preflight")
    if not (running and preflight_ok):
        return "partial", "full resource set but not running/preflight-ok"
    endpoint_ok, endpoint_detail = console_endpoint_ok()
    if not endpoint_ok:
        return "partial", "console endpoint: %s" % endpoint_detail
    return "healthy", endpoint_detail


def port_in_use(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ── gh-webhook secret env-file + runtime role bootstrap (M8-GH-3) ───────────

class GhWebhookSecretFile:
    """Secret env-file for the gh-webhook receiver (M8-GH-3).

    Same transport guarantees as the planner SecretFile classes: fixed
    name (``gh_webhook.env``), values never in argv/logs/manifests,
    0600 where enforceable, refuses to overwrite, idempotent delete.
    Carries GITHUB_INGRESS_DSN (INSERT-only role + forced
    connect_timeout=5 via the structured builder below),
    GITHUB_WEBHOOK_SECRET and the receiver-side repo allowlist.
    """

    _NAME = "gh_webhook.env"

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._path = self._dir / self._NAME

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def build_ingress_dsn(password: str, *, user: str = "github_event_ingress",
                          host: str = "postgres", port: int = 5432,
                          database: str = "mergepilot_audit",
                          connect_timeout: int = 5) -> str:
        """Structured role DSN with a FORCED connect_timeout.

        Built with urllib.parse (quoted password, explicit query) — never
        bare interpolation; the receiver/reporter re-validates it via
        dsn_guard.ensure_connect_timeout at startup and per connection.
        """
        import urllib.parse
        query = urllib.parse.urlencode(
            {"connect_timeout": str(connect_timeout)})
        return "postgresql://%s:%s@%s:%d/%s?%s" % (
            user, urllib.parse.quote(password, safe=""), host, port,
            database, query)

    @staticmethod
    def _validate(password: str, webhook_secret: str, allowlist: str) -> None:
        for name, value in (("ingress password", password),
                            ("webhook secret", webhook_secret)):
            if not isinstance(value, str) or not value.strip():
                raise Failure("CONFIG_INVALID", "%s empty" % name,
                              exit_code=EXIT_PRECHECK)
            for ch in ("\r", "\n", "\0"):
                if ch in value:
                    raise Failure(
                        "CONFIG_INVALID",
                        "%s contains a rejected control character" % name,
                        exit_code=EXIT_PRECHECK)
        if not isinstance(allowlist, str) or not allowlist.strip():
            raise Failure("CONFIG_INVALID", "allowlist empty",
                          exit_code=EXIT_PRECHECK)

    def write(self, ingress_dsn: str, webhook_secret: str,
              allowlist: str, publisher_dsn: str = "") -> None:
        self._validate("x" * 8, webhook_secret, allowlist)  # dsn pre-built
        if "connect_timeout=5" not in ingress_dsn:
            raise Failure("CONFIG_INVALID",
                          "ingress DSN missing forced connect_timeout=5",
                          exit_code=EXIT_PRECHECK)
        if publisher_dsn and "connect_timeout=5" not in publisher_dsn:
            raise Failure("CONFIG_INVALID",
                          "publisher DSN missing forced connect_timeout=5",
                          exit_code=EXIT_PRECHECK)
        if self._path.exists():
            raise Failure("SECRET_FILE_EXISTS",
                          "refusing to overwrite an existing gh-webhook "
                          "secret file", exit_code=EXIT_CONFLICT)
        self._dir.mkdir(parents=True, exist_ok=True)
        content = ("GITHUB_INGRESS_DSN=%s\n"
                   "GITHUB_WEBHOOK_SECRET=%s\n"
                   "GITHUB_REPO_ALLOWLIST=%s\n"
                   % (ingress_dsn, webhook_secret, allowlist))
        self._path.write_text(content, encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # Windows: recorded honestly in capability, not enforced
        if publisher_dsn:
            reporter = self._dir / "gh_reporter.env"
            if reporter.exists():
                raise Failure("SECRET_FILE_EXISTS",
                              "refusing to overwrite an existing reporter "
                              "secret file", exit_code=EXIT_CONFLICT)
            reporter.write_text("GITHUB_PUBLISHER_DSN=%s\n" % publisher_dsn,
                                encoding="utf-8")
            try:
                reporter.chmod(0o600)
            except OSError:
                pass

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()
        reporter = self._dir / "gh_reporter.env"
        if reporter.exists():
            reporter.unlink()

    def exists(self) -> bool:
        return self._path.exists()


GH_RUNTIME_ROLE_SQL_TEMPLATE = (
    "ALTER ROLE github_event_ingress PASSWORD '%s';\n"
    "ALTER ROLE github_check_publisher PASSWORD '%s';\n"
)


def _sql_literal(value: str) -> str:
    """PostgreSQL single-quoted literal escaping (the established repo
    transport: the secret travels only inside SQL piped over psql stdin,
    never argv/logs — see the ephemeral reader-role bootstrap)."""
    return value.replace("\\", "\\\\").replace("'", "''")


# ── Failure diagnostics (rollback 前取证;脱敏;owned 临时文件) ──────────────

_DIAG_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|dsn|api_key|private_key)"
    r"|MERGEPILOT_PG_DSN|GITHUB_WEBHOOK_SECRET|GITHUB_INGRESS_DSN"
    r"|GITHUB_PUBLISHER_DSN|PG_PASS|ADMIN_PW|POSTGRES_PASSWORD",
    re.IGNORECASE)


def _redact_env_value(key: str) -> str:
    """环境变量值脱敏:秘密类键只保留 '<redacted>'。"""
    if _DIAG_SECRET_KEY_RE.search(key):
        return "<redacted>"
    return "<present>"


def _first_stable_error(logs: str) -> str:
    """First STABLE error line in container logs (m9 D): startup-probe
    failures, exception types, or FAILED <CODE> markers — never the
    preflight banner."""
    import re as _re
    if not logs:
        return ""
    banner_marks = ('preflight passed', 'Config preflight')
    probe = _re.compile(r'STARTUP PROBE FAILED[^\n]*')
    code = _re.compile(r'\b([A-Z][A-Z0-9]+_[A-Z0-9_]{3,})\b')
    exc = _re.compile(r'^(\w*(?:Error|Exception)\w*):[^\n]*', _re.M)
    failed = _re.compile(r'FAILED [A-Z0-9_]+[^\n]*')
    for line in logs.splitlines():
        stripped = line.strip()
        if any(m in stripped for m in banner_marks):
            continue
        m = probe.search(stripped) or failed.search(stripped)
        if m:
            return m.group(0)
        m = code.search(stripped)
        if m:
            return stripped[:120]
        m = exc.search(stripped)
        if m:
            return stripped[:120]
    return ""


def _tail_lines(text: str, n: int) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[-400:]

def capture_failure_diagnostics(docker, planner, paths, session):
    """Rollback 前捕获本次 owned 容器的失败取证(M8-GH-3 §1)。

    精简摘要打到 stdout;完整诊断(最多 200 行日志+inspect 摘要)写入
    .mergepilot/diagnostics.json(owned 临时文件,cleanup 删除)。
    取证失败绝不覆盖 primary failure(每步独立 try/except)。
    """
    summary = {}
    details = {}
    for svc, cid in list((session.get("containers") or {}).items()):
        name = container_name(planner, svc)
        entry = {"container_id": cid}
        detail = dict(entry)
        try:
            state, info = docker.container_state(name)
            entry["state"] = state
            entry["status"] = info.get("status", "")
            entry["exit_code"] = info.get("exit_code", "")
            entry["health"] = info.get("health", "")
            detail.update({k: entry[k] for k in
                           ("state", "status", "exit_code", "health")})
        except Exception:
            entry["state"] = "inspect_error"
        try:
            logs = docker.container_logs(name)
            lines = [ln for ln in logs.splitlines() if ln.strip()]
            detail["logs_tail"] = lines[-200:]
        except Exception:
            detail["logs_tail"] = []
        try:
            cp = docker.docker(
                ["inspect", name, "--format", "{{json .Config}}"],
                check=False, log_tag="diag-config")
            if cp.returncode == 0:
                config = json.loads(
                    (cp.stdout or b"").decode("utf-8", "replace"))
                detail["image"] = config.get("Image", "")
                detail["entrypoint"] = config.get("Entrypoint") or []
                detail["cmd"] = config.get("Cmd") or []
                env_keys = sorted(
                    (e.split("=", 1)[0] for e in (config.get("Env") or [])
                     if "=" in e))
                detail["env_keys"] = env_keys
                detail["env_redacted"] = {
                    k: _redact_env_value(k) for k in env_keys}
        except Exception:
            pass
        summary[svc] = "%s exit=%s health=%s" % (
            entry.get("status", entry.get("state", "?")),
            entry.get("exit_code", "?"), entry.get("health", "?"))
        details[name] = detail
        log("diag %s: %s" % (svc, summary[svc]))
    diag_path = paths["state"] / "diagnostics.json"
    result = {"summary": summary}
    try:
        _atomic_write_json(diag_path, {"containers": details})
        result["file"] = str(diag_path)
    except Exception:
        pass
    return result


def bootstrap_gh_roles(docker, planner, ingress_pw: str,
                       publisher_pw: str) -> None:
    """Inject the two gh runtime LOGIN role passwords (M8-GH-3 §4).

    Runs INSIDE the start work transaction (after m8gh1 migrations, before
    the gh-webhook container): any failure raises Failure and the normal
    journal rollback removes the one-shot postgres container — no
    half-configured role state can persist.
    """
    pg_container = container_name(planner, "postgres")
    sql = GH_RUNTIME_ROLE_SQL_TEMPLATE % (_sql_literal(ingress_pw),
                                          _sql_literal(publisher_pw))
    docker.psql_exec(pg_container, sql)


def _policy_repo_allowlist(project_dir: Path) -> str:
    """Comma-separated repos.allowlist from policy.yaml (restricted parse,
    same line contract as github_drain.parse_policy_repo_allowlist)."""
    policy = project_dir / "tools" / "policy-gateway" / "policy.yaml"
    lines = policy.read_text(encoding="utf-8").splitlines()
    allowlist = []
    in_repos = in_allowlist = False
    for line in lines:
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
            match = re.fullmatch(r'    - "([^"]+)"', stripped)
            if match:
                allowlist.append(match.group(1))
                continue
            in_allowlist = False
    if not allowlist:
        raise Failure("CONFIG_INVALID",
                      "policy.yaml repos.allowlist empty/absent",
                      exit_code=EXIT_PRECHECK)
    return ",".join(allowlist)


# ── Start-plan construction (planner output, byte-for-byte) ──────────────────

def build_start_steps(planner, *, env_file, controller_env_file,
                      reader_dsn_env_file, gh_webhook_env_file,
                      run_id, bridge_ip, m4f,
                      session_public_dir=None, pg_image_ref=None):
    """The eleven-step plan, composed from the planner's own public plan
    functions in plan_orchestrated_start's exact order. Returns
    (steps, argv_list) where each step carries its wait semantics.

    The env-file arguments are WSL-visible paths (docker reads them inside
    the distro); the controller env-file CONTRACT is validated separately by
    the caller against the Windows-side file (same bytes).
    session_public_dir: WSL-visible path of the derived read-only
    status projection mounted into demo-console (maintenance §7).
    """
    demo_env = planner._demo_console_environment(run_id, bridge_ip)
    argv_steps = [
        ("network-create", planner.ORCHESTRATOR_NETWORK,
         planner.plan_network_create()),
        ("network-create", planner.PUBLICATION_NETWORK,
         planner.plan_publication_network_create()),
        ("container-run", "postgres",
         planner.plan_service_run(
             "postgres",
             image_ref=pg_image_ref or planner.get_pgvector_run_ref(),
             env_file=env_file)),
        ("container-run", "policy-gateway",
         planner.plan_service_run(
             "policy-gateway",
             image_ref=planner.get_built_image_identity("policy-gateway"),
             gateway_env=planner._gateway_environment())),
        ("container-run", "controller",
         planner.plan_service_run(
             "controller",
             image_ref=planner.get_built_image_identity("controller"),
             controller_env=planner._controller_environment(),
             env_file=controller_env_file, m4f_enabled=m4f)),
        ("container-run", "gh-webhook",
         planner.plan_gh_webhook_run(
             planner.get_built_image_identity("gh-webhook"),
             env_file=gh_webhook_env_file)),
        ("network-connect", planner.ORCHESTRATOR_NETWORK,
         planner.plan_gh_webhook_connect_backend()),
        ("container-run", "demo-console",
         planner.plan_service_run(
             "demo-console",
             image_ref=planner.get_built_image_identity("demo-console"),
             demo_console_env=demo_env,
             reader_dsn_env_file=reader_dsn_env_file,
             session_public_dir=session_public_dir)),
        ("container-run", "console-edge",
         planner.plan_console_edge_run(
             planner.get_built_image_identity("console-edge"))),
        ("network-connect", planner.ORCHESTRATOR_NETWORK,
         planner.plan_console_edge_connect_backend()),
        ("container-run", "preflight",
         planner.plan_service_run(
             "preflight",
             image_ref=planner.get_built_image_identity("preflight"),
             declared_pg_image=planner.PGVECTOR_IMAGE_ID,
             reader_dsn_env_file=reader_dsn_env_file)),
    ]
    return argv_steps


_WAIT_KIND = {
    "network-create": None,
    "network-connect": None,
    "container-run": "healthy",
}


def record_planner_image_identities(planner, install):
    """Feed the install manifest's real image IDs into the planner's
    in-process identity registry (floating tags are never authoritative)."""
    for service in planner.BUILT_SERVICES:
        tag = image_tag(planner, service)
        img_id = (install.get("images") or {}).get(tag)
        if not img_id or not _CONTAINER_ID_RE.match(img_id):
            raise Failure(
                "INSTALL_MANIFEST_INVALID",
                "missing/invalid image id for %s (run install)" % tag,
                exit_code=EXIT_PRECHECK)
        planner.record_built_image_identity(service, img_id)


# ── Database preparation (inside the fresh postgres container) ───────────────

def build_reader_role_sql(password, role):
    escaped = password.replace("\\", "\\\\").replace("'", "''")
    return (
        "CREATE ROLE %s\n"
        "    LOGIN PASSWORD '%s'\n"
        "    NOINHERIT\n"
        "    NOSUPERUSER\n"
        "    NOCREATEDB\n"
        "    NOCREATEROLE\n"
        "    NOREPLICATION\n"
        "    NOBYPASSRLS;\n"
        "ALTER ROLE %s\n"
        "    SET default_transaction_read_only = on;\n"
        % (role, escaped, role)
    )


def prepare_database(docker, planner, showcase, project_dir, reader_password):
    """Apply the canonical bootstrap to the FRESH postgres container:
    prerequisite roles -> 13-entry audit-db chain -> reader role ->
    ISOLATED_LIVE migrations -> environment marker -> showcase seed.

    All SQL rides stdin (never argv); ON_ERROR_STOP=1 makes it transactional
    per script; the reader password exists only inside the piped SQL bytes.
    """
    pg_container = container_name(planner, "postgres")
    audit_dir = project_dir / "tools" / "audit-db"
    iso_dir = project_dir / "tools" / "demo_console" / "migrations"

    def read_sql(directory, filename):
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise Failure("MIGRATION_FILE_INVALID",
                          "missing or non-regular: %s" % filename,
                          exit_code=EXIT_FAILED_CLEANED)
        return path.read_text(encoding="utf-8")

    docker.psql_exec(pg_container, PREREQUISITE_ROLE_SQL)
    for filename in AUDIT_DB_MIGRATION_CHAIN:
        docker.psql_exec(pg_container, read_sql(audit_dir, filename))
    docker.psql_exec(pg_container,
                     build_reader_role_sql(reader_password,
                                           planner.READER_ROLE))
    for filename in ISOLATED_LIVE_MIGRATIONS:
        docker.psql_exec(pg_container, read_sql(iso_dir, filename))
    marker_sql = ("INSERT INTO environment_identity (environment_id) "
                  "VALUES ('%s') ON CONFLICT DO NOTHING;"
                  % planner.ENVIRONMENT_MARKER)
    docker.psql_exec(pg_container, marker_sql)
    docker.psql_exec(pg_container, showcase.build_showcase_seed_sql())


# ── Journal helpers ──────────────────────────────────────────────────────────

#: Whitelisted keys of the derived public status projection. The
#: projection is the ONLY thing the console may mount (maintenance
#: §7): no paths, no argv, no secret-adjacent journal fields, ever.
_PUBLIC_STATUS_KEYS = (
    "schema", "run_id", "github_e2e", "stage", "updated_utc",
    "e2e_stage", "journal_complete", "transport_profile",
    "direct_routing_verified", "receipt_verified", "matrix_verified",
    "prerequisite_summary", "route_probes", "services_started",
    "relay_resources", "firewall_state", "stages", "e2e_last_error",
    "truth_boundaries",
)

#: The canonical 17-stage E2E DAG (console timeline contract). Each
#: entry maps the lifecycle's stage markers to the frozen stage
#: number and its zh label; ONE mapping, consumed by the projection
#: only — the UI never re-derives stage order.
_E2E_STAGE_TIMELINE = (
    (1, "prerequisites", "前置门禁",
     ("prerequisites",)),
    (2, "runtime_files", "运行时文件",
     ("runtime_files",)),
    (3, "networks", "八网络创建",
     ("networks",)),
    (4, "containers", "十一容器接入",
     ("containers",)),
    (5, "firewall", "防火墙安装",
     ("firewall",)),
    (6, "relay", "中继系统",
     ("relay_setup",)),
    (7, "postgres", "PostgreSQL 就绪",
     ("postgres_ready",)),
    (8, "db_bootstrap", "数据库引导",
     ("db_bootstrap",)),
    (9, "proxies", "代理就绪",
     ("proxies_ready",)),
    (10, "route_probes", "路由探测",
     ("route_probes",)),
    (11, "gateway", "策略网关",
     ("gateway_start", "gateway_health")),
    (12, "services", "业务服务",
     ("controller_start", "webhook_start", "demo_console_start",
      "console_edge_start", "gh_reporter_start")),
    (13, "agents", "Agent 就绪",
     ("agents_ready",)),
    (14, "receipt", "Receipt 复核",
     ("receipt_recheck",)),
    (15, "matrix", "Matrix 复核",
     ("matrix_recheck",)),
    (16, "preflight", "语义预检",
     ("final_preflight",)),
    (17, "complete", "完成",
     ("complete",)),
)

#: The five truth boundaries (README frozen section). CLI-side
#: constants keep the console field-driven: flipping one here (only
#: with real production artifacts) flips every mounted projection.
_TRUTH_BOUNDARIES = {
    "application_integration_verified": "NOT_VERIFIED",
    "database_verified": "NOT_VERIFIED",
    "production_verified": "NOT_VERIFIED",
    "revision_producer_contract": "NOT_VERIFIED",
    "audit_producer_contract": "NOT_VERIFIED",
}


def _timeline_stage_index(marker: str):
    for number, _key, _label, markers in _E2E_STAGE_TIMELINE:
        if marker in markers:
            return number
    return None


def _stage_timeline(session: dict) -> list:
    """Per-stage status derived ONLY from the persisted journal.

    complete → all passed. A journaled first stable error → the
    stage it stopped at is failed (verbatim marker mapping, never a
    guess), earlier passed, later pending. An in-flight journal →
    the reached stage is running. An UNKNOWN marker maps to nothing:
    unknown stages never masquerade as any canonical stage."""
    marker = session.get("e2e_stage", "")
    reached = _timeline_stage_index(marker)
    error = session.get("e2e_last_error") or {}
    failed = bool(error.get("code")) and not (
        session.get("e2e_stage") == "complete")
    stages = []
    for number, key, label, _markers in _E2E_STAGE_TIMELINE:
        if session.get("e2e_stage") == "complete":
            status = "passed"
        elif reached is None:
            status = "unknown"
        elif number < reached:
            status = "passed"
        elif number == reached:
            status = "failed" if failed else "running"
        else:
            status = "pending"
        stages.append({"n": number, "key": key, "label": label,
                       "status": status})
    return stages


def public_status_payload(session: dict) -> dict:
    """Derived, sanitized projection of the session journal for the
    read-only console API. Single writer: write_session derives it on
    every persist, so the mounted file can never drift from the
    journal. e2e_stage is reported VERBATIM — a stale or failed
    session is never dressed up as complete (journal_complete is a
    strict equality, not an inference)."""
    payload = {
        "schema": 1,
        "run_id": session.get("run_id", ""),
        "github_e2e": bool(session.get("github_e2e")),
        "stage": session.get("stage", ""),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime()),
    }
    if session.get("github_e2e"):
        summary = session.get("prerequisite_summary") or {}
        route = session.get("route_probe_results") or {}
        last_error = session.get("e2e_last_error") or {}
        payload.update({
            "e2e_stage": session.get("e2e_stage", ""),
            "journal_complete":
                session.get("e2e_stage") == "complete",
            "transport_profile": session.get("transport_profile", ""),
            "direct_routing_verified":
                session.get("direct_routing_verified"),
            "receipt_verified": bool(session.get("receipt_verified")),
            "matrix_verified": bool(session.get("matrix_verified")),
            "prerequisite_summary": {
                "checks_passed": summary.get("checks_passed"),
                "verified": bool(summary.get("verified")),
            },
            "route_probes": {
                edge: {
                    "verified": bool(probe.get("verified")),
                    "vantage": probe.get("vantage", ""),
                    # segments ride through when the journal has them
                    # (newer probes); a legacy journal renders 未提供
                    "segment_a":
                        (probe.get("segments") or {}).get("segment_a"),
                    "segment_b":
                        (probe.get("segments") or {}).get("segment_b"),
                    "application":
                        (probe.get("segments") or {}).get("application"),
                }
                for edge, probe in route.items()},
            "services_started": list(session.get("e2e_started", [])),
            "relay_resources": {
                "containers": len(session.get("relay_containers", [])
                                  or []),
                "host_units": len(session.get("relay_host_units", [])
                                  or []),
                "probe_containers":
                    len(session.get("relay_probe_containers", []) or []),
            },
            "firewall_state": session.get("firewall_state", ""),
            "stages": _stage_timeline(session),
            "e2e_last_error": {
                "code": last_error.get("code", ""),
                "stage": last_error.get("stage", ""),
            } if last_error else {"code": "", "stage": ""},
            "truth_boundaries": dict(_TRUTH_BOUNDARIES),
        })
    assert set(payload) <= set(_PUBLIC_STATUS_KEYS)
    return payload


def write_session(paths, session):
    _atomic_write_json(paths["session"], session)
    # derived, sanitized projection for the read-only console mount
    # (§7): same single writer, same persist instant — the console
    # never reads the journal or the secrets dir directly
    public_dir = Path(paths["state"]) / "public"
    try:
        public_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(public_dir / "status.json",
                           public_status_payload(session))
    except OSError:
        pass  # projection is best-effort; the journal stays
              # authoritative and the console reports unavailable


def new_session(run_id, m4f, github_e2e=False):
    session = {
        "schema_version": 1,
        "run_id": run_id,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime()),
        "m4f": bool(m4f),
        "stage": "init",
        "containers": {},          # service -> real inspected ID (creation order)
        "networks": {},            # network name -> real inspected ID (order)
        "secrets": ["postgres.env", "controller.env", "demo_console.env",
                    "gh_webhook.env", "gh_reporter.env"],
    }
    if github_e2e:
        # E2E-only journal fields — the DEFAULT session manifest stays
        # byte-identical to the pre-B1 shape (default-off contract §2).
        session["github_e2e"] = True
        session["firewall_teardown"] = None   # journaled pin argvs (E2E)
    return session


def rollback_session(docker, planner, paths, session):
    """Reverse-journal rollback of THIS session's created resources.

    Only manifest-recorded IDs are touched; every failure is collected (never
    swallowed, never masks the primary error); the journal and secret files
    are removed only when everything verified clean. Each operation is
    individually guarded so one failure cannot abort the rest.
    """
    codes = []
    container_order = list(session.get("containers", {}).keys())
    for svc in reversed(container_order):
        target_id = session["containers"][svc]
        try:
            cp = docker.docker(["rm", "-fv", target_id], check=False,
                               timeout=120, log_tag="rollback-rm")
            if cp.returncode != 0:
                state, _info = docker.container_state(
                    container_name(planner, svc))
                if state == "present":
                    codes.append("ROLLBACK_CONTAINER_RM_FAILED:%s" % svc)
        except Exception as exc:
            codes.append("ROLLBACK_CONTAINER_RM_FAILED:%s(%s)"
                         % (svc, getattr(exc, "code", type(exc).__name__)))
    # M8-GH-4B1: session-owned firewall pins come out BEFORE the E2E
    # networks (R4 rollback order: containers -> pins -> networks) and ONLY
    # the argvs this session journaled (ownership never guessed).
    for argv in reversed(list(session.get("firewall_teardown") or [])):
        try:
            cp = docker.wsl_exec(list(argv), check=False, timeout=30,
                                 log_tag="rollback-pin")
            if cp.returncode != 0:
                codes.append("ROLLBACK_PIN_FAILED:%s" % argv[:3])
        except Exception as exc:
            codes.append("ROLLBACK_PIN_FAILED:(%s)" % type(exc).__name__)
    for net in reversed(list(session.get("networks", {}).keys())):
        target_id = session["networks"][net]
        try:
            cp = docker.docker(["network", "rm", target_id], check=False,
                               timeout=60, log_tag="rollback-network-rm")
            if cp.returncode != 0:
                state, _nid = docker.inspect_id("network", net)
                if state == "present":
                    codes.append("ROLLBACK_NETWORK_RM_FAILED:%s" % net)
        except Exception as exc:
            codes.append("ROLLBACK_NETWORK_RM_FAILED:%s(%s)"
                         % (net, getattr(exc, "code", type(exc).__name__)))
    for basename in session.get("secrets", []):
        secret_path = paths["secrets"] / basename
        try:
            if secret_path.exists():
                secret_path.unlink()
            if secret_path.exists():
                codes.append("SECRET_FILE_STILL_PRESENT:%s" % basename)
        except OSError:
            codes.append("SECRET_DELETE_FAILED:%s" % basename)
    if not codes:
        try:
            if paths["session"].exists():
                paths["session"].unlink()
        except OSError:
            codes.append("SESSION_MANIFEST_DELETE_FAILED")
        if paths["session"].exists():
            codes.append("SESSION_MANIFEST_STILL_PRESENT")
    return codes


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_install(args):
    project_dir = resolve_project_dir(args.project_dir)
    planner, _showcase = _load_planner(project_dir)
    paths = state_paths(project_dir)

    if args.dry_run:
        steps = [planner.plan_build(s) for s in planner.BUILT_SERVICES]
        for s in planner.BUILT_SERVICES:
            log("DRY-RUN build %s" % image_tag(planner, s))
        return EXIT_OK, {
            "command": "install", "status": "dry-run", "code": EXIT_OK,
            "plans": steps,
            "note": "6 local builds + image-ID recording; no Docker command "
                    "executed, nothing written",
        }

    docker = WslDocker(planner, project_dir, allow_wake=True)
    # §2: bounded wake of a registered-but-dormant distro at
    # command entry (internal gates never restart mid-run)
    _entry_wake(docker)
    require_environment(docker)

    images = {}
    for service in planner.BUILT_SERVICES:
        tag = image_tag(planner, service)
        log("building %s ..." % tag)
        docker.docker(planner.plan_build(service), timeout=1800,
                      check=True, log_tag="build-%s" % service)
        img_id = docker.image_id(tag)
        if not img_id or not _CONTAINER_ID_RE.match(img_id):
            raise Failure("BUILD_VERIFY_FAILED",
                          "built image id missing/invalid for %s" % tag,
                          exit_code=EXIT_FAILED_CLEANED)
        images[tag] = img_id
    manifest = {
        "schema_version": 1,
        "project_root": str(project_dir),
        "images": images,
    }
    _atomic_write_json(paths["install"], manifest)
    log("install manifest written: %s" % paths["install"].name)
    return EXIT_OK, {
        "command": "install", "status": "ok", "code": EXIT_OK,
        "resources": {"images": images},
    }


def cmd_doctor(args):
    project_dir = resolve_project_dir(args.project_dir)
    planner, showcase = _load_planner(project_dir)
    docker = WslDocker(planner, project_dir, allow_wake=True)
    # §2: bounded wake of a registered-but-dormant distro at
    # command entry (internal gates never restart mid-run)
    docker._soft_wake = True  # doctor REPORTS, never dies on wake
    _entry_wake(docker)
    checks = []

    def add(name, code, ok, detail):
        checks.append({"name": name, "code": code, "ok": ok,
                       "detail": _redact(str(detail))})

    version = sys.version_info
    add("python_version", "DOCTOR_PYTHON", True,
        "%d.%d.%d" % (version.major, version.minor, version.micro))

    required = ([("Dockerfile.%s" % s) for s in planner.BUILT_SERVICES]
                + ["docker-compose.yml",
                   "tools/demo_console/one_click_startup.py",
                   "tools/demo_console/showcase_cases.py",
                   "tools/demo_console/migrations/%s"
                   % ISOLATED_LIVE_MIGRATIONS[0],
                   "tools/demo_console/migrations/%s"
                   % ISOLATED_LIVE_MIGRATIONS[1],
                   "config/gh-app/room-map.example.yaml"]
                + [("tools/audit-db/%s" % f)
                   for f in sorted(set(AUDIT_DB_MIGRATION_CHAIN))])
    missing = [r for r in required if not (project_dir / r).is_file()]
    add("project_layout", "DOCTOR_LAYOUT_MISSING" if missing
        else "DOCTOR_LAYOUT_OK", not missing,
        "missing: %s" % missing if missing else
        "%d contract files present" % len(required))

    try:
        with tempfile.TemporaryDirectory(prefix="mp-doctor-") as td:
            tdp = Path(td)
            planner.SecretFile(tdp).write("doctor-probe-admin",
                                          "doctor-probe-reader")
            planner.ControllerSecretFile(tdp).write(
                "doctor-probe-pgpass", "doctor-probe-adminpw")
            planner.ReaderDsnSecretFile(tdp).write(
                "postgresql://mergepilot_reader:doctor-probe@postgres:5432/"
                "mergepilot_audit")
            GhWebhookSecretFile(tdp).write(
                GhWebhookSecretFile.build_ingress_dsn("doctor-probe-ingress"),
                "doctor-probe-webhook-secret", "nghqqa/MergePilot")
            for service in planner.BUILT_SERVICES:
                planner.record_built_image_identity(
                    service, "sha256:" + "0" * 64)
            build_start_steps(
                planner,
                env_file=_to_wsl_path(tdp / "postgres.env"),
                controller_env_file=_to_wsl_path(tdp / "controller.env"),
                reader_dsn_env_file=_to_wsl_path(tdp / "demo_console.env"),
                gh_webhook_env_file=_to_wsl_path(tdp / "gh_webhook.env"),
                run_id="doctor-plan-probe",
                bridge_ip=PLACEHOLDER_BRIDGE_IP, m4f=False)
        add("planner_chain", "DOCTOR_PLAN_OK", True,
            "11-step start plan generated (temp secrets discarded)")
    except Failure as exc:
        add("planner_chain", exc.code, False, exc.detail)
    except Exception as exc:  # planner contract breach is a doctor failure
        add("planner_chain", "DOCTOR_PLAN_INVALID", False,
            "%s" % type(exc).__name__)

    env_checks = probe_environment(docker)
    checks.extend(env_checks)
    env_ok = all(c["ok"] for c in env_checks)

    stack = {"classification": "unknown", "detail": "environment gate failed"}
    images = {}
    if env_ok:
        pg_ok = pgvector_cached_at_recorded_pin(docker, planner)
        add("pgvector_image", "DOCTOR_PGVECTOR_CACHED" if pg_ok
            else "DOCTOR_PGVECTOR_NOT_CACHED", pg_ok,
            planner.PGVECTOR_IMAGE_REF if pg_ok
            else "no recorded pin cached (pull=never)")
        for service in planner.BUILT_SERVICES:
            tag = image_tag(planner, service)
            img = docker.image_id(tag)
            images[tag] = img
            add("local_images", "DOCTOR_IMAGE_NOT_BUILT:%s" % service
                if img is None else "DOCTOR_IMAGE_OK:%s" % service,
                img is not None, img or "tag absent (run install)")
        snapshot = discover_stack(docker, planner)
        classification, detail = classify_stack(docker, planner, snapshot)
        stack = {"classification": classification, "detail": detail}
        add("stack_state",
            "DOCTOR_STACK_%s" % classification.upper(),
            classification in ("absent", "healthy"), detail)
        if classification == "absent":
            busy = port_in_use(CONSOLE_PORT)
            add("port_8600", "DOCTOR_PORT_BUSY" if busy
                else "DOCTOR_PORT_FREE", not busy,
                "127.0.0.1:%d %s" % (CONSOLE_PORT,
                                     "in use" if busy else "free"))

    # M8-GH-4B1: read-only E2E foundation checks (planning capability).
    if getattr(args, "github_e2e", False):
        try:
            e2f.e2e_activation_gate()
            add("e2e_activation_gate", "DOCTOR_E2E_GATE_BROKEN", False,
                "gate did not fail closed")
        except e2f.E2EConfigError as exc:
            add("e2e_activation_gate", exc.code, True,
                "B1 gate intact — a real start fails closed while "
                "G3/G4 components are pending")
        try:
            preview = e2f.build_b1_dry_run_preview(
                run_id="doctor-e2e-probe",
                tuwunel_ip=e2f.E2E_TUWUNEL_DEFAULT_IP,
                room_map_host="/mnt/d/placeholder-room-map.yaml",
                policy_host="/mnt/d/placeholder-policy.yaml")
            add("e2e_b1_plan", "DOCTOR_E2E_PLAN_OK", True,
                "B1 preview generated (sid=%s, %d firewall rules, "
                "%d required room members)"
                % (preview["firewall"]["sid"],
                   sum(preview["firewall"]["counts"].values()),
                   len(preview["membership_preflight"]
                        ["required_members"])))
        except Exception as exc:
            add("e2e_b1_plan", "DOCTOR_E2E_PLAN_INVALID", False,
                type(exc).__name__)
        if env_ok:
            cp = docker.docker(["network", "connect", "--help"],
                               check=False, timeout=30,
                               log_tag="gw-priority-probe")
            text = (cp.stdout or b"").decode("utf-8", "replace")
            has_gwp = "--gw-priority" in text
            add("e2e_gw_priority",
                "DOCTOR_E2E_GW_PRIORITY_OK" if has_gwp
                else "DOCTOR_E2E_GW_PRIORITY_MISSING", has_gwp,
                "docker network connect supports --gw-priority"
                if has_gwp else "daemon lacks --gw-priority")

    ok = all(c["ok"] for c in checks)
    if not ok:
        # §6 diagnostics: the first failing check and its stable code
        # are the headline, never a bare OK-on-failure
        first = next(c for c in checks if not c["ok"])
        if not _JSON_MODE:
            log("DOCTOR_FIRST_FAILURE %s (%s): %s"
                % (first["name"], first["code"], first["detail"]))
        result_first = {"name": first["name"], "code": first["code"]}
    else:
        result_first = None
    result = {
        "command": "doctor", "status": "ok" if ok else "failed",
        "code": EXIT_OK if ok else EXIT_PRECHECK,
        "first_failure": result_first,
        "checks": checks,
        "resources": {"stack": stack, "local_images": images},
    }
    return (EXIT_OK if ok else EXIT_PRECHECK), result


# ── M8-GH-4B3-W3B-R2: E2E CLI wiring (§3/§15) ─────────────────────────────

def _e2e_docker_exec(docker):
    def docker_exec(argv, check=True, timeout=240, log_tag="e2e",
                    input_bytes=None):
        # input_bytes forwarded: the in-container MCP health probe
        # rides its bearer on docker-exec STDIN
        return docker.docker(list(argv), timeout=timeout, check=check,
                             log_tag=log_tag or "e2e",
                             input_bytes=input_bytes)
    return docker_exec


def _e2e_host_exec(docker):
    def host_exec(argv, check=True, timeout=60, input_bytes=None, **_):
        # input_bytes MUST be forwarded: the firewall restore blob
        # rides STDIN (install_firewall's atomicity contract) —
        # dropping it makes the post-commit verify always fail.
        return docker.wsl_exec(list(argv), check=check, timeout=timeout,
                               input_bytes=input_bytes,
                               log_tag="e2e-host")
    return host_exec


def _e2e_hiclaw_docker_exec(docker):
    """HiClaw-side docker executor (Ubuntu-22.04): agent readiness,
    receipt live revalidation and canonical-store reads run against
    the same docker daemon the rewiring harness targeted. `mc cat`
    output logging is suppressed — those bodies carry agent tokens."""
    def hiclaw_exec(argv, check=True, timeout=60, log_tag="e2e-hiclaw",
                    **_):
        suppress = ("mc" in argv and "cat" in argv)
        return docker.docker(list(argv), timeout=timeout, check=check,
                             log_tag=log_tag or "e2e-hiclaw",
                             distro=HICLAW_DISTRO,
                             suppress_output_log=suppress)
    return hiclaw_exec


def _read_hiclaw_role_tokens(hiclaw_exec):
    """Real ROLE_TOKENS for the gateway: each agent's token is what
    that agent PRESENTS to the gateway. Manager carries it inline
    (mcporter.json mcpServers[*].headers.Authorization — the
    canonical store is the single authority for that file); the
    workers' rewired mcporter.json has NO header — their runtime
    injects HICLAW_WORKER_GATEWAY_KEY as the Bearer at mcporter
    call time (the gateway.py client contract), so the container
    env is the authority for workers. Read-only; bodies/tokens
    cross this boundary in-process only and are never logged —
    errors name the ROLE, never the token."""
    import e2e_executors as _ex
    worker_env = {"manager": "HICLAW_MANAGER_GATEWAY_KEY",
                  "reviewer": "HICLAW_WORKER_GATEWAY_KEY",
                  "fixer": "HICLAW_WORKER_GATEWAY_KEY",
                  "verifier": "HICLAW_WORKER_GATEWAY_KEY"}
    tokens = {}
    for role in ("manager", "reviewer", "fixer", "verifier"):
        token = ""
        # 1. canonical mcporter Authorization (manager path)
        key = _ex.HICLAW_CANONICAL_KEYS[role]
        cp = hiclaw_exec(["exec", "hiclaw-controller", "mc", "cat",
                          "hiclaw/hiclaw-storage/" + key],
                         check=False, timeout=30)
        if getattr(cp, "returncode", 1) == 0 and cp.stdout:
            try:
                doc = json.loads(cp.stdout.decode("utf-8", "replace"))
                for _name, srv in (doc.get("mcpServers")
                                   or {}).items():
                    auth = ((srv or {}).get("headers") or {}).get(
                        "Authorization", "")
                    if auth.startswith("Bearer ") \
                            and len(auth) > len("Bearer "):
                        token = auth[len("Bearer "):].strip()
                        break
            except (ValueError, AttributeError, UnicodeDecodeError):
                token = ""
        # 2. container env (worker path: runtime-injected bearer)
        if not token:
            container = _ex.HICLAW_ROLE_FREEZE[role][0]
            cp = hiclaw_exec(["exec", container, "printenv",
                              worker_env[role]],
                             check=False, timeout=20)
            if getattr(cp, "returncode", 1) == 0 and cp.stdout:
                token = cp.stdout.decode("utf-8", "replace").strip()
        if not token:
            raise Failure(
                "E2E_ROLE_TOKEN_EXTRACT_FAILED",
                "no gateway token for role %s (canonical mcporter "
                "and container env both empty)" % role,
                exit_code=EXIT_PRECHECK)
        tokens[role] = token
    return tokens


def _github_e2e_dry_run(planner, run_id):
    """§15: PURE E2E dry-run plan — returned BEFORE any Docker/WSL
    discovery, manifest requirement, distro start, prerequisite probe
    or file write. Zero side effects; zero secret values."""
    import e2e_runtime_specs as _rs
    import e2e_probes as _ep
    preview = e2f.build_b1_dry_run_preview(
        run_id=run_id, tuwunel_ip=e2f.E2E_TUWUNEL_DEFAULT_IP,
        room_map_host="<runtime-room-map-host-path>",
        policy_host="<runtime-fixture-policy-host-path>")
    multi_homed = {}
    for service in _rs.SERVICE_RUNTIME_SPECS:
        multi_homed[service] = {
            "env_file": _rs.SERVICE_RUNTIME_SPECS[service]["env_file"],
            "mounts": _rs.plan_runtime_mounts(service),
            "attachments": [
                {"network": net, "ip": ip, "gw_priority": priority}
                for net, ip, priority in
                _ep.E2E_CONTAINER_ATTACHMENTS.get(service, [])],
        }
    e2e_plans = {
        "activation_gate": preview["activation_gate"],
        "service_order": list(planner.E2E_SERVICE_ORDER),   # 11 services
        "networks_create": preview["networks_create"],      # 8 networks
        "multi_homed_containers": multi_homed,              # 6 CLI-owned
        "default_service_containers": [
            svc for svc in planner.E2E_SERVICE_ORDER
            if svc not in _rs.SERVICE_RUNTIME_SPECS],
        "firewall": preview["firewall"],
        "route_probes": preview.get("route_gate",
                                    preview.get("route_probe")),
        "wiring": {
            "gateway": preview["gateway_planning"],
            "bridge": preview["mcp_bridge_planning"],
            "reporter": preview["reporter_planning"],
            "proxy": preview["proxy_planning"],
            "hiclaw_harness": preview["hiclaw_harness_planning"],
        },
        "membership_preflight": preview["membership_preflight"],
    }
    return EXIT_OK, {
        "command": "start", "status": "dry-run", "code": EXIT_OK,
        "run_id": run_id,
        "github_e2e_plans": e2e_plans,
        "note": "pure plan — no Docker/WSL/prerequisite probe, no "
                "files written; the activation gate marker prevents "
                "a preview being mistaken for a mode",
    }


def _policy_repo_allowlist_from_config(config):
    """Single-repo allowlist string from the prerequisite config."""
    return config.get("fixture_repo", "")


def _build_e2e_runtime_configs(config, planner, reader_dsn,
                               audit_dsn, publisher_dsn, pat_value,
                               role_tokens=None, relay_endpoints=None,
                               controller_db_env=None,
                               controller_pg_pass="",
                               controller_admin_pw=""):
    """§4: the six authoritative runtime configs (values assembled
    from the validated 20-key prerequisite config and CLI-generated
    credentials; the PAT value is read ONLY after the prerequisite
    gate passed).

    role_tokens: the four agents' REAL gateway tokens extracted from
    the canonical mcporter store. ROLE_TOKENS must be valid JSON
    (the gateway json.loads it at startup) and must equal the agents'
    tokens or every agent SSE connect 401s — the placeholder value
    this replaced crashed the gateway deterministically.

    controller_db_env / controller_pg_pass / controller_admin_pw: the
    database contract the controller entrypoint refuses to start
    without (run27 finding: the container exited CONFIG_INVALID in
    milliseconds). controller_db_env carries the four non-secret keys
    (planner._controller_environment() — single source of truth);
    PG_PASS is the per-run POSTGRES_PASSWORD and ADMIN_PW an
    independent per-run secret; both ride github_ingress.env (the
    sanctioned env-file secret transport, never argv)."""
    import e2e_runtime_specs as _e2rs
    tokens = dict(role_tokens or {})
    coordinator_token = "tok-" + "e" * 32
    tokens.setdefault("coordinator", coordinator_token)
    if not controller_pg_pass or not controller_admin_pw:
        raise e2f.E2EConfigError(
            "CONFIG_INVALID",
            "controller secrets required: PG_PASS (per-run "
            "POSTGRES_PASSWORD) and ADMIN_PW must reach the E2E runtime "
            "config — without them the controller entrypoint exits "
            "CONFIG_INVALID before State.Running")
    db_env = dict(controller_db_env or {})
    missing_db = [k for k in ("PG_HOST", "PG_PORT", "PG_DATABASE",
                              "PG_USER") if not db_env.get(k)]
    if missing_db:
        raise e2f.E2EConfigError(
            "CONFIG_INVALID",
            "controller database contract keys missing: %s (use "
            "planner._controller_environment())" % sorted(missing_db))
    return {
        "controller": {
            "GITHUB_INGRESS_ENABLED": "1",
            "GITHUB_ROOM_MAP": "/run/mergepilot/room-map.yaml",
            "GITHUB_POLICY_PATH":
                "/run/mergepilot/policy-fixture.yaml",
            "GITHUB_DELIVERY_LEASE_SECONDS": "120",
            "GITHUB_DELIVERY_MAX_ATTEMPTS": "5",
            "MATRIX_HS": (relay_endpoints or {}).get(
                "controller", {}).get(
                    "MATRIX_HS", "http://matrix-hs:6167"),
            "MATRIX_SERVER_NAME": e2f.E2E_MATRIX_SERVER_NAME,
            "MATRIX_USER": e2f.E2E_CONTROLLER_MXID.split(":")[0][1:],
            "CONTROLLER_CONSUMER_NAME":
                e2f.E2E_CONTROLLER_MXID.split(":")[0][1:],
            "M4F_ALLOWED_ROOMS": config["matrix_room_id"],
            "M4F_ALLOWED_SENDERS": "manager,reviewer,fixer,verifier",
            "M4F_RUN_PREFIX": "gh-",
            "RESERVED_RUN_PREFIXES": "",
            "GATEWAY_URL": "http://policy-gateway:8083",
            "COORDINATOR_TOKEN": coordinator_token,
            "PG_HOST": db_env["PG_HOST"],
            "PG_PORT": db_env["PG_PORT"],
            "PG_DATABASE": db_env["PG_DATABASE"],
            "PG_USER": db_env["PG_USER"],
            "PG_PASS": controller_pg_pass,
            "ADMIN_PW": controller_admin_pw,
        },
        "policy-gateway": {
            "UPSTREAM_URL": (relay_endpoints or {}).get(
                "policy-gateway", {}).get(
                    "UPSTREAM_URL", _e2rs.GATEWAY_E2E_UPSTREAM),
            "POLICY_FILE": "/run/mergepilot/policy-fixture.yaml",
            "ROLE_TOKENS": json.dumps(tokens),
            "AUDIT_DSN": audit_dsn,
        },
        "mcp-bridge": {
            "GITHUB_PERSONAL_ACCESS_TOKEN": pat_value,
            "GITHUB_REPOSITORY": config["fixture_repo"],
            "HTTPS_PROXY": (relay_endpoints or {}).get(
                "mcp-bridge", {}).get(
                    "HTTPS_PROXY", _e2rs.BRIDGE_PROXY),
            "MCP_PROXY_PORT": "8082",
        },
        "gh-reporter": {
            "GITHUB_PUBLISHER_DSN": publisher_dsn,
            "GITHUB_API_BASE": "https://api.github.com",
            "GITHUB_APP_ID": config["app_id"],
            "GITHUB_INSTALLATION_ID": config["installation_id"],
            "GITHUB_REPOSITORY_ID": config["repository_id"],
            "GITHUB_PRIVATE_KEY_PATH":
                "/run/secrets/github-app-private-key.pem",
            "GH_REPORTER_POLL_SECONDS": "5",
            "GH_REPORTER_LEASE_SECONDS": "120",
            "GH_REPORTER_MAX_ATTEMPTS": "8",
            "HTTPS_PROXY": (relay_endpoints or {}).get(
                "gh-reporter", {}).get(
                    "HTTPS_PROXY", e2f.E2E_REPORTER_PROXY_R),
        },
        "gh-proxy-r": _proxy_env(
            config, (relay_endpoints or {}).get("gh-proxy-r")),
        "gh-proxy-b": _proxy_env(
            config, (relay_endpoints or {}).get("gh-proxy-b")),
    }


def _proxy_env(config, relay_overrides=None):
    overrides = relay_overrides or {}
    return {
        "GH_PROXY_BIND": "0.0.0.0",
        "GH_PROXY_PORT": "18090",
        "GH_PROXY_UPSTREAM_IP": overrides.get(
            "GH_PROXY_UPSTREAM_IP", config["windows_proxy_ip"]),
        "GH_PROXY_UPSTREAM_PORT": config["windows_proxy_port"],
    }


def _e2e_spec_env_file(service):
    import e2e_runtime_specs as _rs
    spec = _rs.SERVICE_RUNTIME_SPECS.get(service)
    return spec["env_file"] if spec else ""


#: The demo-console is a read-only VIEW over the showcase seed; its
#: MERGEPILOT_RUN_ID is a showcase case key the seed actually contains
#: (run32 finding: the E2E passed the session run id — e.g.
#: b8-e2e-run32 — which no seeded row matches, so the console's
#: startup probe exited RUN_NOT_FOUND; the default-mode contract is
#: "seeded run_id, e.g. run-showcase-a").
E2E_DEMO_CONSOLE_RUN_ID = "run-showcase-a"


def _e2e_demo_console_measured_argv(docker, planner, run_id, m4f,
                                    env_file_wsl, ctrl_env_wsl,
                                    reader_env_wsl, gh_env_wsl,
                                    session_public_dir=""):
    """The demo-console container argv rebuilt with the MEASURED
    postgres bridge IP (run31 finding: the E2E path served the
    PLACEHOLDER_BRIDGE_IP plan, so the console's expected-server-
    address identity check failed at startup; the default-mode start
    in _execute_start measures the IP after postgres is healthy —
    hardcoding it is forbidden by the planner contract). The console
    run id is the showcase case key, NOT the E2E session run id."""
    bridge_ip = docker.network_ip(container_name(planner, "postgres"))
    canonical = planner.canonicalize_server_address(bridge_ip)
    steps = build_start_steps(
        planner,
        env_file=env_file_wsl,
        controller_env_file=ctrl_env_wsl,
        reader_dsn_env_file=reader_env_wsl,
        gh_webhook_env_file=gh_env_wsl,
        run_id=E2E_DEMO_CONSOLE_RUN_ID, bridge_ip=canonical, m4f=m4f,
        session_public_dir=session_public_dir or None)
    for _kind, name, argv in steps:
        if _kind == "container-run" and name == "demo-console":
            return argv
    return []


def _execute_github_e2e_start(args, project_dir, planner, paths,
                              run_id):
    """§3: the REAL production E2E path.

    prerequisite config (20-key, real file probe — an absent config IS
    the GITHUB_E2E_PREREQUISITES_INCOMPLETE failure, never a fake
    unconditional raise) → install identities → session/journal →
    run_e2e_start with injected WslDocker executors. Any failure is
    uniformly rolled back by the lifecycle (owned resources only)."""
    import e2e_lifecycle as el
    import e2e_executors as ex_validate_hiclaw_receipt_mod

    ex_validate_hiclaw_receipt =         ex_validate_hiclaw_receipt_mod.validate_hiclaw_receipt

    try:
        config = el.load_e2e_prerequisite_config(
            paths["state"] / "github-e2e.json")
    except el.E2ELifecycleError as exc:
        # REAL prerequisite probe failure — safe detail (names +
        # codes only), zero side effects before this point.
        raise Failure(exc.code, exc.detail,
                      exit_code=EXIT_PRECHECK) from None

    install = load_manifest(paths["install"])
    if install is None:
        raise Failure("NOT_INSTALLED",
                      "%s missing (run `mergepilot install` first)"
                      % INSTALL_MANIFEST, exit_code=EXIT_PRECHECK)
    record_planner_image_identities(planner, install)
    image_refs = {}
    for service in ("controller", "policy-gateway", "mcp-bridge",
                    "gh-reporter", "gh-proxy-r", "gh-proxy-b"):
        # gh-reporter is a CONTAINER ROLE, not a built image: per the
        # e2e_foundation reporter planning contract it reuses the
        # gh-webhook image (entrypoint override at run time)
        base = ("gh-proxy" if service.startswith("gh-proxy")
                else "gh-webhook" if service == "gh-reporter"
                else service)
        image_refs[service] = (install.get("images") or {}).get(
            image_tag(planner, base), "")

    docker = WslDocker(planner, project_dir)
    docker_exec = _e2e_docker_exec(docker)
    host_exec = _e2e_host_exec(docker)
    # HiClaw-side executor (Ubuntu-22.04): the rewiring harness's
    # docker daemon — agents, canonical store and receipt targets
    # are invisible to the E2E distro's daemon
    hiclaw_exec = _e2e_hiclaw_docker_exec(docker)

    # ── §3 R3: the REAL read-only prerequisite gate runs BEFORE any
    # side effect. The four probe inputs come from production
    # adapters over REAL environment state (iptables-save text,
    # docker network subnet inventory, --gw-priority capability,
    # homeserver joined-members). On any failure NOTHING has been
    # written: no session manifest, no secret file, no PAT read.
    firewall_scan_text = el.fetch_firewall_scan_text(host_exec)
    existing_network_cidrs = el.fetch_existing_network_cidrs(
        docker_exec)
    docker_gw_priority_supported =         el.fetch_docker_gw_priority_supported(docker_exec)
    matrix_joined_mxids = el.fetch_matrix_joined_mxids(config)
    try:
        el.run_prerequisite_gate(
            config,
            docker_executor=docker_exec,
            host_executor=host_exec,
            matrix_joined_mxids=matrix_joined_mxids,
            docker_gw_priority_supported=docker_gw_priority_supported,
            existing_network_cidrs=existing_network_cidrs,
            firewall_scan_text=firewall_scan_text)
    except el.E2ELifecycleError as exc:
        raise Failure(exc.code, exc.detail,
                      exit_code=EXIT_PRECHECK) from None

    # Gate passed → session/journal, then secrets, then lifecycle.
    session = new_session(run_id, args.m4f, github_e2e=True)
    if getattr(args, "wsl_relay", False):
        session["transport_profile"] = "wsl-user-relay"
        session["direct_routing_verified"] = False
    session["hiclaw_receipt_path"] = config["hiclaw_receipt_path"]
    write_session(paths, session)

    runtime_directory = str(paths["secrets"])
    default_secret_written = []
    created_default_networks = {}

    def persist(s):
        write_session(paths, s)

    try:
        admin_pw = secrets.token_urlsafe(32)
        reader_pw = secrets.token_urlsafe(32)
        reader_dsn = ("postgresql://%s:%s@postgres:5432/%s"
                      "?application_name=%s"
                      % (planner.READER_ROLE, reader_pw, planner.DB_NAME,
                         planner.APP_NAME))
        gh_ingress_pw = secrets.token_urlsafe(24)
        gh_publisher_pw = secrets.token_urlsafe(24)
        publisher_dsn = GhWebhookSecretFile.build_ingress_dsn(
            gh_publisher_pw, user="github_check_publisher")
        audit_dsn = ("postgresql://audit:%s@postgres:5432/%s"
                     "?connect_timeout=5"
                     % (secrets.token_urlsafe(16), planner.DB_NAME))
        # PAT content is read only AFTER the prerequisite gate passed.
        pat_value = Path(config["mcp_pat_path"]).read_text(
            encoding="utf-8").strip()

        paths["secrets"].mkdir(parents=True, exist_ok=True)
        planner.SecretFile(paths["secrets"]).write(admin_pw, reader_pw)
        default_secret_written.append("postgres.env")
        planner.ReaderDsnSecretFile(paths["secrets"]).write(reader_dsn)
        default_secret_written.append("demo_console.env")
        # gh-webhook is one of the five default-mode services the
        # github-e2e DAG reuses; its planned run argv references
        # gh_webhook.env which only the default start path wrote
        # (first real run failed: env file absent). Values come from
        # the provisioned webhook secret file (body read in-process,
        # never logged) + a fresh per-run ingress DSN.
        _wh_secret = Path(config["webhook_secret_path"]
                          ).read_text(encoding="utf-8").strip()
        # no publisher_dsn here: GhWebhookSecretFile.write would also
        # emit gh_reporter.env, which the lifecycle's own
        # create_runtime_files owns (fresh per-run tokens) — writing
        # both in one run collides with itself
        GhWebhookSecretFile(paths["secrets"]).write(
            GhWebhookSecretFile.build_ingress_dsn(
                gh_ingress_pw, user="github_event_ingress"),
            _wh_secret,
            _policy_repo_allowlist_from_config(config))
        default_secret_written.append("gh_webhook.env")

        # the four agents' REAL gateway tokens (canonical store is
        # the single authority); read AFTER the gate, bodies never
        # logged — only the manager token value reaches the health
        # probe (STDIN of the in-distro probe process)
        role_tokens = _read_hiclaw_role_tokens(hiclaw_exec)
        # the gateway health probe authenticates as REVIEWER (its
        # policy exposure is exactly the frozen read-only set)
        role_tokens_reviewer = role_tokens["reviewer"]
        # Set the transport profile for runtime spec validation
        _relay_endpoints_for_specs = {}
        _relay_edges_for_derive = None
        if getattr(args, "wsl_relay", False):
            import e2e_relay as _relay_mod_ep
            _relay_edges_for_derive = _relay_mod_ep.build_relay_edge_contracts(
                config["tuwunel_ip"],
                windows_proxy_ip=config["windows_proxy_ip"],
                windows_proxy_port=int(config["windows_proxy_port"]))
            _relay_endpoints_for_specs = _relay_mod_ep.derive_relay_endpoints(
                _relay_edges_for_derive)
        import e2e_runtime_specs as _rs_profile
        _rs_profile.set_transport_profile(
            "wsl-user-relay" if getattr(args, "wsl_relay", False) else "",
            _relay_endpoints_for_specs)
        runtime_configs = _build_e2e_runtime_configs(
            config, planner, reader_dsn, audit_dsn, publisher_dsn,
            pat_value,
            role_tokens=role_tokens,
            relay_endpoints=_relay_endpoints_for_specs,
            controller_db_env=planner._controller_environment(),
            controller_pg_pass=admin_pw,
            controller_admin_pw=secrets.token_urlsafe(32))

        # The five non-spec DAG services reuse the default-mode plan
        # argv (create + connect steps executed in order).
        env_file_wsl = _to_wsl_path(paths["secrets"] / "postgres.env")
        ctrl_env_wsl = _to_wsl_path(paths["secrets"] / "controller.env")
        reader_env_wsl = _to_wsl_path(paths["secrets"] / "demo_console.env")
        gh_env_wsl = _to_wsl_path(paths["secrets"] / "gh_webhook.env")
        steps = build_start_steps(
            planner,
            env_file=env_file_wsl,
            controller_env_file=ctrl_env_wsl,
            reader_dsn_env_file=reader_env_wsl,
            gh_webhook_env_file=gh_env_wsl,
            run_id=run_id, bridge_ip=PLACEHOLDER_BRIDGE_IP,
            m4f=args.m4f,
            session_public_dir=_to_wsl_path(paths["state"] / "public"))
        by_service = {}
        network_create_steps = []
        for _kind, name, argv in steps:
            if _kind == "container-run":
                by_service[name] = argv
            if _kind == "network-create":
                network_create_steps.append(argv)

        def default_service_plan(service):
            # run31 finding: the default-mode start MEASURES the
            # postgres bridge IP after postgres is healthy and
            # regenerates the demo-console argv with it (a REQUIRED
            # planner input; hardcoding forbidden). The E2E path used
            # the placeholder-IP plan, so the console's expected-
            # server-address identity check failed at startup.
            # demo-console runs AFTER postgres in the frozen DAG
            # order, so the measurement is safe to make lazily here.
            if service == "demo-console":
                return _e2e_demo_console_measured_argv(
                    docker, planner, run_id, args.m4f,
                    env_file_wsl, ctrl_env_wsl, reader_env_wsl,
                    gh_env_wsl,
                    session_public_dir=_to_wsl_path(
                        paths["state"] / "public"))
            return by_service.get(service, [])

        def db_bootstrap():
            # the showcase seed generator comes from the same
            # versioned checkout as the planner (first real run
            # passed None -> AttributeError at the seed stage)
            _, _showcase = _load_planner(project_dir)
            prepare_database(docker, planner, _showcase,
                             project_dir, reader_pw)

        # §5 ownership-precedes-creation: journal the DETERMINISTIC
        # resource names (every planned container/network) BEFORE the
        # first docker command runs, so a crash between "docker run"
        # and the id-journaling below still leaves stop/rollback with
        # an exact ownership list — a Created-state orphan is then
        # removed by name, never guessed by glob.
        _session = load_manifest(paths["session"]) or {}
        _session["stack_owned_containers"] = sorted(
            ["mergepilot-isolated-%s-1" % s for s in by_service])
        _session["stack_owned_networks"] = sorted(
            [argv[-1] for argv in network_create_steps if len(argv) > 2])
        _session["stage"] = "start_pending"
        write_session(paths, _session)

        # the five default-mode services (postgres/gh-webhook/
        # demo-console/console-edge/preflight) reference the
        # default-mode networks in their planned argvs; the e2e
        # lifecycle only creates the 8 mp-e2e networks. Create the
        # two default-mode networks here (idempotent-safe: the
        # lifecycle rolls back only journaled mp-e2e networks, these
        # are managed by the default service plan's own cleanup)
        for argv in network_create_steps:
            cp = docker_exec(list(argv), check=False,
                             log_tag="e2e-net")
            if getattr(cp, "returncode", 1) == 0 and len(argv) > 2:
                # ownership = journaling: cleanup removes exactly what
                # WE created (a pre-existing network is rc!=0 or
                # already journaled)
                net = argv[-1]
                cid = docker_exec(
                    ["network", "inspect", net, "--format",
                     "{{.Id}}"], check=False, log_tag="e2e-net")
                nid = ((cid.stdout or b"").decode().strip()
                       if getattr(cid, "returncode", 1) == 0 else "")
                if nid:
                    created_default_networks[net] = nid

        # §10/§11 R3: the second checks bind the REAL production
        # implementations — homeserver joined-members (read-only) and
        # validate_hiclaw_receipt against live docker inspect.
        def _register_default_networks(_session):
            # ownership journaling for the two default-mode networks
            # this run created (rollback/cleanup removes exactly these)
            _session.setdefault("default_network_ids", {}).update(
                created_default_networks)
        # wsl-user-relay: build edge contracts + write relay script
        relay_edges = None
        if getattr(args, "wsl_relay", False):
            import e2e_relay as _relay_mod
            relay_edges = _relay_mod.build_relay_edge_contracts(
                config["tuwunel_ip"],
                windows_proxy_ip=config["windows_proxy_ip"],
                windows_proxy_port=int(config["windows_proxy_port"]))
            _relay_script = paths["secrets"] / "relay.py"
            _relay_script.write_text(_relay_mod.RELAY_SCRIPT,
                                     encoding="utf-8")
            session["relay_script_path"] = str(_relay_script)
            session["relay_edge_count"] = len(relay_edges)
        session = el.run_e2e_start(
            config=config,
            runtime_configs=runtime_configs,
            runtime_directory=runtime_directory,
            docker_executor=docker_exec,
            host_executor=host_exec,
            image_refs=image_refs,
            default_service_plan=default_service_plan,
            db_bootstrap=db_bootstrap,
            matrix_joined_mxids=matrix_joined_mxids,
            docker_gw_priority_supported=docker_gw_priority_supported,
            existing_network_cidrs=existing_network_cidrs,
            firewall_scan_text=firewall_scan_text,
            gateway_bearer=role_tokens_reviewer,
            agents_docker_executor=hiclaw_exec,
            transport_profile=session.get("transport_profile", ""),
            relay_edges=relay_edges if relay_edges else None,
            matrix_members_provider=(
                lambda: el.fetch_matrix_joined_mxids(config)),
            service_health=None,
            receipt_validator=(
                lambda path: ex_validate_hiclaw_receipt(
                    path, docker_executor=hiclaw_exec,
                    minio_executor=ex_validate_hiclaw_receipt_mod
                    .minio_readonly_via_docker(hiclaw_exec),
                    expected_old_mcp_state=config[
                        "expected_old_mcp_state"])),
            persist_callback=persist,
            session=session,
            env_file_resolver=(
                lambda service: _to_wsl_path(
                    paths["secrets"] /
                    _e2e_spec_env_file(service))),
        )
    except el.E2ELifecycleError as exc:
        for basename in default_secret_written:
            try:
                (paths["secrets"] / basename).unlink()
            except OSError:
                pass
        return EXIT_FAILED_CLEANED, {
            "command": "start", "status": "failed_rolled_back",
            "code": EXIT_FAILED_CLEANED, "run_id": run_id,
            "primary_code": exc.code,
            "primary_detail": _redact(exc.detail),
            "rollback_diagnostics": [
                _redact(d) for d in exc.diagnostics],
        }
    except Exception as exc:
        failure = _as_failure(exc)
        for basename in default_secret_written:
            try:
                (paths["secrets"] / basename).unlink()
            except OSError:
                pass
        for _net in list(created_default_networks):
            try:
                docker_exec(["network", "rm", _net], check=False,
                            log_tag="e2e-net")
            except Exception:
                pass
            created_default_networks.pop(_net, None)
        # §5: NEVER unlink the session on failure — the ownership
        # journal (stack_owned_*) is exactly what lets `stop` /
        # `cleanup --apply` remove a Created-state orphan later. Keep
        # the journal, marked failed, with ownership intact.
        try:
            _fail_session = load_manifest(paths["session"]) or {}
            _fail_session["stage"] = "start_failed"
            _fail_session["start_failure_code"] = getattr(
                failure, "code", "UNKNOWN")
            write_session(paths, _fail_session)
        except Exception:
            pass
        raise failure from None

    if created_default_networks:
        session.setdefault("default_network_ids", {}).update(
            created_default_networks)
        persist(session)
    return EXIT_OK, {
        "command": "start", "status": "ok", "code": EXIT_OK,
        "run_id": run_id,
        "resources": {
            "containers": session.get("e2e_container_ids", {}),
            "networks": session.get("e2e_network_ids", {}),
        },
    }


def cmd_start(args):
    project_dir = resolve_project_dir(args.project_dir)
    planner, showcase = _load_planner(project_dir)
    paths = state_paths(project_dir)

    run_id = args.run_id
    if not run_id or not _RUN_ID_RE.fullmatch(run_id):
        raise Failure("RUN_ID_INVALID",
                      "run_id must match ^[A-Za-z0-9_-]+$",
                      exit_code=EXIT_USAGE)

    # ── M8-GH-4B3-W3B-R2: E2E lifecycle (real production path) ──
    # Order: pure dry-run plan (no Docker discovery, always safe) →
    # honest component gate (development incomplete → a REAL start
    # fails closed BEFORE any config read / manifest load / Docker /
    # WSL / external probe) → strict 20-key prerequisite config →
    # run_prerequisite_gate → session/journal init → run_e2e_start.
    # The default mode below is untouched (seven-service path,
    # byte-identical behavior).
    if getattr(args, "github_e2e", False):
        if args.dry_run:
            return _github_e2e_dry_run(planner, run_id)
        if e2f.E2E_PENDING_COMPONENTS:
            raise Failure(
                "GITHUB_E2E_COMPONENTS_INCOMPLETE",
                "E2E lifecycle incomplete — pending: %s"
                % ", ".join(e2f.E2E_PENDING_COMPONENTS),
                exit_code=EXIT_PRECHECK)
        return _execute_github_e2e_start(
            args, project_dir, planner, paths, run_id)

    install = load_manifest(paths["install"])
    if install is None:
        raise Failure("NOT_INSTALLED",
                      "%s missing (run `mergepilot install` first)"
                      % INSTALL_MANIFEST, exit_code=EXIT_PRECHECK)
    record_planner_image_identities(planner, install)

    docker = WslDocker(planner, project_dir, allow_wake=True)
    # §2: bounded wake of a registered-but-dormant distro at
    # command entry (internal gates never restart mid-run)
    _entry_wake(docker)

    # ── conflict detection BEFORE any side effect ──
    session = load_manifest(paths["session"])
    snapshot = discover_stack(docker, planner)
    classification, detail = classify_stack(docker, planner, snapshot)
    secret_residue = [p.name for p in sorted(paths["secrets"].glob("*.env"))] \
        if paths["secrets"].is_dir() else []
    if session is None:
        if classification != "absent":
            raise Failure(
                "ORPHAN_STACK",
                "stack resources present without a session manifest (%s); "
                "ownership not guessed — run `mergepilot cleanup` after "
                "manual review" % detail, exit_code=EXIT_CONFLICT)
        if secret_residue:
            raise Failure("SECRET_RESIDUE",
                          "secret files present without a session: %s"
                          % secret_residue, exit_code=EXIT_CONFLICT)
    else:
        if classification == "healthy" and session.get("run_id") == run_id:
            log("stack already healthy with run_id=%s (idempotent)" % run_id)
            return EXIT_OK, {
                "command": "start", "status": "ok", "code": EXIT_OK,
                "run_id": run_id, "idempotent": True,
                "resources": {"stack": "healthy", "detail": detail},
            }
        if session.get("run_id") != run_id and classification != "absent":
            raise Failure("RUN_ID_MISMATCH",
                         "healthy/partial stack exists for run_id=%r; stop "
                         "or cleanup first" % session.get("run_id"),
                         exit_code=EXIT_CONFLICT)
        if classification != "absent":
            raise Failure("STACK_PARTIAL",
                          "partial stack present (%s); recover with "
                          "`mergepilot stop` or `mergepilot cleanup "
                          "--apply` (cleanup is dry-run unless --apply)"
                          % detail, exit_code=EXIT_CONFLICT)
        if session.get("stage") != "complete":
            # complete-manifest with absent resources = already stopped;
            # anything else is a stale journal from a failed rollback.
            raise Failure("STALE_SESSION",
                          "session stage=%r with absent resources"
                          % session.get("stage"),
                          exit_code=EXIT_CONFLICT)

    # ── dry-run: zero side effects ──
    if args.dry_run:
        steps = build_start_steps(
            planner,
            env_file=_to_wsl_path(paths["secrets"] / "postgres.env"),
            controller_env_file=_to_wsl_path(paths["secrets"]
                                             / "controller.env"),
            reader_dsn_env_file=_to_wsl_path(paths["secrets"]
                                             / "demo_console.env"),
            gh_webhook_env_file=_to_wsl_path(paths["secrets"]
                                             / "gh_webhook.env"),
            run_id=run_id, bridge_ip=PLACEHOLDER_BRIDGE_IP, m4f=args.m4f,
            session_public_dir=_to_wsl_path(paths["state"] / "public"))
        payload = {
            "command": "start", "status": "dry-run", "code": EXIT_OK,
            "run_id": run_id,
            "plans": [argv for _kind, _name, argv in steps],
            "note": "bridge IP %s is a placeholder — the real run measures "
                    "it after postgres is healthy" % PLACEHOLDER_BRIDGE_IP,
        }
        if getattr(args, "github_e2e", False):
            # pure plan data; zero side effects; carries the activation
            # gate marker so a preview can never be mistaken for a mode.
            payload["github_e2e_plans"] = e2f.build_b1_dry_run_preview(
                run_id=run_id, tuwunel_ip=e2f.E2E_TUWUNEL_DEFAULT_IP,
                room_map_host="<runtime-room-map-host-path>",
                policy_host="<runtime-fixture-policy-host-path>")
        return EXIT_OK, payload

    # ── environment gate (before any Docker write) ──
    require_environment(docker)
    if port_in_use(CONSOLE_PORT):
        raise Failure("PORT_BUSY",
                      "127.0.0.1:%d already in use" % CONSOLE_PORT,
                      exit_code=EXIT_PRECHECK)
    if port_in_use(planner.GH_WEBHOOK_PORT):
        raise Failure("PORT_BUSY",
                      "127.0.0.1:%d already in use (gh-webhook)"
                      % planner.GH_WEBHOOK_PORT,
                      exit_code=EXIT_PRECHECK)

    # ── secrets + journal, then the eleven-step plan ──
    admin_pw = secrets.token_urlsafe(32)
    controller_admin_pw = secrets.token_urlsafe(32)
    reader_pw = secrets.token_urlsafe(32)
    reader_dsn = ("postgresql://%s:%s@postgres:5432/%s"
                  "?application_name=%s"
                  % (planner.READER_ROLE, reader_pw, planner.DB_NAME,
                     planner.APP_NAME))
    m4f_dsn = None
    if args.m4f:
        m4f_pw = secrets.token_urlsafe(32)
        m4f_dsn = ("postgresql://snapshot_worker:%s@postgres:5432/%s"
                   % (m4f_pw, planner.DB_NAME))
    gh_ingress_pw = secrets.token_urlsafe(24)
    gh_publisher_pw = secrets.token_urlsafe(24)
    gh_webhook_secret = secrets.token_urlsafe(32)
    gh_ingress_dsn = GhWebhookSecretFile.build_ingress_dsn(gh_ingress_pw)
    gh_allowlist = _policy_repo_allowlist(project_dir)

    paths["secrets"].mkdir(parents=True, exist_ok=True)
    written = []
    try:
        planner.SecretFile(paths["secrets"]).write(admin_pw, reader_pw)
        written.append("postgres.env")
        planner.ControllerSecretFile(paths["secrets"]).write(
            admin_pw, controller_admin_pw, m4f_dsn)
        written.append("controller.env")
        planner.ReaderDsnSecretFile(paths["secrets"]).write(reader_dsn)
        written.append("demo_console.env")
        GhWebhookSecretFile(paths["secrets"]).write(
            gh_ingress_dsn, gh_webhook_secret, gh_allowlist,
            publisher_dsn=GhWebhookSecretFile.build_ingress_dsn(
                gh_publisher_pw, user="github_check_publisher"))
        written.append("gh_webhook.env")
        written.append("gh_reporter.env")
        # planner-side strict contract validation of the controller env-file
        # (same validator plan_orchestrated_start uses; Windows-side path).
        planner._validate_controller_env_file_contract(
            str(paths["secrets"] / "controller.env"),
            m4f_event_machinery=bool(args.m4f))
    except BaseException as exc:
        for basename in written:
            try:
                (paths["secrets"] / basename).unlink()
            except OSError:
                pass
        raise _as_failure(exc) from None

    session = new_session(run_id, args.m4f,
                          getattr(args, "github_e2e", False))
    write_session(paths, session)     # journal BEFORE the first Docker write

    env_file_wsl = _to_wsl_path(paths["secrets"] / "postgres.env")
    ctrl_env_wsl = _to_wsl_path(paths["secrets"] / "controller.env")
    reader_env_wsl = _to_wsl_path(paths["secrets"] / "demo_console.env")
    gh_env_wsl = _to_wsl_path(paths["secrets"] / "gh_webhook.env")

    primary = None
    try:
        _execute_start(docker, planner, showcase, project_dir, paths,
                       session, run_id, reader_pw,
                       env_file_wsl, ctrl_env_wsl, reader_env_wsl,
                       gh_env_wsl, gh_ingress_pw, gh_publisher_pw,
                       bool(args.m4f))
    except Failure as exc:
        primary = exc
    except KeyboardInterrupt:
        primary = Failure("INTERRUPTED", "start interrupted",
                          exit_code=EXIT_FAILED_CLEANED)
    except Exception as exc:
        # planner gate errors and anything else mid-execution go through the
        # SAME rollback path (converted, never swallowed).
        primary = _as_failure(exc)

    if primary is None:
        result = {
            "command": "start", "status": "ok", "code": EXIT_OK,
            "run_id": run_id,
            "resources": {
                "console": CONSOLE_URL,
                "containers": session["containers"],
                "networks": session["networks"],
            },
        }
        return EXIT_OK, result

    log("start failed (%s); capturing container diagnostics before "
        "rollback ..." % primary.code)
    diagnostics = capture_failure_diagnostics(docker, planner, paths,
                                              session)
    log("rolling back this session's resources ...")
    rb_codes = rollback_session(docker, planner, paths, session)
    payload = {
        "command": "start",
        "status": "failed_rolled_back" if not rb_codes else "failed_residue",
        "code": EXIT_FAILED_CLEANED if not rb_codes else EXIT_RESIDUE,
        "primary_code": primary.code,
        "primary_detail": _redact(primary.detail),
        "rollback_codes": rb_codes,
        "failure_diagnostics": diagnostics.get("summary"),
    }
    if diagnostics.get("file"):
        payload["diagnostics_file"] = diagnostics["file"]
    if rb_codes:
        return EXIT_RESIDUE, payload
    return EXIT_FAILED_CLEANED, payload


def _execute_start(docker, planner, showcase, project_dir, paths, session,
                   run_id, reader_pw, env_file_wsl, ctrl_env_wsl,
                   reader_env_wsl, gh_env_wsl, gh_ingress_pw,
                   gh_publisher_pw, m4f):
    """Sequential execution of the nine-step plan with per-step journaling.

    Steps 0-2 run first; the postgres bridge IP is MEASURED after postgres is
    healthy, then the remaining steps are generated (the measured IP is a
    REQUIRED planner input — hardcoding it is forbidden) and executed.
    """

    def journal_stage(stage):
        session["stage"] = stage
        write_session(paths, session)

    def create_network(net_name, argv):
        docker.docker(argv, timeout=120, check=True,
                      log_tag="network-create")
        state, nid = docker.inspect_id("network", net_name)
        if state != "present" or not nid:
            raise Failure("NETWORK_CREATE_VERIFY_FAILED", net_name,
                          exit_code=EXIT_FAILED_CLEANED)
        session["networks"][net_name] = nid
        write_session(paths, session)

    def create_container(svc, argv, *, healthy_timeout=240):
        docker.docker(argv, timeout=240, check=True, log_tag="run-%s" % svc)
        state, info = docker.container_state(container_name(planner, svc))
        if state != "present" or not info.get("id"):
            raise Failure("CONTAINER_CREATE_VERIFY_FAILED", svc,
                          exit_code=EXIT_FAILED_CLEANED)
        session["containers"][svc] = info["id"]
        write_session(paths, session)
        docker.wait_healthy(container_name(planner, svc), healthy_timeout)

    steps = build_start_steps(
        planner, env_file=env_file_wsl, controller_env_file=ctrl_env_wsl,
        reader_dsn_env_file=reader_env_wsl, gh_webhook_env_file=gh_env_wsl,
        run_id=run_id, bridge_ip=PLACEHOLDER_BRIDGE_IP, m4f=m4f,
        session_public_dir=_to_wsl_path(paths["state"] / "public"))

    journal_stage("networks")
    create_network(steps[0][1], steps[0][2])
    create_network(steps[1][1], steps[1][2])

    journal_stage("postgres")
    create_container("postgres", steps[2][2])

    bridge_ip = docker.network_ip(container_name(planner, "postgres"))
    canonical = planner.canonicalize_server_address(bridge_ip)
    log("measured postgres bridge IP: %s" % canonical)

    journal_stage("db_prepare")
    prepare_database(docker, planner, showcase, project_dir, reader_pw)

    journal_stage("gh_bootstrap")
    bootstrap_gh_roles(docker, planner, gh_ingress_pw, gh_publisher_pw)

    steps = build_start_steps(
        planner, env_file=env_file_wsl, controller_env_file=ctrl_env_wsl,
        reader_dsn_env_file=reader_env_wsl, gh_webhook_env_file=gh_env_wsl,
        run_id=run_id, bridge_ip=canonical, m4f=m4f,
        session_public_dir=_to_wsl_path(paths["state"] / "public"))

    journal_stage("services")
    create_container("policy-gateway", steps[3][2])
    create_container("controller", steps[4][2])

    def create_then_connect(svc, run_argv, connect_argv, connect_tag):
        """Run a publication-bridge service, connect its internal backend
        BEFORE waiting for health — the healthcheck probes cross-network
        upstreams (demo-console) that are unreachable until the connect
        executes. Waiting for health before the connect is a deadlock
        (baseline orchestration-order bug, present on main too).
        """
        docker.docker(run_argv, timeout=240, check=True,
                      log_tag="run-%s" % svc)
        state, info = docker.container_state(container_name(planner, svc))
        if state != "present" or not info.get("id"):
            raise Failure("CONTAINER_CREATE_VERIFY_FAILED", svc,
                          exit_code=EXIT_FAILED_CLEANED)
        session["containers"][svc] = info["id"]
        write_session(paths, session)
        docker.docker(connect_argv, timeout=60, check=True,
                      log_tag=connect_tag)
        docker.wait_healthy(container_name(planner, svc), 240)

    create_then_connect("gh-webhook", steps[5][2], steps[6][2],
                        "network-connect-gh")
    create_container("demo-console", steps[7][2])
    create_then_connect("console-edge", steps[8][2], steps[9][2],
                        "network-connect")

    journal_stage("preflight")
    docker.docker(steps[10][2], timeout=240, check=True, log_tag="run-preflight")
    state, info = docker.container_state(container_name(planner, "preflight"))
    if state == "present" and info.get("id"):
        session["containers"]["preflight"] = info["id"]
        write_session(paths, session)
    exit_code = docker.wait_exited(container_name(planner, "preflight"), 300)
    if exit_code != 0:
        raise Failure("PREFLIGHT_FAILED",
                      "preflight container exit=%d" % exit_code,
                      exit_code=EXIT_FAILED_CLEANED)
    logs = docker.container_logs(container_name(planner, "preflight"))
    lines = [ln for ln in logs.splitlines() if ln.strip()]
    if not lines or lines[-1].strip() != "PREFLIGHT_OK":
        raise Failure("PREFLIGHT_OUTPUT_INVALID",
                      "last log line is not PREFLIGHT_OK",
                      exit_code=EXIT_FAILED_CLEANED)
    journal_stage("complete")


def cmd_status(args):
    project_dir = resolve_project_dir(args.project_dir)
    planner, _showcase = _load_planner(project_dir)
    docker = WslDocker(planner, project_dir, allow_wake=True)
    # §2: bounded wake of a registered-but-dormant distro at
    # command entry (internal gates never restart mid-run)
    _entry_wake(docker)
    paths = state_paths(project_dir)

    snapshot = discover_stack(docker, planner)
    classification, detail = classify_stack(docker, planner, snapshot)
    session = load_manifest(paths["session"])
    install = load_manifest(paths["install"])

    resources = {
        "containers": {svc: (info["state"] if info["state"] == "absent" else
                             info.get("status", info["state"]))
                       for svc, info in snapshot["containers"].items()},
        "networks": {net: n["state"]
                     for net, n in snapshot["networks"].items()},
    }
    meta = {}
    if session is not None:
        meta["session"] = {
            "run_id": session.get("run_id"),
            "stage": session.get("stage"),
            "m4f": session.get("m4f"),
            "github_e2e": bool(session.get("github_e2e")),
        }
        # M8-GH-4B3-W3B-R2 §13: a REAL E2E session gets the sanitized
        # 11-service lifecycle status via run_e2e_status (read-only).
        # Default-mode status keys are unchanged — this only ADDS the
        # github_e2e_services key under the E2E-session condition.
        if session.get("github_e2e"):
            import e2e_lifecycle as el
            # the gateway bearer is re-extracted read-only from the
            # HiClaw side (same authority as start); the probes exec
            # inside the target containers via the docker executor
            try:
                status_bearer = _read_hiclaw_role_tokens(
                    _e2e_hiclaw_docker_exec(docker))["reviewer"]
            except Failure:
                status_bearer = ""
            meta["github_e2e_services"] = el.run_e2e_status(
                docker_executor=_e2e_docker_exec(docker),
                session=session,
                gateway_bearer=status_bearer)
    if install is not None:
        meta["install_images"] = sorted((install.get("images") or {}).keys())
    if classification != "absent" and session is None:
        meta["ownership"] = "conflict: resources present without a session " \
                            "manifest"

    exit_code = EXIT_OK if classification in ("absent", "healthy") \
        else EXIT_PRECHECK
    # M8-GH-3: ingress queue summary when a stack is running (read-only
    # counts only — never payload/DSN/secret values).
    if classification == "healthy":
        try:
            pg = container_name(planner, "postgres")
            deliveries = docker.docker(
                ["exec", pg, "psql", "-U", "mergepilot",
                 "-d", planner.DB_NAME, "-At", "-c",
                 "SELECT count(*) FROM github_deliveries"],
                check=False, log_tag="status-deliveries")
            outbox = docker.docker(
                ["exec", pg, "psql", "-U", "mergepilot",
                 "-d", planner.DB_NAME, "-At", "-c",
                 "SELECT count(*) FROM github_check_outbox"],
                check=False, log_tag="status-outbox")
            def _count(cp):
                try:
                    return int((cp.stdout or b"").decode(
                        "utf-8", "replace").strip() or "-1")
                except ValueError:
                    return -1
            meta["github_ingress"] = {
                "deliveries": _count(deliveries),
                "check_outbox": _count(outbox),
            }
        except Failure:
            meta["github_ingress"] = {"deliveries": -1, "check_outbox": -1}
    return exit_code, {
        "command": "status", "status": classification, "code": exit_code,
        "detail": detail,
        "resources": resources, **meta,
    }


def _as_failure(exc):
    """Map any raised exception to a Failure (planner gate errors keep their
    stable code; unknown errors become a fail-closed INTERNAL_ERROR)."""
    if isinstance(exc, Failure):
        return exc
    planner = _PLANNER
    if planner is not None and isinstance(exc, planner.StartupGateError):
        return Failure(exc.code, str(exc), exit_code=EXIT_PRECHECK)
    return Failure("INTERNAL_ERROR",
                   "%s: %s" % (type(exc).__name__, exc),
                   exit_code=EXIT_PRECHECK)


def _verify_against_manifest(session, kind, key, discovered_state,
                             discovered_id):
    """fail-closed ownership check: present resource must match manifest ID."""
    if discovered_state != "present":
        return
    recorded = (session.get(kind) or {}).get(key)
    if not recorded:
        raise Failure("OWNERSHIP_UNKNOWN",
                      "%s %s present but not in session manifest" % (kind, key),
                      exit_code=EXIT_CONFLICT)
    if (discovered_id or "").strip() != recorded.strip():
        raise Failure("OWNERSHIP_MISMATCH",
                      "%s %s resolves to a different ID than the manifest "
                      "(refusing to delete)" % (kind, key),
                      exit_code=EXIT_CONFLICT)


def cmd_stop(args):
    project_dir = resolve_project_dir(args.project_dir)
    planner, _showcase = _load_planner(project_dir)
    docker = WslDocker(planner, project_dir, allow_wake=True)
    # §2: bounded wake of a registered-but-dormant distro at
    # command entry (internal gates never restart mid-run)
    _entry_wake(docker)
    paths = state_paths(project_dir)

    session = load_manifest(paths["session"])
    snapshot = discover_stack(docker, planner)
    any_present = (any(c["state"] == "present"
                       for c in snapshot["containers"].values())
                   or any(n["state"] == "present"
                          for n in snapshot["networks"].values()))

    if session is None:
        if any_present:
            raise Failure(
                "ORPHAN_STACK",
                "stack resources present without a session manifest; "
                "ownership not guessed — inspect manually before removing",
                exit_code=EXIT_CONFLICT)
        return EXIT_OK, {
            "command": "stop", "status": "ok", "code": EXIT_OK,
            "idempotent": True, "detail": "nothing to stop",
        }

    plan = planner.plan_orchestrated_cleanup()
    if getattr(args, "dry_run", False):
        return EXIT_OK, {
            "command": "stop", "status": "dry-run", "code": EXIT_OK,
            "plans": plan,
            "note": "reverse-order removal + secret deletion; install "
                    "manifest and images are kept",
        }

    # M8-GH-4B3-W3B-R2 §14: a REAL E2E session stops through
    # run_e2e_stop (owned containers → firewall → networks → runtime
    # files → residue verification). The E2E journal owns the
    # resources; the default-mode manifest plan never runs here
    # (its OWNERSHIP_UNKNOWN guard targets default-mode journals).
    if session.get("github_e2e"):
        import e2e_lifecycle as el
        result = el.run_e2e_stop(
            docker_executor=_e2e_docker_exec(docker),
            host_executor=_e2e_host_exec(docker),
            session=session,
            runtime_directory=str(paths["secrets"]),
            persist_callback=lambda s: write_session(paths, s))
        residue = list(result["residue"])
        for basename in session.get("secrets", []):
            secret_path = paths["secrets"] / basename
            try:
                if secret_path.exists():
                    secret_path.unlink()
                if secret_path.exists():
                    residue.append(
                        "SECRET_FILE_STILL_PRESENT:%s" % basename)
            except OSError:
                residue.append("SECRET_DELETE_FAILED:%s" % basename)
        if not residue:
            try:
                if paths["session"].exists():
                    paths["session"].unlink()
            except OSError:
                residue.append("SESSION_MANIFEST_DELETE_FAILED")
        if residue:
            return EXIT_RESIDUE, {
                "command": "stop", "status": "failed_residue",
                "code": EXIT_RESIDUE,
                "residue_codes": residue,
                "diagnostics": result["diagnostics"],
            }
        return EXIT_OK, {
            "command": "stop", "status": "ok", "code": EXIT_OK,
            "actions": result["actions"],
            "kept": ["install manifest", "local images"],
        }

    # M8-GH-4B1: session-owned firewall pins are removed AFTER the owned
    # containers are gone and BEFORE the E2E networks (R4 stop order);
    # only journaled argvs run (ownership never guessed).
    pins_pending = [list(a) for a in
                    reversed(session.get("firewall_teardown") or [])]
    pin_failures = []

    def _drain_pins():
        while pins_pending:
            pin_argv = pins_pending.pop(0)
            try:
                docker.wsl_exec(pin_argv, check=False, timeout=30,
                                log_tag="unpin")
            except Exception as exc:
                pin_failures.append("PIN_TEARDOWN_FAILED:(%s)"
                                    % type(exc).__name__)

    # Ownership verification BEFORE each delete (fixed names from the
    # planner's own cleanup plan; IDs cross-checked against the manifest).
    for argv in plan:
        if argv[0] == "rm":
            name = argv[argv.index("-fv") + 1]
            svc = name[len("mergepilot-isolated-"):-len("-1")]
            state, info = docker.container_state(name)
            _verify_against_manifest(session, "containers",
                                     svc, state, info.get("id"))
            if state == "absent":
                log("already absent: %s" % name)
                continue
            docker.docker(argv, timeout=120, check=True, log_tag="rm")
        elif argv[0] == "network" and argv[1] == "rm":
            if pins_pending:
                _drain_pins()
            net = argv[2]
            state, nid = docker.inspect_id("network", net)
            _verify_against_manifest(session, "networks",
                                     net, state, nid)
            if state == "absent":
                log("already absent: network %s" % net)
                continue
            docker.docker(argv, timeout=60, check=True,
                          log_tag="network-rm")
        else:
            raise Failure("CLEANUP_PLAN_INVALID", "unknown plan step",
                          exit_code=EXIT_RESIDUE)

    if pins_pending:
        _drain_pins()
    residue = list(pin_failures)
    after = discover_stack(docker, planner)
    for svc, info in after["containers"].items():
        if info["state"] == "present":
            residue.append("CONTAINER_STILL_PRESENT:%s" % svc)
    for net, info in after["networks"].items():
        if info["state"] == "present":
            residue.append("NETWORK_STILL_PRESENT:%s" % net)
    for basename in session.get("secrets", []):
        secret_path = paths["secrets"] / basename
        try:
            if secret_path.exists():
                secret_path.unlink()
            if secret_path.exists():
                residue.append("SECRET_FILE_STILL_PRESENT:%s" % basename)
        except OSError:
            residue.append("SECRET_DELETE_FAILED:%s" % basename)
    if not residue:
        try:
            if paths["session"].exists():
                paths["session"].unlink()
        except OSError:
            residue.append("SESSION_MANIFEST_DELETE_FAILED")
        if paths["session"].exists():
            residue.append("SESSION_MANIFEST_STILL_PRESENT")
    # M8-GH-3 §1: owned 诊断临时文件同属清理范围(cleanup 删除)。
    diag = paths["state"] / "diagnostics.json"
    try:
        if diag.exists():
            diag.unlink()
    except OSError:
        residue.append("DIAGNOSTICS_DELETE_FAILED")
    if diag.exists():
        residue.append("DIAGNOSTICS_STILL_PRESENT")
    if residue:
        return EXIT_RESIDUE, {
            "command": "stop", "status": "failed_residue",
            "code": EXIT_RESIDUE, "residue_codes": residue,
        }
    return EXIT_OK, {
        "command": "stop", "status": "ok", "code": EXIT_OK,
        "kept": ["install manifest", "local images"],
    }


def cmd_cleanup(args):
    project_dir = resolve_project_dir(args.project_dir)
    planner, _showcase = _load_planner(project_dir)
    docker = WslDocker(planner, project_dir, allow_wake=True)
    # §2: bounded wake of a registered-but-dormant distro at
    # command entry (internal gates never restart mid-run)
    _entry_wake(docker)
    paths = state_paths(project_dir)

    install = load_manifest(paths["install"])
    session = load_manifest(paths["session"])
    snapshot = discover_stack(docker, planner)

    stop_plan = planner.plan_orchestrated_cleanup() \
        if session is not None else []
    image_plan = []
    if install is not None:
        for tag, img_id in sorted((install.get("images") or {}).items()):
            image_plan.append(["rmi", img_id])

    # M8-GH-4B3-W3B-R2 §14: E2E session → the lifecycle residue scan
    # is the cleanup entry (11 containers, 8 networks, firewall
    # chains, runtime files, route-probe containers). Unowned
    # resources are REPORTED, never guessed-deleted; a non-empty
    # residue maps to a stable non-zero exit. Default-mode cleanup
    # behavior is unchanged.
    e2e_residue = []
    if session is not None and session.get("github_e2e"):
        import e2e_lifecycle as el
        e2e_residue = el.run_e2e_cleanup(
            docker_executor=_e2e_docker_exec(docker),
            host_executor=_e2e_host_exec(docker),
            runtime_directory=str(paths["secrets"]))["residue"]

    if not args.apply:
        return EXIT_OK, {
            "command": "cleanup", "status": "dry-run", "code": EXIT_OK,
            "plans": stop_plan + image_plan,
            "deletes": [INSTALL_MANIFEST, SESSION_MANIFEST,
                        "secret env files"],
            **({"github_e2e_residue": e2e_residue}
               if session is not None and session.get("github_e2e")
               else {}),
            "note": "dry-run (default): nothing executed; pass --apply to "
                    "stop the stack, remove the 5 verified-ID local images "
                    "and the install manifest",
        }

    # 1) stop (idempotent; ownership-verified)
    stop_rc, stop_result = cmd_stop(args)
    if stop_rc != EXIT_OK:
        return stop_rc, {
            "command": "cleanup", "status": "stop_failed",
            "code": stop_rc, "stop": stop_result,
            "residue_codes": stop_result.get("residue_codes", []),
        }

    residue = list(e2e_residue)
    # 2) verified-ID image removal
    if install is not None:
        for tag, expected_id in sorted((install.get("images") or {}).items()):
            current = docker.image_id(tag)
            if current is None:
                log("image already absent: %s" % tag)
                continue
            if current.strip() != expected_id.strip():
                return EXIT_CONFLICT, {
                    "command": "cleanup", "status": "failed_conflict",
                    "code": EXIT_CONFLICT,
                    "conflict": "image %s resolves to a different ID than "
                                "the install manifest (refusing to delete)"
                                % tag,
                }
            cp = docker.docker(["rmi", expected_id], check=False,
                               timeout=300, log_tag="rmi")
            if cp.returncode != 0 and docker.image_id(tag) is not None:
                residue.append("IMAGE_STILL_PRESENT:%s" % tag)
        try:
            if paths["install"].exists():
                paths["install"].unlink()
        except OSError:
            residue.append("INSTALL_MANIFEST_DELETE_FAILED")
        if paths["install"].exists():
            residue.append("INSTALL_MANIFEST_STILL_PRESENT")

    # 3) M8-GH-4B1 firewall rule residue scan — only for E2E sessions
    # (default-mode cleanup behavior stays byte-identical).
    if session is not None and session.get("github_e2e"):
        try:
            cp = docker.wsl_exec(["iptables-save"], check=False, timeout=30,
                                 log_tag="fw-scan")
            if cp.returncode == 0:
                text = (cp.stdout or b"").decode("utf-8", "replace")
                fw_residue = e2f.residue_scan(text)
                if fw_residue:
                    residue.extend(fw_residue)
            else:
                residue.append("FIREWALL_SCAN_FAILED")
        except Failure:
            residue.append("FIREWALL_SCAN_UNAVAILABLE")

    # 4) residue verification (fail-closed: 9)
    for tag in (install.get("images") or {}) if install else {}:
        if docker.image_id(tag) is not None:
            residue.append("IMAGE_STILL_PRESENT:%s" % tag)
    if paths["session"].exists():
        residue.append("SESSION_MANIFEST_STILL_PRESENT")
    # M8-GH-3: sweep the CLI-owned runtime secrets area (session stop
    # already removes the journaled files; this catches orphans).
    if paths["secrets"].is_dir():
        for leftover in sorted(paths["secrets"].glob("*.env")):
            try:
                leftover.unlink()
            except OSError:
                residue.append("SECRET_DELETE_FAILED:%s" % leftover.name)
        for leftover in sorted(paths["secrets"].glob("*.env")):
            residue.append("SECRET_FILE_STILL_PRESENT:%s" % leftover.name)
    if residue:
        return EXIT_RESIDUE, {
            "command": "cleanup", "status": "failed_residue",
            "code": EXIT_RESIDUE, "residue_codes": sorted(set(residue)),
        }
    return EXIT_OK, {
        "command": "cleanup", "status": "ok", "code": EXIT_OK,
        "removed": ["session containers", "networks", "secret env files",
                    "5 local images", "install manifest"],
    }


# ── CLI surface ──────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="mergepilot",
        description="MergePilot isolated-stack local CLI "
                    "(development preview; Windows 10/11 + WSL2 "
                    "MergePilot-Test only)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-dir", default=None,
                        help="MergePilot checkout (default: current dir)")
    common.add_argument("--json", action="store_true",
                        help="stable machine-readable output on stdout")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("install", parents=[common],
                       help="build the 5 local images and record image IDs")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("doctor", parents=[common],
                       help="read-only environment and stack checks")
    p.add_argument("--github-e2e", action="store_true",
                   help="add the GitHub E2E foundation checks (read-only "
                        "planning capability; no side effects)")
    p = sub.add_parser("status", parents=[common],
                       help="absent/partial/healthy classification")

    p = sub.add_parser("start", parents=[common],
                       help="run the isolated stack (preflight-gated)")
    p.add_argument("--run-id", required=True,
                   help="seeded run_id, e.g. run-showcase-a")
    p.add_argument("--m4f", action="store_true",
                   help="enable the M4F event machinery (opt-in)")
    p.add_argument("--github-e2e", action="store_true",
                   help="plan the GitHub E2E controller/Matrix slice "
                        "(B1: dry-run planning only — a REAL start fails "
                        "closed with GITHUB_E2E_PREREQUISITES_INCOMPLETE "
                        "(external readiness gate))")
    p.add_argument("--wsl-relay", action="store_true",
                   help="use the wsl-user-relay transport profile: "
                        "cross-bridge edges via user-space TCP relays "
                        "(bypasses the broken WSL 6.18 IP FORWARD). "
                        "Evidence carries transport_profile=wsl-user-relay, "
                        "direct_routing_verified=false")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("stop", parents=[common],
                       help="remove session containers/networks/secrets")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("cleanup", parents=[common],
                       help="stop + remove local images + install manifest")
    p.add_argument("--apply", action="store_true",
                   help="execute (default is dry-run)")
    return parser


_COMMANDS = {
    "install": cmd_install,
    "doctor": cmd_doctor,
    "start": cmd_start,
    "status": cmd_status,
    "stop": cmd_stop,
    "cleanup": cmd_cleanup,
}


def main(argv=None):
    global _JSON_MODE
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    _JSON_MODE = bool(getattr(args, "json", False))
    try:
        code, result = _COMMANDS[args.command](args)
    except Failure as failure:
        payload = {
            "command": args.command,
            "status": "failed",
            "code": failure.exit_code,
            "error_code": failure.code,
            "error_detail": _redact(failure.detail),
        }
        if _JSON_MODE:
            print(json.dumps(payload, indent=2))
        else:
            log("FAILED %s: %s" % (failure.code, failure.detail))
        return failure.exit_code
    except Exception as exc:  # fail-closed CLI boundary (no tracebacks)
        failure = _as_failure(exc)
        if _JSON_MODE:
            print(json.dumps({"command": args.command, "status": "failed",
                              "code": failure.exit_code,
                              "error_code": failure.code,
                              "error_detail": failure.detail}, indent=2))
        else:
            log("FAILED %s: %s" % (failure.code, failure.detail))
        return failure.exit_code
    if _JSON_MODE:
        print(json.dumps(result, indent=2))
    else:
        log(_status_line(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
