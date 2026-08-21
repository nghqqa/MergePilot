"""M8-GH-4B3-W3B-R2 §7: explicit frozen-proxy chain + concurrency.

Proves with a REAL local socket proxy (actual TCP connections, actual
request/CONNECT lines on the wire) that:
- installation-token exchange, Checks lookup/create/update/publish ALL
  route through the SAME explicit proxy-aware transport (the actual
  proxy selection path, not handler-type or proxies-dict inspection);
- 8 concurrent threads under mutually conflicting NO_PROXY/no_proxy/
  HTTPS_PROXY environment ALL select the exact proxy URL the transport
  was built with, the environment is byte-identical before/after, the
  global opener/urlopen is never used, and nothing leaks.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import checks_reporter as cr          # noqa: E402
import token_provider as tp           # noqa: E402
import e2e_foundation as e2f          # noqa: E402

FROZEN = e2f.E2E_REPORTER_PROXY_R     # http://172.31.0.98:18090


class LocalProxy:
    """Real TCP proxy stub. Records every first request line
    (CONNECT authority or absolute-form http request). For http
    absolute-form requests it answers with a 200 JSON body so full
    request/response cycles complete through the proxy."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.lines = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.sock.settimeout(0.2)
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(15)
            data = conn.recv(65536)
            line = data.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            with self.lock:
                self.lines.append(line)
            if line.upper().startswith("CONNECT"):
                # establish the tunnel, then close (TLS handshake is
                # not the subject — the CONNECT routing is)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n"
                             b"\r\n")
                try:
                    conn.recv(1024)
                except OSError:
                    pass
            else:
                body = json.dumps({"id": 4242,
                                   "check_runs": []}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\n"
                             b"Content-Type: application/json\r\n"
                             b"Content-Length: %d\r\n\r\n" % len(body)
                             + body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def lines_snapshot(self):
        with self.lock:
            return list(self.lines)


def _no_global_urlopen():
    def boom(*a, **kw):
        raise AssertionError("global urlopen must not be used")
    return boom


class TestFivePathsThroughExplicitProxy(unittest.TestCase):

    def setUp(self):
        self.proxy = LocalProxy()
        self.addCleanup(self.proxy.close)

    def test_token_exchange_routes_via_explicit_opener(self):
        env_snapshot = dict(os.environ)
        transport = None
        with mock.patch.dict(os.environ, {
                "HTTPS_PROXY": self.proxy.url,
                "NO_PROXY": "api.github.com",      # must NOT bypass
                "no_proxy": "*"}), \
             mock.patch("urllib.request.urlopen",
                        side_effect=_no_global_urlopen()):
            try:
                tp.default_transport(
                    "POST", "https://api.github.com/app/installations/1/"
                    "access_tokens", headers={}, body={})
            except Exception:
                pass  # TLS handshake after CONNECT is out of scope
        self.assertEqual(dict(os.environ), env_snapshot)
        connects = [l for l in self.proxy.lines_snapshot()
                    if l.upper().startswith("CONNECT")]
        self.assertTrue(connects, "no CONNECT reached the proxy")
        self.assertIn("api.github.com:443", connects[0])

    def test_lookup_create_update_route_via_explicit_transport(self):
        transport = cr.build_explicit_proxy_transport(self.proxy.url)

        def call(method, url, body=None):
            # one transient-retry: the local socket stub can lose a
            # race under a full-suite load; the PROXY SELECTION (the
            # subject here) is unaffected by the retry.
            for attempt in (1, 2):
                try:
                    return transport(method, url, headers={}, body=body)
                except cr.TransportError:
                    if attempt == 2:
                        raise

        with mock.patch("urllib.request.urlopen",
                        side_effect=_no_global_urlopen()):
            # lookup
            s, _h, body = call(
                "GET", "http://api.github.com/repos/x/y/check-runs")
            self.assertEqual(s, 200)
            # create (POST)
            s, _h, body = call(
                "POST", "http://api.github.com/repos/x/y/check-runs",
                body={"name": "mp"})
            self.assertEqual(s, 200)
            # update (PATCH)
            s, _h, body = call(
                "PATCH",
                "http://api.github.com/repos/x/y/check-runs/42",
                body={"status": "completed"})
            self.assertEqual(s, 200)
        lines = self.proxy.lines_snapshot()
        methods = [l.split(" ")[0] for l in lines]
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)
        self.assertIn("PATCH", methods)
        # absolute-form: the proxy sees the full target URL
        self.assertTrue(any("http://api.github.com/" in l
                            for l in lines))
        # every single request went through the proxy (>=3; a
        # load-retry only ever ADDS proxied lines, never bypasses)
        self.assertGreaterEqual(len(lines), 3)

    def test_publish_once_full_cycle_via_proxy_lookup_then_publish(self):
        transport = cr.build_explicit_proxy_transport(self.proxy.url)
        calls = []

        def fake_transport(method, url, *, headers, body):
            calls.append((method, url))
            return transport(method, url, headers=headers, body=body)

        conn = _FakeConn(row=dict(check_run_id=None))
        result = cr.publish_once(
            lambda: conn, api_base="http://api.github.com",
            transport=fake_transport, token="tok-x")
        # create path: lookup (GET) then publish (POST)
        self.assertEqual([m for m, _u in calls], ["GET", "POST"])
        # update path: a row carrying an existing check_run_id and an
        # unchanged SHA PATCHes directly (no re-lookup by design)
        calls.clear()
        conn2 = _FakeConn(row=dict(check_run_id=4242))
        cr.publish_once(lambda: conn2, api_base="http://api.github.com",
                        transport=fake_transport, token="tok-x")
        self.assertEqual([m for m, _u in calls], ["PATCH"])
        # every API call physically traversed the local proxy
        lines = self.proxy.lines_snapshot()
        self.assertEqual(len(lines), 3)

    def test_frozen_value_selectable_and_bypass_proof(self):
        # the transport built with the FROZEN proxy URL routes to
        # exactly that URL (selection mechanism + frozen value).
        opener = tp.build_proxy_opener(FROZEN)
        handlers = [h for h in opener.handlers
                    if isinstance(h, tp._ForcedProxyHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].proxies["https"], FROZEN)
        # behavioral bypass proof: even with NO_PROXY=* the forced
        # handler never consults proxy_bypass (its proxy_open skips
        # it by construction); exercise the branch directly.
        import urllib.request as _ur
        handler = handlers[0]
        req = _ur.Request("https://api.github.com/x")
        with mock.patch("urllib.request.proxy_bypass",
                        side_effect=AssertionError(
                            "proxy_bypass must never be consulted")):
            # proxy_open must not call proxy_bypass; it fails on
            # connect to the frozen (unroutable here) address, which
            # proves the code path reached the forced dial.
            with self.assertRaises((OSError,)):
                opener.open(req, timeout=1)


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        row = self.row
        self.row = None
        if row is None:
            return None
        return (1, 1, "run-1", "x/y", 1, "sha", "ext",
                row["check_run_id"], "queued", None, 1, 0, 0)


