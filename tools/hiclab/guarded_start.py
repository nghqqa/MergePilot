#!/usr/bin/env python3
"""Phased, health-gated guarded startup (testable core).

The supervisor starts managed containers in strict dependency order, waiting
for health between phases. Any unhealthy dependency -> fail-closed -> stop
every container the supervisor started THIS round (previously-running
containers are untouched). No WARN-and-continue.

PROGRAMMATIC BLOCKED_UPSTREAM (hiclaw-manager):
  Without a DEPLOYED+audited socket proxy OR a confirmed upstream
  disable-auto-create capability, the supervisor REFUSES to start
  hiclaw-manager. This is enforced by marker-file checks
  (manager_start_allowed), NOT by documentation convention. hiclaw-manager
  stays stopped; the BLOCKED_UPSTREAM message is written to stderr. Other
  base/dependent containers still start in health order. D2B-3 therefore
  remains non-runnable until a proxy or upstream capability exists.

All Docker operations go through an injectable ``docker_runner(argv) ->
(rc, stdout)`` so the logic is fully unit-testable on the host.

Health probes (see managed_containers.py):
  exec           : docker exec <name> <argv>; rc==0 => healthy
  running_uptime : docker inspect .State.Running == true AND
                   (now - State.StartedAt) >= min_seconds (StartedAt parsed;
                   unparseable -> fail-safe). Deadline uses a monotonic clock.
"""
from __future__ import annotations

import os
import stat as stat_mod
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import managed_containers as mc  # noqa: E402

PROXY_DEPLOYED_MARKER = "/etc/hiclab/proxy-deployed"
UPSTREAM_DISABLE_MARKER = "/etc/hiclab/upstream-disable-auto-create"

# Versioned marker content (operator writes exactly these bytes, mode 0600,
# owner root). Validation rejects anything else.
PROXY_MARKER_CONTENT = b"hiclab-proxy:deployed:v1\n"
UPSTREAM_MARKER_CONTENT = b"hiclab-upstream:disable-auto-create:v1\n"

MANAGER_NAME = "hiclaw-manager"


def _read_file_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def validate_marker(path, expected_content, stat_fn=None, read_fn=None):
    """Strict marker validation via lstat. Returns (ok: bool, reason: str).

    Requires: regular file (NOT symlink), uid == 0, mode & 0o777 == 0o600,
    non-empty content == expected_content. Any check exception (OSError on
    lstat/read, unexpected error) -> rejected. Never raises.
    """
    stat_fn = stat_fn or os.lstat
    read_fn = read_fn or _read_file_bytes
    try:
        st = stat_fn(path)
    except OSError:
        return (False, "absent or lstat failed")
    except Exception as exc:
        return (False, "stat check raised: %s" % exc)
    try:
        if stat_mod.S_ISLNK(st.st_mode):
            return (False, "is symlink")
        if not stat_mod.S_ISREG(st.st_mode):
            return (False, "not a regular file")
        if st.st_uid != 0:
            return (False, "owner uid=%d (need 0)" % st.st_uid)
        if (st.st_mode & 0o777) != 0o600:
            return (False, "mode=%o (need 0600)" % (st.st_mode & 0o777))
        content = read_fn(path)
    except OSError:
        return (False, "read failed")
    except Exception as exc:
        return (False, "read raised: %s" % exc)
    if not content:
        return (False, "empty")
    if content != expected_content:
        return (False, "content/digest mismatch")
    return (True, "valid marker")


def manager_start_allowed(stat_fn=None, read_fn=None):
    """Return (allowed: bool, reason: str).

    allowed is True ONLY if a deployed+audited socket proxy OR an upstream
    disable-auto-create capability is confirmed via a STRICTLY validated
    marker file (see validate_marker). This is a programmatic check, never a
    documentation convention. When no valid marker exists, hiclaw-manager
    must not be started.
    """
    ok, reason = validate_marker(PROXY_DEPLOYED_MARKER, PROXY_MARKER_CONTENT,
                                 stat_fn, read_fn)
    if ok:
        return (True, "audited socket proxy marker valid (%s)"
                % PROXY_DEPLOYED_MARKER)
    ok, reason = validate_marker(UPSTREAM_DISABLE_MARKER,
                                 UPSTREAM_MARKER_CONTENT, stat_fn, read_fn)
    if ok:
        return (True, "upstream disable-auto-create marker valid (%s)"
                % UPSTREAM_DISABLE_MARKER)
    return (False, "no valid deployed-proxy or upstream-disable marker")


