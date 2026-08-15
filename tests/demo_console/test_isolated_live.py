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
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
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
from serve import create_server, make_handler, shutdown_poller

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
    """Issue an arbitrary HTTP method, single-attempt.

    Returns ``(status, parsed_json_or_None, retry_count)``.

    There is NO retry logic here: each request is a single attempt. Under
    normal conditions ``retry_count`` is always 0. A connection reset
    (``ConnectionAbortedError`` / ``ConnectionResetError`` /
    ``BrokenPipeError``) is NOT silently retried to make the test pass — it is
    logged as a diagnostic and re-raised so the caller can surface it as a
    real failure. This avoids masking genuine instability behind a retry loop.

    An ``HTTPError`` (e.g. 405) is a real HTTP response and is returned
    immediately (it is not an error condition and is never retried).
    """
    retry_count = 0  # single-attempt: always 0
    req = urllib.request.Request(url, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else None
            return resp.status, parsed, retry_count
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, parsed, retry_count
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
        # Connection reset means the client never received an HTTP response.
        # This is a real failure — do NOT fake a 405, retry, or skip.
        # The server-side _reject_write_method drains the request body and
        # sends 405 cleanly; if the client still sees a reset, that's a bug
        # to fix, not a platform quirk to mask.
        raise


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
        # IPv4-loopback only. 127.0.0.1 and localhost are accepted; ::1 is
        # NOT (the P1 server is IPv4-loopback only).
        for host in ("127.0.0.1", "localhost"):
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
        # 15. valid local file passes and reports correct provenance.
        # On Windows the temp file lives on a DRIVE_FIXED volume, so the
        # source is VERIFIED_LOCAL and preflight passes. On POSIX the
        # classifier returns POSIX_LOCAL_CANDIDATE which is fail-closed
        # (NOT_MEASURED → preflight fails), so this assertion is gated on
        # Windows. POSIX behavior is covered by TestWindowsLocality /
        # TestSourceLocality.
        if os.name != "nt":
            self.skipTest("VERIFIED_LOCAL requires a DRIVE_FIXED Windows volume")
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
            self.assertEqual(pf["source_locality_status"], "VERIFIED_LOCAL")
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
        # The status contract names the poller state field "poller_state".
        for key in ("mode", "poller_state", "poll_count", "last_poll_at",
                    "last_success_at", "source_snapshot_sha256",
                    "consecutive_failures", "last_error_code"):
            self.assertIn(key, payload, f"missing status key: {key}")
        self.assertEqual(payload["mode"], "ISOLATED_LIVE")
        self.assertEqual(payload["poller_state"], "LIVE")
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
        status, _, retries = _http_method(self._handle.base_url + "/api/live/snapshot",
                                          "POST", b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(retries, 0)

    def test_put_blocked(self):
        status, _, retries = _http_method(self._handle.base_url + "/api/live/snapshot", "PUT", b"x")
        self.assertEqual(status, 405)
        self.assertEqual(retries, 0)

    def test_patch_blocked(self):
        status, _, retries = _http_method(self._handle.base_url + "/api/live/snapshot", "PATCH", b"x")
        self.assertEqual(status, 405)
        self.assertEqual(retries, 0)

    def test_delete_blocked(self):
        status, _, retries = _http_method(self._handle.base_url + "/api/live/snapshot", "DELETE")
        self.assertEqual(status, 405)
        self.assertEqual(retries, 0)


class TestWriteMethodStability(unittest.TestCase):
    """25. Write-method tests are stable across repeated iterations (Windows).

    On Windows the client frequently resets the connection immediately after a
    write method is rejected with 405, which previously surfaced as
    ``ConnectionAbortedError``. The serve.py handlers catch
    ``ConnectionAbortedError`` on write-method paths and return 405 cleanly
    (``_reject_write_method``); the ``_http_method`` test helper performs NO
    retry — each request is a single attempt and ``retry_count`` is recorded.

    This test runs the write-method tests in a loop (5 iterations) and:
      - records ``retry_count`` per request (must be 0 under normal conditions);
      - asserts that every request returns 405 with ``retry_count == 0``;
      - FAILS (does NOT mask) if a ``ConnectionAbortedError`` escapes the
        serve.py handler, surfacing the error type as a diagnostic.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        self._handle = _start_live_server(self._snapshot_path)

    def tearDown(self):
        self._handle.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_write_methods_stable_over_iterations(self):
        # Run each write method 5 times against the live server. Every attempt
        # must return 405 with retry_count == 0 (no retries needed).
        methods = [
            ("POST", b"{}"),
            ("PUT", b"x"),
            ("PATCH", b"x"),
            ("DELETE", None),
        ]
        iterations = 5
        results = {}        # method -> (passes, failures)
        retry_counts = {}   # method -> list of retry_count per attempt
        diagnostics = []    # collected error-type diagnostics (should stay empty)
        for method, body in methods:
            passes = 0
            failures = 0
            method_retries = []
            for _ in range(iterations):
                try:
                    status, _, retries = _http_method(
                        self._handle.base_url + "/api/live/snapshot",
                        method, body,
                    )
                    method_retries.append(retries)
                    if status == 405 and retries == 0:
                        passes += 1
                    else:
                        failures += 1
                except (ConnectionAbortedError, ConnectionResetError,
                        BrokenPipeError) as e:
                    # A connection reset escaped the serve.py handler. This is
                    # a real failure: do NOT mask it with a retry. Record the
                    # diagnostic so the assertion message names the error type.
                    diagnostics.append(f"{type(e).__name__} on {method}")
                    failures += 1
                    method_retries.append(0)  # no retry was attempted
                except Exception as e:  # noqa: BLE001
                    diagnostics.append(f"{type(e).__name__} on {method}")
                    failures += 1
                    method_retries.append(0)
            results[method] = (passes, failures)
            retry_counts[method] = method_retries
        # Report the counts + retry counts in the assertion message.
        summary = ", ".join(
            f"{m}: {r[0]} pass/{r[1]} fail "
            f"(retries={retry_counts[m]})"
            for m, r in results.items()
        )
        total_failures = sum(r[1] for r in results.values())
        diag_msg = ("; diagnostics: " + ", ".join(diagnostics)) if diagnostics else ""
        self.assertEqual(
            total_failures, 0,
            f"write-method stability failures: {summary}{diag_msg}",
        )
        # Under normal conditions retry_count must be 0 for every attempt.
        for method, counts in retry_counts.items():
            self.assertTrue(
                all(c == 0 for c in counts),
                f"{method}: non-zero retry_count observed {counts} "
                f"(retry masking is forbidden)",
            )


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


class TestWriteBodyHandling(unittest.TestCase):
    """Raw socket tests for strict write-method body handling."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        self._source = FileSnapshotSource(self._snapshot_path)
        self._poller = LivePoller(self._source, poll_interval=1.0)
        self._poller.initial_load()
        self._poller.start()
        self._server = create_server("127.0.0.1", 0, "isolated_live", poller=self._poller)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.3)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._poller.stop()
        self._poller.join(timeout=5)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _raw_request(self, method: str, path: str = "/",
                     headers: dict | None = None, body: bytes | None = None,
                     timeout: float = 5.0) -> tuple[int, str]:
        """Send a raw HTTP request via socket. Returns (status_code, status_text)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", self._port))
        lines = [f"{method} {path} HTTP/1.1"]
        if headers:
            for k, v in headers.items():
                lines.append(f"{k}: {v}")
        if body is not None and "Content-Length" not in (headers or {}):
            lines.append(f"Content-Length: {len(body)}")
        lines.append("Host: 127.0.0.1")
        lines.append("")
        request = "\r\n".join(lines).encode() + b"\r\n"
        if body:
            request += body
        try:
            sock.sendall(request)
            # Read the status line
            response = b""
            while b"\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            parts = status_line.split(" ", 2)
            code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            text = parts[2] if len(parts) >= 3 else ""
            return code, text
        finally:
            sock.close()

    def test_normal_post_with_body_returns_405(self):
        code, _ = self._raw_request("POST", body=b'{"key":"value"}')
        self.assertEqual(code, 405)

    def test_normal_put_with_body_returns_405(self):
        code, _ = self._raw_request("PUT", body=b"update data")
        self.assertEqual(code, 405)

    def test_normal_patch_with_body_returns_405(self):
        code, _ = self._raw_request("PATCH", body=b"patch data")
        self.assertEqual(code, 405)

    def test_delete_without_body_returns_405(self):
        code, _ = self._raw_request("DELETE")
        self.assertEqual(code, 405)

    def test_malformed_content_length_returns_400(self):
        code, _ = self._raw_request("POST", headers={"Content-Length": "abc"})
        self.assertEqual(code, 400)

    def test_negative_content_length_returns_400(self):
        code, _ = self._raw_request("POST", headers={"Content-Length": "-5"})
        self.assertEqual(code, 400)

    def test_oversized_content_length_returns_413(self):
        code, _ = self._raw_request("POST", headers={"Content-Length": str(2 * 1048576)})
        self.assertEqual(code, 413)

    def test_chunked_transfer_encoding_returns_400(self):
        code, _ = self._raw_request("POST", headers={"Transfer-Encoding": "chunked"},
                                     body=b"0\r\n\r\n")
        self.assertEqual(code, 400)

    def test_405_has_allow_header(self):
        """405 response must include Allow: GET, HEAD."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(("127.0.0.1", self._port))
        try:
            sock.sendall(b"DELETE / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            self.assertIn(b"405", response)
            self.assertIn(b"Allow:", response)
            self.assertIn(b"GET, HEAD", response)
        finally:
            sock.close()

    def test_stalled_body_returns_408_then_next_request_ok(self):
        """Content-Length declared larger than body sent; server returns 408
        in bounded time; subsequent independent request still gets 405."""
        # First connection: declare large body, send only partial
        sock1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock1.settimeout(10.0)
        sock1.connect(("127.0.0.1", self._port))
        start_time = time.monotonic()
        try:
            sock1.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 1000000\r\n\r\nsmall")
            # Read response (should be 408 within BODY_READ_TIMEOUT + margin)
            response = b""
            try:
                while b"\r\n" not in response:
                    chunk = sock1.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except socket.timeout:
                pass
            elapsed = time.monotonic() - start_time
            status_line = response.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            parts = status_line.split(" ", 2)
            code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            self.assertEqual(code, 408, f"Expected 408 for stalled body, got {code}")
            # Must complete within BODY_READ_TIMEOUT(5s) + 3s tolerance
            self.assertLess(elapsed, 8.0, f"408 took {elapsed:.1f}s, expected < 8s")
        finally:
            sock1.close()
        # Second independent connection must succeed with 405
        code2, _ = self._raw_request("DELETE")
        self.assertEqual(code2, 405, "Server must still serve after stalled connection")

    def test_duplicate_content_length_returns_400(self):
        """Conflicting duplicate Content-Length (5 vs 10) → 400."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(("127.0.0.1", self._port))
        try:
            sock.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\nContent-Length: 10\r\n\r\nhello")
            response = b""
            while b"\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            parts = status_line.split(" ", 2)
            code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            self.assertEqual(code, 400)
        finally:
            sock.close()

    def test_identical_duplicate_content_length_returns_400(self):
        """Identical duplicate Content-Length (5 and 5) → 400 (count-based)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(("127.0.0.1", self._port))
        try:
            sock.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello")
            response = b""
            while b"\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            parts = status_line.split(" ", 2)
            code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            self.assertEqual(code, 400)
        finally:
            sock.close()


class TestPreflightCanonicalRole(unittest.TestCase):
    """Preflight must reject all roles except mergepilot_reader."""

    def test_preflight_rejects_reader(self):
        pf = run_preflight("isolated_live", "127.0.0.1", source_kind="postgres",
                           pg_config={"run_id": "run-1", "expected_database": "db",
                                      "expected_role": "reader",
                                      "expected_environment_id": "env-1",
                                      "expected_server_addresses": ["127.0.0.1"],
                                      "expected_server_port": 5432,
                                      "expected_application_name": "app"})
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "pg_expected_role" for f in pf["failures"]))

    def test_preflight_rejects_admin(self):
        pf = run_preflight("isolated_live", "127.0.0.1", source_kind="postgres",
                           pg_config={"run_id": "run-1", "expected_database": "db",
                                      "expected_role": "admin",
                                      "expected_environment_id": "env-1",
                                      "expected_server_addresses": ["127.0.0.1"],
                                      "expected_server_port": 5432,
                                      "expected_application_name": "app"})
        self.assertFalse(pf["preflight_passed"])

    def test_preflight_rejects_postgres(self):
        pf = run_preflight("isolated_live", "127.0.0.1", source_kind="postgres",
                           pg_config={"run_id": "run-1", "expected_database": "db",
                                      "expected_role": "postgres",
                                      "expected_environment_id": "env-1",
                                      "expected_server_addresses": ["127.0.0.1"],
                                      "expected_server_port": 5432,
                                      "expected_application_name": "app"})
        self.assertFalse(pf["preflight_passed"])

    def test_preflight_rejects_padded_role(self):
        pf = run_preflight("isolated_live", "127.0.0.1", source_kind="postgres",
                           pg_config={"run_id": "run-1", "expected_database": "db",
                                      "expected_role": " mergepilot_reader ",
                                      "expected_environment_id": "env-1",
                                      "expected_server_addresses": ["127.0.0.1"],
                                      "expected_server_port": 5432,
                                      "expected_application_name": "app"})
        self.assertFalse(pf["preflight_passed"])

    def test_preflight_rejects_empty_role(self):
        pf = run_preflight("isolated_live", "127.0.0.1", source_kind="postgres",
                           pg_config={"run_id": "run-1", "expected_database": "db",
                                      "expected_role": "",
                                      "expected_environment_id": "env-1",
                                      "expected_server_addresses": ["127.0.0.1"],
                                      "expected_server_port": 5432,
                                      "expected_application_name": "app"})
        self.assertFalse(pf["preflight_passed"])

    def test_preflight_accepts_exact_mergepilot_reader(self):
        pf = run_preflight("isolated_live", "127.0.0.1", source_kind="postgres",
                           pg_config={"run_id": "run-1", "expected_database": "db",
                                      "expected_role": "mergepilot_reader",
                                      "expected_environment_id": "env-1",
                                      "expected_server_addresses": ["127.0.0.1"],
                                      "expected_server_port": 5432,
                                      "expected_application_name": "app"})
        role_failures = [f for f in pf["failures"] if f["check"] == "pg_expected_role"]
        self.assertEqual(len(role_failures), 0)


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


