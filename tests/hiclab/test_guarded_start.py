"""Unit tests for guarded_start.py (phased startup, BLOCKED_UPSTREAM, uptime)."""
import datetime
import io
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import guarded_start as gs
import managed_containers as mc


def _old_iso(seconds_ago):
    t = (datetime.datetime.now(datetime.timezone.utc)
         - datetime.timedelta(seconds=seconds_ago))
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _marker_fns(spec):
    """Build (stat_fn, read_fn) from a spec dict:
    {path: (content_bytes, uid, mode, is_link)} or {path: None} for absent."""
    import stat as stat_mod

    def stat_fn(path):
        if path not in spec or spec[path] is None:
            raise FileNotFoundError(path)
        content, uid, mode, is_link = spec[path]
        ftype = stat_mod.S_IFLNK if is_link else stat_mod.S_IFREG
        return os.stat_result((ftype | mode, 0, 0, 0, uid, 0, 0, 0, 0, 0))

    def read_fn(path):
        if path not in spec or spec[path] is None:
            raise FileNotFoundError(path)
        return spec[path][0]
    return stat_fn, read_fn


def _valid_markers():
    spec = {
        gs.PROXY_DEPLOYED_MARKER: (gs.PROXY_MARKER_CONTENT, 0, 0o600, False),
        gs.UPSTREAM_DISABLE_MARKER: (gs.UPSTREAM_MARKER_CONTENT, 0, 0o600,
                                     False),
    }
    return _marker_fns(spec)


def _no_markers():
    return _marker_fns({})


def _proxy_marker_only():
    return _marker_fns(
        {gs.PROXY_DEPLOYED_MARKER: (gs.PROXY_MARKER_CONTENT, 0, 0o600, False)})


def _upstream_marker_only():
    return _marker_fns(
        {gs.UPSTREAM_DISABLE_MARKER: (gs.UPSTREAM_MARKER_CONTENT, 0, 0o600,
                                      False)})


# Module-level fn pairs (stateless closures; safe to reuse across tests)
_V_STAT, _V_READ = _valid_markers()
_N_STAT, _N_READ = _no_markers()


class MockRunner:
    """docker_runner(argv) -> (rc, stdout). Tracks running + started_at."""

    def __init__(self):
        self.calls = []
        self._running = set()
        self._health_rc = {}
        self._start_rc = {}
        self._stop_rc = {}
        self._started_at = {}
        self._health_status = {}  # name -> "healthy"/"unhealthy"/"starting"/"<no value>"
        self._socket_rc = {}      # name -> rc for socket probe

    def __call__(self, argv):
        self.calls.append(list(argv))
        if len(argv) < 2:
            return (1, "")
        cmd = argv[1]
        if cmd == "inspect":
            name = argv[-1]
            fmt = argv[3] if len(argv) > 3 else ""
            if "Health.Status" in fmt:
                return (0, self._health_status.get(name, "<no value>"))
            if "Running" in fmt:
                return (0, "true" if name in self._running else "false")
            if "StartedAt" in fmt:
                return (0, self._started_at.get(name, ""))
            return (0, "")
        if cmd == "start":
            name = argv[-1]
            rc = self._start_rc.get(name, 0)
            if rc == 0:
                self._running.add(name)
            return (rc, "")
        if cmd == "stop":
            name = argv[-1]
            self._running.discard(name)
            return (self._stop_rc.get(name, 0), "")
        if cmd == "exec":
            name = argv[2]
            joined = " ".join(str(a) for a in argv[3:5])
            if "python" in joined and "socket" in " ".join(str(a) for a in argv):
                return (self._socket_rc.get(name, 1), "")
            return (self._health_rc.get(name, 0), "")
        return (0, "")


def _all_healthy_setup(runner):
    """Mark all managed containers healthy (exec rc=0, uptime old, health=healthy)."""
    for m in mc.MANAGED:
        runner._health_rc[m["name"]] = 0
        runner._started_at[m["name"]] = _old_iso(100)
        runner._health_status[m["name"]] = "healthy"