def _is_running(name, docker_runner):
    """Return True only when docker inspect succeeds AND the Running field is
    exactly 'true'. rc==0 with stdout 'false' must return False."""
    rc, out = docker_runner(
        ["docker", "inspect", "-f", "{{.State.Running}}", name])
    if rc != 0:
        return False
    return out.strip().lower() == "true"


def _start(name, docker_runner):
    rc, _out = docker_runner(["docker", "start", name])
    return rc


def _stop(name, docker_runner):
    rc, _out = docker_runner(["docker", "stop", name])
    return rc


def _probe_exec(name, probe, docker_runner):
    argv = ["docker", "exec", name] + list(probe.get("argv", []))
    rc, _out = docker_runner(argv)
    return rc == 0


def _parse_started_at(raw):
    """Parse a docker State.StartedAt string to epoch seconds (float).

    Returns None if empty, the docker "never started" sentinel
    (0001-01-01...), or an unparseable value. Fail-safe: callers treat None
    as "cannot confirm uptime -> not healthy".
    """
    if not raw:
        return None
    s = raw.strip()
    if not s or s.startswith("0001-01-01"):
        return None
    from datetime import datetime, timezone
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, rest = s.split(".", 1)
        tz = ""
        for i, ch in enumerate(rest):
            if ch in "+-":
                tz = rest[i:]
                rest = rest[:i]
                break
        rest = rest[:6].ljust(6, "0")
        s = head + "." + rest + tz
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _probe_running_uptime(name, probe, docker_runner, now_fn=None):
    """Verify Running AND uptime >= min_seconds via parsed StartedAt.

    now_fn defaults to wall-clock time (StartedAt is a wall-clock timestamp).
    Unparseable/absent StartedAt -> False (fail-safe: never claim healthy
    without confirming the minimum uptime has elapsed).
    """
    now_fn = now_fn or time.time
    min_seconds = int(probe.get("min_seconds", 0))
    if not _is_running(name, docker_runner):
        return False
    if min_seconds <= 0:
        return True
    rc, out = docker_runner(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", name])
    if rc != 0:
        return False
    started = _parse_started_at(out)
    if started is None:
        return False
    return (now_fn() - started) >= min_seconds


def check_health(name, docker_runner, now_fn=None):
    """Return (healthy: bool, detail: str) for one managed container."""
    m = mc.find(name)
    if m is None:
        return (False, "not in manifest")
    probe = m["health"]
    if probe["kind"] == "exec":
        ok = _probe_exec(name, probe, docker_runner)
        return (ok, "exec rc=%s" % ("0" if ok else "nonzero"))
    if probe["kind"] == "running_uptime":
        ok = _probe_running_uptime(name, probe, docker_runner, now_fn)
        return (ok, "running_uptime")
    return (False, "unknown probe kind %r" % probe["kind"])


def wait_healthy(name, docker_runner, timeout, poll, sleep_fn=None,
                 clock_fn=None, now_fn=None):
    """Poll check_health until healthy or timeout. Returns True/False.

    Uses a monotonic clock (default time.monotonic) for the deadline so
    wall-clock jumps do not shorten or extend the wait. now_fn is passed to
    uptime probes (wall-clock, since StartedAt is wall-clock based).
    """
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.monotonic
    deadline = clock_fn() + timeout
    while True:
        ok, _detail = check_health(name, docker_runner, now_fn=now_fn)
        if ok:
            return True
        if clock_fn() >= deadline:
            return False
        sleep_fn(poll)


def _rollback(started_names, docker_runner):
    """Stop (in reverse order) every container started this round."""
    stopped = []
    for name in reversed(started_names):
        if _stop(name, docker_runner) == 0:
            stopped.append(name)
    return stopped


def start_with_health_gate(docker_runner, timeout=90, poll=3, sleep_fn=None,
                           stat_fn=None, read_fn=None, clock_fn=None,
                           now_fn=None):
    """Run the full phased startup. Returns dict:
      {ok, started_this_round, blocked, failed_at, detail,
       stopped_on_rollback, blocked_reason}

    hiclaw-manager is started ONLY if manager_start_allowed() is True;
    otherwise it is added to ``blocked`` and never started/health-gated.
    Other containers start in health order. On any unhealthy dependency,
    fail-closed: stop every container started this round.
    """
    mc.check_unique()
    sleep_fn = sleep_fn or time.sleep
    started_this_round = []

    mgr_ok, mgr_reason = manager_start_allowed(stat_fn=stat_fn,
                                                read_fn=read_fn)
    blocked = []
    if not mgr_ok:
        blocked.append(MANAGER_NAME)
        sys.stderr.write(
            "BLOCKED_UPSTREAM: hiclaw-manager auto-create cannot be hardened "
            "(%s)\n" % mgr_reason)

    def _gate_phase(members):
        for m in members:
            if m["name"] in blocked:
                continue
            if not _is_running(m["name"], docker_runner):
                rc = _start(m["name"], docker_runner)
                if rc != 0:
                    stopped = _rollback(started_this_round, docker_runner)
                    return (False, m["name"], "start rc=%d" % rc, stopped)
                started_this_round.append(m["name"])
        for m in members:
            if m["name"] in blocked:
                continue
            if not wait_healthy(m["name"], docker_runner, timeout, poll,
                                sleep_fn, clock_fn, now_fn):
                stopped = _rollback(started_this_round, docker_runner)
                return (False, m["name"],
                        "%s unhealthy after %ds" % (m["name"], timeout),
                        stopped)
        return (True, None, None, [])

    ok, failed_at, detail, stopped = _gate_phase(mc.phase_members(mc.PHASE_1))
    if not ok:
        return {"ok": False, "phase": mc.PHASE_1,
                "started_this_round": started_this_round,
                "blocked": blocked, "failed_at": failed_at,
                "detail": detail, "stopped_on_rollback": stopped,
                "blocked_reason": (mgr_reason if blocked else None)}
    ok, failed_at, detail, stopped = _gate_phase(mc.phase_members(mc.PHASE_2))
    if not ok:
        return {"ok": False, "phase": mc.PHASE_2,
                "started_this_round": started_this_round,
                "blocked": blocked, "failed_at": failed_at,
                "detail": detail, "stopped_on_rollback": stopped,
                "blocked_reason": (mgr_reason if blocked else None)}
    return {"ok": True, "phase": mc.PHASE_2,
            "started_this_round": started_this_round,
            "blocked": blocked, "failed_at": None, "detail": None,
            "stopped_on_rollback": stopped,
            "blocked_reason": (mgr_reason if blocked else None)}


def main():
    import subprocess

    def runner(argv):
        r = subprocess.run(argv, capture_output=True, text=True)
        return (r.returncode, r.stdout)

    result = start_with_health_gate(runner)
    if result["blocked"]:
        sys.stderr.write("guarded_start: BLOCKED %s (%s)\n"
                         % (",".join(result["blocked"]),
                            result["blocked_reason"]))
    if result["ok"]:
        sys.stdout.write("guarded_start: OK started=%s\n"
                         % ",".join(result["started_this_round"]))
        return 0
    sys.stderr.write("guarded_start: FAIL phase=%s at=%s detail=%s\n"
                     % (result["phase"], result["failed_at"],
                        result["detail"]))
    sys.stderr.write("guarded_start: rolled back (stopped): %s\n"
                     % ",".join(result["stopped_on_rollback"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
