"""M8-GH-4B4 R1: v1→v2 harness contract migration (52/52 nodes).

This file replaces the v1 execution matrix (same node count, all 52
v1 contracts preserved 1:1 or merged into strictly stronger
direction-aware scenarios). It drives the PRODUCTION state machine in
tools/harness/mp_gh4_harness.py with a FakeSyncWorld that models the
real sync world at its external boundaries only:

- manager live / manager canonical / per-worker live / per-worker
  canonical are SEPARATE stores;
- manager live writes converge canonical ONLY via a simulated
  production push tick; worker canonical writes converge live ONLY
  via a simulated pull tick (lazy, time-based — never auto);
- transaction backups + lock live under the MinIO tx prefix; the
  journal + receipt are REAL files on disk written by the production
  AtomicFileWriter;
- crash recovery always starts a FRESH harness + world instance and
  migrates ONLY external system state (never harness memory).

v1→v2 migration ledger (52 v1 nodes → 52 v2 nodes):

| v1 node                                | v2 node (same class/name unless noted) |
|----------------------------------------|----------------------------------------|
| inspect_identifies_default_gateway…    | same; + canonical etag/sync_mode/legacy metadata |
| plan_zero_writes                       | same; + explicit adapter injection |
| plan_zero_writes_impl                  | plan_fail_closed_details (legacy+fingerprint+noop flags) |
| PlanInjection.plan_with_fake           | plan_injection_without_default_executor |
| four_role_success_receipt_validated    | same; + live==canonical both sides, tx/lock cleanup, no direct worker-live/manager-canonical writes |
| apply_idempotent_when_all_at_target    | same; + zero mc pipes, no journal/lock |
| apply_failure_for_every_role           | same + §4C: per-role mutation AND convergence failure matrix (never/once-then-drift), rollback residue classification |
| verify_failure_for_every_role          | same; split into LIVE_WRITE_VERIFY / CANONICAL_VERIFY exact codes |
| identity_drift_refused_before_writes   | same |
| missing_role_refused                   | + lock-create failure + foreign tx-backup + corrupt tx-copy family (§4D) |
| foreign_journal_refused                | same |
| rollback_failure_preserves_primary     | same (direction-aware restore failure) |
| crash_journal_explicit_rollback…       | same (real crash window via phase hook) |
| rollback_idempotent                    | same |
| in_flight_journal_blocks_new_apply     | same |
| crash_window_recovery                  | same 4 windows incl. manager_live_written / canonical_written |
| crash_no_foreign_touch                 | same |
| write_ahead_persist_failure_zero_writes| same; + lock released on init-persist failure |
| mutated_persist_failure_rolls_back…    | same (manager_live_mutated ordinal) |
| verified_persist_failure_rolls_back…   | same (manager_converged ordinal) |
| complete_persist_failure_no_trusted…   | same (receipt deleted, no trusted state) |
| preexisting_receipt_refused            | same (exclusive preflight before lock/journal) |
| receipt_reparse_refused                | same |
| created_by_foreign_process_before…     | same (write_exclusive loses race fail-closed) |
| validator_failure_removes_only_…       | same (OUR receipt deleted) |
| complete_persist_failure_deletes_…     | same |
| post_publish_pre_ownership_persist…    | same (receipt_state=publishing, ownership proven from disk) |
| a_publishing_without_receipt           | same |
| b_publishing_foreign_preempted         | same |
| c_published_then_foreign_replaced      | same |
| d_same_session_wrong_hash              | same |
| e_hash_field_matches_canonical_doesnot | same |
| f_malformed_oversized_reparse          | same (_RECEIPT_MAX_BYTES) |
| backup_remove_failure_reported         | TX_BACKUP_REMOVE_FAILED + rolled-back-with-residue + retry |
| combined_role_receipt_backup_failures  | same three categories, direction-aware |
| auto_rollback_primary_preserved_…      | same |
| rollback_failed_role_retry_restores    | same (v2 retry contract) |
| backup_residue_retry_cleans_…          | same (rm retried, restore NOT re-run) |
| residue_convergence_preserves_…        | same (unrelated receipt: entry preserved, order stable) |
| g_rollback_time_reparse_receipt        | same |
| post_write_pre_mutated_persist_…       | same (canonical_written window) |
| verify_failure_with_rollback_failure   | same |
| receipt_schema_and_canonical_hash      | schema v2 direction fields |
| receipt_and_journal_zero_secret        | same |
| argv_and_calls_zero_secret             | same (docker+minio audit) |
| old_mcp_never_started_or_stopped       | same |
| verify_uses_production_validator       | same |
| verify_rejects_drift                   | + integrity-vs-production-mismatch split |
| stopped_state_family_normalized        | same |
| reparse_refused                        | same |
| journal_persist_failure_is_primary     | same; + lock released |
| no_pat_or_pem_reads                    | same |

Zero real docker, zero PAT/PEM reads, zero Matrix mutation, zero
live/MinIO writes outside the in-memory fake world.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "harness"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mp_gh4_harness as hw                  # noqa: E402
import e2e_executors as ex                   # noqa: E402

TARGET = {r: ex.hiclaw_role_gateway_url(r) for r in hw.ROLES}
WORKERS = ("reviewer", "fixer", "verifier")
LEGACY_URL = "http://aigw-local.hiclaw.io:8080/mcp-github/mcp"


def _cp(rc=0, stdout=b""):
    return subprocess.CompletedProcess([], rc, stdout, b"")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _etag(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _legacy_config(role: str) -> bytes:
    return ('{"mcpServers":{"gh":{"url":"%s",'
            '"headers":{"Authorization":"Bearer secret-%s"}}}}'
            % (LEGACY_URL, role)).encode("utf-8")


def _target_config(role: str) -> bytes:
    return ('{"mcpServers":{"gh":{"url":"%s",'
            '"headers":{"Authorization":"Bearer secret-%s"}}}}'
            % (TARGET[role], role)).encode("utf-8")


class CrashSimulated(BaseException):
    """Escapes apply()'s except-Exception handlers: a true process
    crash with NO in-transaction cleanup (test-only)."""


class FakeSyncWorld:
    """Realistic boundary simulation of the direction-aware sync
    world: docker (live configs) + MinIO (canonical + tx objects) +
    the four production sync loops, all with a sanitized argv audit.

    Sync ticks are LAZY and time-based: reading the manager canonical
    object may first run a push tick (live->canonical); reading a
    worker live config may first run a pull tick (canonical->live).
    A manager live write therefore NEVER auto-converges canonical and
    a worker canonical write NEVER auto-converges live — only the
    simulated production ticks do, exactly like the deployed system.
    """

    TICK_SECONDS = 0.012     # simulated 10s push / 300s pull cycle

    def __init__(self, *, fingerprint_ok=True, legacy_artifacts=()):
        self.docker_calls = []          # sanitized argv audit
        self.minio_calls = []           # sanitized mc argv audit
        self.live_write_calls = 0       # live config writes (docker)
        self.canonical_put_calls = 0    # canonical writes (mc pipe)
        self.objects = {}               # key -> bytes
        self.live = {}                  # container -> {path: bytes}
        self._legacy = {}
        for role in hw.ROLES:
            container = ex.HICLAW_ROLE_FREEZE[role][0]
            content = _legacy_config(role)
            self._legacy[role] = content
            self.objects[ex.hiclaw_role_canonical_key(role)] = content
            path = ex.hiclaw_role_live_config_path(role)
            self.live.setdefault(container, {})[path] = content
        for k in legacy_artifacts:
            self.objects[k] = b"legacy-backup-body"
        # deployed sync-contract fingerprint
        self._fingerprint_ok = fingerprint_ok
        # sync loop behavior per role: converge | never | drift-once
        self.push_mode = {"manager": "converge"}
        self.pull_mode = {r: "converge" for r in WORKERS}
        self._push_ticks = {"manager": 0}
        self._pull_ticks = {r: 0 for r in WORKERS}
        self._drift_countdown = {}
        self._last_push = {}
        self._last_pull = {}
        # ── failure injection (adapter boundary; tests may flip
        #    these mid-flight, exactly like the v1 flaky adapters)
        self.running_false = set()      # roles reported not running
        self.ip_drift_role = None
        self.fail_live_write = set()    # roles: docker cat> rc=1
        self.fail_canonical_put = set()  # roles: mc pipe rc=1
        self.fail_live_write_once = set()    # disarm after 1st fail
        self.fail_canonical_put_once = set()  # disarm after 1st fail
        self.corrupt_canonical_put = {}  # role -> bytes stored instead
        self.corrupt_canonical_put_once = {}  # disarm after 1st put
        self.drift_live_read = {}        # role -> bytes returned on read
        self.corrupt_tx_copy = set()     # roles: backup copy corrupts
        self.fail_tx_copy = set()        # roles: backup copy rc=1
        self.fail_mc_rm = set()          # keys: mc rm rc=1
        self.fail_lock_put = False
        self.fail_cond_put_once = False  # signer transport fails once
        self.short_read_keys = set()     # mc cat truncated responses
        self.fail_mc_cat = False         # mc cat rc=1 (unmasked)

    # ── external-state fixture helpers (initial world setup only) ──

    def set_role_target(self, role):
        """Start the world with one role already converged at target
        (fixture setup — never used to shortcut a failing path)."""
        container = ex.HICLAW_ROLE_FREEZE[role][0]
        path = ex.hiclaw_role_live_config_path(role)
        content = _target_config(role)
        self.live[container][path] = content
        self.objects[ex.hiclaw_role_canonical_key(role)] = content

    def legacy_bytes(self, role) -> bytes:
        return self._legacy[role]

    def live_bytes(self, role) -> bytes:
        container = ex.HICLAW_ROLE_FREEZE[role][0]
        path = ex.hiclaw_role_live_config_path(role)
        return self.live[container][path]

    def canonical_bytes(self, role) -> bytes:
        return self.objects[ex.hiclaw_role_canonical_key(role)]

    def role_at_target(self, role) -> bool:
        return (TARGET[role] in self.live_bytes(role).decode("utf-8",
                "replace")
                and TARGET[role] in self.canonical_bytes(role).decode(
                    "utf-8", "replace"))

    def role_at_legacy(self, role) -> bool:
        return ("aigw-local" in self.live_bytes(role).decode("utf-8",
                "replace")
                and "aigw-local" in self.canonical_bytes(role).decode(
                    "utf-8", "replace"))

    def tx_keys(self):
        return sorted(k for k in self.objects
                      if k.startswith(ex.HICLAW_TX_PREFIX))

    def clone_for_recovery(self):
        """FRESH world carrying ONLY external system state (live
        configs, objects, sync-loop behavior). No harness memory, no
        failure-injection arming, no audit history."""
        w = FakeSyncWorld.__new__(FakeSyncWorld)
        w.docker_calls = []
        w.minio_calls = []
        w.live_write_calls = 0
        w.canonical_put_calls = 0
        w.live = {c: dict(p) for c, p in self.live.items()}
        w.objects = dict(self.objects)
        w._legacy = dict(self._legacy)
        w._fingerprint_ok = self._fingerprint_ok
        w.push_mode = dict(self.push_mode)
        w.pull_mode = dict(self.pull_mode)
        w._push_ticks = dict(self._push_ticks)
        w._pull_ticks = dict(self._pull_ticks)
        w._drift_countdown = {}
        w._last_push = {}
        w._last_pull = {}
        w.running_false = set()
        w.ip_drift_role = None
        w.fail_live_write = set()
        w.fail_canonical_put = set()
        w.fail_live_write_once = set()
        w.fail_canonical_put_once = set()
        w.corrupt_canonical_put = {}
        w.corrupt_canonical_put_once = {}
        w.drift_live_read = {}
        w.corrupt_tx_copy = set()
        w.fail_tx_copy = set()
        w.fail_mc_rm = set()
        w.fail_lock_put = False
        w.fail_cond_put_once = False
        w.short_read_keys = set()
        w.fail_mc_cat = False
        return w

    # ── simulated production sync ticks ───────────────────────────

    def _tick_due(self, ledger, role):
        now = time.monotonic()
        if now - ledger.get(role, 0.0) < self.TICK_SECONDS:
            return False
        ledger[role] = now
        return True

    def maybe_push(self, role):
        """Production push tick: manager live -> canonical."""
        if role != "manager" or not self._tick_due(
                self._last_push, role):
            return
        self._push_ticks[role] += 1
        mode = self.push_mode.get(role, "converge")
        key = ex.hiclaw_role_canonical_key(role)
        if mode == "never":
            return
        if mode == "drift-once" and self._push_ticks[role] >= 2:
            # converged once, then canonical drifted away again
            self.objects[key] = self._legacy[role]
            return
        self.objects[key] = self.live_bytes(role)

    def maybe_pull(self, role):
        """Production pull tick: worker canonical -> live."""
        if role not in WORKERS or not self._tick_due(
                self._last_pull, role):
            return
        self._pull_ticks[role] += 1
        mode = self.pull_mode.get(role, "converge")
        if mode == "never":
            return
        container = ex.HICLAW_ROLE_FREEZE[role][0]
        path = ex.hiclaw_role_live_config_path(role)
        if mode == "drift-once" and self._pull_ticks[role] >= 2:
            # converged once, then live drifted away again
            self.live[container][path] = self._legacy[role]
            return
        self.live[container][path] = self.canonical_bytes(role)

    def _apply_pending_drift(self, role):
        """Countdown post-convergence external drift (drift-once):
        the read that consumes the last tick reverts live to the
        pre-transaction bytes."""
        n = self._drift_countdown.get(role, 0)
        if n > 0:
            self._drift_countdown[role] = n - 1
            if n == 1:
                cont = ex.HICLAW_ROLE_FREEZE[role][0]
                path = ex.hiclaw_role_live_config_path(role)
                self.live.setdefault(cont, {})[path] =                     self._legacy[role]

    def freeze_ticks(self):
        """Pin every sync tick far into the future: the world stays
        STATIC (used to model externally-caused drift that the
        harness itself must detect, not heal)."""
        horizon = time.monotonic() + 10_000.0
        for r in list(self._last_push) + list(self._last_pull):
            self._last_push[r] = horizon
            self._last_pull[r] = horizon
        for r in ("reviewer", "fixer", "verifier"):
            self._last_pull.setdefault(r, horizon)
            self._last_pull[r] = horizon
        self._last_push.setdefault("manager", horizon)
        self._last_push["manager"] = horizon

    def simulate_push(self, role):
        """Explicit production push tick (hook-driven tests): forces
        one immediate live->canonical copy."""
        self._last_push[role] = 0.0
        self.maybe_push(role)

    def simulate_pull(self, role):
        """Explicit production pull tick (hook-driven tests): forces
        one immediate canonical->live copy."""
        self._last_pull[role] = 0.0
        self.maybe_pull(role)

    # ── conditional S3 semantics (mirrors the probed real server:
    # create-if-absent / replace-if-match enforced; DELETE ignores
    # If-Match so the protocol never deletes the lock) ────────────

    def cond_request(self, op, key, body, if_match):
        """The in-container signer, faithfully simulated: atomic
        conditional create/replace/read against self.objects. The
        lock mirrors the server's single-object atomicity."""
        import threading as _th
        if not hasattr(self, "_cond_store_lock"):
            self._cond_store_lock = _th.Lock()
        with self._cond_store_lock:
            return self._cond_request_locked(op, key, body,
                                             if_match)

    def _cond_request_locked(self, op, key, body, if_match):
        import json as _json
        if op == "put-absent":
            if key in self.objects:
                status, etag, out = 412, "", b""
            else:
                self.objects[key] = body
                status, etag, out = 200, _etag(body), b""
        elif op == "put-match":
            cur = self.objects.get(key)
            cur_etag = _etag(cur) if cur is not None else ""
            if cur is None or if_match != cur_etag:
                status, etag, out = 412, cur_etag, b""
            else:
                self.objects[key] = body
                status, etag, out = 200, _etag(body), b""
        else:   # get-match
            cur = self.objects.get(key)
            if cur is None:
                status, etag, out = 404, "", b""
            elif if_match not in (None, "", "-")                     and if_match != _etag(cur):
                status, etag, out = 412, _etag(cur), b""
            else:
                status, etag, out = 200, _etag(cur), cur
        head = (_json.dumps({"status": status, "etag": etag})
                .encode("utf-8") + b"\n")
        return _cp(0, head + out)

    # ── docker boundary ───────────────────────────────────────────

    def docker_exec(self, argv, check=True, timeout=240,
                    input_bytes=None, **_):
        argv = list(argv)
        self.docker_calls.append(argv)
        args = argv[1:]
        while args and args[0].startswith("-"):
            args = args[1:]
        container = args[0] if args else ""
        op = args[1] if len(args) > 1 else ""
        role = self._role_of_container(container)
        if argv[0] == "inspect":
            return self._inspect(argv, role)
        if container == "hiclaw-controller" and op == "python3"                 and "-c" in args:
            # in-container conditional signer (see _S3_COND_SCRIPT):
            # program rides -c, the OBJECT BODY is stdin-only
            c_idx = args.index("-c")
            sargs = args[c_idx + 2:]      # skip -c and the program
            op2, target = sargs[0], sargs[1]
            if_match = sargs[2] if len(sargs) > 2 else "-"
            # audit-view sanitization (execution argv unchanged): the
            # embedded client source is replaced by a stable marker
            self.docker_calls[-1] = (
                self.docker_calls[-1][:c_idx + 1]
                + ["<embedded-s3-conditional-client>"] + list(sargs))
            if op2 == "put-absent" and self.fail_lock_put                     and target.endswith("%s/lock"
                                        % ex.HICLAW_TX_PREFIX):
                return _cp(1, b"")     # signer transport failure
            if op2 == "put-match" and self.fail_cond_put_once:
                self.fail_cond_put_once = False
                return _cp(1, b"")
            key = self._strip_bucket(target)
            return self.cond_request(op2, key, input_bytes or b"",
                                     if_match)
        if op == "cat":
            if role in WORKERS:
                self._apply_pending_drift(role)
            data = self.live.get(container, {}).get(args[-1], b"")
            if role in self.drift_live_read:
                data = self.drift_live_read[role]
            return _cp(0, data)
        if op == "sha256sum":
            if role in WORKERS:
                self._apply_pending_drift(role)
            data = self.live.get(container, {}).get(args[-1], b"")
            return _cp(0, (_sha(data) + "  " + args[-1]).encode())
        if op == "grep":
            return self._grep(args)
        if op == "sh":
            return self._sh(container, role, args[-1], input_bytes)
        if op == "mc" and len(args) > 2 and args[2] == "cp"                 and role in WORKERS:
            # the production on-demand pull primitive (B6): exact
            # argv is [mc, cp, <bucket-key>, <live-path>] INSIDE the
            # role's own container; wrong role/key/path -> rc=1
            margs = args[2:]
            src = self._strip_bucket(margs[1])
            dst = margs[2]
            if (src != ex.hiclaw_role_canonical_key(role)
                    or dst != ex.hiclaw_role_live_config_path(role)):
                return _cp(1, b"")
            mode = self.pull_mode.get(role, "converge")
            if mode == "never":
                return _cp(1, b"")
            data = self.objects.get(src)
            if data is None:
                return _cp(1, b"")
            self.live.setdefault(container, {})[dst] = data
            if mode == "drift-once":
                # converged once; an external actor reverts live two
                # reads later (the trigger's own read-back passes,
                # the verification loop's stability check catches it)
                self._drift_countdown[role] = 2
            return _cp(0, b"")
        return _cp(0)

    def _inspect(self, argv, role):
        name = argv[1]
        fmt = argv[argv.index("--format") + 1]
        if "{{.Id}}" in fmt:
            return _cp(0, ("cid-%s" % name).encode())
        if "{{.State.Running}}" in fmt:
            running = "false" if (role in self.running_false
                                  or name == "github-mcp") else "true"
            return _cp(0, running.encode())
        if "hiclaw-net" in fmt:
            if role and role == self.ip_drift_role:
                return _cp(0, b"172.21.0.99")
            ip = ex.HICLAW_ROLE_FREEZE[role][2] if role else b""
            return _cp(0, ip.encode() if isinstance(ip, str) else ip)
        if "{{.HostConfig.RestartPolicy.Name}}" in fmt:
            return _cp(0, b"no")
        if "{{.State.Status}}" in fmt:
            return _cp(0, b"exited")
        if "NetworkSettings.Networks" in fmt:
            return _cp(0, b"mcp-backend-net ")
        return _cp(0, ("cid-%s" % name).encode())

    def _grep(self, args):
        path = args[-1]
        pattern = args[args.index("-E") + 1] if "-E" in args \
            else args[-2]
        if not self._fingerprint_ok:
            return _cp(1, b"0")
        if "worker-entrypoint" in path:
            if "mcporter" in pattern:
                return _cp(0, b"1\n")     # worker push excludes it
            if "sleep" in pattern:
                return _cp(0, b"sleep 300\n")
        if "start-manager-agent" in path:
            if "mcporter" in pattern:
                return _cp(0, b"0\n")     # manager push does NOT
        return _cp(0, b"")

    def _sh(self, container, role, script, input_bytes):
        if "grep -oF" in script:
            if "sleep 300" in script:
                return _cp(0, b"sleep 300\n")
            return _cp(0, b"")
        if "cat >" in script:
            # live config write (harness mutation boundary)
            if role in self.fail_live_write:
                return _cp(1)
            if role in self.fail_live_write_once:
                self.fail_live_write_once.discard(role)
                return _cp(1)
            path = script.split("cat > ")[1].strip()
            self.live.setdefault(container, {})[path] = (
                input_bytes or b"")
            self.live_write_calls += 1
            return _cp(0)
        if "mc pipe" in script:
            key = self._strip_bucket(script.split("mc pipe ")[1])
            return self._mc_pipe(key, input_bytes)
        if "sha256sum" in script:
            key = script.split("mc cat ")[1].split(" ")[0]
            key = self._strip_bucket(key.split("2>")[0].strip())
            if key == ex.hiclaw_role_canonical_key("manager"):
                self.maybe_push("manager")
            data = self.objects.get(key, b"")
            return _cp(0, (_sha(data) + "  -").encode())
        if "head -c" in script:
            key = script.split("mc cat ")[1].split("2>")[0]
            key = self._strip_bucket(key.strip())
            return _cp(0, self.objects.get(key, b""))
        return _cp(0)

    # ── minio boundary (mc via docker exec) ───────────────────────

    @staticmethod
    def _strip_bucket(a):
        return a.replace("hiclaw/hiclaw-storage/", "")

    def _mc_pipe(self, key, input_bytes):
        if key == "%s/lock" % ex.HICLAW_TX_PREFIX and \
                self.fail_lock_put:
            return _cp(1)
        role = self._role_of_key(key)
        if role in self.fail_canonical_put:
            return _cp(1)
        if role in self.fail_canonical_put_once:
            self.fail_canonical_put_once.discard(role)
            return _cp(1)
        if role in self.corrupt_canonical_put:
            self.objects[key] = self.corrupt_canonical_put[role]
            self.canonical_put_calls += 1
            return _cp(0)
        if role in self.corrupt_canonical_put_once:
            self.objects[key] = \
                self.corrupt_canonical_put_once.pop(role)
            self.canonical_put_calls += 1
            return _cp(0)
        self.objects[key] = input_bytes or b""
        if role is not None:      # canonical mutation (not tx/lock)
            self.canonical_put_calls += 1
        return _cp(0)

    def make_minio_exec(self):
        world = self

        def mc_exec(argv, check=True, timeout=60, input_bytes=None,
                    **_):
            argv = list(argv)
            world.minio_calls.append(argv)
            sub = argv[1] if len(argv) > 1 else ""
            rest = [a for a in argv[2:] if not a.startswith("--")]
            key = world._strip_bucket(rest[0]) if rest else None
            if sub == "stat":
                if key in world.objects:
                    return _cp(0, ("Name: x\nDate: now\nSize: %d B\n"
                                   "ETag: %s\n"
                                   % (len(world.objects[key]),
                                      _etag(world.objects[key])))
                              .encode())
                return _cp(1, b"")
            if sub == "ls":
                keys = [k for k in world.objects
                        if (key or "") == "" or k.startswith(key)]
                out = "".join("[2026-08-21] %d B STANDARD %s\n"
                              % (len(world.objects[k]), k)
                              for k in sorted(keys))
                return _cp(0, out.encode())
            if sub == "copy":
                # the deployed mc does not recognize `copy` — the
                # fake must be no more permissive than production
                return _cp(1, b"mc: `copy` is not a recognized command")
            if sub == "cp":
                src = world._strip_bucket(rest[0])
                dst = world._strip_bucket(rest[1])
                if src not in world.objects:
                    return _cp(1, b"")
                role = world._role_of_tx_key(dst)
                if role in world.fail_tx_copy:
                    return _cp(1, b"")
                data = world.objects[src]
                if role in world.corrupt_tx_copy:
                    data = data + b"-corrupted"
                world.objects[dst] = data
                return _cp(0, b"")
            if sub == "rm":
                if key in world.fail_mc_rm:
                    return _cp(1, b"")
                world.objects.pop(key, None)
                return _cp(0, b"")
            if sub == "cat":
                if world.fail_mc_cat:
                    return _cp(1, b"")
                if key in world.objects:
                    body = world.objects[key]
                    if key in world.short_read_keys:
                        body = body[:-1]     # truncated response
                    return _cp(0, body)
                return _cp(1, b"")
            if sub == "pipe":
                return world._mc_pipe(key, input_bytes)
            return _cp(0)
        return mc_exec

    # ── key helpers ───────────────────────────────────────────────

    @staticmethod
    def _role_of_container(name):
        for r in hw.ROLES:
            if ex.HICLAW_ROLE_FREEZE[r][0] == name:
                return r
        return None

    @staticmethod
    def _role_of_key(key):
        for r in hw.ROLES:
            if ex.hiclaw_role_canonical_key(r) == key:
                return r
        return None

    @staticmethod
    def _role_of_tx_key(key):
        # tx backup key: mp-gh4-tx/<session>/<role>/mcporter.json
        parts = key.split("/")
        if len(parts) >= 4 and parts[0] == ex.HICLAW_TX_PREFIX:
            if parts[-2] in hw.ROLES:
                return parts[-2]
        return None