class TestPhasedOrder(unittest.TestCase):
    def test_phase1_started_before_phase2(self):
        runner = MockRunner()
        _all_healthy_setup(runner)
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertTrue(result["ok"], result)
        starts = [c[-1] for c in runner.calls if len(c) > 1 and c[1] == "start"]
        p1 = [m["name"] for m in mc.phase_members(mc.PHASE_1)]
        p2 = [m["name"] for m in mc.phase_members(mc.PHASE_2)
              if m["name"] not in result["blocked"]]
        last_p1 = max(starts.index(n) for n in p1)
        first_p2 = min(starts.index(n) for n in p2)
        self.assertLess(last_p1, first_p2)


class TestHealthFailureRollback(unittest.TestCase):
    def test_phase1_unhealthy_stops_started_this_round(self):
        runner = MockRunner()
        runner._health_rc["audit-pg"] = 0
        runner._health_rc["github-mcp"] = 0
        runner._health_rc["agentteams-controller"] = 1  # unhealthy
        runner._started_at["github-mcp"] = _old_iso(100)
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_at"], "agentteams-controller")
        starts = [c[-1] for c in runner.calls if len(c) > 1 and c[1] == "start"]
        for m in mc.phase_members(mc.PHASE_2):
            self.assertNotIn(m["name"], starts)
        self.assertEqual(sorted(result["stopped_on_rollback"]),
                         sorted(result["started_this_round"]))

    def test_phase2_unhealthy_stops_this_round(self):
        runner = MockRunner()
        for n in ("audit-pg", "github-mcp", "agentteams-controller"):
            runner._health_rc[n] = 0
        runner._started_at["github-mcp"] = _old_iso(100)
        runner._health_rc["policy-gw"] = 1  # phase2 unhealthy
        runner._health_status["policy-gw"] = "unhealthy"
        runner._health_rc["mergepilot-controller"] = 0
        runner._started_at["mergepilot-controller"] = _old_iso(100)
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_at"], "policy-gw")
        self.assertEqual(sorted(result["stopped_on_rollback"]),
                         sorted(result["started_this_round"]))


class TestPreviouslyRunning(unittest.TestCase):
    def test_previously_running_not_started_or_stopped(self):
        runner = MockRunner()
        runner._running.add("audit-pg")  # already running
        runner._health_rc["audit-pg"] = 0
        runner._started_at["audit-pg"] = _old_iso(100)
        for n in ("github-mcp", "agentteams-controller", "policy-gw",
                  "mergepilot-controller", "agentteams-manager"):
            runner._health_rc[n] = 0
            runner._started_at[n] = _old_iso(100)
            runner._health_status[n] = "healthy"
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertTrue(result["ok"])
        self.assertNotIn("audit-pg", result["started_this_round"])

    def test_start_failure_rolls_back(self):
        runner = MockRunner()
        runner._start_rc["audit-pg"] = 1
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertFalse(result["ok"])


class TestCapabilityPreflight(unittest.TestCase):
    """No valid marker → 0 production containers started; controller stopped
    if already running (docker-socket bypass)."""

    def test_no_marker_zero_starts(self):
        runner = MockRunner()
        _all_healthy_setup(runner)
        stat_fn, read_fn = _no_markers()
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=stat_fn, read_fn=read_fn)
        self.assertFalse(result["ok"])
        self.assertIn("agentteams-manager", result["blocked"])
        self.assertIn("agentteams-controller", result["blocked"])
        self.assertEqual(result["started_this_round"], [])
        starts = [c for c in runner.calls if len(c) > 1 and c[1] == "start"]
        self.assertEqual(len(starts), 0)

    def test_no_marker_stops_running_controller(self):
        runner = MockRunner()
        runner._running.add("agentteams-controller")
        stat_fn, read_fn = _no_markers()
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=stat_fn, read_fn=read_fn)
        self.assertFalse(result["ok"])
        self.assertIn("agentteams-controller", result["stopped_on_rollback"])
        self.assertNotIn("agentteams-controller", runner._running)

    def test_no_marker_no_partial_stack(self):
        runner = MockRunner()
        stat_fn, read_fn = _no_markers()
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=stat_fn, read_fn=read_fn)
        self.assertEqual(result["started_this_round"], [])
        self.assertEqual(result["phase"], "preflight")
        self.assertIn("BLOCKED_UPSTREAM", result["detail"])

    def test_no_marker_emits_blocked_message(self):
        runner = MockRunner()
        stat_fn, read_fn = _no_markers()
        old_err = sys.stderr
        sys.stderr = io.StringIO()
        try:
            gs.start_with_health_gate(
                runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
                stat_fn=stat_fn, read_fn=read_fn)
            err = sys.stderr.getvalue()
        finally:
            sys.stderr = old_err
        self.assertIn("BLOCKED_UPSTREAM", err)
        self.assertIn("agentteams-controller", err)
        self.assertIn("agentteams-manager", err)

    def test_no_marker_no_workers_started(self):
        runner = MockRunner()
        _all_healthy_setup(runner)
        stat_fn, read_fn = _no_markers()
        gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=stat_fn, read_fn=read_fn)
        starts = [c[-1] for c in runner.calls if len(c) > 1 and c[1] == "start"]
        for s in starts:
            self.assertFalse(s.startswith("agentteams-worker-"))


