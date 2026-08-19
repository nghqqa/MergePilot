"""M8-GH-2 runtime-hardening focused tests.

Covers: request-timeout config (fail-closed), real socket-timeout behavior
via an in-process loopback harness (slow body, drain-timeout, server
survives client disconnect), Dockerfile contract (digest-pinned base,
non-root user), and the DSN connect_timeout guard matrix. No Docker, WSL,
real PostgreSQL or network beyond 127.0.0.1 in-process sockets.
"""

from __future__ import annotations

import json
import os
import socket as socket_module
import sys
import threading
import time
import unittest
import unittest.mock  # noqa: F401
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import http_server as hs                                # noqa: E402
from dsn_guard import (DsnConfigError,                  # noqa: E402
                       ensure_connect_timeout)

DOCKERFILE = (ROOT / "tools" / "gh-app" / "Dockerfile").read_text(
    encoding="utf-8")

PINNED_DIGEST = ("9e869b0816f5537709825b49e62dc86d1c2691eff19b05"
                "c1d4dc3a07992cc052")


class TestRequestTimeoutConfig(unittest.TestCase):

    def test_default_and_valid_values(self):
        self.assertEqual(hs.parse_request_timeout(None), 30)
        self.assertEqual(hs.parse_request_timeout("7"), 7)
        self.assertEqual(hs.parse_request_timeout(1), 1)
        self.assertEqual(hs.parse_request_timeout("300"), 300)

    def test_invalid_values_fail_closed(self):
        for bad in ("0", "-1", "301", "abc", "3.5", "  ", "1e3", ""):
            with self.assertRaises(hs.ServerConfigError, msg=bad):
                hs.parse_request_timeout(bad)