class _McShim:
    """Routes MinioAdapter._checked/_exec to the mc simulator,
    stripping the ["exec","hiclaw-controller","mc"] docker prefix the
    adapter prepends; sh -c mc operations stay on the docker path
    (exactly like the real controller). MIRRORS the production
    DockerAdapter rc semantics: _checked raises on rc!=0."""

    def __init__(self, world):
        self._world = world
        self._mc = world.make_minio_exec()
        self.calls = []

    @staticmethod
    def _strip(argv):
        argv = list(argv)
        if argv[:3] == ["exec", "hiclaw-controller", "mc"]:
            return ["mc"] + argv[3:]
        return None

    def _checked(self, argv, *, input_bytes=None):
        stripped = self._strip(argv)
        if stripped is not None:
            cp = self._mc(stripped, check=True,
                          input_bytes=input_bytes)
            if getattr(cp, "returncode", 0) != 0:
                raise hw.HarnessError(
                    "HARNESS_APPLY_FAILED",
                    "%s rc=%d" % (stripped[1], cp.returncode))
            return cp
        return self._world.docker_exec(argv, check=True,
                                       input_bytes=input_bytes)

    def _exec(self, argv, check=True, timeout=60, input_bytes=None,
              **_):
        stripped = self._strip(argv)
        if stripped is not None:
            return self._mc(stripped, check=check, timeout=timeout,
                            input_bytes=input_bytes)
        return self._world.docker_exec(argv, check=check,
                                       timeout=timeout,
                                       input_bytes=input_bytes)


