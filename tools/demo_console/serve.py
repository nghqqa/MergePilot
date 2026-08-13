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


def _is_loopback(host: str) -> bool:
    return host.lower() in ("127.0.0.1", "localhost", "::1")


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
            poller.stop()
            poller.join(timeout=3)
        sys.exit(1)

    os.chdir(str(serve_dir))

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), ReadOnlyHandler) as httpd:
        print(f"Demo Console serving on http://{args.host}:{args.port}")
        print(f"Serving from: {serve_dir}")
        print(f"Mode: {args.mode.upper()}")
        print(f"Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            if poller:
                poller.stop()
                poller.join(timeout=5)
                stats = poller.get_stats()
                print(f"Poller stopped: state={stats['state']}, "
                      f"polls={stats['poll_count']}, "
                      f"failures={stats['consecutive_failures']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
