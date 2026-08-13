#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISOLATED_LIVE Phase 1 tests — real HTTP integration.

These tests boot an actual read-only HTTP server (port 0 = OS-assigned)
in a background thread and exercise it with real ``urllib.request`` calls.
They cover 24 scenarios:

Mode / preflight
  1.  REPLAY is a valid mode
  2.  ISOLATED_LIVE is a valid mode
  3.  Invalid mode rejected by preflight
  4.  Non-loopback host rejected by preflight
  5.  Loopback hosts accepted

Source validation
  6.  ISOLATED_LIVE without --source-file fails
  7.  http/https source rejected
  8.  Nonexistent source rejected
  9.  Corrupt JSON rejected
  10. Schema-invalid source rejected
  11. Integrity-invalid source rejected (sha mismatch)
  12. file:// URI rejected
  13. UNC/network path rejected
  14. Directory (non-regular-file) rejected
  15. Valid local-file source passes

Polling
  16. Initial load succeeds with valid snapshot
  17. Updated snapshot replaces previous
  18. Invalid (corrupt) snapshot does NOT overwrite valid one
  19. Integrity-invalid snapshot does NOT overwrite valid one
  20. Stats tracked (poll_count, consecutive_failures, state)

HTTP boundaries
  21. GET /api/live/snapshot returns 200 + JSON in ISOLATED_LIVE
  22. GET /api/live/status returns structured status JSON
  23. Write methods (POST/PUT/PATCH/DELETE) blocked with 405
  24. /api/live/* returns 404 in REPLAY mode

All file operations use ``with`` so the suite is compatible with
``-W error::ResourceWarning``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in [str(ROOT), str(ROOT / "tools" / "demo_console")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from preflight import run_preflight, VALID_MODES
from schema import validate_bundle, VALID_DEMO_MODES
from integrity import compute_bundle_sha256, verify_bundle_integrity
from live_poller import FileSnapshotSource, LivePoller
from serve import create_server

# The shipped REPLAY bundle is a complete, schema-valid DemoBundle. The test
# helpers clone it and flip demo_mode + recompute the SHA to produce a valid
# ISOLATED_LIVE fixture without rebuilding from evidence.
BUNDLE_PATH = ROOT / "samples" / "demo-bundles" / "m7-rag-replay.json"


def _load_replay_bundle() -> dict:
    """Load the shipped REPLAY bundle as the basis for test fixtures."""
    with open(BUNDLE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_isolated_live_bundle(**overrides) -> dict:
    """Return a valid ISOLATED_LIVE bundle with a correct bundle_sha256.

    Any keyword overrides are applied before the SHA is (re)computed.
    """
    bundle = _load_replay_bundle()
    bundle["demo_mode"] = "ISOLATED_LIVE"
    bundle.update(overrides)
    bundle["bundle_sha256"] = compute_bundle_sha256(bundle)
    return bundle


def _write_json(path: str, data) -> None:
    """Write JSON to ``path`` using a context manager."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class _ServerHandle:
    """Context-manager-ish lifecycle wrapper for a background HTTP server.

    Holds the TCPServer, its serve_forever thread, and the optional poller.
    ``stop()`` shuts the server down (idempotent) and joins the thread.
    """

    def __init__(self, server, thread, poller=None):
        self.server = server
        self.thread = thread
        self.poller = poller
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def stop(self):
        if self.poller is not None:
            self.poller.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start_live_server(snapshot_path: str, poll_interval: float = 1.0) -> _ServerHandle:
    """Start an ISOLATED_LIVE server with a fresh poller over ``snapshot_path``.

    Performs the initial load (must succeed) and starts serve_forever in a
    daemon thread. Returns a handle whose ``stop()`` cleans everything up.
    """
    source = FileSnapshotSource(snapshot_path)
    poller = LivePoller(source, poll_interval=poll_interval)
    if not poller.initial_load():
        raise AssertionError(
            f"initial snapshot load failed; state={poller.state} "
            f"err={poller.last_error_code}"
        )
    server = create_server("127.0.0.1", 0, "ISOLATED_LIVE", poller=poller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Give the listener a moment to accept connections.
    time.sleep(0.2)
    return _ServerHandle(server, thread, poller=poller)


def _start_replay_server() -> _ServerHandle:
    """Start a REPLAY server (static + 404 for /api/live/*)."""
    server = create_server("127.0.0.1", 0, "REPLAY")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    return _ServerHandle(server, thread, poller=None)


def _http_get_json(url: str):
    """GET ``url`` and return (status, headers, parsed_json-or-None).

    On a non-2xx response, returns the error status and any JSON body the
    server sent (so 404/405/503 paths can be inspected).
    """
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else None
            return resp.status, dict(resp.headers), parsed
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, dict(e.headers), parsed


def _http_method(url: str, method: str, data: bytes | None = None):
    """Issue an arbitrary HTTP method, returning (status, parsed_json-or-None)."""
    req = urllib.request.Request(url, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else None
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, parsed


class TestValidModes(unittest.TestCase):
    """1-2. Both demo modes are recognized."""

    def test_replay_is_valid_mode(self):
        self.assertIn("replay", VALID_MODES)

    def test_isolated_live_is_valid_mode(self):
        self.assertIn("isolated_live", VALID_MODES)

    def test_valid_demo_modes_uppercase(self):
        self.assertEqual(VALID_DEMO_MODES, frozenset({"REPLAY", "ISOLATED_LIVE"}))


class TestPreflightModeValidation(unittest.TestCase):
    """3. Invalid mode rejected."""

    def test_invalid_mode_rejected(self):
        pf = run_preflight("production", "127.0.0.1")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "mode_valid" for f in pf["failures"]))

    def test_empty_mode_rejected(self):
        pf = run_preflight("", "127.0.0.1")
        self.assertFalse(pf["preflight_passed"])


class TestPreflightLoopback(unittest.TestCase):
    """4-5. Loopback enforcement."""

    def test_non_loopback_rejected(self):
        pf = run_preflight("isolated_live", "0.0.0.0")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "loopback_only" for f in pf["failures"]))

    def test_lan_ip_rejected(self):
        pf = run_preflight("replay", "192.168.1.1")
        self.assertFalse(pf["preflight_passed"])

    def test_loopback_accepted(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            pf = run_preflight("replay", host)
            self.assertTrue(pf["preflight_passed"], f"host={host}")


class TestSourceValidation(unittest.TestCase):
    """6-15. Source configuration + path provenance validation."""

    def test_isolated_live_no_source_fails(self):
        # 6. ISOLATED_LIVE requires --source-file
        pf = run_preflight("isolated_live", "127.0.0.1")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_configured" for f in pf["failures"]))

    def test_http_source_rejected(self):
        # 7. http/https rejected
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="https://example.com/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_not_http" for f in pf["failures"]))

    def test_nonexistent_source_rejected(self):
        # 8. missing file rejected
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file=os.path.join(tempfile.gettempdir(),
                                                    "mergepilot_nonexistent_snap.json"))
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_exists" for f in pf["failures"]))

    def test_corrupt_json_rejected(self):
        # 9. corrupt JSON rejected
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ this is not valid json")
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertFalse(pf["preflight_passed"])
            self.assertTrue(any(f["check"] == "source_json_valid" for f in pf["failures"]))
        finally:
            os.unlink(path)

    def test_schema_invalid_rejected(self):
        # 10. schema-invalid rejected
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"not_a_bundle": True}, f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertFalse(pf["preflight_passed"])
            self.assertTrue(any(f["check"] == "source_schema_valid" for f in pf["failures"]))
        finally:
            os.unlink(path)

    def test_integrity_invalid_rejected(self):
        # 11. integrity (sha mismatch) rejected
        bundle = _make_isolated_live_bundle()
        bundle["bundle_sha256"] = "0" * 64  # well-formed but wrong
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bundle, f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertFalse(pf["preflight_passed"])
            self.assertTrue(any(f["check"] == "source_integrity" for f in pf["failures"]))
        finally:
            os.unlink(path)

    def test_file_uri_rejected(self):
        # 12. file:// URI rejected
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="file:///tmp/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_not_file_uri" for f in pf["failures"]))

    def test_unc_path_rejected(self):
        # 13. UNC/network path rejected (forward-slash form)
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="//server/share/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_not_unc" for f in pf["failures"]))

    def test_unc_path_backslash_rejected(self):
        # 13b. UNC path rejected (backslash form)
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="\\\\server\\share\\snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_not_unc" for f in pf["failures"]))

    def test_directory_source_rejected(self):
        # 14. non-regular file (directory) rejected
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=d)
            self.assertFalse(pf["preflight_passed"])
            self.assertTrue(any(f["check"] == "source_is_regular_file"
                                for f in pf["failures"]))

    def test_valid_local_file_source_passes(self):
        # 15. valid local file passes and reports correct provenance
        bundle = _make_isolated_live_bundle()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bundle, f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertTrue(pf["preflight_passed"],
                            f"unexpected failures: {pf['failures']}")
            self.assertEqual(pf["source_kind"], "FILE_FIXTURE")
            self.assertEqual(pf["source_path_kind"], "LOCAL_FILE")
            self.assertTrue(pf["source_is_local_file"])
            self.assertFalse(pf["source_is_network_path"])
            self.assertEqual(pf["source_path_resolved"], os.path.abspath(path))
        finally:
            os.unlink(path)


class TestPreflightProvenanceFields(unittest.TestCase):
    """New provenance/observation fields are present and honest."""

    def test_production_access_not_measured(self):
        pf = run_preflight("replay", "127.0.0.1")
        # None means "not measured", never a false claim of clean access.
        self.assertIsNone(pf["production_resource_accessed"])
        self.assertEqual(pf["production_resource_access_status"], "NOT_MEASURED")

    def test_browser_network_observation_not_measured(self):
        pf = run_preflight("replay", "127.0.0.1")
        self.assertEqual(pf["browser_network_observation_status"], "NOT_MEASURED")
        self.assertIsNone(pf["observed_external_network_requests"])

    def test_github_writes_never_enabled(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["github_writes_enabled"])

    def test_agent_control_never_enabled(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["agent_control_enabled"])

    def test_rag_context_never_consumed(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["runtime_consumes_rag_context"])


class TestSnapshotSourceAndPoller(unittest.TestCase):
    """16-20. Polling behavior: initial load, update, skip, stats."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_initial_load_success(self):
        # 16. valid snapshot loads
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        self.assertTrue(poller.initial_load())
        self.assertEqual(poller.state, "LIVE")
        self.assertIsNotNone(poller.current_snapshot)

    def test_initial_load_failure_no_snapshot(self):
        src = FileSnapshotSource(os.path.join(self._tmpdir, "missing.json"))
        poller = LivePoller(src, poll_interval=1.0)
        self.assertFalse(poller.initial_load())
        self.assertIsNone(poller.current_snapshot)

    def test_poll_updates_snapshot(self):
        # 17. updated snapshot replaces previous
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        sha_before = poller.current_sha256

        # Write a different valid ISOLATED_LIVE snapshot (new generated_at +
        # recomputed bundle_sha256).
        bundle = _make_isolated_live_bundle(generated_at="2099-01-01T00:00:00Z")
        _write_json(self._snapshot_path, bundle)

        poller._poll_once()
        sha_after = poller.current_sha256
        self.assertNotEqual(sha_before, sha_after)
        self.assertEqual(poller.state, "LIVE")

    def test_corrupt_snapshot_does_not_overwrite(self):
        # 18. corrupt JSON does not overwrite the valid snapshot
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        valid_snapshot = poller.current_snapshot
        valid_sha = poller.current_sha256

        with open(self._snapshot_path, "w", encoding="utf-8") as f:
            f.write("{ corrupt")

        poller._poll_once()
        self.assertEqual(poller.current_snapshot, valid_snapshot)
        self.assertEqual(poller.current_sha256, valid_sha)
        self.assertGreater(poller.consecutive_failures, 0)
        self.assertEqual(poller.state, "STALE")

    def test_integrity_invalid_snapshot_does_not_overwrite(self):
        # 19. integrity-invalid (sha mismatch) does not overwrite
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        valid_snapshot = poller.current_snapshot

        # Same content but a wrong bundle_sha256.
        bundle = _make_isolated_live_bundle()
        bundle["bundle_sha256"] = "f" * 64
        _write_json(self._snapshot_path, bundle)

        poller._poll_once()
        self.assertEqual(poller.current_snapshot, valid_snapshot)
        self.assertGreater(poller.consecutive_failures, 0)
        self.assertEqual(poller.last_error_code, "ValueError")

    def test_stats_tracked(self):
        # 20. stats are tracked across polls
        src = FileSnapshotSource(os.path.join(self._tmpdir, "missing.json"))
        poller = LivePoller(src, poll_interval=1.0, max_consecutive_failures=3)
        self.assertEqual(poller.get_stats()["poll_count"], 0)
        poller._poll_once()
        self.assertEqual(poller.poll_count, 1)
        self.assertEqual(poller.consecutive_failures, 1)
        stats = poller.get_stats()
        self.assertEqual(stats["consecutive_failures"], 1)
        self.assertEqual(stats["last_error_code"], "FileNotFoundError")
        self.assertEqual(stats["state"], "INIT")

        poller._poll_once()
        poller._poll_once()
        self.assertEqual(poller.consecutive_failures, 3)
        self.assertEqual(poller.state, "DEGRADED")

    def test_stop_on_max_failures_param_removed(self):
        # The unused stop_on_max_failures parameter was removed.
        import inspect
        sig = inspect.signature(LivePoller.__init__)
        self.assertNotIn("stop_on_max_failures", sig.parameters)

    def test_shutdown_stops_thread(self):
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        poller.start()
        time.sleep(0.3)
        poller.stop()
        poller.join(timeout=5)
        self.assertFalse(poller.is_alive())
        self.assertEqual(poller.state, "STOPPED")

    def test_input_fixture_not_modified(self):
        with open(self._snapshot_path, "rb") as f:
            original = f.read()
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        poller._poll_once()
        with open(self._snapshot_path, "rb") as f:
            after = f.read()
        self.assertEqual(original, after)


class TestHttpLiveSnapshot(unittest.TestCase):
    """21. GET /api/live/snapshot over real HTTP in ISOLATED_LIVE."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        self._bundle = _make_isolated_live_bundle()
        _write_json(self._snapshot_path, self._bundle)
        self._handle = _start_live_server(self._snapshot_path)

    def tearDown(self):
        self._handle.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_snapshot_returns_200_json(self):
        status, headers, payload = _http_get_json(self._handle.base_url + "/api/live/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(payload["demo_mode"], "ISOLATED_LIVE")
        self.assertEqual(payload["bundle_sha256"], self._bundle["bundle_sha256"])

    def test_snapshot_reflects_update(self):
        _, _, before = _http_get_json(self._handle.base_url + "/api/live/snapshot")
        # Change a NON-volatile field so the bundle_sha256 actually differs.
        # (generated_at is volatile and excluded from the digest.)
        new_bundle = _make_isolated_live_bundle(final_status="HELD")
        _write_json(self._snapshot_path, new_bundle)
        self._handle.poller._poll_once()
        _, _, after = _http_get_json(self._handle.base_url + "/api/live/snapshot")
        self.assertNotEqual(before["bundle_sha256"], after["bundle_sha256"])
        self.assertEqual(after["bundle_sha256"], new_bundle["bundle_sha256"])


class TestHttpLiveStatus(unittest.TestCase):
    """22. GET /api/live/status over real HTTP."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        self._handle = _start_live_server(self._snapshot_path)

    def tearDown(self):
        self._handle.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_status_returns_structured_json(self):
        status, headers, payload = _http_get_json(self._handle.base_url + "/api/live/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        for key in ("mode", "state", "poll_count", "last_poll_at",
                    "last_success_at", "source_snapshot_sha256",
                    "consecutive_failures", "last_error_code"):
            self.assertIn(key, payload, f"missing status key: {key}")
        self.assertEqual(payload["mode"], "ISOLATED_LIVE")
        self.assertEqual(payload["state"], "LIVE")
        self.assertGreaterEqual(payload["poll_count"], 1)


class TestHttpWriteMethodsBlocked(unittest.TestCase):
    """23. Write methods blocked with 405 in both modes."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        self._handle = _start_live_server(self._snapshot_path)

    def tearDown(self):
        self._handle.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_post_blocked(self):
        status, _ = _http_method(self._handle.base_url + "/api/live/snapshot",
                                 "POST", b"{}")
        self.assertEqual(status, 405)

    def test_put_blocked(self):
        status, _ = _http_method(self._handle.base_url + "/api/live/snapshot", "PUT", b"x")
        self.assertEqual(status, 405)

    def test_patch_blocked(self):
        status, _ = _http_method(self._handle.base_url + "/api/live/snapshot", "PATCH", b"x")
        self.assertEqual(status, 405)

    def test_delete_blocked(self):
        status, _ = _http_method(self._handle.base_url + "/api/live/snapshot", "DELETE")
        self.assertEqual(status, 405)


class TestReplayApiLive404(unittest.TestCase):
    """24. /api/live/* returns 404 JSON in REPLAY mode."""

    def setUp(self):
        # REPLAY serves the shipped static samples dir; cd there so the
        # SimpleHTTPRequestHandler can find index.html.
        replay_dir = str(ROOT / "samples" / "demo-console")
        self._cwd = os.getcwd()
        os.chdir(replay_dir)
        self._handle = _start_replay_server()

    def tearDown(self):
        self._handle.stop()
        os.chdir(self._cwd)

    def test_live_snapshot_404_in_replay(self):
        status, headers, payload = _http_get_json(
            self._handle.base_url + "/api/live/snapshot")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertIn("error", payload)

    def test_live_status_404_in_replay(self):
        status, _, payload = _http_get_json(
            self._handle.base_url + "/api/live/status")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)


class TestUnknownLiveEndpoint(unittest.TestCase):
    """Unknown /api/live/* subpaths 404 with JSON in ISOLATED_LIVE."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        self._handle = _start_live_server(self._snapshot_path)

    def tearDown(self):
        self._handle.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_unknown_live_endpoint_404(self):
        status, headers, payload = _http_get_json(
            self._handle.base_url + "/api/live/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertIn("error", payload)


class TestSchemaExpectedMode(unittest.TestCase):
    """validate_bundle(expected_mode=...) enforces mode-specific bundles."""

    def test_replay_bundle_passes_with_replay_expected(self):
        bundle = _load_replay_bundle()
        self.assertEqual(validate_bundle(bundle, expected_mode="REPLAY"), [])

    def test_replay_bundle_fails_with_isolated_live_expected(self):
        bundle = _load_replay_bundle()
        errors = validate_bundle(bundle, expected_mode="ISOLATED_LIVE")
        self.assertTrue(any("demo_mode mismatch" in e for e in errors))

    def test_isolated_live_bundle_fails_with_replay_expected(self):
        bundle = _make_isolated_live_bundle()
        errors = validate_bundle(bundle, expected_mode="REPLAY")
        self.assertTrue(any("demo_mode mismatch" in e for e in errors))


class TestIntegrityHelpers(unittest.TestCase):
    """Integrity module sanity checks used by the server stack."""

    def test_verify_valid_bundle(self):
        bundle = _make_isolated_live_bundle()
        self.assertEqual(verify_bundle_integrity(bundle), [])

    def test_verify_missing_sha(self):
        bundle = _make_isolated_live_bundle()
        del bundle["bundle_sha256"]
        errors = verify_bundle_integrity(bundle)
        self.assertTrue(any("missing" in e for e in errors))

    def test_verify_malformed_sha(self):
        bundle = _make_isolated_live_bundle()
        bundle["bundle_sha256"] = "not-hex"
        errors = verify_bundle_integrity(bundle)
        self.assertTrue(any("not valid" in e for e in errors))

    def test_compute_sha_excludes_volatile(self):
        b1 = _make_isolated_live_bundle(generated_at="2020-01-01T00:00:00Z")
        b2 = _make_isolated_live_bundle(generated_at="2099-12-31T00:00:00Z")
        # generated_at is volatile → identical canonical digest
        self.assertEqual(
            compute_bundle_sha256(b1),
            compute_bundle_sha256(b2),
        )


if __name__ == "__main__":
    unittest.main()