class HarnessTestBase(unittest.TestCase):
    """Fast convergence budgets + fresh adapters per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / "journal.json"
        self.receipt = self.root / "receipt.json"
        import unittest.mock
        self._conv_patcher = unittest.mock.patch.dict(
            "e2e_executors.HICLAW_CONVERGENCE", {
                "manager": {"poll_seconds": 0.004,
                            "timeout_seconds": 0.4,
                            "stability_checks": 2},
                "worker": {"poll_seconds": 0.004,
                           "timeout_seconds": 0.4,
                           "stability_checks": 1},
            })
        self._conv_patcher.start()
        self.addCleanup(self._conv_patcher.stop)

    def _adapters(self, world):
        docker = hw.DockerAdapter(world.docker_exec)
        minio = hw.MinioAdapter(world.docker_exec)
        minio._docker = _McShim(world)
        return docker, minio

    def _apply(self, world, *, journal=None, receipt=None,
               session="s", **kw):
        docker, minio = self._adapters(world)
        return hw.apply(journal_path=journal or self.journal,
                        receipt_path=receipt or self.receipt,
                        docker=docker, minio=minio,
                        session=session, **kw)

    def _lock_state(self, world):
        import json as _json
        raw = world.objects.get("%s/lock" % ex.HICLAW_TX_PREFIX)
        if raw is None:
            return None
        try:
            return _json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {"state": "unparseable"}

    def _lock_released(self, world):
        doc = self._lock_state(world)
        return bool(doc) and doc.get("state") == hw.LOCK_STATE_RELEASED

    def _replace_lock_foreign(self, world, session,
                              foreign_session):
        """A foreign actor takes the lock via the REAL conditional
        replace primitive (adapter-path injection, never a raw
        dict write)."""
        _, minio = self._adapters(world)
        status, cur_etag, cur = minio.cond_get_match(
            "%s/lock" % ex.HICLAW_TX_PREFIX,
            expect_prefix=ex.HICLAW_TX_PREFIX)
        body = hw._lock_body(foreign_session, foreign_session,
                             hw.LOCK_STATE_HELD)
        st2, _etag, _ = minio.cond_put_match(
            "%s/lock" % ex.HICLAW_TX_PREFIX, body, cur_etag,
            expect_prefix=ex.HICLAW_TX_PREFIX)
        assert st2 == 200, st2

    def _fixture_lock(self, world, session):
        """Acquire a REAL held lock through the production adapter
        and return the journal fields a crash-state fixture needs."""
        _, minio = self._adapters(world)
        info = hw._tx_lock(minio, session, session)
        return {"tx_lock": info["key"],
                "tx_lock_session": info["session"],
                "tx_lock_txid": info["txid"],
                "tx_lock_etag": info["etag"],
                "tx_lock_state": "acquired"}

    def _rollback(self, world, journal, *, session="s", **kw):
        docker, minio = self._adapters(world)
        return hw.rollback(journal_path=journal, docker=docker,
                           minio=minio, session=session, **kw)

    # ── v2 journal fixtures (hand-built crash states) ─────────────

    def _stage_mutated_role(self, world, session, role, status):
        """Role's external state at target; tx backup holds the
        legacy bytes; returns the journal entry (v2 shape)."""
        txkey = "%s/%s/%s/mcporter.json" % (
            ex.HICLAW_TX_PREFIX, session, role)
        world.objects[txkey] = world.legacy_bytes(role)
        before = world.legacy_bytes(role)
        return {"status": status, "backup_key": txkey,
                "before_live": _sha(before),
                "before_canonical": _sha(before),
                "before_etag": _etag(before)}

    def _write_journal(self, journal_path, session, roles, *,
                       receipt_state=None, receipt_path=None,
                       sha=None, residue=None, tx_lock=None):
        doc = {"ownership": hw.HARNESS_IDENTITY, "session": session,
               "status": "in-progress", "roles": roles}
        if tx_lock:
            doc["tx_lock"] = tx_lock
        if receipt_state is not None:
            doc["receipt_state"] = receipt_state
            doc["receipt_path"] = str(receipt_path)
            doc["receipt_session"] = session
            doc["receipt_sha256"] = sha or "0" * 64
        if residue is not None:
            doc["rollback_residue"] = residue
        journal_path.write_text(json.dumps(doc), encoding="utf-8")


def _journal_writer(**policy):
    """AtomicFileWriter subclass failing journal persists per policy:
    fail_ordinals={2} (Nth journal persist) and/or
    fail_body_contains='"status": "complete"' (phase-exact)."""
    fail_ordinals = policy.get("fail_ordinals", set())
    fail_body = policy.get("fail_body_contains")

    class W(hw.AtomicFileWriter):
        count = 0

        @classmethod
        def write(cls, path, data, *, root=None):
            if str(path).endswith("journal.json"):
                W.count += 1
                if W.count in fail_ordinals:
                    raise OSError("disk full (ordinal %d)" % W.count)
                if fail_body and fail_body in data.decode(
                        "utf-8", "replace"):
                    raise OSError("disk full (phase)")
            return hw.AtomicFileWriter.write(path, data, root=root)
    return W()


class TestReadOnlyCommands(HarnessTestBase):

    def test_inspect_identifies_default_gateway_and_targets(self):
        world = FakeSyncWorld(
            legacy_artifacts=("manager/config/mcporter.json"
                              ".mp-gh4-bak",))
        docker, minio = self._adapters(world)
        state = hw.inspect_roles(docker, minio)
        for role in hw.ROLES:
            info = state["roles"][role]
            self.assertTrue(info["running"])
            self.assertTrue(info["ip_matches"])
            self.assertFalse(info["already_target"])
            self.assertIn("aigw-local.hiclaw.io",
                          " ".join(info["current_gateway_urls"]))
            self.assertEqual(info["target_gateway_url"], TARGET[role])
            self.assertEqual(info["sync_mode"],
                             ex.hiclaw_role_sync_mode(role))
            self.assertEqual(
                info["canonical_key"],
                ex.hiclaw_role_canonical_key(role))
            self.assertEqual(info["canonical_sha256"],
                             _sha(world.canonical_bytes(role)))
            self.assertRegex(info["canonical_etag"], r"^[0-9a-f]{32}$")
        self.assertEqual(state["old_github_mcp"]["state"], "exited")
        # a STOPPED role is reported honestly (running=false, no
        # config read) instead of crashing the read-only inventory
        world2 = FakeSyncWorld()
        world2.running_false = {"verifier"}
        d2, m2 = self._adapters(world2)
        state2 = hw.inspect_roles(d2, m2)
        self.assertFalse(state2["roles"]["verifier"]["running"])
        self.assertIsNone(state2["roles"]["verifier"]["live_sha256"])
        self.assertEqual(
            state2["roles"]["verifier"]["current_gateway_urls"], [])
        self.assertTrue(state2["roles"]["manager"]["running"])
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.plan(self.journal, d2, m2)
        self.assertEqual(ctx.exception.code, "HARNESS_ROLE_MISSING")
        self.assertEqual(world2.live_write_calls, 0)
        # legacy artifacts: SAFE METADATA ONLY — keys listed, body
        # never read (no cat/cat-bounded audit entry for the key)
        self.assertEqual(state["legacy_sync_artifacts"],
                         ["manager/config/mcporter.json.mp-gh4-bak"])
        audited = json.dumps(
            world.docker_calls + world.minio_calls)
        self.assertNotIn("cat-bounded", audited)
        self.assertNotIn("legacy-backup-body", audited)

    def test_plan_zero_writes(self):
        world = FakeSyncWorld()
        docker, minio = self._adapters(world)
        result = hw.plan(self.journal, docker, minio)
        self.assertEqual(result["writes_executed"], 0)
        self.assertEqual(len(result["actions"]), 4)
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(world.tx_keys(), [])

    def test_plan_fail_closed_details(self):
        world = FakeSyncWorld(
            legacy_artifacts=("manager/config/mcporter.json"
                              ".mp-gh4-bak",
                              "agents/fixer/config/mcporter.json"
                              ".mp-gh4-bak"))
        docker, minio = self._adapters(world)
        result = hw.plan(self.journal, docker, minio)
        # direction-aware actions with correct targets/keys/paths
        for action in result["actions"]:
            role = action["role"]
            self.assertEqual(action["sync_mode"],
                             ex.hiclaw_role_sync_mode(role))
            self.assertEqual(action["mutation_target"],
                             "live" if role == "manager"
                             else "canonical")
            self.assertEqual(action["target_gateway"], TARGET[role])
            self.assertEqual(action["canonical_key"],
                             ex.hiclaw_role_canonical_key(role))
            self.assertEqual(action["live_path"],
                             ex.hiclaw_role_live_config_path(role))
            self.assertFalse(action["noop"])
        self.assertTrue(result["sync_fingerprint_ok"])
        self.assertEqual(len(result["legacy_sync_artifacts"]), 2)
        # both fail-closed triggers present, still zero writes
        self.assertTrue(result["apply_would_fail_closed"])
        self.assertEqual(result["writes_executed"], 0)
        self.assertFalse(self.journal.exists())
        # fingerprint drift alone also fails closed
        world2 = FakeSyncWorld(fingerprint_ok=False)
        d2, m2 = self._adapters(world2)
        plan2 = hw.plan(self.journal, d2, m2)
        self.assertFalse(plan2["sync_fingerprint_ok"])
        self.assertTrue(plan2["apply_would_fail_closed"])
        # status on an ABSENT journal fails closed with a stable
        # code (never a raw traceback)
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.status(self.root / "no-such-journal.json")
        self.assertEqual(ctx.exception.code, "HARNESS_JOURNAL_ABSENT")


class TestApply(HarnessTestBase):

    def test_four_role_success_receipt_validated(self):
        world = FakeSyncWorld()
        lock_seen = {}

        def hook(phase, role):
            if phase == "manager_live_applying":
                lock_seen["key"] = "%s/lock" % ex.HICLAW_TX_PREFIX
                lock_seen["held"] = lock_seen["key"] in world.objects

        self._apply(world, session="ok1", phase_hook=hook)
        # exclusive lock WAS held mid-transaction; after success it
        # is a RELEASED tombstone (never deleted — the server cannot
        # make conditional delete safe)
        self.assertTrue(lock_seen.get("held"))
        self.assertTrue(self._lock_released(world), self._lock_state(world))
        journal_doc = json.loads(self.journal.read_text())
        self.assertEqual(journal_doc["tx_lock_state"], "released")
        for role in hw.ROLES:
            self.assertTrue(world.role_at_target(role), role)
            self.assertEqual(_sha(world.live_bytes(role)),
                             _sha(world.canonical_bytes(role)))
        journal = json.loads(self.journal.read_text())
        self.assertEqual(journal["status"], "complete")
        self.assertEqual(journal["ownership"], hw.HARNESS_IDENTITY)
        # receipt passes the PRODUCTION validator on the same world
        docker, minio = self._adapters(world)
        verdict = ex.validate_hiclaw_receipt(
            str(self.receipt), docker_executor=docker._exec,
            minio_executor=ex.minio_readonly_via_docker(
                minio._docker._exec),
            expected_old_mcp_state="stopped")
        self.assertTrue(verdict["verified"], verdict)
        # §4A/B direction: apply NEVER writes a worker live config
        # and NEVER writes the manager canonical object directly —
        # convergence happens ONLY via the simulated sync ticks
        for argv in world.docker_calls:
            if len(argv) > 4 and argv[4] == "sh" \
                    and "cat >" in argv[-1]:
                role = FakeSyncWorld._role_of_container(argv[
                    argv.index("sh") - 1] if "sh" in argv else "")
                container = argv[2] if not argv[1].startswith("-") \
                    else argv[3]
                role = FakeSyncWorld._role_of_container(container)
                self.assertEqual(role, "manager",
                                 "worker live written directly: %r"
                                 % (argv,))
        for argv in world.minio_calls:
            if argv[1:2] == ["pipe"]:
                key = FakeSyncWorld._strip_bucket(argv[2])
                self.assertNotEqual(
                    key, ex.hiclaw_role_canonical_key("manager"),
                    "manager canonical written directly")
        # tx prefix fully cleaned apart from the released tombstone
        self.assertEqual(
            [k for k in world.tx_keys()
             if k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])
        self.assertTrue(self.receipt.exists())

    def test_apply_idempotent_when_all_at_target(self):
        world = FakeSyncWorld()
        for role in hw.ROLES:
            world.set_role_target(role)
        result = self._apply(world, session="s2")
        self.assertEqual(result["result"], "idempotent-noop")
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)
        self.assertFalse(self.journal.exists())
        self.assertIsNone(self._lock_state(world))

    def test_apply_failure_for_every_role(self):
        # §4C matrix: EVERY role × {mutation failure, convergence
        # failure(never), convergence failure(drift-once)}.
        for fail_role in hw.ROLES:
            for mode in ("mutation", "never", "drift-once"):
                with self.subTest(role=fail_role, mode=mode):
                    j = self.root / ("j-af-%s-%s.json"
                                     % (fail_role, mode))
                    r = self.root / ("r-af-%s-%s.json"
                                     % (fail_role, mode))
                    world = FakeSyncWorld()
                    if mode == "mutation":
                        if fail_role == "manager":
                            world.fail_live_write_once = {"manager"}
                        else:
                            world.fail_canonical_put_once = {fail_role}
                        expected = "HARNESS_APPLY_FAILED"
                    elif fail_role == "manager":
                        world.push_mode["manager"] = "never"
                        expected = \
                            "HARNESS_MANAGER_PUSH_CONVERGENCE_TIMEOUT"
                    else:
                        world.pull_mode[fail_role] = "never"
                        expected = \
                            "HARNESS_WORKER_PULL_TRIGGER_FAILED"
                    with self.assertRaises(hw.HarnessError) as ctx:
                        self._apply(world, journal=j, receipt=r,
                                    session="af-%s-%s"
                                    % (fail_role, mode))
                    self.assertEqual(ctx.exception.code, expected)
                    self.assertFalse(r.exists())
                    journal = json.loads(j.read_text())
                    if mode == "mutation":
                        self.assertEqual(journal["status"],
                                         "rolled-back")
                        self.assertEqual(
                            journal["rollback_residue"], [])
                        for role in hw.ROLES:
                            self.assertTrue(world.role_at_legacy(role),
                                            role)
                    else:
                        # the production side NEVER drifted, so the
                        # rollback is trivially consistent: clean
                        # convergence back to legacy on both sides
                        self.assertEqual(journal["status"],
                                         "rolled-back")
                        self.assertEqual(
                            journal["rollback_residue"], [])
                        for role in hw.ROLES:
                            self.assertTrue(world.role_at_legacy(role),
                                            role)
                    # later roles were never mutated at all
                    idx = hw.ROLES.index(fail_role)
                    for role in hw.ROLES[idx + 1:]:
                        self.assertTrue(world.role_at_legacy(role))
                    # zero secrets anywhere
                    blob = j.read_text()
                    self.assertNotIn("Bearer", blob)
                    self.assertNotIn("secret-", blob)
                    # lock always tombstoned released (never deleted)
                    self.assertTrue(self._lock_released(world),
                                    self._lock_state(world))
        # drift-once: two stable checks are REQUIRED — a single
        # matching sample must NOT let manager complete
        world = FakeSyncWorld()
        world.push_mode["manager"] = "drift-once"
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=self.root / "j-drift.json",
                        receipt=self.root / "r-drift.json",
                        session="drift")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_MANAGER_PUSH_CONVERGENCE_TIMEOUT")

    def test_verify_failure_for_every_role(self):
        # read-back drift per role: manager live read-back sees other
        # bytes; worker canonical hashes to something else after put
        for fail_role in hw.ROLES:
            with self.subTest(role=fail_role):
                j = self.root / ("j-vf-%s.json" % fail_role)
                r = self.root / ("r-vf-%s.json" % fail_role)
                world = FakeSyncWorld()
                if fail_role == "manager":
                    expected = "HARNESS_LIVE_WRITE_VERIFY_FAILED"

                    def arm(phase, role):
                        if phase == "manager_live_written":
                            world.drift_live_read = {
                                "manager": b'{"gh":{"url":"drift"}}'}
                else:
                    expected = "HARNESS_CANONICAL_VERIFY_FAILED"
                    world.corrupt_canonical_put_once = {
                        fail_role: b'{"corrupted": true}'}

                    def arm(phase, role):
                        return None
                with self.assertRaises(hw.HarnessError) as ctx:
                    self._apply(world, journal=j, receipt=r,
                                session="vf-%s" % fail_role,
                                phase_hook=arm)
                self.assertEqual(ctx.exception.code, expected)
                # the failing role's mutation DID happen first
                if fail_role == "manager":
                    self.assertGreaterEqual(world.live_write_calls, 1)
                else:
                    self.assertGreaterEqual(
                        world.canonical_put_calls, 1)
                self.assertFalse(r.exists())
                journal = json.loads(j.read_text())
                self.assertEqual(journal["status"], "rolled-back")
                self.assertEqual(journal["rollback_residue"], [])
                self.assertEqual(
                    journal["roles"][fail_role]["status"],
                    "rolled-back")
                for role in hw.ROLES:
                    self.assertTrue(world.role_at_legacy(role), role)

    def test_identity_drift_refused_before_writes(self):
        world = FakeSyncWorld()
        world.ip_drift_role = "verifier"
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="s5")
        self.assertEqual(ctx.exception.code, "HARNESS_IDENTITY_DRIFT")
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)
        self.assertEqual(world.tx_keys(), [])

    def test_missing_role_refused(self):
        # §4D refusal family: not-running, lock-create failure,
        # foreign tx-backup object, corrupt tx-backup copy — all
        # fail closed with ZERO mutations and no journal residue.
        cases = []
        w1 = FakeSyncWorld()
        w1.running_false = {"verifier"}
        cases.append((w1, "HARNESS_ROLE_MISSING", "not-running",
                      False))
        w2 = FakeSyncWorld()
        w2.fail_lock_put = True
        cases.append((w2, "HARNESS_TX_LOCK_UNAVAILABLE",
                      "lock-create-fail", False))
        w3 = FakeSyncWorld()
        foreign = "%s/%s/manager/mcporter.json" % (
            ex.HICLAW_TX_PREFIX, "mr-foreign-tx-object")
        w3.objects[foreign] = b'{"foreign": true}'
        # preflight-stage refusal: journal exists but rolled back
        cases.append((w3, "HARNESS_TX_BACKUP_EXISTS",
                      "foreign-tx-object", True))
        w4 = FakeSyncWorld()
        w4.corrupt_tx_copy = {"manager"}
        cases.append((w4, "HARNESS_TX_BACKUP_VERIFY_FAILED",
                      "corrupt-tx-copy", True))
        for world, code, label, journal_expected in cases:
            with self.subTest(case=label):
                j = self.root / ("j-mr-%s.json" % label)
                r = self.root / ("r-mr-%s.json" % label)
                with self.assertRaises(hw.HarnessError) as ctx:
                    self._apply(world, journal=j, receipt=r,
                                session="mr-%s" % label)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(world.live_write_calls, 0)
                self.assertEqual(world.canonical_put_calls, 0)
                self.assertEqual(j.exists(), journal_expected)
                if journal_expected:
                    disk = json.loads(j.read_text())
                    self.assertEqual(disk["status"], "rolled-back")
                    self.assertEqual(disk["rollback_residue"], [])
                # nothing under the tx prefix survives (lock released)
                self.assertEqual(
                    [k for k in world.tx_keys()
                     if k != foreign
                     and k != "%s/lock"
                     % ex.HICLAW_TX_PREFIX], [])
                # a foreign tx object is never overwritten/deleted
                if label == "foreign-tx-object":
                    self.assertEqual(world.objects[foreign],
                                     b'{"foreign": true}')
        # real-stack regression: tolerant MinIO probes (exists/list)
        # must NEVER raise when the controller is unreachable — the
        # production DockerAdapter raises on rc!=0 for checked calls,
        # and _mc used to route check=False paths through it
        def failing_exec(argv, check=True, **_):
            return _cp(1, b"")

        minio = hw.MinioAdapter(failing_exec)
        self.assertFalse(minio.exists("any/key"))
        self.assertEqual(minio.list_prefix(""), [])
        with self.assertRaises(hw.HarnessError):
            minio.stat("any/key")     # checked probe stays fail-closed

    def test_foreign_journal_refused(self):
        foreign = json.dumps({"ownership": "someone-else"})
        self.journal.write_text(foreign, encoding="utf-8")
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="s7")
        self.assertEqual(ctx.exception.code, "HARNESS_FOREIGN_JOURNAL")
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(self.journal.read_text(), foreign)

    def test_rollback_failure_preserves_primary(self):
        # reviewer canonical put fails (primary); manager live restore
        # also fails during the automatic rollback: the primary error
        # survives, rollback errors live in diagnostics only
        world = FakeSyncWorld()

        def arm(phase, role):
            if phase == "manager_converged":
                world.fail_live_write = {"manager"}
                world.fail_canonical_put = {"reviewer"}

        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="s8", phase_hook=arm)
        self.assertEqual(ctx.exception.code, "HARNESS_APPLY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any(d.startswith("ROLLBACK_FAILED:manager")
                            for d in diags), diags)
        journal = json.loads(self.journal.read_text())
        self.assertEqual(journal["status"], "rollback-failed")
        self.assertIn("role:manager", journal["rollback_residue"])
        self.assertFalse(self.receipt.exists())
        # the held lock survives a failed rollback (retry contract);
        # it is only tombstoned after a clean one
        self.assertEqual(self._lock_state(world).get("state"),
                         hw.LOCK_STATE_HELD)


class TestRollbackCommand(HarnessTestBase):

    def _crash_at(self, phase, role, journal, receipt, session="cr"):
        world = FakeSyncWorld()

        def hook(ph, rl):
            if ph == phase and rl == role:
                raise CrashSimulated(ph)
        try:
            self._apply(world, journal=journal, receipt=receipt,
                        session=session, phase_hook=hook)
        except CrashSimulated:
            pass
        return world

    def test_crash_journal_explicit_rollback_restores(self):
        j = self.root / "j-cr1.json"
        r = self.root / "r-cr1.json"
        world = self._crash_at("manager_converged", "manager", j, r)
        self.assertFalse(r.exists())
        fresh = world.clone_for_recovery()
        result = self._rollback(fresh, j, session="cr")
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertTrue(fresh.role_at_legacy("manager"))
        self.assertEqual(result["residue"], [])
        self.assertEqual(
            [k for k in fresh.tx_keys()
             if k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])
        self.assertTrue(self._lock_released(fresh),
                        self._lock_state(fresh))
        disk = json.loads(j.read_text())
        self.assertEqual(disk["status"], "rolled-back")
        self.assertEqual(disk["tx_lock_state"], "released")

    def test_rollback_idempotent(self):
        j = self.root / "j-cr2.json"
        r = self.root / "r-cr2.json"
        world = self._crash_at("manager_converged", "manager", j, r)
        fresh = world.clone_for_recovery()
        self._rollback(fresh, j, session="cr")
        second = self._rollback(fresh, j, session="cr")
        self.assertEqual(second["rolled_back"], [])
        self.assertEqual(second["residue"], [])

    def test_in_flight_journal_blocks_new_apply(self):
        j = self.root / "j-cr3.json"
        r = self.root / "r-cr3.json"
        world = self._crash_at("manager_converged", "manager", j, r)
        fresh = world.clone_for_recovery()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(fresh, journal=j, receipt=r, session="new")
        self.assertEqual(ctx.exception.code, "HARNESS_FOREIGN_JOURNAL")


class TestCrashRecovery(HarnessTestBase):

    WINDOWS = (
        ("manager_live_written", "manager"),   # write ran, not durable
        ("manager_live_mutated", "manager"),   # verified, pre-push
        ("canonical_written", "reviewer"),     # put ran, not durable
        ("manager_converged", "manager"),      # converged, pre-receipt
        ("canonical_mutated", "verifier"),     # multi-role mid crash
    )

    def _crash_at(self, phase, role, journal, receipt):
        world = FakeSyncWorld()

        def hook(ph, rl):
            if ph == phase and rl == role:
                raise CrashSimulated(ph)
        try:
            self._apply(world, journal=journal, receipt=receipt,
                        session="crash", phase_hook=hook)
        except CrashSimulated:
            pass
        return world

    def test_crash_window_recovery(self):
        for phase, role in self.WINDOWS:
            with self.subTest(window=phase, role=role):
                j = self.root / ("j-cw-%s-%s.json" % (phase, role))
                r = self.root / ("r-cw-%s-%s.json" % (phase, role))
                world = self._crash_at(phase, role, j, r)
                # partial transaction: NO receipt, lock + backups held
                self.assertFalse(r.exists())
                self.assertIn("%s/lock" % ex.HICLAW_TX_PREFIX,
                              world.objects)
                self.assertEqual(
                    self._lock_state(world).get("state"),
                    hw.LOCK_STATE_HELD)
                # FRESH instance: recovery from the disk journal only,
                # external state migrated, harness memory not
                fresh = world.clone_for_recovery()
                result = self._rollback(fresh, j, session="crash")
                mutated = hw.ROLES[:hw.ROLES.index(role) + 1]
                self.assertEqual(result["rolled_back"],
                                 list(reversed(mutated)))
                for rl in hw.ROLES:
                    self.assertTrue(fresh.role_at_legacy(rl),
                                    "%s not recovered" % rl)
                self.assertEqual(result["residue"], [])
                self.assertEqual(
                    [k for k in fresh.tx_keys()
                     if k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])
                self.assertTrue(self._lock_released(fresh),
                                self._lock_state(fresh))
                self.assertFalse(r.exists())
                second = self._rollback(fresh, j, session="crash")
                self.assertEqual(second["rolled_back"], [])

    def test_crash_no_foreign_touch(self):
        j = self.root / "j-cf.json"
        r = self.root / "r-cf.json"
        world = self._crash_at("canonical_mutated", "fixer", j, r)
        foreign = self.root / "foreign.txt"
        foreign.write_text("untouched", encoding="utf-8")
        fresh = world.clone_for_recovery()
        self._rollback(fresh, j, session="crash")
        self.assertEqual(foreign.read_text(), "untouched")


class TestJournalPersistFailures(HarnessTestBase):
    """Persist-failure windows with precise consequences. Journal
    persist ordinals in v2: 1 init; 2-5 per-role WAL; 6-8 manager
    (applying/mutated/converged); 9-17 workers; 18 publishing; 19
    published; 20 complete; 21 final."""

    def test_write_ahead_persist_failure_zero_writes(self):
        # ordinal 2 = the FIRST per-role write-ahead persist
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="p1",
                        writer=_journal_writer(
                            fail_body_contains='"backup_copying"'))
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)
        self.assertFalse(self.receipt.exists())
        # the lock is tombstoned RELEASED (never a stale held lock)
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))

    def test_mutated_persist_failure_rolls_back_role(self):
        # ordinal 7 = manager's post-live-write persist: the live
        # config was REALLY rewritten, journal on disk still says
        # applying — rollback must restore it
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="p2",
                        writer=_journal_writer(
                            fail_body_contains='"manager_live_mutated"'))
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        # live written once by the mutation, once more by the
        # rollback restore — and nothing else
        self.assertEqual(world.live_write_calls, 2)
        self.assertTrue(world.role_at_legacy("manager"))
        self.assertFalse(self.receipt.exists())

    def test_verified_persist_failure_rolls_back_progress(self):
        # ordinal 8 = manager converged persist
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="p3",
                        writer=_journal_writer(
                            fail_body_contains='"manager_converged"'))
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertTrue(world.role_at_legacy("manager"))
        for role in WORKERS:
            self.assertTrue(world.role_at_legacy(role))
        self.assertFalse(self.receipt.exists())

    def test_complete_persist_failure_no_trusted_state(self):
        # persist succeeds through every stage INCLUDING the receipt
        # publish + ownership persist, fails only at complete: the
        # published receipt must be removed (no trusted state) and
        # everything rolled back
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="p4",
                        writer=_journal_writer(
                            fail_body_contains='"status": "complete"'))
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        journal = json.loads(self.journal.read_text())
        self.assertNotEqual(journal["status"], "complete")
        self.assertFalse(self.receipt.exists())
        for role in hw.ROLES:
            self.assertTrue(world.role_at_legacy(role), role)
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))
        self.assertEqual(
            [k for k in world.tx_keys()
             if k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])


class TestReceiptOwnership(HarnessTestBase):
    """R3: receipt ownership — pre-existing/foreign targets are never
    read, overwritten or deleted; exclusive publish loses races
    fail-closed; only THIS session's receipt is cleaned."""

    def test_preexisting_receipt_refused(self):
        foreign = b'{"foreign": true, "keep": 1}'
        self.receipt.write_bytes(foreign)
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="own-a")
        self.assertEqual(ctx.exception.code, "HARNESS_RECEIPT_EXISTS")
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.receipt.read_bytes(), foreign)
        self.assertIsNone(self._lock_state(world))
        self.assertEqual(world.tx_keys(), [])

    def test_receipt_reparse_refused(self):
        outside = self.root / "outside.txt"
        outside.write_text("foreign-target", encoding="utf-8")
        ctx_mgr = None
        try:
            self.receipt.symlink_to(outside)
        except (OSError, NotImplementedError):
            import unittest.mock
            ctx_mgr = unittest.mock.patch("os.path.islink",
                                          return_value=True)
        world = FakeSyncWorld()
        import contextlib
        with (ctx_mgr or contextlib.nullcontext()):
            with self.assertRaises(hw.HarnessError) as ctx:
                self._apply(world, session="own-b")
        self.assertIn(ctx.exception.code,
                      ("HARNESS_REPARSE_REFUSED",
                       "HARNESS_RECEIPT_EXISTS"))
        self.assertEqual(world.live_write_calls, 0)
        if ctx_mgr is None:
            self.assertEqual(outside.read_text(), "foreign-target")

    def test_created_by_foreign_process_before_commit_refused(self):
        # preflight saw no target; a foreign actor creates the receipt
        # between the publishing-intent persist and the exclusive
        # commit — the production exclusive writer must lose the race
        world = FakeSyncWorld()
        foreign = b'{"raced": true}'

        def hook(phase, role):
            if phase == "receipt_publishing_persisted":
                self.receipt.write_bytes(foreign)

        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="own-c", phase_hook=hook)
        self.assertEqual(ctx.exception.code, "HARNESS_RECEIPT_EXISTS")
        self.assertEqual(self.receipt.read_bytes(), foreign)
        for role in hw.ROLES:
            self.assertTrue(world.role_at_legacy(role), role)
        journal = json.loads(self.journal.read_text())
        self.assertNotEqual(journal["status"], "complete")
        self.assertEqual(journal["status"], "rollback-residue")
        self.assertIn("receipt:ownership-unverified",
                      journal["rollback_residue"])
        # roles ARE fully restored, so the lock is tombstoned even
        # though the foreign-receipt residue keeps the journal
        # non-clean (retry converges once an operator resolves it)
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))
        self.assertEqual(journal["tx_lock_state"], "released")

    def test_validator_failure_removes_only_session_receipt(self):
        world = FakeSyncWorld()
        bystander = self.root / "bystander.json"
        bystander.write_text("untouched", encoding="utf-8")

        def bad_validator(path):
            return {"verified": False, "checks": {}}

        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="own-d",
                        receipt_validator=bad_validator)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_RECEIPT_VALIDATION_FAILED")
        # OUR exclusively-created receipt is removed; foreign file
        # untouched; every role rolled back
        self.assertFalse(self.receipt.exists())
        for role in hw.ROLES:
            self.assertTrue(world.role_at_legacy(role), role)
        self.assertEqual(bystander.read_text(), "untouched")
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))
        self.assertEqual(
            [k for k in world.tx_keys()
             if k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])

    def test_complete_persist_failure_deletes_session_receipt_only(self):
        foreign = self.root / "foreign-receipt.json"
        foreign.write_text("foreign", encoding="utf-8")
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="own-e",
                        writer=_journal_writer(
                            fail_body_contains='"status": "complete"'))
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertFalse(self.receipt.exists())
        journal = json.loads(self.journal.read_text())
        self.assertNotEqual(journal["status"], "complete")
        for role in hw.ROLES:
            self.assertTrue(world.role_at_legacy(role), role)
        self.assertEqual(foreign.read_text(), "foreign")