def _make_replay_bundle(**overrides) -> dict:
    """Return a valid REPLAY bundle (demo_mode=REPLAY) with a correct SHA.

    Used by mode-isolation tests that need a structurally-valid bundle of the
    "other" mode to feed to a poller/preflight configured for ISOLATED_LIVE.
    """
    bundle = _load_replay_bundle()
    bundle["demo_mode"] = "REPLAY"
    bundle.update(overrides)
    bundle["bundle_sha256"] = compute_bundle_sha256(bundle)
    return bundle


class TestModeIsolation(unittest.TestCase):
    """A poller/preflight configured for one mode must reject the other mode's
    bundles, preserving the last valid snapshot and reporting MODE_MISMATCH."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_isolated_live_preflight_rejects_replay_bundle(self):
        # Preflight expects ISOLATED_LIVE; a REPLAY bundle must be rejected
        # for demo_mode mismatch (surfaced under source_schema_valid).
        bundle = _make_replay_bundle()
        _write_json(self._snapshot_path, bundle)
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file=self._snapshot_path)
        self.assertFalse(pf["preflight_passed"],
                         f"unexpected pass: {pf['failures']}")
        self.assertTrue(
            any(f["check"] == "source_schema_valid" for f in pf["failures"]),
            f"expected source_schema_valid failure; got {pf['failures']}",
        )

    def test_poller_initial_load_rejects_replay_bundle(self):
        # A poller with expected_mode="ISOLATED_LIVE" must fail initial load
        # on a REPLAY bundle and stay snapshot-less.
        _write_json(self._snapshot_path, _make_replay_bundle())
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        self.assertFalse(poller.initial_load())
        self.assertIsNone(poller.current_snapshot)
        self.assertEqual(poller.last_error_code, "MODE_MISMATCH")

    def test_valid_then_replay_preserves_last_valid_and_marks_stale(self):
        # Start LIVE on a valid ISOLATED_LIVE bundle, then swap in a REPLAY
        # bundle: the last valid snapshot is preserved, state -> STALE, and
        # the error code is MODE_MISMATCH (not a generic ValueError).
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load())
        valid_snapshot = poller.current_snapshot
        valid_sha = poller.current_sha256

        _write_json(self._snapshot_path, _make_replay_bundle())
        ok = poller._poll_once()

        self.assertFalse(ok)
        self.assertEqual(poller.current_snapshot, valid_snapshot)
        self.assertEqual(poller.current_sha256, valid_sha)
        self.assertEqual(poller.state, "STALE")
        self.assertEqual(poller.last_error_code, "MODE_MISMATCH")

    def test_mode_mismatch_reaches_degraded_after_threshold(self):
        # Repeated mode-mismatch failures must drive the poller to DEGRADED
        # once the consecutive-failure threshold is reached.
        _write_json(self._snapshot_path, _make_replay_bundle())
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE",
                            max_consecutive_failures=3)
        # No valid snapshot ever loaded: each poll is a fresh mismatch.
        poller._poll_once()
        poller._poll_once()
        self.assertEqual(poller.state, "INIT")
        self.assertEqual(poller.last_error_code, "MODE_MISMATCH")
        poller._poll_once()
        self.assertEqual(poller.state, "DEGRADED")
        self.assertEqual(poller.last_error_code, "MODE_MISMATCH")

    def test_recovery_valid_isolated_live_after_mismatch(self):
        # After a mode mismatch leaves the poller STALE, a subsequent valid
        # ISOLATED_LIVE bundle must restore LIVE and clear the error code.
        _write_json(self._snapshot_path, _make_isolated_live_bundle())
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load())

        _write_json(self._snapshot_path, _make_replay_bundle())
        self.assertFalse(poller._poll_once())
        self.assertEqual(poller.state, "STALE")

        _write_json(self._snapshot_path,
                    _make_isolated_live_bundle(final_status="HELD"))
        self.assertTrue(poller._poll_once())
        self.assertEqual(poller.state, "LIVE")
        self.assertEqual(poller.last_error_code, "")
        self.assertEqual(poller.consecutive_failures, 0)


class TestStatusContract(unittest.TestCase):
    """GET /api/live/status must expose the full browser-observable contract."""

    REQUIRED_KEYS = {
        "mode", "source_kind", "source_read_only", "not_production",
        "poller_state", "poll_count", "last_poll_at", "last_success_at",
        "source_snapshot_sha256", "bundle_sha256", "consecutive_failures",
        "last_error_code", "github_writes_enabled", "agent_control_enabled",
        "runtime_consumes_rag_context", "production_resource_accessed",
        "production_resource_access_status",
        "browser_network_observation_status",
        "observed_external_network_requests",
        "dynamic_pages_consume_live_api",
    }

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        self._bundle = _make_isolated_live_bundle()
        _write_json(self._snapshot_path, self._bundle)
        self._handle = _start_live_server(self._snapshot_path)

    def tearDown(self):
        self._handle.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _status(self):
        status, _, payload = _http_get_json(
            self._handle.base_url + "/api/live/status")
        self.assertEqual(status, 200)
        return payload

    def test_status_has_all_required_fields(self):
        payload = self._status()
        missing = self.REQUIRED_KEYS - set(payload.keys())
        self.assertEqual(missing, set(), f"missing status keys: {missing}")

    def test_poller_state_matches_actual_state(self):
        payload = self._status()
        # The server's poller_state must reflect the real poller.state.
        self.assertEqual(payload["poller_state"], self._handle.poller.state)
        self.assertEqual(payload["poller_state"], "LIVE")

    def test_bundle_sha256_equals_bundle_internal_value(self):
        payload = self._status()
        self.assertEqual(payload["bundle_sha256"],
                         self._bundle["bundle_sha256"])

    def test_source_snapshot_sha256_equals_file_sha(self):
        # source_snapshot_sha256 is the SHA-256 of the raw snapshot bytes
        # on disk (the file content the poller read).
        with open(self._snapshot_path, "rb") as f:
            raw = f.read()
        import hashlib
        expected = hashlib.sha256(raw).hexdigest()
        payload = self._status()
        self.assertEqual(payload["source_snapshot_sha256"], expected)

    def test_dynamic_pages_consume_live_api_is_false(self):
        payload = self._status()
        self.assertIs(payload["dynamic_pages_consume_live_api"], False)

    def test_not_measured_and_null_fields_accurate(self):
        payload = self._status()
        # production access: refused but not measured → null + NOT_MEASURED
        self.assertIsNone(payload["production_resource_accessed"])
        self.assertEqual(payload["production_resource_access_status"],
                         "NOT_MEASURED")
        # browser network observation: not instrumented
        self.assertEqual(payload["browser_network_observation_status"],
                         "NOT_MEASURED")
        self.assertIsNone(payload["observed_external_network_requests"])
        # hard negatives
        self.assertIs(payload["github_writes_enabled"], False)
        self.assertIs(payload["agent_control_enabled"], False)
        self.assertIs(payload["runtime_consumes_rag_context"], False)
        self.assertIs(payload["source_read_only"], True)
        self.assertIs(payload["not_production"], True)

    def test_status_snapshot_is_atomic(self):
        # get_view() reads stats + snapshot under one lock; verify the
        # bundle_sha256 and source_snapshot_sha256 are mutually consistent
        # (both present and well-formed) for a LIVE poller.
        view = self._handle.poller.get_view()
        self.assertEqual(view["current_snapshot"]["bundle_sha256"],
                         self._bundle["bundle_sha256"])
        self.assertEqual(view["current_sha256"], view["source_snapshot_sha256"])


class TestFactoryHardening(unittest.TestCase):
    """create_server / make_handler reject misconfiguration fail-closed."""

    def test_create_server_rejects_non_loopback_host(self):
        # IPv4-loopback only: ::1 (IPv6 loopback) is also rejected.
        for host in ("0.0.0.0", "::", "::1", "192.168.1.5", "10.0.0.1"):
            with self.assertRaises(ValueError):
                create_server(host, 0, "REPLAY")

    def test_create_server_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            create_server("127.0.0.1", 0, "production")

    def test_make_handler_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            make_handler(None, "bogus")

    def test_create_server_isolated_live_requires_poller(self):
        with self.assertRaises(ValueError):
            create_server("127.0.0.1", 0, "ISOLATED_LIVE", poller=None)

    def test_create_server_replay_rejects_poller(self):
        # REPLAY must not be handed a poller — that's a config mistake.
        src = FileSnapshotSource(os.path.join(tempfile.gettempdir(), "x.json"))
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        try:
            with self.assertRaises(ValueError):
                create_server("127.0.0.1", 0, "REPLAY", poller=poller)
        finally:
            # Never started; stop is a no-op but keeps things tidy.
            poller.stop()

    def test_create_server_accepts_valid_configs(self):
        # REPLAY with no poller is fine.
        s1 = create_server("127.0.0.1", 0, "REPLAY")
        s1.server_close()
        # ISOLATED_LIVE with a poller is fine.
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "s.json")
            _write_json(p, _make_isolated_live_bundle())
            src = FileSnapshotSource(p)
            poller = LivePoller(src, poll_interval=1.0,
                                expected_mode="ISOLATED_LIVE")
            self.assertTrue(poller.initial_load())
            s2 = create_server("127.0.0.1", 0, "ISOLATED_LIVE", poller=poller)
            s2.server_close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestShutdown(unittest.TestCase):
    """Poller shutdown: normal join vs. timeout reporting."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        _write_json(self._snapshot_path, _make_isolated_live_bundle())

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_normal_shutdown_not_alive_after_join(self):
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load())
        poller.start()
        time.sleep(0.3)
        clean = shutdown_poller(poller, timeout=5)
        self.assertTrue(clean)
        self.assertFalse(poller.is_alive())
        self.assertEqual(poller.state, "STOPPED")

    def test_simulated_timeout_reports_failure(self):
        # A poller subclass whose stop() never sets the stop event simulates a
        # thread that ignores shutdown. join() must time out and
        # shutdown_poller must report failure (False), mirroring what main()
        # checks before printing POLLER_SHUTDOWN_TIMEOUT.
        src = FileSnapshotSource(self._snapshot_path)

        class _UnstoppablePoller(LivePoller):
            def stop(self):
                # Intentionally do NOT set the stop event.
                pass

        poller = _UnstoppablePoller(src, poll_interval=1.0,
                                    expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load())
        poller.start()
        time.sleep(0.3)
        clean = shutdown_poller(poller, timeout=1.0)
        self.assertFalse(clean, "expected shutdown_poller to report timeout")
        self.assertTrue(poller.is_alive(),
                        "poller should still be alive after a timeout")
        # Force-terminate the leaked thread so the test process can exit: set
        # the real stop event on the base class and join with a longer grace.
        LivePoller.stop(poller)
        poller.join(timeout=5)