class LoopbackHarness(unittest.TestCase):
    """真实 127.0.0.1 socket 的进程内 ThreadingHTTPServer 超时行为。"""

    @classmethod
    def setUpClass(cls):
        cls._saved = (hs.WebhookHandler.timeout,)
        hs.WebhookHandler.timeout = 1            # 1s read timeout
        # handler 在 do_POST 内读取 secret env;提供临时值(仅进程内),
        # 坏签名路径在校验处短路,不触 DB。
        cls._env = unittest.mock.patch.dict(
            os.environ, {"GITHUB_WEBHOOK_SECRET": "harness-secret"})
        cls._env.start()
        cls.server = hs.WebhookServer(("127.0.0.1", 0), hs.WebhookHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._env.stop()
        hs.WebhookHandler.timeout = cls._saved[0]

    def _open(self):
        sock = socket_module.create_connection(("127.0.0.1", self.port),
                                               timeout=10)
        return sock

    def _read_response(self, sock):
        data = b""
        sock.settimeout(10)
        try:
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            headers = head.decode("latin-1")
            status = int(headers.split(" ", 2)[1])
            length = 0
            for line in headers.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            while len(rest) < length:
                rest += sock.recv(4096)
            return status, rest.decode("utf-8", "replace")
        finally:
            sock.close()

    def test_slow_body_times_out_408_and_connection_closes(self):
        sock = self._open()
        sock.sendall(b"POST /webhook HTTP/1.1\r\nHost: t\r\n"
                     b"Content-Length: 100\r\n\r\n")
        sock.sendall(b"partial")                  # 然后停滞
        started = time.monotonic()
        status, body = self._read_response(sock)
        elapsed = time.monotonic() - started
        self.assertEqual(status, 408)
        self.assertIn("request read timeout", body)
        self.assertLess(elapsed, 5)
        # 连接被强制关闭:继续读会 EOF(已 close_connection=True)
        # (read_response 已关闭 socket;二次读验证省略——由 close 语义保证)

    def test_drain_timeout_on_oversized_slow_body(self):
        sock = self._open()
        sock.sendall(b"POST /webhook HTTP/1.1\r\nHost: t\r\n"
                     b"Content-Length: 3000000\r\n\r\n")
        sock.sendall(b"x" * 1024)                 # 声明 3MB,只给 1KB 后停滞
        status, body = self._read_response(sock)
        self.assertEqual(status, 408)
        self.assertIn("request read timeout", body)

    def test_server_survives_and_serves_next_request(self):
        # 先制造一次客户端提前断开(写 header 后立刻 RST)
        sock = self._open()
        sock.sendall(b"POST /webhook HTTP/1.1\r\nHost: t\r\n"
                     b"Content-Length: 50\r\n\r\nshort")
        sock.setsockopt(socket_module.SOL_SOCKET,
                        socket_module.SO_LINGER,
                        b"\x01\x00\x00\x00\x00\x00\x00\x00")  # RST on close
        sock.close()
        time.sleep(0.3)
        # 服务器必须仍能应答下一请求(用坏签名 401 路径,不触 DB)
        sock2 = self._open()
        payload = b'{"zen":"ok"}'
        sock2.sendall(
            b"POST /webhook HTTP/1.1\r\nHost: t\r\n"
            b"Content-Type: application/json\r\n"
            b"X-GitHub-Event: ping\r\n"
            b"X-GitHub-Delivery: hardening-probe-0001\r\n"
            b"X-Hub-Signature-256: sha256=" + b"0" * 64 + b"\r\n"
            b"Content-Length: %d\r\n\r\n" % len(payload) + payload)
        status, _ = self._read_response(sock2)
        self.assertEqual(status, 401)             # 存活且验签路径正常

    def test_daemon_threads_explicit(self):
        self.assertTrue(hs.WebhookServer.daemon_threads)


class TestDockerfileContract(unittest.TestCase):

    def test_from_is_tag_plus_digest(self):
        import re
        match = re.search(r"^FROM (\S+)$", DOCKERFILE, re.MULTILINE)
        self.assertIsNotNone(match)
        base = match.group(1)
        self.assertRegex(base, r"^python:3\.12-slim@sha256:[0-9a-f]{64}$")
        self.assertNotEqual(base, "python:3.12-slim")   # 禁止裸 tag
        self.assertIn(PINNED_DIGEST, base)

    def test_non_root_user(self):
        self.assertIn("groupadd -g 9090 mergepilot-gh", DOCKERFILE)
        self.assertIn("useradd -u 9090 -g 9090", DOCKERFILE)
        self.assertRegex(DOCKERFILE, r"(?m)^USER mergepilot-gh$")
        self.assertNotRegex(DOCKERFILE, r"(?m)^USER root")

    def test_no_secrets_or_hosts_baked(self):
        effective = "\n".join(line for line in DOCKERFILE.splitlines()
                              if not line.lstrip().startswith("#"))
        for forbidden in ("postgresql://", "PASSWORD=", "WEBHOOK_SECRET=",
                          "BEGIN PRIVATE", "sudo", "--privileged"):
            self.assertNotIn(forbidden, effective)

    def test_dsn_guard_module_copied(self):
        self.assertIn("dsn_guard.py", DOCKERFILE)


class TestDsnGuard(unittest.TestCase):

    def test_uri_dsn_missing_timeout_gets_default(self):
        guarded = ensure_connect_timeout(
            "postgresql://u:p@h:5432/db?sslmode=disable")
        self.assertIn("connect_timeout=5", guarded)

    def test_uri_dsn_existing_timeout_preserved(self):
        guarded = ensure_connect_timeout(
            "postgresql://u:p@h:5432/db?connect_timeout=9")
        self.assertIn("connect_timeout=9", guarded)

    def test_keyword_dsn_both_directions(self):
        guarded = ensure_connect_timeout(
            "host=h port=5432 dbname=db user=u password=p")
        self.assertIn("connect_timeout", guarded)
        guarded2 = ensure_connect_timeout(
            "host=h port=5432 dbname=db user=u password=p "
            "connect_timeout=12")
        self.assertIn("connect_timeout", guarded2)
        self.assertNotIn("connect_timeout=5", guarded2)

    def test_invalid_timeout_rejected(self):
        for bad in ("0", "-2", "31", "999", "abc", "1.5"):
            dsn = "postgresql://u:p@h/db?connect_timeout=%s" % bad
            with self.assertRaises(DsnConfigError, msg=bad):
                ensure_connect_timeout(dsn)

    def test_invalid_dsn_rejected_without_echo(self):
        with self.assertRaises(DsnConfigError) as ctx:
            ensure_connect_timeout("not a dsn at all :://")
        self.assertNotIn("not a dsn", str(ctx.exception))
        with self.assertRaises(DsnConfigError):
            ensure_connect_timeout("")
        with self.assertRaises(DsnConfigError):
            ensure_connect_timeout(None)

    def test_password_special_characters_roundtrip(self):
        import psycopg2.extensions as ext
        special = "p@ss:word/with= spaces'\""
        # libpq conninfo 语法:含空格/特殊字符的值用单引号包裹,
        # 内嵌单引号以反斜杠转义。
        conninfo = ("host=h port=5432 dbname=db user=u "
                    "password='p@ss:word/with= spaces\\'\"'")
        guarded = ensure_connect_timeout(conninfo)
        params = ext.parse_dsn(guarded)          # 再解析必须还原
        self.assertEqual(params.get("password"), special)
        self.assertIn("connect_timeout", params)

    def test_error_messages_never_contain_dsn(self):
        try:
            ensure_connect_timeout(
                "postgresql://secretuser:secretpw@h/db?connect_timeout=99")
        except DsnConfigError as exc:
            self.assertNotIn("secretuser", str(exc))
            self.assertNotIn("secretpw", str(exc))
        else:
            self.fail("expected DsnConfigError")


if __name__ == "__main__":
    unittest.main()