class TestReceiptCrashRecovery(HarnessTestBase):
    """R4 core window: exclusive receipt published, the ownership
    persist never happened. Recovery must PROVE ownership
    cryptographically before deleting."""

    def test_post_publish_pre_ownership_persist_crash(self):
        j = self.root / "j-r4.json"
        r = self.root / "r-r4.json"
        world = FakeSyncWorld()

        def hook(phase, role):
            if phase == "receipt_published":
                raise CrashSimulated(phase)

        try:
            self._apply(world, journal=j, receipt=r, session="r4core",
                        phase_hook=hook)
        except CrashSimulated:
            pass
        # crash state: receipt EXISTS on disk, journal on disk still
        # claims publishing (the ownership persist never ran)
        self.assertTrue(r.exists())
        journal = json.loads(j.read_text())
        self.assertEqual(journal["receipt_state"], "publishing")
        self.assertEqual(journal["receipt_session"], "r4core")
        # FRESH instance: only the disk journal + receipt file
        fresh = world.clone_for_recovery()
        result = self._rollback(fresh, j, session="r4core")
        # ownership proven -> receipt deleted, all agents restored
        self.assertFalse(r.exists())
        self.assertEqual(result["rolled_back"],
                         ["verifier", "fixer", "reviewer", "manager"])
        for role in hw.ROLES:
            self.assertTrue(fresh.role_at_legacy(role), role)
        self.assertEqual(result["residue"], [])
        self.assertEqual(
            [k for k in fresh.tx_keys()
             if k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])
        self.assertTrue(self._lock_released(fresh),
                        self._lock_state(fresh))
        second = self._rollback(fresh, j, session="r4core")
        self.assertEqual(second["rolled_back"], [])


class TestReceiptForeignVariants(HarnessTestBase):
    """R4 §7 A-F: foreign/indeterminate receipt targets are never
    deleted; agents still roll back; diagnostics/residue honest."""

    def _journal_with_state(self, state, session, path, sha=None,
                            roles=("manager",)):
        world = FakeSyncWorld()
        for role in roles:
            world.set_role_target(role)
        entries = {}
        for role in roles:
            status = ("manager_converged" if role == "manager"
                      else "live_converged")
            entries[role] = self._stage_mutated_role(
                world, session, role, status)
        doc = {"ownership": hw.HARNESS_IDENTITY, "session": session,
               "status": "in-progress", "roles": entries,
               "receipt_state": state, "receipt_path": str(path),
               "receipt_session": session,
               "receipt_sha256": sha or "0" * 64,
               "tx_lock": "%s/lock" % ex.HICLAW_TX_PREFIX}
        world.objects["%s/lock" % ex.HICLAW_TX_PREFIX] = \
            b"%s:now" % session.encode()
        return world, doc

    def _rollback_fixture(self, world, doc, journal_path, session):
        journal_path.write_text(json.dumps(doc), encoding="utf-8")
        return self._rollback(world, journal_path, session=session)

    def test_a_publishing_without_receipt(self):
        r = self.root / "r-a.json"
        world, doc = self._journal_with_state(
            "publishing", "s-a", r)
        j = self.root / "j-a.json"
        result = self._rollback_fixture(world, doc, j, "s-a")
        self.assertFalse(r.exists())
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertEqual(result["residue"], [])
        disk = json.loads(j.read_text())
        self.assertNotIn("RECEIPT_OWNERSHIP_UNVERIFIED",
                         disk.get("rollback_diagnostics", []))
        self.assertTrue(world.role_at_legacy("manager"))

    def test_b_publishing_foreign_preempted(self):
        r = self.root / "r-b.json"
        foreign = b'{"something": "else"}'
        r.write_bytes(foreign)
        world, doc = self._journal_with_state(
            "publishing", "s-b", r)
        j = self.root / "j-b.json"
        result = self._rollback_fixture(world, doc, j, "s-b")
        self.assertTrue(r.exists())
        self.assertEqual(r.read_bytes(), foreign)
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])
        self.assertIn("RECEIPT_OWNERSHIP_UNVERIFIED",
                      result["diagnostics"])
        self.assertTrue(world.role_at_legacy("manager"))

    def test_c_published_then_foreign_replaced(self):
        # our real receipt published, crash, foreign swaps the file
        # with a self-consistent receipt of ANOTHER session
        j = self.root / "j-c.json"
        r = self.root / "r-c.json"
        world = FakeSyncWorld()

        def hook(phase, role):
            if phase == "receipt_published":
                raise CrashSimulated(phase)

        try:
            self._apply(world, journal=j, receipt=r, session="s-c",
                        phase_hook=hook)
        except CrashSimulated:
            pass
        replaced = json.loads(r.read_text())
        replaced["rewire_session"] = "someone-else"
        replaced["receipt_sha256"] = ex._compute_receipt_sha256(
            replaced)
        r.write_text(json.dumps(replaced), encoding="utf-8")
        fresh = world.clone_for_recovery()
        result = self._rollback(fresh, j, session="s-c")
        self.assertTrue(r.exists())          # NOT deleted
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])
        for role in hw.ROLES:
            self.assertTrue(fresh.role_at_legacy(role), role)

    def test_d_same_session_wrong_hash(self):
        r = self.root / "r-d.json"
        body = {"rewire_session": "s-d", "receipt_sha256": "f" * 64}
        r.write_text(json.dumps(body), encoding="utf-8")
        world, doc = self._journal_with_state(
            "publishing", "s-d", r, sha="0" * 64)
        j = self.root / "j-d.json"
        result = self._rollback_fixture(world, doc, j, "s-d")
        self.assertTrue(r.exists())
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])

    def test_e_hash_field_matches_canonical_does_not(self):
        r = self.root / "r-e.json"
        body = {"rewire_session": "s-e",
                "receipt_sha256": "0" * 64, "junk": True}
        r.write_text(json.dumps(body), encoding="utf-8")
        world, doc = self._journal_with_state(
            "publishing", "s-e", r, sha="0" * 64)
        j = self.root / "j-e.json"
        result = self._rollback_fixture(world, doc, j, "s-e")
        self.assertTrue(r.exists())
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])

    def test_f_malformed_oversized_reparse(self):
        # malformed JSON
        r1 = self.root / "r-f1.json"
        r1.write_bytes(b"{not json")
        world, doc = self._journal_with_state(
            "publishing", "s-f1", r1)
        j = self.root / "j-f1.json"
        result = self._rollback_fixture(world, doc, j, "s-f1")
        self.assertTrue(r1.exists())
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])
        self.assertTrue(world.role_at_legacy("manager"))
        # oversized (> _RECEIPT_MAX_BYTES)
        r2 = self.root / "r-f2.json"
        r2.write_bytes(b"x" * (hw._RECEIPT_MAX_BYTES + 1))
        world2, doc2 = self._journal_with_state(
            "publishing", "s-f2", r2)
        j2 = self.root / "j-f2.json"
        result2 = self._rollback_fixture(world2, doc2, j2, "s-f2")
        self.assertTrue(r2.exists())
        self.assertIn("receipt:ownership-unverified",
                      result2["residue"])
        self.assertTrue(world2.role_at_legacy("manager"))