class TestMarkerValidStartup(unittest.TestCase):
    """Valid marker → original phased startup (all 6, controller included)."""

    def test_valid_marker_starts_all_six(self):
        runner = MockRunner()
        _all_healthy_setup(runner)
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertTrue(result["ok"])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(sorted(result["started_this_round"]),
                         sorted(mc.names()))

    def test_valid_marker_controller_started(self):
        runner = MockRunner()
        _all_healthy_setup(runner)
        result = gs.start_with_health_gate(
            runner, timeout=1, poll=0, sleep_fn=lambda _s: None,
            stat_fn=_V_STAT, read_fn=_V_READ)
        self.assertIn("agentteams-controller", result["started_this_round"])
        self.assertIn("agentteams-manager", result["started_this_round"])


class TestRollbackControllerFirst(unittest.TestCase):
    """Rollback must stop the controller BEFORE other containers (it
    re-spawns them via docker socket)."""

    def test_controller_stopped_first(self):
        runner = MockRunner()
        started = ["audit-pg", "github-mcp", "agentteams-controller",
                   "policy-gw", "mergepilot-controller"]
        for n in started:
            runner._running.add(n)
        gs._rollback(started, runner)
        stops = [c[-1] for c in runner.calls if len(c) > 1 and c[1] == "stop"]
        self.assertEqual(stops[0], "agentteams-controller")

    def test_all_stopped_after_rollback(self):
        runner = MockRunner()
        started = ["audit-pg", "github-mcp", "agentteams-controller"]
        for n in started:
            runner._running.add(n)
        gs._rollback(started, runner)
        for n in started:
            self.assertNotIn(n, runner._running)

    def test_rollback_re_verifies_respawned(self):
        """After controller stop, re-verify catches containers it re-spawned."""
        runner = MockRunner()
        started = ["audit-pg", "agentteams-controller", "policy-gw"]
        for n in started:
            runner._running.add(n)
        original_stop = gs._stop
        call_count = [0]

        def tracking_stop(name, dr):
            rc = original_stop(name, dr)
            call_count[0] += 1
            # Simulate controller re-spawning policy-gw before dying
            if name == "agentteams-controller":
                runner._running.add("policy-gw")
            return rc

        gs._stop = tracking_stop
        try:
            gs._rollback(started, runner)
        finally:
            gs._stop = original_stop
        self.assertNotIn("policy-gw", runner._running)


