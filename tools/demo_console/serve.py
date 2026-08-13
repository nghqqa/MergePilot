#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo Console local HTTP server (read-only).

Serves pre-rendered static HTML on localhost. No write operations,
no external network. Supports REPLAY (default) and ISOLATED_LIVE modes.

Usage:
    # REPLAY mode (default):
    python tools/demo_console/serve.py [--port 8080]

    # ISOLATED_LIVE mode:
    python tools/demo_console/serve.py \
        --mode isolated_live \
        --source-file PATH \
        [--poll-interval 2]
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from preflight import run_preflight, VALID_MODES
from live_poller import FileSnapshotSource, LivePoller


# Modes as used by the handler layer (uppercase, matching schema demo_mode).
_MODE_REPLAY = "REPLAY"
_MODE_LIVE = "ISOLATED_LIVE"


def _send_json(handler, status: int, payload: dict) -> None:
    """Serialize ``payload`` as JSON and send it as an HTTP response.

    Always sets ``Content-Type: application/json`` and
    ``Cache-Control: no-store`` so API responses are never cached or
    misinterpreted as HTML.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_json_error(handler, status: int, error: str) -> None:
    """Send a uniform ``{"error": ...}`` JSON error response."""
    _send_json(handler, status, {"error": error})


def _send_status(poller: LivePoller) -> dict:
    """Build the full browser-observable ISOLATED_LIVE status contract.

    Uses ``poller.get_view()`` so every field is read in a single locked
    snapshot (no torn read between stats and the live bundle). Every field
    is honest about what the read-only console actually does:

    - No writes, no control, no RAG consumption, no production access.
    - ``production_resource_accessed`` is ``null`` (not false) and its
      companion ``*_status`` is ``NOT_MEASURED``: the console refuses
      production access but does not actively measure it.
    - Browser network observation is ``NOT_MEASURED``: the console does not
      instrument outbound browser traffic; ``observed_external_network_requests``
      is therefore ``null``.
    - ``dynamic_pages_consume_live_api`` is ``false``: the served pages are
      static frozen REPLAY HTML, not a SPA that polls the live API.
    """
    view = poller.get_view()
    snapshot = view.get("current_snapshot") or {}

    return {
        # Identity / source
        "mode": _MODE_LIVE,
        "source_kind": "FILE_FIXTURE",
        "source_read_only": True,
        "not_production": True,
        # Poller state (atomic snapshot)
        "poller_state": view["state"],
        "poll_count": view["poll_count"],
        "last_poll_at": view["last_poll_at"],
        "last_success_at": view["last_success_at"],
        "source_snapshot_sha256": view["source_snapshot_sha256"],
        "bundle_sha256": snapshot.get("bundle_sha256"),
        "consecutive_failures": view["consecutive_failures"],
        "last_error_code": view["last_error_code"],
        # Hard negatives: the console performs none of these.
        "github_writes_enabled": False,
        "agent_control_enabled": False,
        "runtime_consumes_rag_context": False,
        # Production access: refused, not measured. null + NOT_MEASURED so
        # the absence of measurement is explicit, never mistaken for clean.
        "production_resource_accessed": None,
        "production_resource_access_status": "NOT_MEASURED",
        # Browser-side network observation: not instrumented by the console.
        "browser_network_observation_status": "NOT_MEASURED",
        "observed_external_network_requests": None,
        # The served pages are static frozen REPLAY HTML; they do not
        # dynamically consume the live API.
        "dynamic_pages_consume_live_api": False,
    }


class ReadOnlyHandler(http.server.SimpleHTTPRequestHandler):
    """Read-only HTTP handler — blocks all write methods."""

    def do_PUT(self):
        self.send_error(405, "Method Not Allowed")

    def do_POST(self):
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self):
        self.send_error(405, "Method Not Allowed")

    def do_PATCH(self):
        self.send_error(405, "Method Not Allowed")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        # Suppress default logging to stderr unless verbose
        pass


class LiveApiHandler(ReadOnlyHandler):
    """HTTP handler for ISOLATED_LIVE mode.

    Adds two live endpoints on top of read-only static file serving:

      GET /api/live/snapshot  → current valid bundle JSON (503 if none)
      GET /api/live/status    → structured poller status JSON

    All other GET paths fall through to SimpleHTTPRequestHandler (static
    files). Write methods remain blocked with 405.
    """

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/live/snapshot":
            self._handle_snapshot()
        elif path == "/api/live/status":
            self._handle_status()
        elif path.startswith("/api/live/"):
            # Unknown live API endpoint.
            _send_json_error(self, 404, f"unknown live API endpoint: {path}")
        else:
            # Delegate everything else to static file serving.
            super().do_GET()

    def _handle_snapshot(self):
        poller = getattr(self.server, "poller", None)
        snapshot = poller.current_snapshot if poller is not None else None
        if snapshot is None:
            _send_json(self, 503, {
                "error": "no valid snapshot available",
                "state": poller.state if poller is not None else "UNAVAILABLE",
            })
            return
        # Serve the current valid bundle. Cache-Control: no-store is set by
        # _send_json so a stale snapshot is never cached by a client.
        _send_json(self, 200, snapshot)

    def _handle_status(self):
        poller = getattr(self.server, "poller", None)
        if poller is None:
            _send_json(self, 503, {"error": "live poller not configured"})
            return
        _send_json(self, 200, _send_status(poller))


class ReplayApiHandler(ReadOnlyHandler):
    """HTTP handler for REPLAY mode.

    REPLAY serves only static files. Any ``/api/live/*`` request is a
    client error in REPLAY mode (live endpoints do not exist) and returns
    a 404 JSON error. All other GET paths delegate to static serving.
    """

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/live/"):
            _send_json_error(self, 404, "live API not available in REPLAY mode")
        else:
            super().do_GET()


def make_handler(poller: LivePoller | None, mode: str):
    """Return an HTTPRequestHandler class for the given mode.

    - REPLAY         → ReplayApiHandler (static + 404 for /api/live/*)
    - ISOLATED_LIVE  → LiveApiHandler   (snapshot + status + static fallback)

    Unknown modes raise ValueError rather than silently defaulting to REPLAY:
    a typo'd mode must surface as a hard failure, not silently degrade to a
    different handler. The poller is attached to the server instance (see
    ``create_server``) and read by the handler via ``self.server.poller``.
    """
    mode_u = mode.upper() if isinstance(mode, str) else mode
    if mode_u == _MODE_LIVE:
        return LiveApiHandler
    if mode_u == _MODE_REPLAY:
        return ReplayApiHandler
    raise ValueError(
        f"unknown mode {mode!r}; must be one of "
        f"{sorted({_MODE_REPLAY, _MODE_LIVE})}"
    )


class _DemoTCPServer(socketserver.TCPServer):
    """TCPServer that exposes mode + poller to request handlers.

    ``allow_reuse_address`` is enabled so the port can be rebound quickly
    across test runs. ``poller`` and ``mode`` are read by the handlers.
    """

    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, *, mode=None,
                 poller=None):
        super().__init__(server_address, RequestHandlerClass)
        self.mode = mode
        self.poller = poller


def create_server(host: str, port: int, mode: str,
                  poller: LivePoller | None = None) -> _DemoTCPServer:
    """Construct a configured TCPServer for the requested mode.

    The caller is responsible for ``serve_forever()`` (typically in a
    background thread) and for ``shutdown()`` + ``server_close()`` when
    done. ``port=0`` requests an OS-assigned port, available afterwards
    via ``server.server_address[1]``.

    The handler class is selected from ``mode`` via ``make_handler``.
    The ``poller`` is attached to the server so live handlers can read
    the current snapshot.

    Hardening (fail-closed):

    - ``host`` must be loopback. Non-loopback bind addresses (0.0.0.0, ::,
      LAN IPs) raise ValueError — the demo console never exposes itself off
      the local machine.
    - ``mode`` must be one of the known modes; anything else raises
      ValueError via ``make_handler`` (no silent REPLAY fallback).
    - ISOLATED_LIVE requires a poller (the live endpoints read it); a missing
      poller raises ValueError.
    - REPLAY must NOT receive a poller (REPLAY serves static files only); a
      non-None poller raises ValueError to catch configuration mistakes.
    """
    if not _is_loopback(host):
        raise ValueError(
            f"host must be loopback (127.0.0.1/localhost/::1), got {host!r}; "
            "the demo console is local-only and never binds off-machine"
        )

    mode_u = mode.upper() if isinstance(mode, str) else mode

    if mode_u == _MODE_LIVE:
        if poller is None:
            raise ValueError(
                "ISOLATED_LIVE mode requires a poller; got poller=None"
            )
    elif mode_u == _MODE_REPLAY:
        if poller is not None:
            raise ValueError(
                "REPLAY mode does not use a poller; "
                f"got poller={poller!r}"
            )
    # Unknown modes are rejected by make_handler below.

    handler_cls = make_handler(poller, mode)
    return _DemoTCPServer((host, port), handler_cls, mode=mode, poller=poller)


def _is_loopback(host: str) -> bool:
    return host.lower() in ("127.0.0.1", "localhost", "::1")


def shutdown_poller(poller: LivePoller, timeout: float = 5.0) -> bool:
    """Stop and join the poller; return True if it shut down cleanly.

    Used by ``main()`` and directly testable. A poller that does not honor
    its stop event within ``timeout`` seconds returns False so the caller
    can report ``POLLER_SHUTDOWN_TIMEOUT`` and exit non-zero.
    """
    poller.stop()
    poller.join(timeout=timeout)
    return not poller.is_alive()


def main():
    parser = argparse.ArgumentParser(description="Demo Console local server")
    parser.add_argument("--mode", choices=sorted(VALID_MODES), default="replay",
                        help="Run mode: replay (default) or isolated_live")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (loopback only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--source-file", default=None,
                        help="Path to snapshot JSON file (required for isolated_live)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Poll interval in seconds (min 1.0)")
    args = parser.parse_args()

    # Run preflight
    pf = run_preflight(args.mode, args.host, args.source_file)

    if not pf["preflight_passed"]:
        print("PREFLIGHT FAILED", file=sys.stderr)
        for f in pf["failures"]:
            print(f"  {f['check']}: {f['detail']}", file=sys.stderr)
        sys.exit(1)

    print(f"Preflight passed: mode={pf['mode']}, source_kind={pf['source_kind']}")
    print(f"  loopback_only={pf['loopback_only']}, read_only={pf['source_read_only']}")
    print(f"  production_resource_accessed={pf['production_resource_accessed']}")
    print(f"  production_resource_access_status={pf['production_resource_access_status']}")
    print(f"  github_writes_enabled={pf['github_writes_enabled']}")
    print(f"  agent_control_enabled={pf['agent_control_enabled']}")
    print(f"  runtime_consumes_rag_context={pf['runtime_consumes_rag_context']}")

    # Determine serve directory
    root = Path(_HERE).resolve().parent.parent
    serve_dir = root / "samples" / "demo-console"

    # Handle ISOLATED_LIVE mode
    poller = None
    if args.mode == "isolated_live":
        source = FileSnapshotSource(args.source_file)
        poller = LivePoller(
            source,
            poll_interval=max(1.0, args.poll_interval),
            max_consecutive_failures=10,
        )

        # Initial load must succeed before starting server
        if not poller.initial_load():
            stats = poller.get_stats()
            print(f"INITIAL SNAPSHOT LOAD FAILED: state={stats['state']}, "
                  f"error={stats['last_error_code']}", file=sys.stderr)
            sys.exit(1)

        print(f"Initial snapshot loaded: sha256={poller.current_sha256[:24]}...")
        print(f"  source_kind={source.kind}")
        print(f"  mode_banner=ISOLATED_LIVE")
        print(f"  read_only=true, not_production=true")
        print(f"  no_github_writes=true, no_agent_control=true")
        print(f"  runtime_consumes_rag_context=false")

        # Start polling thread
        poller.start()
        print(f"Polling started: interval={max(1.0, args.poll_interval)}s")
    else:
        print(f"Mode: REPLAY (static file serving)")

    # Serve
    if not (serve_dir / "index.html").exists():
        print(f"Error: {serve_dir}/index.html not found.", file=sys.stderr)
        if poller:
            shutdown_poller(poller, timeout=3)
        sys.exit(1)

    os.chdir(str(serve_dir))

    mode_upper = args.mode.upper()
    with create_server(args.host, args.port, mode_upper, poller=poller) as httpd:
        print(f"Demo Console serving on http://{args.host}:{args.port}")
        print(f"Serving from: {serve_dir}")
        print(f"Mode: {mode_upper}")
        print(f"Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            if poller:
                if not shutdown_poller(poller, timeout=5):
                    # The poller thread did not honor the stop event within
                    # the grace period. Report the failure explicitly and
                    # exit non-zero so supervisors/jobs can detect it. Do
                    # NOT print "Poller stopped" — it did not stop.
                    print("POLLER_SHUTDOWN_TIMEOUT", file=sys.stderr)
                    return 1
                stats = poller.get_stats()
                print(f"Poller stopped: state={stats['state']}, "
                      f"polls={stats['poll_count']}, "
                      f"failures={stats['consecutive_failures']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