class TestRollbackReporting(HarnessTestBase):
    """R6: honest split of config-restore vs backup-cleanup."""

    def _manager_fixture(self, journal_path, session, *,
                         residue=None, receipt_state=None,
                         receipt_path=None):
        world = FakeSyncWorld()
        world.set_role_target("manager")
        entry = self._stage_mutated_role(
            world, session, "manager", "manager_converged")
        doc = {"ownership": hw.HARNESS_IDENTITY, "session": session,
               "status": "in-progress",
               "roles": {"manager": entry},
               "tx_lock": "%s/lock" % ex.HICLAW_TX_PREFIX}
        world.objects["%s/lock" % ex.HICLAW_TX_PREFIX] = \
            b"%s:now" % session.encode()
        if residue is not None:
            doc["rollback_residue"] = residue
        if receipt_state is not None:
            doc["receipt_state"] = receipt_state
            doc["receipt_path"] = str(receipt_path)
            doc["receipt_session"] = session
            doc["receipt_sha256"] = "0" * 64
        journal_path.write_text(json.dumps(doc), encoding="utf-8")
        return world

    def test_backup_remove_failure_reported(self):
        # restore OK; tx-backup rm rc=1 -> reported, not swallowed,
        # not misreported as a restore failure; retried on rollback
        j = self.root / "j-brm.json"
        world = self._manager_fixture(j, "brm")
        txkey = json.loads(j.read_text())["roles"]["manager"][
            "backup_key"]
        world.fail_mc_rm = {txkey}
        result = self._rollback(world, j, session="brm")
        # config restored + role IS in rolled_back
        self.assertTrue(world.role_at_legacy("manager"))
        self.assertEqual(result["rolled_back"], ["manager"])
        # backup still exists; honest diagnostic + residue
        self.assertIn(txkey, world.objects)
        disk = json.loads(j.read_text())
        self.assertIn("TX_BACKUP_REMOVE_FAILED:manager",
                      disk["rollback_diagnostics"])
        self.assertIn("tx-backup:manager", disk["rollback_residue"])
        self.assertNotIn("role:manager", disk["rollback_residue"])
        self.assertEqual(disk["status"], "rollback-residue")
        self.assertEqual(disk["roles"]["manager"]["status"],
                         "rolled-back-with-residue")
        self.assertNotIn("Bearer", j.read_text())
        # second rollback RETRIES the cleanup (restore NOT re-run)
        writes_before = world.live_write_calls
        world.fail_mc_rm = set()
        second = self._rollback(world, j, session="brm")
        self.assertEqual(world.live_write_calls, writes_before)
        self.assertNotIn(txkey, world.objects)
        self.assertEqual(second["residue"], [])
        disk2 = json.loads(j.read_text())
        self.assertEqual(disk2["roles"]["manager"]["status"],
                         "rolled-back")
        self.assertEqual(disk2["status"], "rolled-back")

    def test_combined_role_receipt_backup_failures(self):
        # reviewer restore FAILS; fixer restore OK but tx-backup rm
        # rc=1; receipt ownership unverifiable (foreign bytes)
        j = self.root / "j-comb.json"
        r = self.root / "r-comb.json"
        foreign = b'{"foreign": true}'
        r.write_bytes(foreign)
        world = FakeSyncWorld()
        roles = {}
        for role in ("reviewer", "fixer"):
            world.set_role_target(role)
            status = ("canonical_mutated" if role == "fixer"
                      else "live_converged")
            roles[role] = self._stage_mutated_role(
                world, "comb", role, status)
        fixer_key = roles["fixer"]["backup_key"]
        world.fail_mc_rm = {fixer_key}
        world.fail_canonical_put = {"reviewer"}   # restore fails
        doc = {"ownership": hw.HARNESS_IDENTITY, "session": "comb",
               "status": "in-progress", "roles": roles,
               "receipt_state": "publishing",
               "receipt_path": str(r), "receipt_session": "comb",
               "receipt_sha256": "0" * 64,
               "tx_lock": "%s/lock" % ex.HICLAW_TX_PREFIX}
        world.objects["%s/lock" % ex.HICLAW_TX_PREFIX] = b"comb:now"
        j.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(hw.HarnessError) as ctx:
            self._rollback(world, j, session="comb")
        # primary error from the REAL restore exception
        self.assertEqual(ctx.exception.code, "HARNESS_ROLLBACK_FAILED")
        self.assertIn("ROLLBACK_FAILED:reviewer", ctx.exception.detail)
        disk = json.loads(j.read_text())
        diags = disk["rollback_diagnostics"]
        residue = disk["rollback_residue"]
        self.assertTrue(any(d.startswith("ROLLBACK_FAILED:reviewer")
                            for d in diags))
        for needed in ("TX_BACKUP_REMOVE_FAILED:fixer",
                       "RECEIPT_OWNERSHIP_UNVERIFIED"):
            self.assertIn(needed, diags)
        for needed in ("role:reviewer", "tx-backup:fixer",
                       "receipt:ownership-unverified"):
            self.assertIn(needed, residue)
        self.assertEqual(len(residue), len(set(residue)))
        # journal overall + per-role honesty
        self.assertEqual(disk["status"], "rollback-failed")
        self.assertEqual(disk["roles"]["reviewer"]["status"],
                         "rollback-failed")
        self.assertEqual(disk["roles"]["fixer"]["status"],
                         "rolled-back-with-residue")
        # resource end-states
        self.assertTrue(r.exists())
        self.assertEqual(r.read_bytes(), foreign)
        self.assertTrue(world.role_at_target("reviewer"))  # not restored
        self.assertTrue(world.role_at_legacy("fixer"))
        self.assertIn(fixer_key, world.objects)
        blob = json.dumps(disk) + ctx.exception.detail
        for forbidden in ("Bearer", "secret-", "foreign", "true"):
            self.assertNotIn(forbidden, blob)

    def test_auto_rollback_primary_preserved_with_backup_failure(self):
        # verify failure DURING apply + tx-backup rm failure during
        # the automatic rollback: the primary stays the verify error
        j = self.root / "j-auto.json"
        r = self.root / "r-auto.json"
        world = FakeSyncWorld()

        def arm(phase, role):
            if phase == "manager_live_written":
                world.drift_live_read = {
                    "manager": b'{"gh":{"url":"http://drift"}}'}
                txkey = "%s/%s/%s/mcporter.json" % (
                    ex.HICLAW_TX_PREFIX, "auto", "manager")
                world.fail_mc_rm = {txkey}

        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="auto",
                        phase_hook=arm)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_LIVE_WRITE_VERIFY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any("TX_BACKUP_REMOVE_FAILED:manager" in d
                            for d in diags), diags)
        self.assertFalse(r.exists())
        disk = json.loads(j.read_text())
        self.assertIn("tx-backup:manager", disk["rollback_residue"])
        self.assertTrue(world.role_at_legacy("manager"))

    def test_rollback_failed_role_retry_restores(self):
        # a restore-failed role converges on retry: restore
        # RE-EXECUTED, stale role: residue EXACTLY removed, backup
        # cleaned, overall rolled-back; third call idempotent
        j = self.root / "j-rf.json"
        world = self._manager_fixture(j, "rf")
        manager_container = ex.HICLAW_ROLE_FREEZE["manager"][0]
        world.fail_live_write = {"manager"}     # restore fails
        with self.assertRaises(hw.HarnessError) as ctx:
            self._rollback(world, j, session="rf")
        self.assertEqual(ctx.exception.code, "HARNESS_ROLLBACK_FAILED")
        d1 = json.loads(j.read_text())
        self.assertEqual(d1["roles"]["manager"]["status"],
                         "rollback-failed")
        self.assertIn("role:manager", d1["rollback_residue"])
        self.assertEqual(d1["status"], "rollback-failed")
        txkey = d1["roles"]["manager"]["backup_key"]
        self.assertIn(txkey, world.objects)
        writes_after_first = world.live_write_calls
        # 2nd: restore succeeds (fresh production call, same disk j)
        world.fail_live_write = set()
        result = self._rollback(world, j, session="rf")
        d2 = json.loads(j.read_text())
        self.assertGreater(world.live_write_calls,
                           writes_after_first)  # restore re-ran
        self.assertTrue(world.role_at_legacy("manager"))
        self.assertNotIn(txkey, world.objects)
        self.assertEqual(d2["roles"]["manager"]["status"],
                         "rolled-back")
        self.assertEqual(d2["rollback_residue"], [])
        self.assertEqual(d2["status"], "rolled-back")
        # 3rd: idempotent — no further restore attempts
        writes_before_third = world.live_write_calls
        third = self._rollback(world, j, session="rf")
        self.assertEqual(world.live_write_calls,
                         writes_before_third)
        self.assertEqual(third["rolled_back"], [])

    def test_backup_residue_retry_cleans_without_restore(self):
        # a backup-removal residue converges on retry WITHOUT
        # re-running the (already successful) restore
        j = self.root / "j-br.json"
        world = self._manager_fixture(j, "br")
        txkey = json.loads(j.read_text())["roles"]["manager"][
            "backup_key"]
        world.fail_mc_rm = {txkey}
        result = self._rollback(world, j, session="br")
        d1 = json.loads(j.read_text())
        writes_after_first = world.live_write_calls
        self.assertEqual(d1["roles"]["manager"]["status"],
                         "rolled-back-with-residue")
        self.assertIn("tx-backup:manager", d1["rollback_residue"])
        self.assertNotIn("role:manager", d1["rollback_residue"])
        self.assertIn(txkey, world.objects)
        self.assertEqual(d1["status"], "rollback-residue")
        # 2nd: rm now succeeds; restore MUST NOT re-run
        world.fail_mc_rm = set()
        result2 = self._rollback(world, j, session="br")
        d2 = json.loads(j.read_text())
        self.assertEqual(world.live_write_calls,
                         writes_after_first)    # no re-restore
        self.assertNotIn(txkey, world.objects)
        self.assertEqual(d2["roles"]["manager"]["status"],
                         "rolled-back")
        self.assertEqual(d2["rollback_residue"], [])
        self.assertEqual(d2["status"], "rolled-back")
        # 3rd: fully idempotent (no writes, no rm)
        rms_before = sum(1 for a in world.minio_calls
                         if a[1:2] == ["rm"])
        writes_before = world.live_write_calls
        self._rollback(world, j, session="br")
        self.assertEqual(world.live_write_calls, writes_before)
        self.assertEqual(sum(1 for a in world.minio_calls
                             if a[1:2] == ["rm"]), rms_before)

    def test_residue_convergence_preserves_unrelated_entries(self):
        # converging role:/tx-backup: entries never touches an
        # unrelated REAL residue (receipt:...); order stays stable
        j = self.root / "j-mix.json"
        world = self._manager_fixture(
            j, "mix",
            residue=["receipt:ownership-unverified", "role:manager"],
            receipt_state="publishing",
            receipt_path=self.root / "no-such-receipt.json")
        txkey = json.loads(j.read_text())["roles"]["manager"][
            "backup_key"]
        world.fail_mc_rm = {txkey}
        # 1st: restore OK, rm fails -> role: converged away,
        # receipt: and tx-backup: remain, order preserved
        self._rollback(world, j, session="mix")
        d1 = json.loads(j.read_text())
        self.assertNotIn("role:manager", d1["rollback_residue"])
        self.assertIn("tx-backup:manager", d1["rollback_residue"])
        self.assertIn("receipt:ownership-unverified",
                      d1["rollback_residue"])
        self.assertLess(
            d1["rollback_residue"].index(
                "receipt:ownership-unverified"),
            d1["rollback_residue"].index("tx-backup:manager"))
        self.assertEqual(d1["status"], "rollback-residue")
        # 2nd: rm succeeds -> only the receipt entry remains
        world.fail_mc_rm = set()
        self._rollback(world, j, session="mix")
        d2 = json.loads(j.read_text())
        self.assertEqual(d2["rollback_residue"],
                         ["receipt:ownership-unverified"])
        self.assertEqual(d2["status"], "rollback-residue")


class TestRollbackTimeReparse(HarnessTestBase):
    """R6 §7 (R4-B): receipt target is a symlink/reparse AT ROLLBACK
    time; production verifier refuses to follow/delete."""

    def test_g_rollback_time_reparse_receipt(self):
        r = self.root / "r-g.json"
        outside = self.root / "foreign-target.json"
        foreign = b'{"foreign": "symlink-target-bytes"}'
        outside.write_bytes(foreign)
        world = FakeSyncWorld()
        world.set_role_target("manager")
        entry = self._stage_mutated_role(
            world, "s-g", "manager", "manager_converged")
        doc = {"ownership": hw.HARNESS_IDENTITY, "session": "s-g",
               "status": "in-progress", "roles": {"manager": entry},
               "receipt_state": "publishing", "receipt_path": str(r),
               "receipt_session": "s-g",
               "receipt_sha256": "0" * 64,
               "tx_lock": "%s/lock" % ex.HICLAW_TX_PREFIX}
        world.objects["%s/lock" % ex.HICLAW_TX_PREFIX] = b"s-g:now"
        j = self.root / "j-g.json"
        j.write_text(json.dumps(doc), encoding="utf-8")
        ctx_mgr = None
        try:
            r.symlink_to(outside)
        except (OSError, NotImplementedError):
            import unittest.mock
            ctx_mgr = unittest.mock.patch("os.path.islink",
                                          return_value=True)
        import contextlib
        with (ctx_mgr or contextlib.nullcontext()):
            result = self._rollback(world, j, session="s-g")
        # symlink / foreign target untouched
        if ctx_mgr is None:
            self.assertTrue(r.is_symlink())
        self.assertEqual(outside.read_bytes(), foreign)
        # agents still recovered
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertTrue(world.role_at_legacy("manager"))
        # honest reporting
        self.assertIn("RECEIPT_OWNERSHIP_UNVERIFIED",
                      result["diagnostics"])
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])
        disk = json.loads(j.read_text())
        self.assertNotEqual(disk["status"], "complete")


class TestPostWritePrePersistCrash(HarnessTestBase):
    """the exact window: canonical write succeeded -> the mutated
    status NOT yet persisted. Disk journal says applying, the
    canonical object already points at the target gateway."""

    def test_post_write_pre_mutated_persist_crash_reviewer(self):
        j = self.root / "j-pw.json"
        r = self.root / "r-pw.json"
        world = FakeSyncWorld()

        def hook(phase, role):
            if phase == "canonical_written" and role == "reviewer":
                raise CrashSimulated(phase)

        try:
            self._apply(world, journal=j, receipt=r, session="pw",
                        phase_hook=hook)
        except CrashSimulated:
            pass
        # reviewer (2nd role) was REALLY mutated before the crash:
        # canonical at target, disk journal still says applying
        self.assertIn(TARGET["reviewer"],
                      world.canonical_bytes("reviewer").decode())
        journal = json.loads(j.read_text())
        self.assertEqual(journal["roles"]["reviewer"]["status"],
                         "canonical_applying")
        self.assertEqual(journal["roles"]["manager"]["status"],
                         "manager_converged")
        self.assertFalse(r.exists())
        # FRESH harness instance; disk-journal rollback only
        fresh = world.clone_for_recovery()
        foreign = self.root / "foreign.txt"
        foreign.write_text("keep", encoding="utf-8")
        result = self._rollback(fresh, j, session="pw")
        # strict reverse order over possibly-written roles
        self.assertEqual(result["rolled_back"],
                         ["reviewer", "manager"])
        for role in hw.ROLES:
            self.assertTrue(fresh.role_at_legacy(role), role)
        # later roles were never written
        self.assertNotIn(TARGET["fixer"],
                         fresh.canonical_bytes("fixer").decode())
        # the fresh instance wrote ONLY the manager live restore
        self.assertEqual(fresh.live_write_calls, 1)
        self.assertFalse(r.exists())
        self.assertEqual(foreign.read_text(), "keep")
        second = self._rollback(fresh, j, session="pw")
        self.assertEqual(second["rolled_back"], [])


class TestPrimaryErrorPreservation(HarnessTestBase):
    """verify failure + rollback failure -> primary survives,
    rollback errors only in diagnostics."""

    def test_verify_failure_with_rollback_failure(self):
        world = FakeSyncWorld()
        world.corrupt_canonical_put = {
            "reviewer": b'{"corrupted": true}'}

        def arm(phase, role):
            if phase == "canonical_mutated" and role == "reviewer":
                # every subsequent restore attempt fails
                world.fail_live_write = {"manager"}
                world.fail_canonical_put = set(WORKERS)

        j = self.root / "j-ep.json"
        r = self.root / "r-ep.json"
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="ep1",
                        phase_hook=arm)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_CANONICAL_VERIFY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any(d.startswith("ROLLBACK_FAILED")
                            for d in diags), diags)
        journal = json.loads(j.read_text())
        self.assertEqual(journal["status"], "rollback-failed")
        self.assertTrue(any("reviewer" in x or "manager" in x for x in
                            journal.get("rollback_residue", [])))
        self.assertFalse(r.exists())
        # §4C: rollback convergence failure classifies the failing
        # side honestly — manager converged to target during apply,
        # then the production push refuses to re-converge canonical
        # back to legacy while a LATER role's failure triggers the
        # automatic rollback
        world2 = FakeSyncWorld()

        def arm2(phase, role):
            if phase == "manager_converged":
                world2.push_mode["manager"] = "never"
                world2.fail_canonical_put_once = {"reviewer"}

        with self.assertRaises(hw.HarnessError) as ctx2:
            self._apply(world2, journal=self.root / "j-cc.json",
                        receipt=self.root / "r-cc.json", session="cc",
                        phase_hook=arm2)
        self.assertEqual(ctx2.exception.code, "HARNESS_APPLY_FAILED")
        disk2 = json.loads((self.root / "j-cc.json").read_text())
        self.assertIn("canonical:manager", disk2["rollback_residue"])
        self.assertTrue(any(
            d == "ROLLBACK_CONVERGENCE_FAILED:manager"
            for d in disk2.get("rollback_diagnostics", [])))
        self.assertEqual(disk2["roles"]["manager"]["status"],
                         "rollback-failed")
        # live WAS restored; canonical honestly reported NOT recovered
        self.assertIn("aigw-local",
                      world2.live_bytes("manager").decode())
        self.assertIn(TARGET["manager"],
                      world2.canonical_bytes("manager").decode())


