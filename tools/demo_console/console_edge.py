"""Secretless loopback publication edge for the ISOLATED_LIVE console.

1-G network-design spike outcome (experiment C): Docker silently drops
``-p`` port publishing on internal networks, so the DSN-bearing
demo-console can never both stay internal-only and be published. This
edge is the selected publication plumbing:

  - the demo-console (and every secret-bearing service) stays on the
    internal-only backend network with NO host ports and NO external
    default route;
  - this edge is the ONLY component on the publication bridge; it holds
    NO secrets (no DSN, no passwords, no database coordinates), proxies
    exactly ONE fixed in-network upstream, and publishes loopback-only.

It is publication plumbing, NOT a fifth application service, and does
NOT constitute application integration.

Security contract (fail-closed everywhere):
  - listens on 0.0.0.0:8600 inside its container; the HOST-side publish
    is 127.0.0.1-only (enforced by compose/orchestrator, not here);
  - fixed upstream http://demo-console:8600 — a CONSTANT. The upstream
    can never be influenced by the request path, query, Host header, or
    any other request data;
  - method whitelist: GET only (POST/PUT/PATCH/DELETE/OPTIONS/TRACE/HEAD
    -> 405; CONNECT -> 403);
  - path whitelist (origin-form only): /, /index.html, /live-refresh.js,
    /api/live/status, /api/live/snapshot. Absolute-form URIs, any
    scheme/netloc, backslashes, control characters -> 403. ``/`` may
    carry a query (e.g. ``interval_ms``); the query is passed through
    verbatim and never parsed for routing;
  - Host header must be 127.0.0.1 or localhost (optionally with a port);
    anything else -> 403;
  - NO client headers are forwarded upstream (Authorization, Cookie,
    Proxy-*, X-Forwarded-* die here); the upstream request carries only
    fixed, minimal headers;
  - upstream failures and timeouts fail closed as 502; response bodies
    are capped (MAX_BODY_BYTES);
  - responses forward ONLY whitelisted headers (Content-Type, ETag,
    Cache-Control) with a recomputed Content-Length; hop-by-hop headers
    never transit; a Location header is forwarded only if it targets the
    loopback (relative Locations pass, external hosts are dropped);
  - logging records method + allowlisted path (query stripped) + status
    only — never headers, secrets, or bodies.

Pure Python stdlib (no requests/httpx/aiohttp); no outbound connections
except the single fixed upstream.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8600

# FIXED upstream — a module constant, deliberately NOT configurable via
# environment, request, or any other input.
UPSTREAM = "http://demo-console:8600"

ALLOWED_PATHS = frozenset({
    "/",
    "/index.html",
    "/live-refresh.js",
    "/api/live/status",
    "/api/live/snapshot",
})

ALLOWED_HOST_PREFIXES = ("127.0.0.1", "localhost")

# Fixed minimal upstream headers — client headers never transit.
UPSTREAM_HEADERS = {"Accept": "*/*", "User-Agent": "mergepilot-console-edge/1"}

UPSTREAM_TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 8 * 1024 * 1024

# Response headers we forward upstream->client (hop-by-hop and anything
# else — including Set-Cookie and Location-by-default — never transit).
FORWARDED_RESPONSE_HEADERS = ("content-type", "etag", "cache-control")

# Loopback-only redirect targets; anything else is dropped.
LOOPBACK_REDIRECT_HOSTS = ("127.0.0.1", "localhost")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """The edge never FOLLOWS redirects — it forwards the upstream's
    (filtered) response as-is. Following would let an upstream response
    chain turn the edge into an indirect fetcher."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class EdgeRejected(Exception):
    """Internal control-flow: (status, reason) for a rejected request."""


def _reject(status, reason):
    raise EdgeRejected(status, reason)


class ConsoleEdgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── request validation ──────────────────────────────────────────

    def _validate(self):
        raw = self.path
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
            _reject(403, "control characters in request target")
        if "\\" in raw:
            _reject(403, "backslash in request target")
        parts = urllib.parse.urlsplit(raw)
        if parts.scheme or parts.netloc:
            _reject(403, "absolute-form URI rejected")
        if parts.username is not None or parts.password is not None:
            _reject(403, "userinfo rejected")
        if parts.path not in ALLOWED_PATHS:
            _reject(404, "path not allowlisted")
        host = (self.headers.get("Host") or "").strip().lower()
        host_host = host.split("]", 1)[-1].split(":")[0] if host.startswith(
            "[") else host.split(":")[0]
        if not host_host or not host_host.startswith(ALLOWED_HOST_PREFIXES):
            _reject(403, "host not permitted")

    # ── upstream fetch ──────────────────────────────────────────────

    def _fetch_upstream(self):
        # self.path is validated origin-form; the query (if any) rides
        # along verbatim and never influences the upstream host.
        url = UPSTREAM + self.path
        request = urllib.request.Request(url, method="GET",
                                         headers=dict(UPSTREAM_HEADERS))
        try:
            response = _OPENER.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as err:
            # Non-2xx (including the un-followed 3xx) is a REAL upstream
            # response — forward it, don't mask it as a 502.
            response = err
        except Exception:
            _reject(502, "upstream unreachable (fail-closed)")
        try:
            body = response.read(MAX_BODY_BYTES + 1)
        except Exception:
            _reject(502, "upstream read failed (fail-closed)")
        finally:
            try:
                response.close()
            except Exception:
                pass
        if len(body) > MAX_BODY_BYTES:
            _reject(502, "upstream body exceeds cap (fail-closed)")
        return response.status, response.headers, body

    # ── response shaping ────────────────────────────────────────────

    def _send(self, status, headers, body):
        self.send_response(status)
        for name in FORWARDED_RESPONSE_HEADERS:
            value = headers.get(name)
            if value:
                self.send_header(name, value)
        location = headers.get("Location")
        if location:
            loc = urllib.parse.urlsplit(location)
            host = loc.hostname or ""
            if not loc.netloc or \
                    host.startswith(LOOPBACK_REDIRECT_HOSTS):
                self.send_header("Location", location)
            # external redirect targets are dropped, not forwarded
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_error_body(self, status, reason):
        body = json.dumps(
            {"error": "edge_rejected", "status": status,
             "reason": reason}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── methods ─────────────────────────────────────────────────────

    def do_GET(self):
        try:
            self._validate()
            status, headers, body = self._fetch_upstream()
        except EdgeRejected as exc:
            self._send_error_body(exc.args[0], exc.args[1])
            self._note(exc.args[0])
            return
        self._send(status, headers, body)
        self._note(status)

    def do_CONNECT(self):
        self._send_error_body(403, "CONNECT forbidden")
        self._note(403)

    def _method_forbidden(self):
        self._send_error_body(405, "method forbidden")
        self._note(405)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = \
        do_HEAD = _method_forbidden

    # ── logging (secret-free by construction) ───────────────────────

    def _note(self, status):
        path = self.path.split("?")[0] if self.path else "?"
        sys.stderr.write("edge %s %s -> %d\n"
                         % (self.command, path, status))

    def log_message(self, fmt, *args):  # silence default header logging
        pass


def main() -> int:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT),
                                 ConsoleEdgeHandler)
    server.daemon_threads = True
    print("[console-edge] secretless loopback publication edge on "
          "%s:%d -> %s" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM),
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
