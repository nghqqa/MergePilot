#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live snapshot poller for ISOLATED_LIVE mode.

Reads snapshots from a configured source at fixed intervals,
validates them against DemoBundle schema, and atomically replaces
the current snapshot.

Fail-closed: invalid snapshots never overwrite the last valid one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

# Add tools/demo_console to sys.path for schema import
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from schema import validate_bundle
from integrity import verify_bundle_integrity


class SnapshotSource:
    """Abstract interface for read-only snapshot sources."""

    def read_snapshot(self) -> bytes:
        """Return raw snapshot bytes. Raise on error."""
        raise NotImplementedError

    @property
    def kind(self) -> str:
        raise NotImplementedError


class FileSnapshotSource(SnapshotSource):
    """Read snapshots from a JSON file on disk."""

    def __init__(self, path: str):
        self._path = path

    def read_snapshot(self) -> bytes:
        with open(self._path, "rb") as f:
            return f.read()

    @property
    def kind(self) -> str:
        return "FILE_FIXTURE"


class LivePoller(threading.Thread):
    """Background thread that polls a snapshot source at fixed intervals.

    - Validates each snapshot against DemoBundle schema before replacing.
    - Invalid snapshots do NOT overwrite the last valid one.
    - Tracks poll statistics for observability.
    - Daemon thread: does not block process exit.
    """

    def __init__(self, source: SnapshotSource, poll_interval: float = 2.0,
                 max_consecutive_failures: int = 10):
        super().__init__(daemon=True)
        self._source = source
        self._poll_interval = max(1.0, poll_interval)
        self._max_failures = max_consecutive_failures

        self._lock = threading.Lock()
        self._current_snapshot: dict | None = None
        self._current_sha256: str = ""
        self._state = "INIT"  # INIT, LIVE, STALE, DEGRADED, STOPPED
        self._stop_event = threading.Event()

        # Stats
        self.poll_count = 0
        self.last_poll_at: str = ""
        self.last_success_at: str = ""
        self.consecutive_failures = 0
        self.last_error_code: str = ""

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def current_snapshot(self) -> dict | None:
        with self._lock:
            return self._current_snapshot

    @property
    def current_sha256(self) -> str:
        with self._lock:
            return self._current_sha256

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "poll_count": self.poll_count,
                "last_poll_at": self.last_poll_at,
                "last_success_at": self.last_success_at,
                "source_snapshot_sha256": self._current_sha256,
                "consecutive_failures": self.consecutive_failures,
                "last_error_code": self.last_error_code,
                "state": self._state,
            }

    def initial_load(self) -> bool:
        """Attempt first snapshot load. Returns True on success."""
        return self._poll_once()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            self._poll_once()
            self._stop_event.wait(self._poll_interval)

        with self._lock:
            self._state = "STOPPED"

    def _poll_once(self) -> bool:
        self.poll_count += 1
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.last_poll_at = now

        try:
            raw = self._source.read_snapshot()
            data = json.loads(raw.decode("utf-8"))

            # Validate against DemoBundle schema
            errors = validate_bundle(data)
            if errors:
                raise ValueError(f"schema validation failed: {errors[:3]}")

            # Verify bundle integrity (bundle_sha256 matches recomputed value)
            integrity_errors = verify_bundle_integrity(data)
            if integrity_errors:
                raise ValueError(f"integrity check failed: {integrity_errors[:3]}")

            sha = hashlib.sha256(raw).hexdigest()

            with self._lock:
                self._current_snapshot = data
                self._current_sha256 = sha
                self._state = "LIVE"
                self.consecutive_failures = 0
                self.last_error_code = ""
                self.last_success_at = now

            return True

        except Exception as e:
            with self._lock:
                self.consecutive_failures += 1
                self.last_error_code = type(e).__name__

                if self.consecutive_failures >= self._max_failures:
                    self._state = "DEGRADED"
                elif self._current_snapshot is None:
                    self._state = "INIT"
                else:
                    self._state = "STALE"

            return False
