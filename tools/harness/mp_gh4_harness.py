"""M8-GH-4B3 HiClaw Rewiring Harness (mp-gh4-harness).

Formal, testable, rollback-capable implementation of the rewiring
step the B3 planning contract always referenced but was never
delivered ("operator-authorized script; NOT part of mergepilot CLI
ownership").

Single-authority contracts (imported, never duplicated here):
- HICLAW_ROLE_FREEZE / hiclaw_role_gateway_url  (e2e_executors)
- E2E_MATRIX_SERVER_NAME                        (e2e_foundation)

Commands: inspect (default, read-only) | plan (read-only) | apply
(requires explicit --apply) | verify | rollback | status.

Transaction: read state -> validate 4 roles -> capture before hash +
in-container backup -> persist journal -> apply one role -> verify ->
persist progress -> ... -> verify all four -> generate receipt
atomically -> validate receipt with the PRODUCTION validator ->
commit complete state. Any failure rolls back in reverse order; a
rollback failure NEVER replaces the primary apply error.

Every external operation goes through an injectable adapter. The
harness never reads PAT/PEM files, never touches Matrix membership,
never starts/stops/deletes the old github-mcp, never creates or
renames containers, and never prints token/config bodies (hashes and
gateway URLs only — both are receipt fields by contract).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_TOOLS_CLI = str(Path(__file__).resolve().parents[1] / "cli")
if _TOOLS_CLI not in sys.path:
    sys.path.insert(0, _TOOLS_CLI)

import e2e_executors as ex                       # noqa: E402
import e2e_foundation as e2f                     # noqa: E402

HARNESS_IDENTITY = "mp-gh4-harness"
ROLES = ("manager", "reviewer", "fixer", "verifier")

#: mcporter config path per role (frozen; same paths the production
#: receipt validator probes with sha256sum).
MCPORTER_PATH = {
    "manager": "/root/manager-workspace/config/mcporter.json",
    "reviewer": "/root/hiclaw-fs/agents/reviewer/config/mcporter.json",
    "fixer": "/root/hiclaw-fs/agents/fixer/config/mcporter.json",
    "verifier": "/root/hiclaw-fs/agents/verifier/config/mcporter.json",
}

BACKUP_SUFFIX = ".mp-gh4-bak"

_URL_RE = re.compile(r"https?://[^\"'\s]+")


class HarnessError(Exception):
    """Stable-code error; detail is always sanitized (no config
    bodies, no tokens, no secrets)."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


# ── adapters (all injectable; production defaults hit docker) ─────────────

class DockerAdapter:
    """Production adapter: docker inspect/exec via argv lists only
    (never shell=True, never secret-bearing argv)."""

    def __init__(self, docker_executor: Callable):
        self._exec = docker_executor
        self.calls = []          # sanitized argv audit (no secrets)

    def _checked(self, argv, *, input_bytes=None):
        self.calls.append(list(argv))
        cp = self._exec(argv, check=True, input_bytes=input_bytes)
        if getattr(cp, "returncode", 0) != 0:
            raise HarnessError("HARNESS_APPLY_FAILED",
                               "%s rc=%d" % (argv[0], cp.returncode))
        return cp

    def inspect_format(self, name: str, fmt: str) -> str:
        cp = self._exec(["inspect", name, "--format", fmt], check=True)
        return (cp.stdout or b"").decode("utf-8", "replace").strip()

    def read_config(self, container: str, path: str) -> bytes:
        cp = self._checked(["exec", container, "cat", path])
        return cp.stdout or b""

    def write_config(self, container: str, path: str,
                     data: bytes) -> None:
        self._checked(
            ["exec", "-i", container, "sh", "-c",
             "cat > %s" % path],      # path is a frozen constant
            input_bytes=data)

    def backup_config(self, container: str, path: str,
                      backup_path: str) -> None:
        self._checked(["exec", container, "cp", path, backup_path])

    def restore_config(self, container: str, backup_path: str,
                       path: str) -> None:
        self._checked(["exec", container, "cp", backup_path, path])

    def remove_backup(self, container: str, backup_path: str) -> None:
        """Remove a journal-owned backup file. NEVER silently
        fails: rc != 0 (or a transport error) raises
        HARNESS_BACKUP_REMOVE_FAILED with the safe ROLE name only —
        no stderr, no backup body, no path secrets."""
        role = next((r for r, f in ex.HICLAW_ROLE_FREEZE.items()
                     if f[0] == container), container)
        try:
            cp = self._exec(
                ["exec", container, "rm", "-f", backup_path],
                check=False)
        except OSError:
            raise HarnessError("HARNESS_BACKUP_REMOVE_FAILED",
                               role) from None
        if getattr(cp, "returncode", 0) != 0:
            raise HarnessError("HARNESS_BACKUP_REMOVE_FAILED", role)

    def sha256_file(self, container: str, path: str) -> str:
        cp = self._exec(["exec", container, "sha256sum", path],
                        check=True)
        out = (cp.stdout or b"").decode("utf-8", "replace").strip()
        return out.split()[0] if out else ""