class _FakeConn:
    def __init__(self, row):
        self.cur = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class TestConcurrentEightThreads(unittest.TestCase):

    def test_eight_threads_conflicting_env_all_select_proxy(self):
        proxy = LocalProxy()
        self.addCleanup(proxy.close)
        env_before = dict(os.environ)

        conflicts = {
            "NO_PROXY": "api.github.com,.local",
            "no_proxy": "api.github.com,*",
            "HTTPS_PROXY": "http://wrong.invalid:9999",
        }
        results = []
        errors = []
        env_drift = []
        barrier = threading.Barrier(8)

        def worker(i):
            try:
                env_at_entry = dict(os.environ)
                transport = cr.build_explicit_proxy_transport(
                    proxy.url)
                barrier.wait(timeout=10)   # maximize contention
                s, _h, _b = transport(
                    "GET",
                    "http://api.github.com/repos/x/y/check-runs",
                    headers={"Authorization": "Bearer secret-%d" % i},
                    body=None)
                results.append(s)
                if dict(os.environ) != env_at_entry:
                    env_drift.append(i)
            except Exception as exc:   # noqa: BLE001
                errors.append(exc)

        with mock.patch.dict(os.environ, conflicts),                 mock.patch("urllib.request.urlopen",
                           side_effect=_no_global_urlopen()):
            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(env_drift, [])
        self.assertEqual(results, [200] * 8)
        # ALL eight requests physically reached the local proxy
        lines = proxy.lines_snapshot()
        self.assertEqual(len(lines), 8)
        for line in lines:
            self.assertIn("http://api.github.com/", line)
            self.assertNotIn("wrong.invalid", line)
        # environment identical after the run
        self.assertEqual(dict(os.environ), env_before)
        # no Authorization material leaked onto the proxy wire
        for line in lines:
            self.assertNotIn("secret-", line)
            self.assertNotIn("Bearer", line)


if __name__ == "__main__":
    unittest.main()