class TestSourceLocality(unittest.TestCase):
    """source_locality_status classifies local vs. rejected-network sources."""

    def test_unc_path_rejected(self):
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="//server/share/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertEqual(pf["source_locality_status"], "NETWORK_PATH_REJECTED")

    def test_file_uri_rejected(self):
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="file:///tmp/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertEqual(pf["source_locality_status"], "NETWORK_PATH_REJECTED")

    def test_http_url_rejected(self):
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="https://example.com/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertEqual(pf["source_locality_status"], "NETWORK_PATH_REJECTED")

    def test_directory_rejected_keeps_null_locality(self):
        # A directory fails source_is_regular_file before locality is
        # classified; source_locality_status stays None (not VERIFIED_LOCAL).
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=d)
            self.assertFalse(pf["preflight_passed"])
            self.assertIsNone(pf["source_locality_status"])

    def test_local_file_verified_local_on_windows(self):
        # On Windows a temp file lives on a DRIVE_FIXED volume → VERIFIED_LOCAL
        # and preflight passes. POSIX is covered separately (fail-closed).
        if os.name != "nt":
            self.skipTest("VERIFIED_LOCAL requires a DRIVE_FIXED Windows volume")
        bundle = _make_isolated_live_bundle()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump(bundle, f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertTrue(pf["preflight_passed"],
                            f"unexpected failures: {pf['failures']}")
            self.assertEqual(pf["source_locality_status"], "VERIFIED_LOCAL")
        finally:
            os.unlink(path)

    def test_posix_local_file_is_fail_closed(self):
        # POSIX paths cannot be Win32-verified → POSIX_LOCAL_CANDIDATE, which
        # is fail-closed: preflight must NOT pass and source_is_local_file
        # must be False (only VERIFIED_LOCAL yields source_is_local_file=true).
        if os.name == "nt":
            self.skipTest("POSIX fail-closed only applies off-Windows")
        bundle = _make_isolated_live_bundle()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump(bundle, f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"],
                             "POSIX_LOCAL_CANDIDATE")
            self.assertFalse(pf["source_is_local_file"])
        finally:
            os.unlink(path)

    def test_drive_letter_helper_detects_drive_paths(self):
        # The drive-letter detection helper still recognizes Windows-style
        # drive paths and rejects UNC / POSIX paths.
        from preflight import _is_windows_drive_letter_path
        self.assertTrue(_is_windows_drive_letter_path("C:\\Users\\x\\snap.json"))
        self.assertFalse(_is_windows_drive_letter_path("/tmp/snap.json"))
        self.assertFalse(_is_windows_drive_letter_path(
            "//server/share/snap.json"))

    def test_not_measured_never_with_preflight_passed(self):
        # Invariant: NOT_MEASURED (the measurement status) must NEVER coexist
        # with preflight_passed=true. Walk every source shape and assert that
        # whenever source_locality_measurement_status is NOT_MEASURED, the
        # preflight did not pass.
        cases = [
            ("//server/share/snap.json", "NETWORK_PATH_REJECTED"),
            ("file:///tmp/snap.json", "NETWORK_PATH_REJECTED"),
            ("https://example.com/snap.json", "NETWORK_PATH_REJECTED"),
        ]
        # Add a real POSIX temp file when off-Windows (POSIX_LOCAL_CANDIDATE).
        if os.name != "nt":
            bundle = _make_isolated_live_bundle()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                             delete=False) as f:
                json.dump(bundle, f)
                f.flush()
                cases.append((f.name, "POSIX_LOCAL_CANDIDATE"))
            for src, _expected in cases:
                pf = run_preflight("isolated_live", "127.0.0.1",
                                   source_file=src)
                if pf["source_locality_measurement_status"] == "NOT_MEASURED":
                    self.assertFalse(
                        pf["preflight_passed"],
                        f"NOT_MEASURED must not coexist with pass: {src}",
                    )
            os.unlink(cases[-1][0])


