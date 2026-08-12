#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo Console local HTTP server (read-only).

Serves the pre-rendered static HTML on localhost. No write operations,
no external network. REPLAY mode only — loads pre-generated files.

Usage:
    python tools/demo_console/serve.py [--port 8080]
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Demo Console local server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent.parent
    serve_dir = root / "samples" / "demo-console"

    if not (serve_dir / "index.html").exists():
        print(f"Error: {serve_dir}/index.html not found. Run render.py first.")
        sys.exit(1)

    os.chdir(str(serve_dir))

    handler = http.server.SimpleHTTPRequestHandler

    class ReadOnlyHandler(handler):
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

    with socketserver.TCPServer((args.host, args.port), ReadOnlyHandler) as httpd:
        print(f"Demo Console serving on http://{args.host}:{args.port}")
        print(f"Serving from: {serve_dir}")
        print(f"Mode: REPLAY (read-only)")
        print(f"Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