class TestReceiptContract(HarnessTestBase):

    def _successful_world(self):
        world = FakeSyncWorld()
        self._apply(world, session="r-ok")
        return world

    def test_receipt_schema_and_canonical_hash(self):
        self._successful_world()
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(len(receipt["agents"]), 4)
        self.assertEqual(receipt["rollback_ownership"],
                         "mp-gh4-harness")
        self.assertTrue(receipt["sync_contract_fingerprint"])
        # canonical hash: production authority
        self.assertEqual(receipt["receipt_sha256"],
                         ex._compute_receipt_sha256(receipt))
        for agent in receipt["agents"]:
            for f in ("config_hash_before", "config_hash_after",
                      "token_hash", "live_hash_before",
                      "live_hash_after", "canonical_hash_before",
                      "canonical_hash_after"):
                self.assertRegex(agent[f], r"^[0-9a-f]{64}$",
                                 "%s.%s" % (agent["role"], f))
            self.assertEqual(agent["gateway_url"],
                             TARGET[agent["role"]])
            self.assertEqual(agent["sync_mode"],
                             ex.hiclaw_role_sync_mode(agent["role"]))
            self.assertEqual(agent["canonical_key"],
                             ex.hiclaw_role_canonical_key(
                                 agent["role"]))
            self.assertEqual(agent["live_path"],
                             ex.hiclaw_role_live_config_path(
                                 agent["role"]))
            self.assertTrue(agent["convergence_evidence"])

    def test_receipt_and_journal_zero_secret(self):
        self._successful_world()
        blob = (self.receipt.read_text()
                + self.journal.read_text())
        for forbidden in ("secret-", "Bearer ", "ghp_", "syt_"):
            self.assertNotIn(forbidden, blob)

    def test_argv_and_calls_zero_secret(self):
        world = self._successful_world()
        for argv in world.docker_calls + world.minio_calls:
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("Bearer", joined)
            self.assertNotIn("secret-", joined)

    def test_old_mcp_never_started_or_stopped(self):
        world = self._successful_world()
        for argv in world.docker_calls + world.minio_calls:
            if argv[0] in ("start", "stop", "rm", "restart", "create"):
                joined = " ".join(str(a) for a in argv)
                self.assertNotIn("github-mcp", joined)
                self.assertNotIn("hiclaw", joined)

    def test_verify_uses_production_validator(self):
        world = self._successful_world()
        docker, minio = self._adapters(world)
        verdict = hw.verify(self.receipt, docker=docker, minio=minio)
        self.assertTrue(verdict["verified"], verdict)

    def test_verify_rejects_drift(self):
        # (a) tamper WITHOUT rehash -> integrity mismatch (stable
        # code via the production validator)
        world = self._successful_world()
        receipt = json.loads(self.receipt.read_text())
        receipt["agents"][0]["live_hash_after"] = "d" * 64
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        docker, minio = self._adapters(world)
        verdict = hw.verify(self.receipt, docker=docker, minio=minio)
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["code"],
                         "RECEIPT_INTEGRITY_MISMATCH")
        # (b) rehash but live state drifted -> production mismatch
        world2 = FakeSyncWorld()
        j2 = self.root / "j-drift2.json"
        r2 = self.root / "r-drift2.json"
        self._apply(world2, journal=j2, receipt=r2, session="rd2")
        receipt2 = json.loads(r2.read_text())
        receipt2["agents"][0]["live_hash_after"] = "e" * 64
        receipt2["receipt_sha256"] = \
            ex._compute_receipt_sha256(receipt2)
        r2.write_text(json.dumps(receipt2), encoding="utf-8")
        docker2, minio2 = self._adapters(world2)
        verdict2 = hw.verify(r2, docker=docker2, minio=minio2)
        self.assertFalse(verdict2["verified"])

    def test_stopped_state_family_normalized(self):
        # docker 'exited' satisfies expected 'stopped' (production
        # STOPPED_STATE_FAMILY normalization)
        world = self._successful_world()
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["old_github_mcp"]["state"], "exited")
        docker, minio = self._adapters(world)
        verdict = ex.validate_hiclaw_receipt(
            str(self.receipt), docker_executor=docker._exec,
            minio_executor=ex.minio_readonly_via_docker(
                minio._docker._exec),
            expected_old_mcp_state="stopped")
        self.assertEqual(
            verdict["checks"]["old_github_mcp"]["state"], "OK")


class TestFileSafety(HarnessTestBase):

    def test_reparse_refused(self):
        class BadWriter(hw.AtomicFileWriter):
            @classmethod
            def write(cls, path, data, *, root=None):
                raise hw.HarnessError("HARNESS_REPARSE_REFUSED", "x")

        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="f1", writer=BadWriter())
        self.assertEqual(ctx.exception.code, "HARNESS_REPARSE_REFUSED")
        self.assertEqual(world.live_write_calls, 0)
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))

    def test_journal_persist_failure_is_primary(self):
        world = FakeSyncWorld()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, session="f2",
                        writer=_journal_writer(
                            fail_ordinals={2, 3, 4, 5, 6}))
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertEqual(world.live_write_calls, 0)
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))

    def test_no_pat_or_pem_reads(self):
        world = FakeSyncWorld()
        self._apply(world, session="f3")
        for argv in world.docker_calls + world.minio_calls:
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("fgpat", joined)
            self.assertNotIn(".pem", joined)


class TestPlanInjection(HarnessTestBase):

    def test_plan_injection_without_default_executor(self):
        # v2 plan takes adapters directly: NO production executor
        # factory is consulted (patched to explode — must not fire)
        import unittest.mock
        world = FakeSyncWorld(
            legacy_artifacts=("a.mp-gh4-bak",))
        docker, minio = self._adapters(world)
        with unittest.mock.patch.object(
                hw, "_default_docker_executor",
                side_effect=AssertionError(
                    "default executor must not be consulted")):
            result = hw.plan(self.journal, docker, minio)
        self.assertEqual(result["writes_executed"], 0)
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)


class TestAtomicLockOwnership(HarnessTestBase):
    """R2 S3/S4: the conditional tombstone lock - concurrent
    exclusivity, per-mutation ownership asserts, ownership-verified
    release, crash recovery from the disk journal."""

    def _crash(self, phase, role, j, r, session="lk"):
        world = FakeSyncWorld()

        def hook(ph, rl):
            if ph == phase and rl == role:
                raise CrashSimulated(ph)
        try:
            self._apply(world, journal=j, receipt=r, session=session,
                        phase_hook=hook)
        except CrashSimulated:
            pass
        return world

    def test_lock_body_contents_and_journal_fields(self):
        world = FakeSyncWorld()
        _, minio = self._adapters(world)
        info = hw._tx_lock(minio, "sess-A", "txid-A")
        doc = self._lock_state(world)
        for f in ("schema", "rewire_session", "txid", "created",
                  "state", "harness_sha256"):
            self.assertIn(f, doc)
        self.assertEqual(doc["rewire_session"], "sess-A")
        self.assertEqual(doc["state"], hw.LOCK_STATE_HELD)
        self.assertNotIn("Bearer",
                         world.objects[info["key"]].decode())
        # release to a tombstone, then a NEW transaction recycles it
        self.assertEqual(hw._tx_release(minio, info), "released")
        j = self.root / "j-lk.json"
        r = self.root / "r-lk.json"
        self._apply(world, journal=j, receipt=r, session="ok-lk")
        disk = json.loads(j.read_text())
        for f in ("tx_lock", "tx_lock_session", "tx_lock_txid",
                  "tx_lock_etag", "tx_lock_state"):
            self.assertIn(f, disk)
        self.assertEqual(disk["tx_lock_state"], "released")

    def test_interleaved_acquire_exactly_one_winner(self):
        # S4-1: operator B tries to acquire DURING A's transaction -
        # the atomic conditional create must exclude B with zero
        # mutations on B's side
        world = FakeSyncWorld()

        def hook(phase, role):
            if phase == "manager_live_applying":
                _, minio_b = self._adapters(world)
                with self.assertRaises(hw.HarnessError) as ctx:
                    hw._tx_lock(minio_b, "operator-B", "operator-B")
                self.assertEqual(ctx.exception.code,
                                 "HARNESS_TX_LOCK_CONFLICT")
                # B's failed acquire mutated nothing anywhere
                self.assertEqual(world.live_write_calls, 0)
                self.assertEqual(world.canonical_put_calls, 0)

        j = self.root / "j-il.json"
        r = self.root / "r-il.json"
        result = self._apply(world, journal=j, receipt=r,
                             session="op-A", phase_hook=hook)
        self.assertEqual(result["result"], "complete")

    def test_takeover_of_released_tombstone_single_winner(self):
        _, minio = self._adapters(FakeSyncWorld())
        info_a = hw._tx_lock(minio, "op-A", "op-A")
        self.assertEqual(hw._tx_release(minio, info_a), "released")
        info_b = hw._tx_lock(minio, "op-B", "op-B")
        self.assertEqual(info_b["session"], "op-B")
        # a HELD lock is never taken over regardless of age
        with self.assertRaises(hw.HarnessError) as ctx:
            hw._tx_lock(minio, "op-C", "op-C")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_TX_LOCK_CONFLICT")

    def test_foreign_replacement_detected_before_manager_mutation(
            self):
        # S4-2: the lock is replaced by a foreign session after
        # acquire; the NEXT mutation point must fail closed with
        # zero further external writes
        world = FakeSyncWorld()

        def hook(phase, role):
            if phase == "manager_live_applying":
                self._replace_lock_foreign(world, "op-A",
                                           "op-FOREIGN")

        j = self.root / "j-fr.json"
        r = self.root / "r-fr.json"
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="op-A",
                        phase_hook=hook)
        self.assertEqual(ctx.exception.code, "HARNESS_TX_LOCK_LOST")
        self.assertEqual(world.live_write_calls, 0)
        # the foreign lock is NEVER touched by our failure handling
        self.assertEqual(self._lock_state(world)["rewire_session"],
                         "op-FOREIGN")
        disk = json.loads(j.read_text())
        self.assertTrue(any(d.startswith("TX_LOCK_LOST")
                            for d in disk["rollback_diagnostics"]))
        self.assertIn("lock:ownership-unverified",
                      disk["rollback_residue"])

    def test_lock_lost_before_each_role_mutation(self):
        # S4-3/S4-4: manager + every worker mutation gate
        for role, phase in (("manager", "manager_live_applying"),
                            ("reviewer", "canonical_applying"),
                            ("fixer", "canonical_applying"),
                            ("verifier", "canonical_applying")):
            with self.subTest(role=role):
                world = FakeSyncWorld()
                j = self.root / ("j-ll-%s.json" % role)
                r = self.root / ("r-ll-%s.json" % role)

                def hook(ph, rl, role=role, phase=phase):
                    if ph == phase and rl == role:
                        world.objects.pop(
                            "%s/lock" % ex.HICLAW_TX_PREFIX, None)

                with self.assertRaises(hw.HarnessError) as ctx:
                    self._apply(world, journal=j, receipt=r,
                                session="ll-%s" % role,
                                phase_hook=hook)
                self.assertEqual(ctx.exception.code,
                                 "HARNESS_TX_LOCK_LOST")

    def test_lock_lost_before_receipt_publication(self):
        # S4-5
        world = FakeSyncWorld()

        def hook(phase, role):
            if phase == "receipt_publishing_persisted":
                world.objects.pop(
                    "%s/lock" % ex.HICLAW_TX_PREFIX, None)

        j = self.root / "j-lr.json"
        r = self.root / "r-lr.json"
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="lr",
                        phase_hook=hook)
        self.assertEqual(ctx.exception.code, "HARNESS_TX_LOCK_LOST")
        self.assertFalse(r.exists())

    def test_lock_lost_before_rollback_restore(self):
        # S4-6: crash leaves mutations; a foreign lock blocks the
        # destructive rollback restores - nothing is written through
        # an unverified lock
        j = self.root / "j-lb.json"
        r = self.root / "r-lb.json"
        world = self._crash("canonical_mutated", "reviewer", j, r)
        fresh = world.clone_for_recovery()
        self._replace_lock_foreign(fresh, "lk", "op-FOREIGN")
        with self.assertRaises(hw.HarnessError) as ctx:
            self._rollback(fresh, j, session="lk")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_ROLLBACK_FAILED")
        self.assertIn("TX_LOCK_LOST", ctx.exception.detail)
        # nothing restored: reviewer's canonical mutation stands and
        # manager (converged before the crash) stays at target
        self.assertIn(TARGET["reviewer"],
                      fresh.canonical_bytes("reviewer").decode())
        self.assertTrue(fresh.role_at_target("manager"))
        self.assertEqual(fresh.live_write_calls, 0)
        self.assertEqual(fresh.canonical_put_calls, 0)
        disk = json.loads(j.read_text())
        self.assertIn("lock:ownership-unverified",
                      disk["rollback_residue"])

    def test_release_never_touches_foreign_lock(self):
        # S4-7: session A's release against session B's lock
        world = FakeSyncWorld()
        _, minio = self._adapters(world)
        info_a = hw._tx_lock(minio, "op-A", "op-A")
        self._replace_lock_foreign(world, "op-A", "op-B")
        outcome = hw._tx_release(minio, info_a)
        self.assertEqual(outcome, "unverified")
        doc = self._lock_state(world)
        self.assertEqual(doc["rewire_session"], "op-B")
        self.assertEqual(doc["state"], hw.LOCK_STATE_HELD)

    def test_conditional_replace_etag_conflict_leaves_object(self):
        # S4-8: the atomic replace primitive refuses a stale etag
        world = FakeSyncWorld()
        _, minio = self._adapters(world)
        info = hw._tx_lock(minio, "op-A", "op-A")
        body = hw._lock_body("op-A", "op-A",
                             hw.LOCK_STATE_RELEASED)
        st, etag, _ = minio.cond_put_match(
            info["key"], body, "0" * 32,
            expect_prefix=ex.HICLAW_TX_PREFIX)
        self.assertEqual(st, 412)
        self.assertEqual(self._lock_state(world)["state"],
                         hw.LOCK_STATE_HELD)

    def test_release_transport_failure_then_retry_converges(self):
        # S4-9: signer transport fails once; residue lock:unremovable;
        # a retry converges to a released tombstone
        j = self.root / "j-rf.json"
        r = self.root / "r-rf.json"
        world = self._crash("manager_converged", "manager", j, r)
        fresh = world.clone_for_recovery()
        fresh.fail_cond_put_once = True
        result = self._rollback(fresh, j, session="lk")
        disk = json.loads(j.read_text())
        self.assertIn("TX_LOCK_RELEASE_FAILED",
                      disk["rollback_diagnostics"])
        self.assertIn("lock:unremovable", disk["rollback_residue"])
        self.assertEqual(disk["tx_lock_state"], "unremovable")
        self.assertEqual(self._lock_state(fresh).get("state"),
                         hw.LOCK_STATE_HELD)
        # retry: release succeeds, stale residue converges
        second = self._rollback(fresh, j, session="lk")
        disk2 = json.loads(j.read_text())
        self.assertTrue(self._lock_released(fresh))
        self.assertEqual(disk2["tx_lock_state"], "released")
        self.assertNotIn("lock:unremovable", disk2["rollback_residue"])

    def test_crash_recovery_uses_journal_lock_ownership(self):
        # S4-10: a TAMPERED journal etag cannot drive rollback -
        # ownership is re-proved against the live object
        j = self.root / "j-cr.json"
        r = self.root / "r-cr.json"
        world = self._crash("canonical_mutated", "fixer", j, r)
        disk = json.loads(j.read_text())
        disk["tx_lock_etag"] = "f" * 32
        j.write_text(json.dumps(disk), encoding="utf-8")
        fresh = world.clone_for_recovery()
        with self.assertRaises(hw.HarnessError) as ctx:
            self._rollback(fresh, j, session="lk")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_ROLLBACK_FAILED")
        self.assertIn("TX_LOCK_LOST", ctx.exception.detail)
        self.assertEqual(fresh.live_write_calls, 0)