class TestMarkerValidation(unittest.TestCase):
    """Strict lstat-based marker validation failure modes."""

    def test_valid_marker(self):
        stat_fn, read_fn = _proxy_marker_only()
        ok, _ = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertTrue(ok)

    def test_symlink_rejected(self):
        spec = {gs.PROXY_DEPLOYED_MARKER: (gs.PROXY_MARKER_CONTENT, 0, 0o600,
                                           True)}
        stat_fn, read_fn = _marker_fns(spec)
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertFalse(ok)
        self.assertIn("symlink", reason)

    def test_wrong_uid_rejected(self):
        spec = {gs.PROXY_DEPLOYED_MARKER: (gs.PROXY_MARKER_CONTENT, 1000,
                                           0o600, False)}
        stat_fn, read_fn = _marker_fns(spec)
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertFalse(ok)
        self.assertIn("uid", reason)

    def test_wrong_mode_rejected(self):
        spec = {gs.PROXY_DEPLOYED_MARKER: (gs.PROXY_MARKER_CONTENT, 0,
                                           0o644, False)}
        stat_fn, read_fn = _marker_fns(spec)
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertFalse(ok)
        self.assertIn("mode", reason)

    def test_empty_rejected(self):
        spec = {gs.PROXY_DEPLOYED_MARKER: (b"", 0, 0o600, False)}
        stat_fn, read_fn = _marker_fns(spec)
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_wrong_content_rejected(self):
        spec = {gs.PROXY_DEPLOYED_MARKER: (b"wrong-content\n", 0, 0o600, False)}
        stat_fn, read_fn = _marker_fns(spec)
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertFalse(ok)
        self.assertIn("mismatch", reason)

    def test_absent_rejected(self):
        stat_fn, read_fn = _no_markers()
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, stat_fn, read_fn)
        self.assertFalse(ok)

    def test_stat_exception_rejected(self):
        def bad_stat(path):
            raise RuntimeError("boom")
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT, bad_stat,
            lambda p: b"")
        self.assertFalse(ok)

    def test_read_exception_rejected(self):
        import stat as stat_mod
        good_stat = os.stat_result(
            (stat_mod.S_IFREG | 0o600, 0, 0, 0, 0, 0, 0, 0, 0, 0))

        def bad_read(path):
            raise RuntimeError("read boom")
        ok, reason = gs.validate_marker(
            gs.PROXY_DEPLOYED_MARKER, gs.PROXY_MARKER_CONTENT,
            lambda p: good_stat, bad_read)
        self.assertFalse(ok)

    def test_invalid_marker_does_not_allow_manager(self):
        """A present-but-invalid marker (wrong mode) must NOT unblock."""
        spec = {gs.PROXY_DEPLOYED_MARKER: (gs.PROXY_MARKER_CONTENT, 0,
                                           0o644, False)}  # wrong mode
        stat_fn, read_fn = _marker_fns(spec)
        ok, _ = gs.manager_start_allowed(stat_fn=stat_fn, read_fn=read_fn)
        self.assertFalse(ok)


class TestStartedAtParse(unittest.TestCase):
    def test_z_suffix(self):
        self.assertIsNotNone(gs._parse_started_at("2025-01-01T00:00:00Z"))

    def test_nanoseconds(self):
        self.assertIsNotNone(
            gs._parse_started_at("2025-01-01T00:00:00.123456789Z"))

    def test_never_started_sentinel(self):
        self.assertIsNone(gs._parse_started_at("0001-01-01T00:00:00Z"))

    def test_empty(self):
        self.assertIsNone(gs._parse_started_at(""))
        self.assertIsNone(gs._parse_started_at(None))

    def test_garbage(self):
        self.assertIsNone(gs._parse_started_at("not-a-date"))


class TestUptimeGate(unittest.TestCase):
    def test_too_recent_not_healthy(self):
        runner = MockRunner()
        runner._running.add("github-mcp")
        ts = "2025-01-01T00:00:00Z"
        runner._started_at["github-mcp"] = ts
        started = gs._parse_started_at(ts)
        now = started + 2  # 2s elapsed; min_seconds=5
        ok, _ = gs.check_health("github-mcp", runner, now_fn=lambda: now)
        self.assertFalse(ok)

    def test_old_enough_healthy(self):
        runner = MockRunner()
        runner._running.add("github-mcp")
        ts = "2025-01-01T00:00:00Z"
        runner._started_at["github-mcp"] = ts
        started = gs._parse_started_at(ts)
        now = started + 100  # 100s elapsed; min_seconds=5
        ok, _ = gs.check_health("github-mcp", runner, now_fn=lambda: now)
        self.assertTrue(ok)

    def test_unparseable_started_at_not_healthy(self):
        runner = MockRunner()
        runner._running.add("github-mcp")
        runner._started_at["github-mcp"] = "garbage"
        ok, _ = gs.check_health("github-mcp", runner,
                                now_fn=lambda: time.time())
        self.assertFalse(ok)

    def test_not_running_not_healthy(self):
        runner = MockRunner()
        # github-mcp not in _running
        ok, _ = gs.check_health("github-mcp", runner)
        self.assertFalse(ok)

    def test_stopped_with_old_started_at_not_healthy(self):
        """Regression: State.Running=false must not be masked by old StartedAt."""
        runner = MockRunner()
        # github-mcp stopped (not in _running -> Running=false) but StartedAt old
        runner._started_at["github-mcp"] = _old_iso(100)
        ok, _ = gs.check_health("github-mcp", runner)
        self.assertFalse(ok)


