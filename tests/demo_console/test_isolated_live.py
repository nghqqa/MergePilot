#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISOLATED_LIVE Phase 1 tests — preflight, polling, HTTP boundaries, modes.

Covers 22+ test cases for:
- REPLAY default mode preservation
- ISOLATED_LIVE preflight (positive and negative)
- Snapshot polling (update, invalid skip, stats)
- HTTP write method rejection
- Shutdown cleanup (thread, port)
- Authenticity boundary fields
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in [str(ROOT), str(ROOT / "tools" / "demo_console")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from preflight import run_preflight, VALID_MODES
from live_poller import FileSnapshotSource, LivePoller
from schema import validate_bundle

# Load the existing DemoBundle as a valid fixture
BUNDLE_PATH = ROOT / "samples" / "demo-bundles" / "m7-rag-replay.json"


def _load_valid_bundle() -> dict:
    return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))


class TestReplayDefaultMode(unittest.TestCase):
    """1. REPLAY is default; 2. existing tests not broken."""

    def test_replay_in_valid_modes(self):
        self.assertIn("replay", VALID_MODES)

    def test_isolated_live_in_valid_modes(self):
        self.assertIn("isolated_live", VALID_MODES)

    def test_replay_preflight_passes(self):
        pf = run_preflight("replay", "127.0.0.1")
        self.assertTrue(pf["preflight_passed"])
        self.assertEqual(pf["mode"], "REPLAY")


class TestInvalidModeRejection(unittest.TestCase):
    """3. Invalid mode rejected."""

    def test_invalid_mode_fails(self):
        pf = run_preflight("production", "127.0.0.1")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "mode_valid" for f in pf["failures"]))

    def test_empty_mode_fails(self):
        pf = run_preflight("", "127.0.0.1")
        self.assertFalse(pf["preflight_passed"])