class AtomicFileWriter:
    """Host-side atomic writer with 0600/ACL-restricted files and
    symlink/reparse/path-escape refusal."""

    @staticmethod
    def _safe_path(path: Path, root: Optional[Path] = None) -> None:
        p = str(path)
        if os.path.islink(p):
            raise HarnessError("HARNESS_REPARSE_REFUSED", path.name)
        try:
            if getattr(path.lstat(), "st_reparse_tag", 0):
                raise HarnessError("HARNESS_REPARSE_REFUSED",
                                   path.name)
        except FileNotFoundError:
            pass          # first write: target not created yet
        if root is not None:
            real = os.path.realpath(p)
            root_real = os.path.realpath(str(root))
            me = os.path.normcase(real)
            rt = os.path.normcase(root_real)
            if not (me == rt or me.startswith(rt + os.sep)):
                raise HarnessError("HARNESS_PATH_ESCAPE_REFUSED",
                                   path.name)

    @classmethod
    def write(cls, path, data: bytes, *, root: Path = None) -> None:
        path = Path(path)
        cls._safe_path(path, root)
        tmp = str(path) + ".harness-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def read(cls, path) -> bytes:
        path = Path(path)
        cls._safe_path(path)
        return path.read_bytes()


    @classmethod
    def write_exclusive(cls, path, data: bytes, *,
                        root: Path = None) -> None:
        """Exclusive-create publish (receipt contract, R3):

        - target absent -> atomic creation (same-dir exclusive temp
          + os.link publish; link fails if ANY competitor created the
          target after our preflight — never overwrites/truncates)
        - target present -> OSError (mapped by the caller to
          HARNESS_RECEIPT_EXISTS)
        - 0600 on success; temp cleaned on any failure
        - journal keeps its overwrite-capable atomic write (untouched)
        """
        path = Path(path)
        cls._safe_path(path, root)
        if path.exists():
            raise FileExistsError(str(path))
        tmp = str(path) + ".harness-xcl"
        # exclusive temp creation in the SAME controlled directory
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # atomic exclusive publish: hard-link fails with
            # FileExistsError if a competitor won the race
            os.link(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.unlink(tmp)
        except OSError:
            pass
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _canonical_sha256(receipt: dict) -> str:
    """Production-canonical receipt hash (identical rules to
    ex._compute_receipt_sha256; that helper stays the authority —
    this delegates to it)."""
    return ex._compute_receipt_sha256(receipt)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── read-only state inspection ────────────────────────────────────────────

def inspect_roles(docker: DockerAdapter) -> dict:
    """Read-only four-role + old-mcp state inventory."""
    state = {"harness": HARNESS_IDENTITY,
             "observed_utc": _now_iso(), "roles": {}, "old_github_mcp": {}}
    for role in ROLES:
        container, mxid, ip, _path = ex.HICLAW_ROLE_FREEZE[role]
        live_id = docker.inspect_format(container, "{{.Id}}")
        running = docker.inspect_format(
            container, "{{.State.Running}}").lower() == "true"
        live_ip = docker.inspect_format(
            container,
            "{{(index .NetworkSettings.Networks \"hiclaw-net\")"
            ".IPAddress}}")
        config = docker.read_config(container, MCPORTER_PATH[role])
        urls = sorted(set(_URL_RE.findall(
            config.decode("utf-8", "replace"))))
        target = ex.hiclaw_role_gateway_url(role)
        state["roles"][role] = {
            "container": container,
            "container_id": live_id,
            "running": running,
            "mxid_matches": mxid.endswith(":" + e2f.E2E_MATRIX_SERVER_NAME)
            and mxid.split("@")[1].split(":")[0] == role,
            "ip_matches": live_ip == ip,
            "current_gateway_urls": urls,     # URLs only; no tokens
            "target_gateway_url": target,
            "already_target": target in urls,
            "config_sha256": hashlib.sha256(config).hexdigest(),
        }
    old = state["old_github_mcp"]
    old["container_id"] = docker.inspect_format(
        "github-mcp", "{{.Id}}")
    old["state"] = docker.inspect_format(
        "github-mcp", "{{.State.Status}}")
    old["restart_policy"] = docker.inspect_format(
        "github-mcp", "{{.HostConfig.RestartPolicy.Name}}")
    old["networks"] = sorted(docker.inspect_format(
        "github-mcp",
        "{{range $k, $v := .NetworkSettings.Networks}}"
        "{{$k}} {{end}}").split())
    return state


def _validate_freeze(state: dict) -> None:
    """All four roles must exist with frozen identities before ANY
    write (hard §5 requirement)."""
    for role in ROLES:
        info = state["roles"][role]
        if not info["container_id"]:
            raise HarnessError("HARNESS_ROLE_MISSING", role)
        if not info["running"]:
            raise HarnessError("HARNESS_ROLE_MISSING",
                               "%s:not-running" % role)
        if not info["ip_matches"]:
            raise HarnessError("HARNESS_IDENTITY_DRIFT",
                               "%s:ip" % role)


def plan(journal_path) -> dict:
    """Read-only sanitized change plan."""
    docker = DockerAdapter(_default_docker_executor())
    state = inspect_roles(docker)
    _validate_freeze(state)
    actions = []
    for role in ROLES:
        info = state["roles"][role]
        actions.append({
            "role": role,
            "container": info["container"],
            "config_path": MCPORTER_PATH[role],
            "backup_path": MCPORTER_PATH[role] + BACKUP_SUFFIX,
            "current_gateways": info["current_gateway_urls"],
            "target_gateway": info["target_gateway_url"],
            "noop": info["already_target"],
        })
    return {"command": "plan", "actions": actions,
            "journal_path": str(journal_path),
            "writes_executed": 0}


# ── journal ───────────────────────────────────────────────────────────────

def _load_journal(journal_path, *, expect_session=None) -> dict:
    raw = AtomicFileWriter.read(journal_path)
    journal = json.loads(raw.decode("utf-8"))
    if journal.get("ownership") != HARNESS_IDENTITY:
        raise HarnessError("HARNESS_FOREIGN_JOURNAL",
                           "ownership mismatch")
    if expect_session is not None \
            and journal.get("session") != expect_session:
        raise HarnessError("HARNESS_FOREIGN_JOURNAL", "session mismatch")
    return journal


def _persist_journal(writer: AtomicFileWriter, journal_path: Path,
                     journal: dict, root: Path) -> None:
    journal["updated_utc"] = _now_iso()
    try:
        writer.write(journal_path,
                     json.dumps(journal, indent=1,
                                ensure_ascii=True).encode("utf-8"),
                     root=root)
    except OSError as exc:
        raise HarnessError("HARNESS_JOURNAL_PERSIST_FAILED",
                           type(exc).__name__) from None


# ── config rewriting (URL only; body never logged) ───────────────────────

def _rewrite_config(config_bytes: bytes, target_url: str) -> bytes:
    """Replace every http(s) gateway URL in the mcporter JSON with
    the frozen E2E gateway URL. Deterministic; body never printed."""
    text = config_bytes.decode("utf-8", "replace")
    new_text = _URL_RE.sub(target_url, text)
    return new_text.encode("utf-8")


def _token_hash(config_bytes: bytes) -> str:
    """Safe hash of the auth material in the config (field value is
    never emitted; only this digest enters the receipt, as the
    production contract requires a token_hash per role)."""
    try:
        cfg = json.loads(config_bytes.decode("utf-8", "replace"))
    except ValueError:
        return hashlib.sha256(b"unparseable").hexdigest()
    auth_blob = json.dumps(
        {k: v for k, v in cfg.items()
         if any(t in k.lower() for t in ("token", "auth", "header",
                                         "credential", "key"))},
        sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(auth_blob.encode("utf-8")).hexdigest()


# ── receipt (production-schema compatible) ────────────────────────────────

def _build_receipt(state: dict, before: dict, after: dict,
                   token_hashes: dict, session: str = None) -> dict:
    agents = []
    for role in ROLES:
        container, mxid, ip, _p = ex.HICLAW_ROLE_FREEZE[role]
        agents.append({
            "role": role,
            "container_name": container,
            "container_id": state["roles"][role]["container_id"],
            "mxid": mxid,
            "hiclaw_net_ip": ip,
            "gateway_url": ex.hiclaw_role_gateway_url(role),
            "config_hash_before": before[role],
            "config_hash_after": after[role],
            "token_hash": token_hashes[role],
        })
    old = state["old_github_mcp"]
    receipt = {
        "schema_version": 1,
        "rewire_session": session or ("rewire-" + _now_iso()),
        "agents": agents,
        "old_github_mcp": {
            "container_id": old["container_id"],
            "state": old["state"],
            "restart_policy": old["restart_policy"],
            "network_attachments": old["networks"],
        },
        "rollback_ownership": HARNESS_IDENTITY,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return receipt


# ── apply / rollback transactions ─────────────────────────────────────────

def apply(*, journal_path, receipt_path, docker: DockerAdapter = None,
          writer: AtomicFileWriter = None,
          receipt_validator: Callable = None,
          session: str = None,
          phase_hook: Callable = None) -> dict:
    """Transactional rewiring of the four roles to the frozen E2E
    Gateway (write-ahead rollback journal).

    Per-role order (crash-recoverable at EVERY point):
      1. read + validate current config
      2. create + verify the in-container before backup
      3. before hash
      4. journal[role].status = "applying" (+ backup/hash metadata)
      5. atomic journal persist          <-- WRITE-AHEAD POINT
      6. write_config(target)
      7. journal[role].status = "mutated"; immediate persist
      8. read-back + verify
      9. journal[role].status = "verified"; immediate persist
     10. all four verified -> atomic receipt -> PRODUCTION validator
     11. journal status = complete (only after 10 succeeds)

    Rollback processes journal roles in strict reverse mutation
    order for ANY of applying/mutated/verified (disk journal is the
    authority — never an in-memory list). phase_hook(phase, role) is
    an injectable TEST-ONLY crash point; production passes None."""
    docker = docker or DockerAdapter(_default_docker_executor())
    writer = writer or AtomicFileWriter()
    session = session or ("rewire-" + _now_iso())
    journal_path = Path(journal_path)
    receipt_path = Path(receipt_path)
    root = journal_path.parent

    def _hook(phase, role):
        if phase_hook is not None:
            phase_hook(phase, role)

    def _persist():
        _persist_journal(writer, journal_path, journal, root)

    state = inspect_roles(docker)
    _validate_freeze(state)

    # receipt ownership preflight (R3): fail-closed BEFORE the
    # journal, any backup, or any agent write. A pre-existing or
    # foreign receipt target is never read, truncated or removed.
    AtomicFileWriter._safe_path(receipt_path, root)
    if receipt_path.exists():
        raise HarnessError("HARNESS_RECEIPT_EXISTS", "receipt target")

    # idempotent re-apply: everything already at target
    if all(state["roles"][r]["already_target"] for r in ROLES):
        return {"command": "apply", "result": "idempotent-noop",
                "session": session, "receipt": None}

    # foreign/in-flight journal refusal (never absorbed)
    if journal_path.exists():
        existing = json.loads(
            AtomicFileWriter.read(journal_path).decode("utf-8"))
        if existing.get("ownership") != HARNESS_IDENTITY:
            raise HarnessError("HARNESS_FOREIGN_JOURNAL",
                               "ownership mismatch")
        raise HarnessError("HARNESS_FOREIGN_JOURNAL",
                           "journal exists; run rollback first")

    receipt_created_by_session = False     # R3 ownership flag
    before = {}
    token_hashes = {}
    journal = {"ownership": HARNESS_IDENTITY, "session": session,
               "status": "in-progress", "created_utc": _now_iso(),
               "roles": {}}
    _persist()

    primary = None
    try:
        for role in ROLES:
            info = state["roles"][role]
            container = info["container"]
            cfg_path = MCPORTER_PATH[role]
            backup_path = cfg_path + BACKUP_SUFFIX
            original = docker.read_config(container, cfg_path)
            before[role] = hashlib.sha256(original).hexdigest()
            token_hashes[role] = _token_hash(original)
            target = ex.hiclaw_role_gateway_url(role)
            if target in _URL_RE.findall(
                    original.decode("utf-8", "replace")):
                journal["roles"][role] = {"status": "already-target"}
                _persist()
                continue
            # 2. before backup (must exist BEFORE the write-ahead
            #    persist so any later state is recoverable)
            docker.backup_config(container, cfg_path, backup_path)
            # 4+5. write-ahead point
            journal["roles"][role] = {
                "status": "applying",
                "backup": backup_path,
                "hash_before": before[role]}
            try:
                _persist()
            except HarnessError:
                # persist failed BEFORE any write: remove the just
                # created backup (best-effort; failure -> diagnostic)
                try:
                    docker.remove_backup(container, backup_path)
                except Exception:
                    pass
                raise
            _hook("applying_persisted", role)
            # 6. mutate
            new_config = _rewrite_config(original, target)
            docker.write_config(container, cfg_path, new_config)
            _hook("mutated_written", role)
            # 7. mutated + immediate persist
            journal["roles"][role]["status"] = "mutated"
            _persist()
            _hook("mutated_persisted", role)
            # 8. read-back verify
            live = docker.read_config(container, cfg_path)
            live_hash = hashlib.sha256(live).hexdigest()
            if live != new_config or (
                    docker.sha256_file(container, cfg_path)
                    != live_hash):
                raise HarnessError("HARNESS_VERIFY_FAILED",
                                   "%s:post-write" % role)
            if target not in _URL_RE.findall(
                    live.decode("utf-8", "replace")):
                raise HarnessError("HARNESS_VERIFY_FAILED",
                                   "%s:target-url-absent" % role)
            # 9. verified + immediate persist
            journal["roles"][role]["status"] = "verified"
            journal["roles"][role]["hash_after"] = live_hash
            _persist()
            _hook("verified_persisted", role)
    except HarnessError as exc:
        primary = exc
    except Exception as exc:
        primary = HarnessError("HARNESS_APPLY_FAILED",
                               type(exc).__name__)

    if primary is not None:
        rb = _transaction_rollback(docker, writer, journal_path, root)
        primary.diagnostics = rb["diagnostics"]
        raise primary

    # 10. all four verified -> receipt (atomic) -> production check
    if not all(e.get("status") in ("verified", "already-target")
               for e in journal["roles"].values()):
        _transaction_rollback(docker, writer, journal_path, root)
        raise HarnessError("HARNESS_RECEIPT_GENERATION_FAILED",
                           "not all roles verified")
    after = {}
    for role in ROLES:
        after[role] = docker.sha256_file(
            state["roles"][role]["container"], MCPORTER_PATH[role])
    receipt = _build_receipt(state, before, after, token_hashes,
                             session=session)
    receipt_bytes = json.dumps(receipt, indent=1,
                               ensure_ascii=True).encode("utf-8")
    # receipt WAL point: persist the PUBLISH INTENT (path/session/
    # expected canonical hash — never the body) BEFORE publishing.
    # "publishing" grants the right to ATTEMPT publication only; it
    # never asserts target ownership.
    journal["receipt_state"] = "publishing"
    journal["receipt_path"] = str(receipt_path)
    journal["receipt_session"] = session
    journal["receipt_sha256"] = receipt["receipt_sha256"]
    try:
        _persist()
    except HarnessError:
        rb = _transaction_rollback(docker, writer, journal_path, root)
        exc = HarnessError("HARNESS_JOURNAL_PERSIST_FAILED",
                           "receipt-publishing")
        exc.diagnostics = rb["diagnostics"]
        raise exc
    try:
        AtomicFileWriter.write_exclusive(receipt_path, receipt_bytes,
                                         root=root)
    except FileExistsError:
        # a competitor created the target after our preflight:
        # foreign bytes win, this session fails, nothing of ours
        # exists at that path
        rb = _transaction_rollback(docker, writer, journal_path, root)
        exc = HarnessError("HARNESS_RECEIPT_EXISTS",
                           "lost exclusive publish race")
        exc.diagnostics = rb["diagnostics"]
        raise exc
    except OSError as exc:
        _transaction_rollback(docker, writer, journal_path, root)
        raise HarnessError("HARNESS_RECEIPT_GENERATION_FAILED",
                           type(exc).__name__) from None
    receipt_created_by_session = True
    _hook("receipt_published", session)
    # only NOW does the journal claim creation (crash between the
    # hook above and this persist leaves state=publishing, which
    # crash recovery resolves by cryptographic ownership proof)
    journal["receipt_state"] = "created"
    try:
        _persist()
    except HarnessError:
        try:
            receipt_path.unlink(missing_ok=True)
        except OSError:
            pass
        receipt_created_by_session = False
        rb = _transaction_rollback(docker, writer, journal_path, root)
        exc = HarnessError("HARNESS_JOURNAL_PERSIST_FAILED",
                           "receipt-created")
        exc.diagnostics = rb["diagnostics"]
        raise exc

    # bind the PRODUCTION validator to the SAME adapter (injected
    # fakes stay fake; the default binds the real docker executor)
    validator = receipt_validator or _production_validator_with(docker)
    def _drop_session_receipt(diags):
        nonlocal receipt_created_by_session
        if receipt_created_by_session:
            try:
                receipt_path.unlink(missing_ok=True)
                receipt_created_by_session = False
            except OSError as exc:
                diags.append("RECEIPT_REMOVE_FAILED:(%s)"
                             % type(exc).__name__)

    try:
        result = validator(str(receipt_path))
    except Exception as exc:
        diags = []
        _drop_session_receipt(diags)
        rb = _transaction_rollback(docker, writer, journal_path, root)
        exc2 = HarnessError("HARNESS_RECEIPT_VALIDATION_FAILED",
                            type(exc).__name__)
        exc2.diagnostics = diags + rb["diagnostics"]
        raise exc2 from None
    if not result.get("verified", False):
        diags = []
        _drop_session_receipt(diags)
        rb = _transaction_rollback(docker, writer, journal_path, root)
        exc2 = HarnessError("HARNESS_RECEIPT_VALIDATION_FAILED",
                            "production validator rejected receipt")
        exc2.diagnostics = diags + rb["diagnostics"]
        raise exc2

    # 11. complete (only after the receipt validated)
    journal["status"] = "complete"
    journal["receipt"] = str(receipt_path)
    journal["receipt_sha256"] = receipt["receipt_sha256"]
    try:
        _persist()
    except HarnessError:
        # complete-persist failed: no trusted complete/receipt state.
        # The receipt path never reached the on-disk journal, so the
        # transaction rollback cannot know it — remove the receipt
        # THIS apply created (our own file) before rolling back.
        if receipt_created_by_session:
            try:
                receipt_path.unlink(missing_ok=True)
            except OSError:
                pass
        rb = _transaction_rollback(docker, writer, journal_path,
                                   root)
        exc = HarnessError("HARNESS_JOURNAL_PERSIST_FAILED",
                           "complete-stage")
        exc.diagnostics = rb["diagnostics"]
        raise exc
    _hook("complete_persisted", None)
    return {"command": "apply", "result": "complete",
            "session": session,
            "receipt": str(receipt_path),
            "receipt_sha256": receipt["receipt_sha256"]}


_RECEIPT_MAX_BYTES = 65536


def _verify_receipt_ownership(journal: dict) -> str:
    """Cryptographic ownership verdict for the receipt file named by
    the journal's publishing/created intent.

    Returns "ours" | "foreign" | "absent". Deletion is allowed ONLY
    on "ours": ordinary file, within size bound, parseable JSON,
    rewire_session == journal intent session, receipt_sha256 field
    == journal's expected hash, AND the recomputed canonical hash of
    the body matches (session binding is hash-protected). Never
    prints or returns the body."""
    path_str = journal.get("receipt_path") or ""
    if not path_str:
        return "absent"
    path = Path(path_str)
    try:
        AtomicFileWriter._safe_path(path)
    except HarnessError:
        return "foreign"
    try:
        if os.path.islink(path_str) or getattr(
                path.lstat(), "st_reparse_tag", 0):
            return "foreign"
        st = path.stat()
    except OSError:
        return "absent"
    if not stat.S_ISREG(st.st_mode) or st.st_size > _RECEIPT_MAX_BYTES:
        return "foreign"
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return "foreign"
    if not isinstance(receipt, dict):
        return "foreign"
    if receipt.get("rewire_session") != journal.get(
            "receipt_session"):
        return "foreign"
    if receipt.get("receipt_sha256") != journal.get("receipt_sha256"):
        return "foreign"
    if _canonical_sha256(receipt) != journal.get("receipt_sha256"):
        return "foreign"
    return "ours"


def _residue_add(residue, item):
    """Append a stable residue identifier, keeping order and
    preventing duplicates (also collapses pre-existing dups on the
    next touch of the same item)."""
    while residue.count(item) > 1:
        residue.remove(item)
    if item not in residue:
        residue.append(item)


def _residue_remove(residue, item):
    """EXACT-item removal of a resolved residue identifier: only
    the full stable string is removed (never prefix-matched), other
    roles'/receipt entries keep their order."""
    while item in residue:
        residue.remove(item)


def _transaction_rollback(docker, writer, journal_path, root):
    """Disk-authoritative reverse-order rollback of every role in
    applying/mutated/verified state. Removes the session receipt if
    the journal never reached complete. Failures become diagnostics
    and NEVER replace any primary error."""
    diags = []
    try:
        journal = json.loads(
            AtomicFileWriter.read(journal_path).decode("utf-8"))
    except (OSError, ValueError):
        return {"rolled_back": [], "diagnostics": ["JOURNAL_UNREADABLE"]}
    if journal.get("ownership") != HARNESS_IDENTITY:
        return {"rolled_back": [], "diagnostics": ["FOREIGN_JOURNAL"]}
    rolled = []
    for role in reversed(list(journal.get("roles", {}))):
        entry = journal["roles"].get(role, {})
        if entry.get("status") not in ("applying", "mutated",
                                       "verified", "rollback-failed",
                                       "rolled-back-with-backup-residue"):
            continue
        container = ex.HICLAW_ROLE_FREEZE[role][0]
        backup_path = entry.get("backup")
        residue = journal.setdefault("rollback_residue", [])

        # ── phase 1: restore the CONFIG (failure keeps the backup
        # for a later retry and never blocks other roles). Retries
        # re-attempt the restore; only a prior
        # rolled-back-with-backup-residue skips straight to cleanup.
        if entry.get("status") != "rolled-back-with-backup-residue":
            try:
                if backup_path:
                    # idempotent restore: valid even if the write
                    # never actually happened (applying window)
                    docker.restore_config(container, backup_path,
                                          MCPORTER_PATH[role])
            except Exception as exc:
                entry["status"] = "rollback-failed"
                diags.append("ROLLBACK_FAILED:%s(%s)"
                             % (role, type(exc).__name__))
                _residue_add(residue, "role:%s" % role)
                continue        # next role keeps going

        # config restored (or a prior attempt already restored it):
        # any STALE role residue from a previous failed attempt is
        # now resolved — remove it exactly
        _residue_remove(residue, "role:%s" % role)
        entry["status"] = "rolled-back"
        rolled.append(role)

        # ── phase 2: cleanup the BACKUP (its failure NEVER
        # misreports the role as restore-failed; a retry of a
        # residue entry only re-attempts this removal)
        if backup_path:
            try:
                docker.remove_backup(container, backup_path)
                # removal succeeded: clear any STALE backup residue
                # from a previous attempt (absent backup + rc=0 is
                # the same idempotent success)
                _residue_remove(residue, "backup:%s" % role)
            except HarnessError:
                entry["status"] = "rolled-back-with-backup-residue"
                diags.append("BACKUP_REMOVE_FAILED:%s" % role)
                _residue_add(residue, "backup:%s" % role)
    # Receipt cleanup for an incomplete journal. The journal's
    # publishing/created INTENT (path + session + expected canonical
    # hash) drives a cryptographic ownership verdict on the FILE:
    # delete only on full proof; foreign/indeterminate files are
    # reported as residue and NEVER deleted.
    if journal.get("receipt_state") in ("publishing", "created") \
            and journal.get("status") != "complete":
        verdict = _verify_receipt_ownership(journal)
        if verdict == "absent":
            journal["receipt_state"] = "absent"   # nothing to clean
        elif verdict == "ours":
            try:
                Path(journal["receipt_path"]).unlink(missing_ok=True)
                journal["receipt_state"] = "removed"
            except OSError as exc:
                diags.append("RECEIPT_REMOVE_FAILED:(%s)"
                             % type(exc).__name__)
                _residue_add(
                    journal.setdefault("rollback_residue", []),
                    "receipt:unremovable")
        else:
            diags.append("RECEIPT_OWNERSHIP_UNVERIFIED")
            _residue_add(
                journal.setdefault("rollback_residue", []),
                "receipt:ownership-unverified")
    role_restore_failed = any(
        isinstance(e, dict) and e.get("status") == "rollback-failed"
        for e in journal.get("roles", {}).values())
    residue_now = journal.get("rollback_residue", [])
    if role_restore_failed:
        journal["status"] = "rollback-failed"
    elif residue_now:
        # every config restored, but REAL residue persists — either
        # from this call or converged history (never silently
        # "clean" while entries remain)
        journal["status"] = "rollback-residue"
    else:
        journal["status"] = "rolled-back"
    journal["rollback_diagnostics"] = diags
    try:
        _persist_journal(writer, journal_path, journal, root)
    except HarnessError as exc:
        diags.append(exc.code)
    return {"rolled_back": rolled, "diagnostics": diags}


def rollback(*, journal_path, docker: DockerAdapter = None,
             writer: AtomicFileWriter = None,
             session: str = None) -> dict:
    """Explicit rollback from the on-disk journal (crash recovery or
    manual). Idempotent: nothing rolls back twice. Only this
    session's journal-owned backups/receipt are touched."""
    docker = docker or DockerAdapter(_default_docker_executor())
    writer = writer or AtomicFileWriter()
    journal_path = Path(journal_path)
    journal = _load_journal(journal_path, expect_session=session)
    if journal.get("status") == "complete":
        return {"command": "rollback", "rolled_back": [],
                "residue": [], "note": "journal already complete"}
    result = _transaction_rollback(docker, writer, journal_path,
                                   journal_path.parent)
    # agent-restore failures ARE rollback failures; receipt
    # ownership/remove REPORTS are honest residue disclosures (the
    # agents still rolled back — the caller must see the residue,
    # not a misleading failure)
    hard = [d for d in result["diagnostics"]
            if d.startswith("ROLLBACK_FAILED")
            or d == "JOURNAL_UNREADABLE" or d == "FOREIGN_JOURNAL"]
    if hard:
        raise HarnessError("HARNESS_ROLLBACK_FAILED",
                           ";".join(hard))
    journal = json.loads(
        AtomicFileWriter.read(journal_path).decode("utf-8"))
    return {"command": "rollback",
            "rolled_back": result["rolled_back"],
            "residue": journal.get("rollback_residue", []),
            "diagnostics": result["diagnostics"]}


def _journal_residue(journal: dict) -> list:
    return list(journal.get("rollback_residue", []))


def verify(receipt_path, docker: DockerAdapter = None) -> dict:
    """Run the PRODUCTION receipt validator (read-only)."""
    docker = docker or DockerAdapter(_default_docker_executor())
    validator = _production_validator_with(docker)
    try:
        result = validator(str(receipt_path))
    except ex.ReceiptValidationError as exc:
        return {"verified": False, "code": exc.code}
    return result


def status(journal_path) -> dict:
    journal = _load_journal(journal_path)
    out = {"command": "status", "ownership": journal["ownership"],
           "session": journal.get("session"),
           "journal_status": journal.get("status"),
           "roles": {r: e.get("status")
                     for r, e in journal.get("roles", {}).items()},
           "residue": _journal_residue(journal)}
    if journal.get("receipt_sha256"):
        out["receipt_sha256"] = journal["receipt_sha256"]
    return out


# ── production validator binding ──────────────────────────────────────────

def _default_docker_executor():
    """Real docker via the WSL test distro (argv lists, redacted)."""
    def docker_exec(argv, check=True, timeout=60, input_bytes=None,
                    **_):
        # HiClaw containers live in the Ubuntu-22.04 distro (the
        # E2E stack's MergePilot-Test distro hosts only the E2E
        # containers — see the frozen topology contract).
        cmd = ["wsl", "-d", "Ubuntu-22.04", "-u", "root",
               "--", "docker"] + list(argv)
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            input=input_bytes)
    return docker_exec


def _production_validator_with(docker: DockerAdapter):
    def validator(receipt_path: str) -> dict:
        return ex.validate_hiclaw_receipt(
            receipt_path,
            docker_executor=docker._exec,
            expected_old_mcp_state="stopped")
    return validator


def _production_validator(receipt_path: str) -> dict:
    return _production_validator_with(
        DockerAdapter(_default_docker_executor()))(receipt_path)


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mp-gh4-harness",
        description="HiClaw rewiring harness (default command: "
                    "read-only inspect)")
    parser.add_argument("command",
                        choices=["inspect", "plan", "apply", "verify",
                                 "rollback", "status"],
                        nargs="?", default="inspect")
    parser.add_argument("--apply", action="store_true",
                        help="EXPLICIT consent for real modification "
                             "(apply only)")
    parser.add_argument("--journal",
                        default=".mp-gh4-journal.json")
    parser.add_argument("--receipt",
                        default=".mp-gh4-receipt.json")
    args = parser.parse_args(argv)

    try:
        if args.command == "inspect":
            print(json.dumps(inspect_roles(
                DockerAdapter(_default_docker_executor())),
                indent=1, ensure_ascii=True))
            return 0
        if args.command == "plan":
            print(json.dumps(plan(args.journal), indent=1,
                             ensure_ascii=True))
            return 0
        if args.command == "status":
            print(json.dumps(status(args.journal), indent=1,
                             ensure_ascii=True))
            return 0
        if args.command == "verify":
            result = verify(args.receipt)
            print(json.dumps(result, indent=1, ensure_ascii=True))
            return 0 if result.get("verified") else 1
        if args.command == "rollback":
            print(json.dumps(rollback(journal_path=args.journal),
                             indent=1, ensure_ascii=True))
            return 0
        if args.command == "apply":
            if not args.apply:
                print("refusing: apply requires explicit --apply")
                return 2
            result = apply(journal_path=args.journal,
                           receipt_path=args.receipt)
            print(json.dumps(result, indent=1, ensure_ascii=True))
            return 0
    except HarnessError as exc:
        print("HARNESS_ERROR %s: %s" % (exc.code, exc.detail))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