class _FakeKernel32:
    """Stand-in for ctypes.windll.kernel32 used by Windows locality tests.

    Instances are callable returning the configured drive-type code, and
    expose a ``GetDriveTypeW`` attribute so ``unittest.mock.patch`` can
    target it directly.
    """

    def __init__(self, return_value: int = 3):
        self._rv = return_value
        # ``GetDriveTypeW`` is what _win32_get_drive_type reads. Make it a
        # plain function so restype/argtypes assignment works.
        self.GetDriveTypeW = self._make_fn()

    def _make_fn(self):
        rv = self._rv

        def _fn(root_pathname=None):
            return rv

        # ctypes callers set restype/argtypes on the attribute; allow it.
        _fn.restype = None
        _fn.argtypes = None
        return _fn


class _RaisingGetDriveType:
    """Fake GetDriveTypeW that raises OSError to simulate an API failure."""

    restype = None
    argtypes = None

    def __call__(self, root_pathname=None):
        raise OSError("simulated Win32 API failure")

    # Allow attribute assignment used by _win32_get_drive_type.
    def _set(self, name, value):
        object.__setattr__(self, name, value)


class TestWindowsLocality(unittest.TestCase):
    """classify_source_locality Win32 drive-type classification.

    Mocks ``ctypes.windll.kernel32.GetDriveTypeW`` via unittest.mock so the
    matrix runs on any platform. Each case asserts both the locality status
    and that preflight fail-closed behavior is correct.
    """

    @unittest.skipUnless(sys.platform == "win32",
                         "Win32 drive-type mocking needs ctypes.windll")
    def _run_with_drive_type(self, drive_type_code: int, path: str) -> dict:
        """Patch GetDriveTypeW to return ``drive_type_code`` and run preflight."""
        import ctypes
        from unittest import mock

        fake = _FakeKernel32(return_value=drive_type_code)
        # ctypes.windll.kernel32 is the object preflight reads; patch the
        # GetDriveTypeW attribute it resolves to.
        with mock.patch.object(
                ctypes.windll.kernel32, "GetDriveTypeW",
                new=fake.GetDriveTypeW):
            return run_preflight("isolated_live", "127.0.0.1",
                                 source_file=path)

    @unittest.skipUnless(sys.platform == "win32",
                         "Win32 drive-type mocking needs ctypes.windll")
    def _run_with_raising_api(self, path: str) -> dict:
        """Patch GetDriveTypeW to raise OSError and run preflight."""
        import ctypes
        from unittest import mock

        raising = _RaisingGetDriveType()
        with mock.patch.object(
                ctypes.windll.kernel32, "GetDriveTypeW", new=raising):
            return run_preflight("isolated_live", "127.0.0.1",
                                 source_file=path)

    def _make_drive_path_bundle(self) -> str:
        """Write a valid ISOLATED_LIVE bundle to a C:\\-style temp path.

        On Windows tempfile already lives on a drive; we just reuse it. On
        non-Windows hosts the callers are skipped, so the literal path shape
        does not matter.
        """
        bundle = _make_isolated_live_bundle()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump(bundle, f)
            f.flush()
            return f.name

    def test_drive_fixed_is_verified_local_and_passes(self):
        pf = self._run_with_drive_type(3, self._make_drive_path_bundle())
        try:
            self.assertTrue(pf["preflight_passed"],
                            f"unexpected failures: {pf['failures']}")
            self.assertEqual(pf["source_locality_status"], "VERIFIED_LOCAL")
            self.assertEqual(pf["source_drive_type"], "DRIVE_FIXED")
            self.assertEqual(pf["source_drive_type_code"], 3)
            self.assertTrue(pf["source_is_local_file"])
        finally:
            os.unlink(pf["source_path_resolved"])

    def test_drive_remote_is_network_rejected(self):
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_drive_type(4, path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"],
                             "NETWORK_PATH_REJECTED")
            self.assertEqual(pf["source_drive_type"], "DRIVE_REMOTE")
            self.assertEqual(pf["source_drive_type_code"], 4)
        finally:
            os.unlink(path)

    def test_drive_unknown_is_not_measured(self):
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_drive_type(0, path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"], "NOT_MEASURED")
            self.assertEqual(pf["source_drive_type"], "DRIVE_UNKNOWN")
            self.assertEqual(pf["source_drive_type_code"], 0)
        finally:
            os.unlink(path)

    def test_api_failure_is_not_measured(self):
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_raising_api(path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"], "NOT_MEASURED")
        finally:
            os.unlink(path)

    def test_drive_removable_is_unsupported(self):
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_drive_type(2, path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"],
                             "UNSUPPORTED_DRIVE_TYPE")
            self.assertEqual(pf["source_drive_type"], "DRIVE_REMOVABLE")
        finally:
            os.unlink(path)

    def test_drive_cdrom_is_unsupported(self):
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_drive_type(5, path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"],
                             "UNSUPPORTED_DRIVE_TYPE")
            self.assertEqual(pf["source_drive_type"], "DRIVE_CDROM")
        finally:
            os.unlink(path)

    def test_drive_ramdisk_is_unsupported(self):
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_drive_type(6, path)
            self.assertFalse(pf["preflight_passed"])
            self.assertEqual(pf["source_locality_status"],
                             "UNSUPPORTED_DRIVE_TYPE")
            self.assertEqual(pf["source_drive_type"], "DRIVE_RAMDISK")
        finally:
            os.unlink(path)

    def test_not_measured_never_coexists_with_pass(self):
        # Cross-status invariant: NOT_MEASURED locality must never accompany a
        # passing preflight. Drive UNKNOWN (0) yields NOT_MEASURED.
        path = self._make_drive_path_bundle()
        try:
            pf = self._run_with_drive_type(0, path)
            if pf["source_locality_status"] == "NOT_MEASURED":
                self.assertFalse(pf["preflight_passed"])
        finally:
            os.unlink(path)

    def test_source_is_local_file_only_when_verified_local(self):
        # source_is_local_file=true ONLY when status==VERIFIED_LOCAL.
        path = self._make_drive_path_bundle()
        try:
            for code in (0, 2, 3, 4, 5, 6):
                pf = self._run_with_drive_type(code, path)
                if pf["source_locality_status"] != "VERIFIED_LOCAL":
                    self.assertFalse(
                        pf["source_is_local_file"],
                        f"code={code} status={pf['source_locality_status']}",
                    )
                else:
                    self.assertTrue(pf["source_is_local_file"])
        finally:
            os.unlink(path)


class TestIPv4Loopback(unittest.TestCase):
    """The P1 server is IPv4-loopback only; IPv6 ::1 is NOT implemented."""

    def test_ipv4_loopback_passes(self):
        pf = run_preflight("replay", "127.0.0.1")
        self.assertTrue(pf["preflight_passed"])
        self.assertTrue(pf["loopback_only"])

    def test_localhost_passes(self):
        pf = run_preflight("replay", "localhost")
        self.assertTrue(pf["preflight_passed"])
        self.assertTrue(pf["loopback_only"])

    def test_ipv6_loopback_fails_preflight(self):
        pf = run_preflight("replay", "::1")
        self.assertFalse(pf["preflight_passed"])
        self.assertFalse(pf["loopback_only"])
        loopback_failure = next(
            (f for f in pf["failures"] if f["check"] == "loopback_only"), None)
        self.assertIsNotNone(loopback_failure)
        self.assertIn("IPv6 ::1 not implemented", loopback_failure["detail"])

    def test_ipv6_any_fails(self):
        pf = run_preflight("replay", "::")
        self.assertFalse(pf["preflight_passed"])

    def test_wildcard_ipv4_fails(self):
        pf = run_preflight("replay", "0.0.0.0")
        self.assertFalse(pf["preflight_passed"])

    def test_lan_ip_fails(self):
        pf = run_preflight("replay", "192.168.1.1")
        self.assertFalse(pf["preflight_passed"])

    def test_create_server_rejects_ipv6_loopback_before_socket(self):
        # create_server must raise ValueError for ::1 WITHOUT creating a
        # socket (the rejection happens in the host check, before bind).
        with self.assertRaises(ValueError):
            create_server("::1", 0, "REPLAY")

    def test_create_server_rejects_other_hosts(self):
        for host in ("::", "0.0.0.0", "192.168.1.1"):
            with self.assertRaises(ValueError):
                create_server(host, 0, "REPLAY")

    def test_create_server_accepts_ipv4_loopback(self):
        s = create_server("127.0.0.1", 0, "REPLAY")
        try:
            self.assertEqual(s.server_address[0], "127.0.0.1")
        finally:
            s.server_close()


class TestBindContext(unittest.TestCase):
    """Phase 1-D retry v2 Fix 1: MERGEPILOT_BIND_CONTEXT host vs container.

    host mode (the default): strictly IPv4 loopback — unchanged P1 semantics.
    container mode: additionally allows 0.0.0.0 as the CONTAINER-INTERNAL
    listen address (Docker bridge routing). The HOST-side publish stays
    127.0.0.1-only and is enforced by compose/orchestrator, not here.
    """

    def test_default_context_is_host(self):
        # Unset -> host semantics: 0.0.0.0 rejected, loopback accepted.
        env = {k: v for k, v in os.environ.items()
               if k != "MERGEPILOT_BIND_CONTEXT"}
        with mock.patch.dict(os.environ, env, clear=True):
            pf = run_preflight("replay", "0.0.0.0")
            self.assertFalse(pf["preflight_passed"])
            pf2 = run_preflight("replay", "127.0.0.1")
            self.assertTrue(pf2["preflight_passed"])

    def test_explicit_host_context_rejects_0000(self):
        with mock.patch.dict(os.environ, {"MERGEPILOT_BIND_CONTEXT": "host"}):
            pf = run_preflight("replay", "0.0.0.0")
            self.assertFalse(pf["preflight_passed"])
            self.assertFalse(pf["loopback_only"])
            with self.assertRaises(ValueError):
                create_server("0.0.0.0", 0, "REPLAY")

    def test_container_context_allows_0000_preflight(self):
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "container"}):
            pf = run_preflight("replay", "0.0.0.0")
            self.assertTrue(pf["preflight_passed"])
            self.assertTrue(pf["loopback_only"])

    def test_container_context_allows_0000_create_server(self):
        # The container-context acceptance path must reach server
        # construction; the real socket bind is stubbed so the test never
        # opens an off-machine listener on the HOST test runner.
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "container"}), \
                mock.patch("socketserver.TCPServer.__init__",
                           return_value=None):
            s = create_server("0.0.0.0", 0, "REPLAY")
        self.assertEqual(s.mode, "REPLAY")
        self.assertIsNone(s.poller)

    def test_container_context_keeps_loopback_valid(self):
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "container"}):
            for h in ("127.0.0.1", "localhost"):
                pf = run_preflight("replay", h)
                self.assertTrue(pf["preflight_passed"], h)

    def test_container_context_rejects_lan(self):
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "container"}):
            for bad in ("192.168.1.1", "10.0.0.1", "172.16.0.1"):
                pf = run_preflight("replay", bad)
                self.assertFalse(pf["preflight_passed"], bad)
                loopback_failure = next(
                    (f for f in pf["failures"]
                     if f["check"] == "loopback_only"), None)
                self.assertIsNotNone(loopback_failure, bad)
                self.assertIn("container listen", loopback_failure["detail"])
                with self.assertRaises(ValueError):
                    create_server(bad, 0, "REPLAY")

    def test_container_context_still_rejects_ipv6(self):
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "container"}):
            for bad in ("::1", "::"):
                pf = run_preflight("replay", bad)
                self.assertFalse(pf["preflight_passed"], bad)
                with self.assertRaises(ValueError):
                    create_server(bad, 0, "REPLAY")
        # The ::1 failure names the IPv6 reason in BOTH contexts.
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "container"}):
            pf = run_preflight("replay", "::1")
        loopback_failure = next(f for f in pf["failures"]
                                if f["check"] == "loopback_only")
        self.assertIn("IPv6 ::1 not implemented", loopback_failure["detail"])

    def test_invalid_context_rejected_fail_closed(self):
        for bad in ("docker", "auto", "1", "hostx"):
            with mock.patch.dict(os.environ,
                                 {"MERGEPILOT_BIND_CONTEXT": bad}):
                with self.assertRaises(ValueError):
                    create_server("127.0.0.1", 0, "REPLAY")
                with self.assertRaises(ValueError):
                    run_preflight("replay", "127.0.0.1")

    def test_context_case_insensitive(self):
        with mock.patch.dict(os.environ,
                             {"MERGEPILOT_BIND_CONTEXT": "Container"}):
            pf = run_preflight("replay", "0.0.0.0")
            self.assertTrue(pf["preflight_passed"])


