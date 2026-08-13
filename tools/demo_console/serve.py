#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo Console local HTTP server (read-only).

Serves pre-rendered static HTML on localhost. No write operations,
no external network. Supports REPLAY (default) and ISOLATED_LIVE modes.

ISOLATED_LIVE startup has two distinct phases:

  1. **config_preflight** (``run_preflight``): checks config presence and
     shape WITHOUT a DB connection. Verifies the DSN env var is present,
     ``run_id`` is well-formed, and the expected identity fields are
     configured. No network, no database round-trip.

  2. **startup_probe** (``poller.initial_load()``): the actual DB
     identity/catalog/role/initial-snapshot read. This opens a DB connection
     (for source-kind=postgres) and reads the first snapshot. The server
     socket is NOT created until this succeeds.

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
        # Identity / source — source_kind and source_read_only come from the
        # actual SnapshotSource via the poller view (never hardcoded), so a
        # custom source type reports its own kind and read-only status.
        "mode": _MODE_LIVE,
        "source_kind": view.get("source_kind", "UNKNOWN"),
        "source_read_only": view.get("source_read_only", True),
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
    """Read-only HTTP handler — blocks all write methods.

    Write methods (PUT/POST/DELETE/PATCH) are blocked with 405. On Windows the
    client frequently resets the connection after the 405 is sent (the client
    did not expect a body and closes early), which surfaces as
    ``ConnectionAbortedError`` when writing the response. We catch that on the
    write-method paths and treat it as a clean 405 (the client already saw the
    status line) rather than letting it propagate as a 500-style traceback. On
    GET paths we deliberately do NOT catch it: a read-path abort is an
    unexpected condition worth surfacing.
    """

    def do_PUT(self):
        self._reject_write_method()

    def do_POST(self):
        self._reject_write_method()

    def do_DELETE(self):
        self._reject_write_method()

    def do_PATCH(self):
        self._reject_write_method()

    def _reject_write_method(self):
        # Send a minimal 405 without an HTML body to avoid Windows
        # ConnectionAbortedError. send_error() writes an HTML body which
        # triggers the client to reset before the body is fully sent.
        # Instead we craft a bare response with Content-Length: 0.
        try:
            self.send_response_only(405, "Method Not Allowed")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Connection", "close")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

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

    - ``host`` must be IPv4 loopback (``127.0.0.1`` or ``localhost``).
      Non-loopback bind addresses (``0.0.0.0``, ``::``, LAN IPs) raise
      ValueError — the demo console never exposes itself off the local
      machine. IPv6 loopback (``::1``) is also rejected: the P1 server is
      IPv4-loopback only and IPv6 ::1 is NOT implemented.
    - ``mode`` must be one of the known modes; anything else raises
      ValueError via ``make_handler`` (no silent REPLAY fallback).
    - ISOLATED_LIVE requires a poller (the live endpoints read it); a missing
      poller raises ValueError.
    - REPLAY must NOT receive a poller (REPLAY serves static files only); a
      non-None poller raises ValueError to catch configuration mistakes.
    """
    if not _is_loopback(host):
        host_lower = host.lower() if isinstance(host, str) else host
        if host_lower == "::1":
            reason = "P1 server is IPv4-loopback only; IPv6 ::1 not implemented"
        else:
            reason = (
                "the demo console is IPv4-loopback only and never binds "
                "off-machine"
            )
        raise ValueError(
            f"host must be IPv4 loopback (127.0.0.1/localhost), got {host!r}; "
            f"{reason}"
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
    """IPv4-loopback only.

    The P1 demo server binds a single-machine HTTP listener and is
    IPv4-loopback only; IPv6 loopback (``::1``) is NOT implemented.
    Only ``127.0.0.1`` and ``localhost`` are accepted. Any other host —
    including ``::1``, ``::``, ``0.0.0.0``, and LAN IPs — is rejected.
    """
    return host.lower() in ("127.0.0.1", "localhost")


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
    parser.add_argument(
        "--source-kind", choices=("file", "postgres"), default="file",
        help="Snapshot source type: file (default) or postgres "
             "(ISOLATED_LIVE only)",
    )
    parser.add_argument("--source-file", default=None,
                        help="Path to snapshot JSON file (required for "
                             "isolated_live + source-kind=file)")
    parser.add_argument("--run-id", default=None,
                        help="task_runs.run_id to read "
                             "(source-kind=postgres only)")
    parser.add_argument("--expected-database", default=None,
                        help="Required current_database() value "
                             "(source-kind=postgres; also via "
                             "MERGEPILOT_PG_EXPECTED_DATABASE)")
    parser.add_argument("--expected-role", default=None,
                        help="Required current_user value "
                             "(source-kind=postgres; the canonical viewer "
                             "role is mergepilot_reader; also via "
                             "MERGEPILOT_PG_EXPECTED_ROLE). REQUIRED for "
                             "--source-kind postgres.")
    parser.add_argument("--expected-environment-id", default=None,
                        help="Required environment marker value "
                             "(source-kind=postgres; also via "
                             "MERGEPILOT_PG_ENVIRONMENT_ID)")
    parser.add_argument("--expected-server-addresses", default=None,
                        help="Comma-separated allowed inet_server_addr() values "
                             "(source-kind=postgres; e.g. '127.0.0.1'; also via "
                             "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES)")
    parser.add_argument("--expected-server-port", type=int, default=None,
                        help="Required inet_server_port() value "
                             "(source-kind=postgres; also via "
                             "MERGEPILOT_PG_EXPECTED_SERVER_PORT)")
    parser.add_argument("--expected-application-name", default=None,
                        help="Required application_name value "
                             "(source-kind=postgres; also via "
                             "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Poll interval in seconds (min 1.0)")
    args = parser.parse_args()

    # Build the postgres config (only used when source-kind=postgres). The DSN
    # is NEVER taken from argv — it is read from MERGEPILOT_PG_DSN inside
    # preflight (and again at read time) so it never appears in process argv.
    pg_config = None
    if args.source_kind == "postgres":
        # Parse the comma-separated server addresses list.
        server_addrs = (
            args.expected_server_addresses
            or os.environ.get("MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES")
        )
        if server_addrs:
            server_addrs_list = [
                a.strip() for a in server_addrs.split(",") if a.strip()
            ]
        else:
            server_addrs_list = []
        server_port = (
            args.expected_server_port
            if args.expected_server_port is not None
            else (
                int(os.environ["MERGEPILOT_PG_EXPECTED_SERVER_PORT"])
                if os.environ.get("MERGEPILOT_PG_EXPECTED_SERVER_PORT")
                else None
            )
        )
        pg_config = {
            "run_id": args.run_id,
            "expected_database": (
                args.expected_database
                or os.environ.get("MERGEPILOT_PG_EXPECTED_DATABASE")
            ),
            "expected_role": (
                args.expected_role
                or os.environ.get("MERGEPILOT_PG_EXPECTED_ROLE")
            ),
            "expected_environment_id": (
                args.expected_environment_id
                or os.environ.get("MERGEPILOT_PG_ENVIRONMENT_ID")
            ),
            "expected_server_addresses": server_addrs_list,
            "expected_server_port": server_port,
            "expected_application_name": (
                args.expected_application_name
                or os.environ.get("MERGEPILOT_PG_EXPECTED_APPLICATION_NAME")
            ),
        }

    # Run the CONFIG PREFLIGHT: this checks config presence and shape WITHOUT
    # a DB connection (no network, no database round-trip). It verifies the
    # DSN env var is present, run_id is well-formed, and the expected identity
    # fields are configured. The actual DB identity/catalog/initial-snapshot
    # gate is the STARTUP PROBE (poller.initial_load()) which runs next and
    # DOES open a DB connection. The two concepts are deliberately distinct:
    # config_preflight = "is the configuration present and well-shaped?";
    # startup_probe = "can we actually read a valid first snapshot?".
    pf = run_preflight(
        args.mode, args.host, args.source_file,
        source_kind=args.source_kind, pg_config=pg_config,
    )

    if not pf["preflight_passed"]:
        print("CONFIG PREFLIGHT FAILED", file=sys.stderr)
        for f in pf["failures"]:
            print(f"  {f['check']}: {f['detail']}", file=sys.stderr)
        sys.exit(1)

    print(f"Config preflight passed: mode={pf['mode']}, "
          f"source_kind={pf['source_kind']}")
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
        if args.source_kind == "postgres":
            # Build the read-only PostgreSQL source. The DSN is read from the
            # env var here (preflight already verified it is present); it is
            # never logged and never placed in repr/str.
            from postgres_source import PostgresSnapshotSource
            dsn = os.environ["MERGEPILOT_PG_DSN"]
            source = PostgresSnapshotSource(
                dsn=dsn,
                run_id=pg_config["run_id"],
                expected_database=pg_config["expected_database"],
                expected_role=pg_config["expected_role"],
                expected_environment_id=pg_config["expected_environment_id"],
                expected_server_addresses=pg_config["expected_server_addresses"],
                expected_server_port=pg_config["expected_server_port"],
                expected_application_name=pg_config["expected_application_name"],
            )
        else:
            source = FileSnapshotSource(args.source_file)
        poller = LivePoller(
            source,
            poll_interval=max(1.0, args.poll_interval),
            max_consecutive_failures=10,
        )

        # STARTUP PROBE: the actual DB identity/catalog/initial snapshot read.
        # This opens a DB connection (for postgres) and reads the first
        # snapshot. The server socket is NOT created until this succeeds, so a
        # misconfigured/unknown database never results in a serving listener.
        if not poller.initial_load():
            stats = poller.get_stats()
            print(f"STARTUP PROBE FAILED: state={stats['state']}, "
                  f"error={stats['last_error_code']}", file=sys.stderr)
            sys.exit(1)

        print(f"Startup probe succeeded: sha256={poller.current_sha256[:24]}...")
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