class TestR2FixClosures(HarnessTestBase):
    """F5/F6/F7 closures exercised against the production code."""

    def test_f5_key_validation_rejections(self):
        structural = ["", "/abs", "a//b", "a/../b", "a/./b",
                      "http://x/y", "agents/fixer\x00/config"]
        for key in structural:
            with self.subTest(key=key):
                with self.assertRaises(hw.HarnessError) as ctx:
                    hw._validate_object_key(key)
                self.assertEqual(ctx.exception.code,
                                 "HARNESS_KEY_INVALID")
        prefix_refused = [
            ("mp-gh4-tx-foreign/lock", ex.HICLAW_TX_PREFIX),
            ("manager/config/mcporter.json", "agents"),
            ("agents/fixer2/config/mcporter.json", "agents/fixer")]
        for key, prefix in prefix_refused:
            with self.subTest(prefix=key):
                with self.assertRaises(hw.HarnessError) as ctx:
                    hw._validate_object_key(key, expect_prefix=prefix)
                self.assertEqual(ctx.exception.code,
                                 "HARNESS_KEY_PREFIX_REFUSED")
        # role mismatch refused
        with self.assertRaises(hw.HarnessError) as ctx:
            hw._validate_object_key(
                "agents/fixer/config/mcporter.json", role="reviewer")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_KEY_ROLE_MISMATCH")
        # legit keys pass
        self.assertEqual(
            hw._validate_object_key("manager/config/mcporter.json"),
            "manager/config/mcporter.json")
        self.assertEqual(
            hw._validate_object_key(
                "%s/s/x/manager/mcporter.json" % ex.HICLAW_TX_PREFIX,
                expect_prefix=ex.HICLAW_TX_PREFIX),
            "%s/s/x/manager/mcporter.json" % ex.HICLAW_TX_PREFIX)

    def test_f5_remove_rejects_journal_foreign_key(self):
        # a tampered journal feeding a production-prefix key into
        # remove() must be refused, never delete
        world = FakeSyncWorld()
        _, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            minio.remove("manager/config/mcporter.json",
                         expect_prefix=ex.HICLAW_TX_PREFIX)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_KEY_PREFIX_REFUSED")
        self.assertIn("manager/config/mcporter.json", world.objects)

    def _crash_like(self, j, r, phase, role):
        world = FakeSyncWorld()

        def hook(ph, rl):
            if ph == phase and rl == role:
                raise CrashSimulated(ph)
        try:
            self._apply(world, journal=j, receipt=r, session="bw",
                        phase_hook=hook)
        except CrashSimulated:
            pass
        return world

    def _crash_copy_window(self, j, r):
        """TRUE crash after the backup copy landed but before the
        'pending' persist (production backup_copied hook): no
        in-transaction cleanup runs."""
        world = FakeSyncWorld()

        def hook(ph, rl):
            if ph == "backup_copied" and rl == "manager":
                raise CrashSimulated(ph)
        try:
            self._apply(world, journal=j, receipt=r, session="bw2",
                        phase_hook=hook)
        except CrashSimulated:
            pass
        return world

    # (c) shares the crash helper; its rollback uses the SAME
    # session identity the crashed transaction recorded

    def test_f6_backup_wal_crash_windows(self):
        # (a) crash between the intent persist and the copy: journal
        # says backup_copying, no object - safe no-op rollback
        j = self.root / "j-bwa.json"
        r = self.root / "r-bwa.json"
        world = self._crash_like(j, r, "backup_intent_persisted",
                                 "manager")
        disk = json.loads(j.read_text())
        self.assertEqual(disk["roles"]["manager"]["status"],
                         "backup_copying")
        self.assertNotIn(disk["roles"]["manager"]["backup_key"],
                         world.objects)
        fresh = world.clone_for_recovery()
        result = self._rollback(fresh, j, session="bw")
        self.assertEqual(result["residue"], [])
        self.assertTrue(self._lock_released(fresh))
        # (b) crash AFTER the copy but BEFORE the verified persist:
        # the object exists and matches - rollback deletes it as
        # session-owned (hash re-verified from the journal)
        j2 = self.root / "j-bwb.json"
        r2 = self.root / "r-bwb.json"
        world2 = self._crash_copy_window(j2, r2)
        fresh2 = world2.clone_for_recovery()
        bkey = json.loads(j2.read_text())["roles"][
            "manager"]["backup_key"]
        self.assertIn(bkey, fresh2.objects)
        result2 = self._rollback(fresh2, j2, session="bw2")
        self.assertEqual(result2["residue"], [])
        self.assertNotIn(bkey, fresh2.objects)
        self.assertTrue(self._lock_released(fresh2))
        # (c) backup_copying with a MISMATCHED object at the key:
        # foreign residue, NEVER deleted
        j3 = self.root / "j-bwc.json"
        r3 = self.root / "r-bwc.json"
        world3 = self._crash_copy_window(j3, r3)
        fresh3 = world3.clone_for_recovery()
        bkey3 = json.loads(j3.read_text())["roles"][
            "manager"]["backup_key"]
        fresh3.objects[bkey3] = b'{"foreign": true}'
        result3 = self._rollback(fresh3, j3, session="bw2")
        self.assertIn("tx-backup-foreign:manager", result3["residue"])
        self.assertEqual(fresh3.objects[bkey3], b'{"foreign": true}')

    def test_f7_read_bytes_fail_closed_matrix(self):
        world = FakeSyncWorld()
        _, minio = self._adapters(world)
        key = ex.hiclaw_role_canonical_key("reviewer")
        body = world.canonical_bytes("reviewer")
        self.assertEqual(
            minio.read_bytes(key, max_bytes=len(body) + 1), body)
        # oversized: stat reports more than the cap
        with self.assertRaises(hw.HarnessError) as ctx:
            minio.read_bytes(key, max_bytes=len(body) - 1)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_TX_OBJECT_READ_OVERSIZED")
        # short read: cat returns fewer bytes than stat size
        world.short_read_keys = {key}
        with self.assertRaises(hw.HarnessError) as ctx:
            minio.read_bytes(key, max_bytes=65536)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_TX_OBJECT_READ_SHORT")
        world.short_read_keys = set()
        # mc cat rc!=0 can no longer hide behind a pipeline tail
        world.fail_mc_cat = True
        with self.assertRaises(hw.HarnessError) as ctx:
            minio.read_bytes(key, max_bytes=65536)
        self.assertEqual(ctx.exception.code, "HARNESS_APPLY_FAILED")
        self.assertIn("rc=", ctx.exception.detail)


class _LocalS3Fixture:
    """A threaded local HTTP server speaking just enough S3 to host
    the PRODUCTION signer: conditional PUT/GET semantics identical
    to the deployed MinIO (create-if-absent / replace-if-match are
    atomic under a lock), full request capture, plus redirect and
    oversized-response fault modes. The fixture NEVER validates the
    SigV4 signature — the contract tests assert its PRESENCE and
    signed-header coverage, not crypto against this server."""

    def __init__(self, mode=None, location=None):
        import http.server
        import threading
        self.store = {}          # path bytes -> (body, etag)
        self.requests = []       # (method, path, headers, body)
        self.mode = mode
        self.location = location
        lock = threading.Lock()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self, body):
                fixture.requests.append(
                    (self.command, self.path, dict(self.headers),
                     body))

            def _redirect(self):
                self.send_response(
                    301 if fixture.mode == "r301" else
                    307 if fixture.mode == "r307" else 302)
                self.send_header("Location", fixture.location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _oversize(self):
                if fixture.mode == "oversize-cl":
                    self.send_response(200)
                    self.send_header("Content-Length", "9999999")
                    self.end_headers()
                    self.wfile.write(b"x" * 100)
                else:   # oversize-stream: no Content-Length
                    self.send_response(200)
                    self.send_header(
                        "Transfer-Encoding", "chunked")
                    self.end_headers()
                    chunk = b"x" * 65537
                    self.wfile.write(b"%x\r\n" % len(chunk)
                                     + chunk + b"\r\n0\r\n\r\n")

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                self._record(body)
                if fixture.mode in ("r301", "r302", "r307"):
                    return self._redirect()
                inm = self.headers.get("If-None-Match")
                im = self.headers.get("If-Match")
                with lock:
                    cur = fixture.store.get(self.path)
                    if inm == "*":
                        if cur is not None:
                            self.send_response(412)
                            self.send_header(
                                "ETag", '"%s"' % cur[1])
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                    elif im is not None and im.strip('"') != "*":
                        if cur is None or cur[1] != im.strip('"'):
                            self.send_response(412)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                    import hashlib
                    etag = hashlib.md5(body).hexdigest()
                    fixture.store[self.path] = (body, etag)
                self.send_response(200)
                self.send_header("ETag", '"%s"' % etag)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                self._record(b"")
                if fixture.mode in ("r301", "r302", "r307"):
                    return self._redirect()
                if fixture.mode in ("oversize-cl",
                                    "oversize-stream"):
                    return self._oversize()
                im = self.headers.get("If-Match")
                with lock:
                    cur = fixture.store.get(self.path)
                if cur is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if im is not None and im.strip('"') != cur[1]:
                    self.send_response(412)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, etag = cur
                self.send_response(200)
                self.send_header("ETag", '"%s"' % etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        import socket
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                              Handler)
        self.port = srv.server_address[1]
        self._srv = srv
        import threading as _th
        self._th = _th.Thread(target=srv.serve_forever, daemon=True)
        self._th.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def _prod_exec_factory(config_path, argv_log=None, stdin_log=None,
                       script_override=None, op_override=None):
    """A docker-exec boundary that REALLY executes the production
    signer bytes in a local python subprocess (the same
    _S3_COND_SCRIPT constant the docker argv carries; no second
    implementation exists anywhere)."""
    import subprocess as _sp
    import os as _os

    def exec(argv, check=True, timeout=60, input_bytes=None, **_):
        argv = list(argv)
        if argv_log is not None:
            argv_log.append(list(argv))
        if stdin_log is not None:
            stdin_log.append(input_bytes)
        assert argv[0:5] == ["exec", "-i", "hiclaw-controller",
                             "python3", "-c"], argv[:5]
        script = script_override or argv[5]
        op = op_override or argv[6]
        target, cond = argv[7], argv[8]
        env = dict(_os.environ)
        env["MC_CONFIG_PATH"] = str(config_path)
        return _sp.run(
            [sys.executable, "-c", script, op, target, cond],
            input=input_bytes or b"", capture_output=True,
            env=env, timeout=30)
    return exec


def _fixture_config(path, url):
    path.write_text(json.dumps({
        "aliases": {"hiclaw": {
            "url": url, "accessKey": "AKIAFIXTURE",
            "secretKey": "fixture-secret", "api": "S3v4",
            "path": "auto"}}}), encoding="utf-8")


class TestProductionByteContract(HarnessTestBase):
    """R3 §6: the bytes that pass these tests ARE the bytes docker
    executes — argv construction, stdin payload and the embedded
    signer all come from the same production constant/function."""

    KEY = "mp-gh4-tx/r3byte/lock"

    def _adapter(self, fixture, **kw):
        import tempfile
        cfg = self.root / ("mc-cfg-%d.json" % id(fixture))
        _fixture_config(cfg, fixture.url)
        argv_log, stdin_log = [], []
        exec_fn = _prod_exec_factory(cfg, argv_log, stdin_log,
                                     **kw)
        adapter = hw.MinioAdapter(exec_fn)
        return adapter, argv_log, stdin_log

    def test_argv_carries_program_stdin_carries_body(self):
        fixture = _LocalS3Fixture()
        self.addCleanup(fixture.stop)
        adapter, argv_log, stdin_log = self._adapter(fixture)
        body = b'{"rewire_session":"r3","state":"held"}'
        st, etag, _ = adapter.cond_put_absent(self.KEY, body)
        self.assertEqual(st, 200)
        # 1. argv actually contains the executable program: the
        # newline-free base64 bootstrap decoding the EXACT constant
        import base64
        inner = argv_log[0][5]
        self.assertNotIn("\n", inner)
        self.assertTrue(inner.startswith("import base64;"))
        b64 = inner.split("'")[1]
        self.assertEqual(
            base64.b64decode(b64).decode("utf-8"),
            hw._S3_COND_SCRIPT)
        # 2. stdin is EXACTLY the object body
        self.assertEqual(stdin_log[0], body)
        # 3. stdin is not the program (channels cannot be swapped)
        self.assertNotEqual(stdin_log[0], hw._S3_COND_SCRIPT)
        # 4. body never appears in the sanitized argv audit
        for entry in adapter.calls:
            joined = " ".join(str(a) for a in entry)
            self.assertNotIn("rewire_session", joined)
        self.assertEqual(adapter.calls[-1],
                         ["cond-absent", self.KEY])

    def test_conditional_headers_sent_and_signed(self):
        fixture = _LocalS3Fixture()
        self.addCleanup(fixture.stop)
        adapter, _, _ = self._adapter(fixture)
        body = b'lock-body-A'
        st, etag, _ = adapter.cond_put_absent(self.KEY, body)
        self.assertEqual(st, 200)
        # 5. put-absent actually sends AND signs If-None-Match: *
        req = fixture.requests[-1]
        self.assertEqual(req[2].get("If-None-Match"), "*")
        auth = req[2].get("Authorization", "")
        self.assertIn("if-none-match", auth.split("SignedHeaders=")[-1])
        # second create must conflict
        st2, _, _ = adapter.cond_put_absent(self.KEY, b'lock-body-B')
        self.assertEqual(st2, 412)
        # 6. put-match actually sends AND signs If-Match
        st3, etag3, _ = adapter.cond_put_match(
            self.KEY, b'released-body', etag)
        self.assertEqual(st3, 200)
        req3 = fixture.requests[-1]
        self.assertEqual(req3[2].get("If-Match"),
                         '"%s"' % etag)
        auth3 = req3[2].get("Authorization", "")
        self.assertIn("if-match", auth3.split("SignedHeaders=")[-1])
        # the server saw exactly the body bytes we put on stdin
        self.assertEqual(req3[3], b'released-body')

    def test_get_sends_empty_body_and_real_conditional(self):
        fixture = _LocalS3Fixture()
        self.addCleanup(fixture.stop)
        adapter, _, stdin_log = self._adapter(fixture)
        st, etag, _ = adapter.cond_put_absent(self.KEY, b'X')
        # 7. GET carries no leftover body from previous operations
        st2, etag2, body = adapter.cond_get_match(self.KEY, etag)
        self.assertEqual((st2, body), (200, b'X'))
        self.assertEqual(stdin_log[-1], b"")
        req = fixture.requests[-1]
        hdrs = {k.lower(): v for k, v in req[2].items()}
        self.assertEqual(hdrs.get("if-match"), '"%s"' % etag)
        import hashlib
        self.assertEqual(
            hdrs.get("x-amz-content-sha256"),
            hashlib.sha256(b"").hexdigest())

    def test_syntax_error_and_stderr_map_to_stable_code(self):
        fixture = _LocalS3Fixture()
        self.addCleanup(fixture.stop)
        adapter, _, _ = self._adapter(
            fixture, script_override="this is ( not python")
        with self.assertRaises(hw.HarnessError) as ctx:
            adapter.cond_put_absent(self.KEY, b'x')
        # 8. rc!=0 maps to the stable transport code
        self.assertEqual(ctx.exception.code,
                         "HARNESS_TX_LOCK_UNAVAILABLE")
        # 9. stderr never leaks into the exception detail
        self.assertNotIn("not python", ctx.exception.detail)
        self.assertRegex(ctx.exception.detail, r"signer rc=\d+")

    def test_unknown_op_rejected(self):
        fixture = _LocalS3Fixture()
        self.addCleanup(fixture.stop)
        adapter, _, _ = self._adapter(fixture, op_override="bogus")
        with self.assertRaises(hw.HarnessError) as ctx:
            adapter.cond_put_absent(self.KEY, b'x')
        # 10. unknown op exits nonzero -> stable rejection
        self.assertEqual(ctx.exception.code,
                         "HARNESS_TX_LOCK_UNAVAILABLE")

    def test_redirects_never_followed(self):
        cases = [
            ("same-host", "http://127.0.0.1:%d/other-place"),
            ("cross-host", "http://evil.example.invalid/x"),
            ("loop", None),     # Location = own URL
            ("sensitive-query",
             "http://127.0.0.1:%d/x?token=SECRETVALUE"),
        ]
        for label, loc in cases:
            for rmode in ("r301", "r302", "r307"):
                with self.subTest(case=label, redirect=rmode):
                    fixture = _LocalS3Fixture(mode=rmode)
                    self.addCleanup(fixture.stop)
                    location = (loc % fixture.port if loc and
                                "%d" in loc else
                                loc or fixture.url + self.KEY)
                    fixture.location = location
                    adapter, _, _ = self._adapter(fixture)
                    st, etag, _ = adapter.cond_put_absent(
                        self.KEY, b'x')
                    self.assertIn(st, (301, 302, 307))
                    # exactly ONE request reached the server: the
                    # redirect was never followed, so no signed
                    # request could propagate anywhere
                    self.assertEqual(len(fixture.requests), 1)

    def test_response_read_bounds(self):
        for mode in ("oversize-cl", "oversize-stream"):
            with self.subTest(mode=mode):
                fixture = _LocalS3Fixture(mode=mode)
                self.addCleanup(fixture.stop)
                adapter, _, _ = self._adapter(fixture)
                # bounded read fails closed with the stable code
                with self.assertRaises(hw.HarnessError) as ctx:
                    adapter.cond_get_match(self.KEY)
                    # fixture returns 200+oversize on GET
                self.assertEqual(
                    ctx.exception.code,
                    "HARNESS_TX_LOCK_UNAVAILABLE")
                self.assertNotIn("xxxx", ctx.exception.detail)


class TestFakeProductionParity(HarnessTestBase):
    """R3 §7: the FakeSyncWorld conditional primitive and the
    PRODUCTION signer over a real HTTP boundary must agree on every
    protocol outcome — the fake is never stronger than production."""

    KEY = "mp-gh4-tx/r3parity/lock"

    def _both(self):
        fixture = _LocalS3Fixture()
        self.addCleanup(fixture.stop)
        cfg = self.root / "mc-parity.json"
        _fixture_config(cfg, fixture.url)
        prod = hw.MinioAdapter(_prod_exec_factory(cfg))
        fake_world = FakeSyncWorld()
        # the FAKE side also goes through the production ADAPTER
        # (argv construction + stdout parsing identical to prod)
        fake = hw.MinioAdapter(fake_world.docker_exec)
        return fixture, prod, fake, fake_world

    def test_protocol_parity_table(self):
        fixture, prod, fake, fake_world = self._both()
        body_a = b'{"session":"A","state":"held"}'
        body_b = b'{"session":"B","state":"held"}'

        # row 1: absent create -> 200 on both
        self.assertEqual(
            prod.cond_put_absent(self.KEY, body_a)[0],
            fake.cond_put_absent(self.KEY, body_a)[0])
        # row 2: second absent create -> 412 on both
        self.assertEqual(
            prod.cond_put_absent(self.KEY, body_b)[0],
            fake.cond_put_absent(self.KEY, body_b)[0])
        # row 3: concurrent absent create -> exactly one winner on
        # EACH side (two contenders per side, same key)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            f1 = ex.submit(prod.cond_put_absent,
                           self.KEY + "c", body_a)
            f2 = ex.submit(prod.cond_put_absent,
                           self.KEY + "c", body_b)
            f3 = ex.submit(fake.cond_put_absent,
                           self.KEY + "c", body_a)
            f4 = ex.submit(fake.cond_put_absent,
                           self.KEY + "c", body_b)
            rp = sorted([f1.result()[0], f2.result()[0]])
            rf = sorted([f3.result()[0], f4.result()[0]])
        self.assertEqual(rp, [200, 412])
        self.assertEqual(rf, [200, 412])
        self.assertIn(fixture.store["/hiclaw-storage/"
                        + self.KEY + "c"][0], (body_a, body_b))
        self.assertIn(fake_world.objects[self.KEY + "c"],
                      (body_a, body_b))
        # row 4: matching replace -> 200 on both
        etag_p = prod.cond_get_match(self.KEY)[1]
        etag_f = fake.cond_get_match(self.KEY)[1]
        rel = b'{"session":"A","state":"released"}'
        self.assertEqual(
            prod.cond_put_match(self.KEY, rel, etag_p)[0],
            fake.cond_put_match(self.KEY, rel, etag_f)[0])
        # row 5: released takeover -> exactly one winner on EACH
        # side (two contenders per side race the same tombstone etag)
        etag_p2 = prod.cond_get_match(self.KEY)[1]
        etag_f2 = fake.cond_get_match(self.KEY)[1]
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            g1 = ex.submit(prod.cond_put_match, self.KEY, body_b,
                           etag_p2)
            g2 = ex.submit(prod.cond_put_match, self.KEY, body_a,
                           etag_p2)
            g3 = ex.submit(fake.cond_put_match, self.KEY, body_b,
                           etag_f2)
            g4 = ex.submit(fake.cond_put_match, self.KEY, body_a,
                           etag_f2)
            rp = sorted([g1.result()[0], g2.result()[0]])
            rf = sorted([g3.result()[0], g4.result()[0]])
        self.assertEqual(rp, [200, 412])
        self.assertEqual(rf, [200, 412])
        # row 6/7: wrong and ABA-stale etags -> 412 on both (the
        # pre-takeover tombstone etags are now stale by construction)
        stale_p = etag_p2
        stale_f = etag_f2
        for et_p, et_f in (("0" * 32, "0" * 32),
                           (stale_p, stale_f)):
            self.assertEqual(
                prod.cond_put_match(self.KEY, body_a, et_p)[0],
                fake.cond_put_match(self.KEY, body_a, et_f)[0])
        # ownership: each side's final object is exactly ONE of
        # its own two contenders' bodies (the two sides race
        # independently — cross-side winners need not coincide)
        self.assertIn(
            fixture.store["/hiclaw-storage/" + self.KEY][0],
            (body_a, body_b))
        self.assertIn(fake_world.objects[self.KEY],
                      (body_a, body_b))