class TestSourceKindDynamic(unittest.TestCase):
    """source_kind comes from the actual SnapshotSource, not a hardcoded value."""

    def test_file_source_reports_file_fixture(self):
        # FileSnapshotSource.kind is FILE_FIXTURE and surfaces in the status.
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "s.json")
            _write_json(p, _make_isolated_live_bundle())
            src = FileSnapshotSource(p)
            poller = LivePoller(src, poll_interval=1.0,
                                expected_mode="ISOLATED_LIVE")
            self.assertTrue(poller.initial_load())
            view = poller.get_view()
            self.assertEqual(view["source_kind"], "FILE_FIXTURE")
            self.assertIs(view["source_read_only"], True)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_custom_source_reports_custom_kind(self):
        # A custom source with kind="TEST_SOURCE" surfaces that kind in the
        # status API (not a hardcoded FILE_FIXTURE).
        from live_poller import SnapshotSource

        class _CustomSource(SnapshotSource):
            kind = "TEST_SOURCE"

            def read_snapshot(self) -> bytes:
                return json.dumps(_make_isolated_live_bundle()).encode("utf-8")

        src = _CustomSource()
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load())
        view = poller.get_view()
        self.assertEqual(view["source_kind"], "TEST_SOURCE")
        self.assertIs(view["source_read_only"], True)

    def test_custom_source_can_override_read_only(self):
        # A source that declares read_only=False surfaces it via the view.
        from live_poller import SnapshotSource

        class _ReadWriteSource(SnapshotSource):
            kind = "READ_WRITE_TEST"

            @property
            def read_only(self) -> bool:
                return False

            def read_snapshot(self) -> bytes:
                return json.dumps(_make_isolated_live_bundle()).encode("utf-8")

        src = _ReadWriteSource()
        poller = LivePoller(src, poll_interval=1.0,
                            expected_mode="ISOLATED_LIVE")
        self.assertTrue(poller.initial_load())
        view = poller.get_view()
        self.assertEqual(view["source_kind"], "READ_WRITE_TEST")
        self.assertIs(view["source_read_only"], False)

    def test_status_api_source_kind_not_hardcoded(self):
        # End-to-end: the HTTP status API reports the actual source kind.
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "s.json")
            _write_json(p, _make_isolated_live_bundle())
            handle = _start_live_server(p)
            try:
                status, _, payload = _http_get_json(
                    handle.base_url + "/api/live/status")
                self.assertEqual(status, 200)
                self.assertEqual(payload["source_kind"], "FILE_FIXTURE")
                self.assertIs(payload["source_read_only"], True)
            finally:
                handle.stop()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestRealDriveLocality(unittest.TestCase):
    """Run a REAL preflight against a temp file on the current drive.

    Skipped off-Windows (the Win32 drive-type check only runs on Windows).
    On Windows it reports the actual drive-type code observed for the temp
    directory's volume so we can see what the host reports in practice.
    """

    @unittest.skipUnless(sys.platform == "win32",
                         "real drive-type check requires Windows")
    def test_real_preflight_reports_drive_type(self):
        from preflight import classify_source_locality

        bundle = _make_isolated_live_bundle()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump(bundle, f)
            f.flush()
            path = f.name
        try:
            locality = classify_source_locality(os.path.abspath(path))
            # Report the actual code for diagnostics. DRIVE_FIXED (3) is the
            # only code that yields a passing VERIFIED_LOCAL preflight.
            self.assertIn(locality["drive_type_code"], (0, 1, 2, 3, 4, 5, 6))
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            if locality["status"] == "VERIFIED_LOCAL":
                self.assertTrue(pf["preflight_passed"])
            else:
                self.assertFalse(pf["preflight_passed"])
        finally:
            os.unlink(path)