class TestCheckHealthExec(unittest.TestCase):
    def test_exec_healthy(self):
        runner = MockRunner()
        runner._health_rc["audit-pg"] = 0
        ok, _ = gs.check_health("audit-pg", runner)
        self.assertTrue(ok)

    def test_exec_unhealthy(self):
        runner = MockRunner()
        runner._health_rc["audit-pg"] = 1
        ok, _ = gs.check_health("audit-pg", runner)
        self.assertFalse(ok)

    def test_unknown_container(self):
        runner = MockRunner()
        ok, _ = gs.check_health("bogus", runner)
        self.assertFalse(ok)


class TestDockerHealthProbe(unittest.TestCase):
    """Fix 1: policy-gw Docker Health.Status probe (no container-internal curl)."""

    def test_healthy(self):
        runner = MockRunner()
        runner._health_status["policy-gw"] = "healthy"
        ok, detail = gs.check_health("policy-gw", runner)
        self.assertTrue(ok)
        self.assertEqual(detail, "docker_health")

    def test_unhealthy(self):
        runner = MockRunner()
        runner._health_status["policy-gw"] = "unhealthy"
        ok, _ = gs.check_health("policy-gw", runner)
        self.assertFalse(ok)

    def test_starting(self):
        runner = MockRunner()
        runner._health_status["policy-gw"] = "starting"
        ok, _ = gs.check_health("policy-gw", runner)
        self.assertFalse(ok)

    def test_no_healthcheck_socket_success(self):
        """No HEALTHCHECK (<no value>) -> socket fallback succeeds."""
        runner = MockRunner()
        runner._health_status["policy-gw"] = "<no value>"
        runner._socket_rc["policy-gw"] = 0
        ok, _ = gs.check_health("policy-gw", runner)
        self.assertTrue(ok)

    def test_no_healthcheck_socket_fail(self):
        runner = MockRunner()
        runner._health_status["policy-gw"] = "<no value>"
        runner._socket_rc["policy-gw"] = 1
        ok, _ = gs.check_health("policy-gw", runner)
        self.assertFalse(ok)

    def test_none_status_socket_fallback(self):
        runner = MockRunner()
        runner._health_status["policy-gw"] = "none"
        runner._socket_rc["policy-gw"] = 0
        ok, _ = gs.check_health("policy-gw", runner)
        self.assertTrue(ok)

    def test_socket_exception_fail_closed(self):
        runner = MockRunner()
        runner._health_status["policy-gw"] = "<no value>"

        def bad_runner(argv):
            if "python" in " ".join(str(a) for a in argv):
                raise RuntimeError("exec boom")
            return (0, "<no value>")

        ok, _ = gs.check_health("policy-gw", bad_runner)
        self.assertFalse(ok)

    def test_no_port_no_healthcheck_fail_closed(self):
        """No HEALTHCHECK + no port in probe -> fail-closed."""
        runner = MockRunner()
        runner._health_status["bogus"] = "<no value>"
        ok = gs._probe_docker_health("bogus", {"kind": "docker_health"}, runner)
        self.assertFalse(ok)

    def test_does_not_use_running_uptime(self):
        """docker_health must not fall back to running_uptime."""
        runner = MockRunner()
        runner._health_status["policy-gw"] = "unhealthy"
        # running_uptime would see it as running+old -> True; but docker_health
        # must report False because the healthcheck says unhealthy.
        runner._running.add("policy-gw")
        runner._started_at["policy-gw"] = _old_iso(100)
        ok, detail = gs.check_health("policy-gw", runner)
        self.assertFalse(ok)
        self.assertEqual(detail, "docker_health")


if __name__ == "__main__":
    unittest.main()