class TestMinioCpHotfix(HarnessTestBase):
    """M8-GH-4B5: the production copy verb is `cp` (the deployed mc
    RELEASE.2025-08-13 does not recognize `copy`); the fake is no
    more permissive than the real client; a cp failure fails closed
    before any role mutation."""

    def test_minio_copy_uses_real_cp_command(self):
        # the production audit must carry the REAL verb that the
        # deployed mc executes
        world = FakeSyncWorld()
        docker, minio = self._adapters(world)
        src = ex.hiclaw_role_canonical_key("manager")
        dst = "%s/cp-audit/manager/mcporter.json" % (
            ex.HICLAW_TX_PREFIX,)
        minio.copy(src, dst, dst_prefix=ex.HICLAW_TX_PREFIX)
        cp_cmds = [c for c in minio.calls
                   if len(c) > 1 and c[1] in ("cp", "copy")]
        self.assertEqual(len(cp_cmds), 1)
        self.assertEqual(cp_cmds[0][:2], ["mc", "cp"])
        # destination actually materialized with source bytes
        self.assertEqual(
            world.objects[dst], world.objects[src])

    def test_fake_rejects_removed_copy_alias(self):
        # a raw `copy` invocation must be refused by the fake exactly
        # as the deployed mc refuses it (rc=1)
        world = FakeSyncWorld()
        _, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            minio._mc(["copy",
                       "hiclaw/hiclaw-storage/%s" % ex
                       .hiclaw_role_canonical_key("manager"),
                       "hiclaw/hiclaw-storage/%s/x" % ex
                       .HICLAW_TX_PREFIX])
        self.assertEqual(ctx.exception.code, "HARNESS_APPLY_FAILED")

    def test_cp_failure_precedes_role_mutation(self):
        # backup cp failure: zero live/canonical mutation, clean
        # auto-rollback, no receipt, lock released, no backup residue
        j = self.root / "j-cpfail.json"
        r = self.root / "r-cpfail.json"
        world = FakeSyncWorld()
        world.fail_tx_copy = {"manager"}
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="cpfail")
        self.assertEqual(ctx.exception.code, "HARNESS_APPLY_FAILED")
        # zero mutations anywhere
        self.assertEqual(world.live_write_calls, 0)
        self.assertEqual(world.canonical_put_calls, 0)
        for role in hw.ROLES:
            self.assertTrue(world.role_at_legacy(role), role)
        # honest rolled-back journal, no receipt
        disk = json.loads(j.read_text())
        self.assertEqual(disk["status"], "rolled-back")
        self.assertEqual(disk["rollback_diagnostics"], [])
        self.assertEqual(disk["rollback_residue"], [])
        self.assertFalse(r.exists())
        # no backup object survived (cp failed -> nothing created)
        bkey = "%s/%s/manager/mcporter.json" % (
            ex.HICLAW_TX_PREFIX, "cpfail")
        self.assertNotIn(bkey, world.objects)
        # lock cleanly tombstoned RELEASED
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))


class TestWorkerOnDemandPull(HarnessTestBase):
    """M8-GH-4B6: worker live converges ONLY via the explicit
    production pull trigger; the 300s fallback tick is never a
    transaction prerequisite."""

    def test_three_workers_trigger_success_and_manager_rejected(self):
        # manager trigger is refused at the argv-construction level
        with self.assertRaises(hw.HarnessError) as ctx:
            hw._worker_pull_argv("manager")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_WORKER_PULL_TRIGGER_FAILED")
        # each worker's argv uses the frozen authorities exactly
        for r in ("reviewer", "fixer", "verifier"):
            container, key, live, argv = hw._worker_pull_argv(r)
            self.assertEqual(
                container, ex.HICLAW_ROLE_FREEZE[r][0])
            self.assertEqual(
                key, ex.hiclaw_role_canonical_key(r))
            self.assertEqual(
                live, ex.hiclaw_role_live_config_path(r))
            self.assertEqual(argv, [
                "exec", container, "mc", "cp",
                "hiclaw/hiclaw-storage/" + key, live])

    def test_apply_triggers_pull_per_worker(self):
        # full success path: each worker's live converges via the
        # EXPLICIT trigger (no lazy convergence anywhere)
        world = FakeSyncWorld()
        docker, minio = self._adapters(world)
        j = self.root / "j-trig.json"
        r = self.root / "r-trig.json"
        result = hw.apply(journal_path=j, receipt_path=r,
                          docker=docker, minio=minio,
                          session="trig")
        self.assertEqual(result["result"], "complete")
        for role in hw.ROLES:
            self.assertTrue(world.role_at_target(role), role)
        # adapter audit: exactly one worker-pull entry per worker
        pulls = [c for c in docker.calls
                 if len(c) > 1 and c[0] == "worker-pull"]
        self.assertEqual(
            sorted(c[1] for c in pulls),
            ["fixer", "reviewer", "verifier"])
        # the world-level audit carries the REAL argv (mc cp inside
        # each worker container with the frozen key/path)
        real_argv = [c for c in world.docker_calls
                     if len(c) > 3 and c[2] == "mc"
                     and c[3] == "cp"]
        self.assertEqual(len(real_argv), 3)
        for c in real_argv:
            role = FakeSyncWorld._role_of_container(c[1])
            self.assertIn(role, WORKERS)
            self.assertEqual(
                c[4], "hiclaw/hiclaw-storage/"
                + ex.hiclaw_role_canonical_key(role))
            self.assertEqual(
                c[5], ex.hiclaw_role_live_config_path(role))
        # journal carries the pull window states
        disk = json.loads(j.read_text())
        self.assertEqual(disk["status"], "complete")
        for role in ("reviewer", "fixer", "verifier"):
            self.assertEqual(
                disk["roles"][role]["status"], "live_converged")

    def test_fake_requires_exact_role_key_path_mapping(self):
        # wrong key/path inside a worker -> rc=1 -> trigger failure
        world = FakeSyncWorld()
        docker, _ = self._adapters(world)
        cp = world.docker_exec(
            ["exec", "hiclaw-worker-fixer", "mc", "cp",
             "hiclaw/hiclaw-storage/agents/reviewer/config/"
             "mcporter.json",
             "/root/hiclaw-fs/agents/fixer/config/mcporter.json"])
        self.assertEqual(cp.returncode, 1)

    def test_trigger_failure_fails_closed_before_receipt(self):
        # pull_mode never: trigger rc=1 -> stable code, clean
        # rollback, no receipt, lock released
        j = self.root / "j-tf.json"
        r = self.root / "r-tf.json"
        world = FakeSyncWorld()
        world.pull_mode = {"reviewer": "never"}
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="tf")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_WORKER_PULL_TRIGGER_FAILED")
        self.assertFalse(r.exists())
        disk = json.loads(j.read_text())
        self.assertEqual(disk["status"], "rolled-back")
        self.assertEqual(disk["rollback_residue"], [])
        self.assertTrue(self._lock_released(world))
        for role in hw.ROLES:
            self.assertTrue(world.role_at_legacy(role), role)

    def test_post_trigger_drift_caught_by_stability(self):
        # drift-once: trigger converges, external actor reverts live
        # on the next read -> the stability re-check must fail
        j = self.root / "j-dr.json"
        r = self.root / "r-dr.json"
        world = FakeSyncWorld()
        world.pull_mode = {"reviewer": "drift-once"}
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="dr")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_WORKER_PULL_CONVERGENCE_TIMEOUT")
        self.assertFalse(r.exists())

    def test_crash_matrix_pull_windows(self):
        # crash after canonical verify, before/after trigger
        for phase in ("worker_pull_triggering",
                      "worker_pull_triggered"):
            with self.subTest(window=phase):
                world = FakeSyncWorld()

                def hook(ph, rl):
                    if ph == phase and rl == "reviewer":
                        raise CrashSimulated(ph)
                j = self.root / ("j-cm-%s.json" % phase)
                r = self.root / ("r-cm-%s.json" % phase)
                try:
                    self._apply(world, journal=j, receipt=r,
                                session="cm", phase_hook=hook)
                except CrashSimulated:
                    pass
                disk = json.loads(j.read_text())
                self.assertEqual(
                    disk["roles"]["reviewer"]["status"], phase)
                self.assertFalse(r.exists())
                fresh = world.clone_for_recovery()
                result = self._rollback(fresh, j, session="cm")
                self.assertEqual(result["residue"], [])
                for role in hw.ROLES:
                    self.assertTrue(fresh.role_at_legacy(role), role)
                self.assertTrue(self._lock_released(fresh))

    def test_rollback_trigger_failure_then_retry(self):
        # reviewer fully converged (live at target), then verifier
        # fails; rollback trigger for reviewer FAILS (never) ->
        # live:<role> residue + rollback-failed + HELD lock; retry
        # (trigger works) converges and clears residue
        j = self.root / "j-rbtf.json"
        r = self.root / "r-rbtf.json"
        world = FakeSyncWorld()
        world.corrupt_canonical_put_once = {
            "verifier": b'{"corrupted": true}'}

        def hook(ph, rl):
            if ph == "live_converged" and rl == "reviewer":
                world.pull_mode = {"reviewer": "never"}
        try:
            self._apply(world, journal=j, receipt=r, session="rbtf",
                        phase_hook=hook)
        except hw.HarnessError:
            pass
        d1 = json.loads(j.read_text())
        self.assertEqual(d1["status"], "rollback-failed")
        self.assertIn("live:reviewer", d1["rollback_residue"])
        self.assertTrue(any(
            d.startswith("ROLLBACK_PULL_TRIGGER_FAILED")
            for d in d1["rollback_diagnostics"]))
        # reviewer live still at TARGET (trigger refused)
        self.assertIn(TARGET["reviewer"],
                      world.live_bytes("reviewer").decode())
        # lock stays HELD (not converged)
        self.assertEqual(self._lock_state(world).get("state"),
                         hw.LOCK_STATE_HELD)
        # retry with working trigger converges
        world.pull_mode = {"reviewer": "converge"}
        result = self._rollback(world, j, session="rbtf")
        d2 = json.loads(j.read_text())
        self.assertEqual(d2["status"], "rolled-back")
        self.assertEqual(d2["rollback_residue"], [])
        self.assertTrue(world.role_at_legacy("reviewer"))
        self.assertTrue(self._lock_released(world))

    def test_primary_error_not_overridden_by_trigger_failure(self):
        # reviewer converged, verifier verify fails, rollback
        # trigger also fails: primary stays the verify error
        world = FakeSyncWorld()
        world.corrupt_canonical_put_once = {
            "verifier": b'{"corrupted": true}'}

        def arm(ph, rl):
            if ph == "live_converged" and rl == "reviewer":
                world.pull_mode = {"reviewer": "never"}
        j = self.root / "j-pe.json"
        r = self.root / "r-pe.json"
        with self.assertRaises(hw.HarnessError) as ctx:
            self._apply(world, journal=j, receipt=r, session="pe",
                        phase_hook=arm)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_CANONICAL_VERIFY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any(
            d.startswith("ROLLBACK_PULL_TRIGGER_FAILED")
            for d in diags), diags)

    def test_worker_live_never_written_by_apply(self):
        # the ONLY writer of worker live is the production pull
        world = FakeSyncWorld()
        j = self.root / "j-nw.json"
        r = self.root / "r-nw.json"
        self._apply(world, journal=j, receipt=r, session="nw")
        for argv in world.docker_calls:
            if len(argv) > 4 and argv[4] == "sh" \
                    and "cat >" in argv[-1]:
                # find container of this write
                idx = argv.index("sh")
                container = argv[idx - 1] if idx > 0 else argv[2]
                role = FakeSyncWorld._role_of_container(container)
                self.assertEqual(role, "manager",
                                 "worker live written: %r" % (argv,))


if __name__ == "__main__":
    unittest.main()