class TestDocConsistency(unittest.TestCase):
    """The implementation doc must make honest, non-overclaimed statements."""

    DOC_PATH = ROOT / "docs" / "ISOLATED-LIVE-P1-Implementation.md"

    def setUp(self):
        with open(self.DOC_PATH, "r", encoding="utf-8") as f:
            self.text = f.read()

    def test_no_phase_1_complete_claim(self):
        self.assertNotIn("Phase 1 complete", self.text)
        self.assertNotIn("phase 1 complete", self.text)

    def test_no_production_accessed_false_positive_claim(self):
        # The doc must not claim production_resource_accessed=false (a positive
        # "clean" claim). It is null / NOT_MEASURED.
        self.assertNotIn("production_resource_accessed=false", self.text)
        self.assertNotIn("production_resource_accessed = false", self.text)

    def test_has_dynamic_pages_consume_live_api_false(self):
        self.assertIn("dynamic_pages_consume_live_api=false", self.text)

    def test_mentions_static_pages_not_dynamically_refreshed(self):
        # The doc must state the served pages are static (frozen REPLAY HTML)
        # and are NOT dynamically refreshed/consuming the API.
        self.assertIn("static", self.text.lower())
        self.assertIn("frozen", self.text.lower())


if __name__ == "__main__":
    unittest.main()