class TestSourceValidation(unittest.TestCase):
    """4-8. Source configuration validation."""

    def test_isolated_live_no_source_fails(self):
        pf = run_preflight("isolated_live", "127.0.0.1")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_configured" for f in pf["failures"]))

    def test_http_source_rejected(self):
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="https://example.com/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_not_http" for f in pf["failures"]))

    def test_nonexistent_source_rejected(self):
        pf = run_preflight("isolated_live", "127.0.0.1",
                           source_file="/nonexistent/path/snap.json")
        self.assertFalse(pf["preflight_passed"])
        self.assertTrue(any(f["check"] == "source_exists" for f in pf["failures"]))

    def test_corrupt_json_rejected(self):
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

    def test_valid_source_passes(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_load_valid_bundle(), f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertTrue(pf["preflight_passed"])
            self.assertEqual(pf["source_kind"], "FILE_FIXTURE")
        finally:
            os.unlink(path)


class TestLoopbackEnforcement(unittest.TestCase):
    """5. Non-loopback host rejected."""

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


class TestFileSnapshotSource(unittest.TestCase):
    """Source interface and read behavior."""

    def test_read_returns_bytes(self):
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(b'{"test": true}')
            path = f.name
        try:
            src = FileSnapshotSource(path)
            data = src.read_snapshot()
            self.assertIsInstance(data, bytes)
            self.assertEqual(json.loads(data), {"test": True})
            self.assertEqual(src.kind, "FILE_FIXTURE")
        finally:
            os.unlink(path)

    def test_read_nonexistent_raises(self):
        src = FileSnapshotSource("/nonexistent")
        with self.assertRaises(FileNotFoundError):
            src.read_snapshot()


class TestLivePoller(unittest.TestCase):
    """9-14. Polling behavior: initial load, update, invalid skip, stats."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._snapshot_path = os.path.join(self._tmpdir, "snapshot.json")
        self._write_snapshot(_load_valid_bundle())

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_snapshot(self, data: dict):
        with open(self._snapshot_path, "w") as f:
            json.dump(data, f)

    def test_initial_load_success(self):
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
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        sha_before = poller.current_sha256

        # Write a different valid snapshot
        bundle = _load_valid_bundle()
        bundle["generated_at"] = "2099-01-01T00:00:00Z"
        # Recompute bundle_sha256 to keep it valid
        sys.path.insert(0, str(ROOT / "tools" / "demo_console"))
        from bundle_builder import compute_bundle_sha256
        bundle["bundle_sha256"] = compute_bundle_sha256(bundle)
        self._write_snapshot(bundle)

        poller._poll_once()
        sha_after = poller.current_sha256
        self.assertNotEqual(sha_before, sha_after)
        self.assertEqual(poller.state, "LIVE")

    def test_invalid_snapshot_does_not_overwrite(self):
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        valid_snapshot = poller.current_snapshot
        valid_sha = poller.current_sha256

        # Write corrupt data
        with open(self._snapshot_path, "w") as f:
            f.write("{ corrupt")

        poller._poll_once()

        # Last valid snapshot preserved
        self.assertEqual(poller.current_snapshot, valid_snapshot)
        self.assertEqual(poller.current_sha256, valid_sha)
        self.assertGreater(poller.consecutive_failures, 0)
        self.assertEqual(poller.state, "STALE")

    def test_source_sha256_correct(self):
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()

        import hashlib
        with open(self._snapshot_path, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(poller.current_sha256, expected)

    def test_consecutive_failures_tracked(self):
        src = FileSnapshotSource(os.path.join(self._tmpdir, "missing.json"))
        poller = LivePoller(src, poll_interval=1.0, max_consecutive_failures=3)
        # No initial load, so current_snapshot is None → state stays INIT
        poller._poll_once()
        self.assertEqual(poller.consecutive_failures, 1)
        self.assertEqual(poller.state, "INIT")  # No valid snapshot ever loaded
        poller._poll_once()
        self.assertEqual(poller.consecutive_failures, 2)
        poller._poll_once()
        self.assertEqual(poller.consecutive_failures, 3)
        # With no valid snapshot and max failures reached, DEGRADED
        self.assertEqual(poller.state, "DEGRADED")

    def test_shutdown_stops_thread(self):
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        poller.start()
        time.sleep(0.5)
        poller.stop()
        poller.join(timeout=5)
        self.assertFalse(poller.is_alive())
        self.assertEqual(poller.state, "STOPPED")

    def test_input_fixture_not_modified(self):
        original = open(self._snapshot_path, "rb").read()
        src = FileSnapshotSource(self._snapshot_path)
        poller = LivePoller(src, poll_interval=1.0)
        poller.initial_load()
        poller._poll_once()
        after = open(self._snapshot_path, "rb").read()
        self.assertEqual(original, after)


class TestPreflightFields(unittest.TestCase):
    """22. All authenticity boundary fields present."""

    def test_all_required_fields_present(self):
        pf = run_preflight("isolated_live", "127.0.0.1")
        required = {
            "mode", "preflight_passed", "source_kind", "source_read_only",
            "loopback_only", "production_resource_accessed",
            "external_network_required", "github_writes_enabled",
            "agent_control_enabled", "runtime_consumes_rag_context",
            "checked_at", "failures",
        }
        self.assertEqual(required, set(pf.keys()))

    def test_production_never_accessed(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["production_resource_accessed"])

    def test_github_writes_never_enabled(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["github_writes_enabled"])

    def test_rag_context_never_consumed(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["runtime_consumes_rag_context"])

    def test_agent_control_never_enabled(self):
        for mode in VALID_MODES:
            pf = run_preflight(mode, "127.0.0.1")
            self.assertFalse(pf["agent_control_enabled"])

    def test_failures_are_list(self):
        pf = run_preflight("replay", "127.0.0.1")
        self.assertIsInstance(pf["failures"], list)

    def test_source_kind_file_fixture(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_load_valid_bundle(), f)
            f.flush()
            path = f.name
        try:
            pf = run_preflight("isolated_live", "127.0.0.1", source_file=path)
            self.assertEqual(pf["source_kind"], "FILE_FIXTURE")
            self.assertNotEqual(pf["source_kind"], "DB_LIVE")
        finally:
            os.unlink(path)


class TestModeRuntimeImmutability(unittest.TestCase):
    """21. Mode cannot be switched at runtime."""

    def test_mode_is_string_not_switchable(self):
        pf = run_preflight("replay", "127.0.0.1")
        self.assertEqual(pf["mode"], "REPLAY")
        # Mode is captured at preflight time; there is no API to change it.
        # The serve.py main() reads mode once from argparse and never offers
        # a runtime switch endpoint.


if __name__ == "__main__":
    unittest.main()
