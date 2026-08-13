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

from schema import validate_bundle, VALID_DEMO_MODES
from integrity import verify_bundle_integrity


class _ModeMismatchError(Exception):
    """Raised when a bundle's demo_mode does not match the poller's expected mode.

    Carrying a dedicated exception type lets ``_poll_once`` translate it into
    the stable, machine-readable ``MODE_MISMATCH`` error code (rather than the
    generic ``ValueError`` used for schema/integrity failures).
    """

    def __init__(self, actual, expected):
        self.actual = actual
        self.expected = expected
        super().__init__(
            f"demo_mode mismatch: bundle reports {actual!r} but poller "
            f"expected {expected!r}"
        )


class SnapshotSource:
    """Abstract interface for read-only snapshot sources."""

    def read_snapshot(self) -> bytes:
        """Return raw snapshot bytes. Raise on error."""
        raise NotImplementedError

    @property
    def kind(self) -> str:
        raise NotImplementedError

    @property
    def read_only(self) -> bool:
        """Whether this source is read-only.

        Defaults to True. A future read/write source may override this to
        declare False; the status API surfaces it so consumers can tell a
        read-only fixture apart from a live (potentially mutating) source
        without assuming.
        """
        return True


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
                 max_consecutive_failures: int = 10,
                 expected_mode: str = "ISOLATED_LIVE"):
        super().__init__(daemon=True)
        self._source = source
        self._poll_interval = max(1.0, poll_interval)
        self._max_failures = max_consecutive_failures

        # Mode isolation: the poller only accepts bundles whose demo_mode
        # matches expected_mode. This prevents an ISOLATED_LIVE poller from
        # ever adopting a REPLAY bundle (and vice versa). The value is
        # validated up front so a misconfigured poller fails fast.
        if expected_mode not in VALID_DEMO_MODES:
            raise ValueError(
                f"expected_mode must be one of {sorted(VALID_DEMO_MODES)}, "
                f"got {expected_mode!r}"
            )
        self._expected_mode = expected_mode

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

    def get_view(self) -> dict:
        """Return an atomic, single-locked snapshot of all poller state.

        Combines the stats with the current snapshot dict and the current
        source SHA-256 under one lock acquisition, so a status reader sees a
        consistent point-in-time view (no torn read between stats and the
        snapshot being replaced by another thread).

        ``source_kind`` and ``source_read_only`` are read from the actual
        ``SnapshotSource`` (never hardcoded), so future source types report
        their own kind and read-only status.
        """
        with self._lock:
            return {
                "poll_count": self.poll_count,
                "last_poll_at": self.last_poll_at,
                "last_success_at": self.last_success_at,
                "source_snapshot_sha256": self._current_sha256,
                "consecutive_failures": self.consecutive_failures,
                "last_error_code": self.last_error_code,
                "state": self._state,
                "expected_mode": self._expected_mode,
                "current_snapshot": self._current_snapshot,
                "current_sha256": self._current_sha256,
                # Source identity is sourced from the actual SnapshotSource
                # instance (self._source.kind / self._source.read_only), not a
                # hardcoded constant, so custom sources report truthfully.
                "source_kind": self._source.kind,
                "source_read_only": getattr(self._source, "read_only", True),
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

            # Mode isolation: detect a demo_mode mismatch BEFORE generic
            # schema validation so the recorded error code is the specific,
            # machine-readable "MODE_MISMATCH" rather than a generic
            # ValueError. A poller configured for ISOLATED_LIVE must never
            # adopt a REPLAY bundle.
            if data.get("demo_mode") != self._expected_mode:
                raise _ModeMismatchError(
                    data.get("demo_mode"), self._expected_mode)

            # Validate against DemoBundle schema, enforcing expected_mode.
            errors = validate_bundle(data, expected_mode=self._expected_mode)
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

        except _ModeMismatchError:
            with self._lock:
                self.consecutive_failures += 1
                # Distinct, stable error code for mode-mismatch failures so
                # consumers can tell a mode isolation rejection apart from
                # a generic schema/integrity failure.
                self.last_error_code = "MODE_MISMATCH"

                if self.consecutive_failures >= self._max_failures:
                    self._state = "DEGRADED"
                elif self._current_snapshot is None:
                    self._state = "INIT"
                else:
                    self._state = "STALE"

            return False

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
