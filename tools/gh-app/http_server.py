#!/usr/bin/env python3
"""http_server.py — GitHub webhook HTTP 入口 + healthz(M8-GH-1/2,stdlib-only)。

    POST /webhook  → receiver.handle_webhook(HMAC 验签 → INSERT-only)
    GET  /healthz  → SELECT 1 连接活性(无需表权限)

环境契约(秘密零日志/零 argv):
    GITHUB_WEBHOOK_SECRET  HMAC 密钥(必填)
    GITHUB_INGRESS_DSN     INSERT-only 角色连接串(必填;启动时经
                           dsn_guard.ensure_connect_timeout 结构化校验,
                           缺 connect_timeout 补默认,非法值启动即拒)
    GITHUB_REPO_ALLOWLIST  逗号分隔 repo 列表(可选;receiver 侧早期 IGNORE)
    GH_APP_BIND / GH_APP_PORT  监听地址(默认 127.0.0.1:8090;隔离栈内由
                               编排注入 0.0.0.0)
    GH_APP_REQUEST_TIMEOUT_SECONDS  请求读取超时(默认 30;合法 1..300;
                                    非整数/越界启动失败 exit 3,绝不静默
                                    修正)。超时设置在 accepted socket 上
                                    ——读 header/body/413 排水任一阶段触发
                                    socket.timeout → 408 + 关连接,零
                                    receiver/DB 调用,零 body/secret 泄漏。

慢客户端合同(M8-GH-2 §1):ThreadingHTTPServer 显式 daemon_threads=True,
shutdown 不被慢连接阻塞;handler 线程内的一切读写异常(BrokenPipe/
ConnectionReset)由 socketserver 捕获为单连接错误——server 进程不退出。
"""

from __future__ import annotations

import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dsn_guard import DsnConfigError, ensure_connect_timeout
from receiver import (HTTP_ACCEPTED, HTTP_BAD_REQUEST, HTTP_OK,
                      HTTP_TOO_LARGE, HTTP_UNAVAILABLE,
                      handle_webhook, healthz, MAX_BODY_BYTES,
                      connect_from_env, ReceiverError)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
REQUEST_TIMEOUT_LOW = 1
REQUEST_TIMEOUT_HIGH = 300
HTTP_REQUEST_TIMEOUT = 408


class ServerConfigError(Exception):
    """启动配置错误(稳定码;不含 secret/DSN)。"""


def parse_request_timeout(raw) -> int:
    """fail-closed 解析请求超时:None(未设置)=默认;显式提供的任何
    非整数/空白/越界值一律抛错,绝不静默修正。"""
    if raw is None:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    text = str(raw).strip()
    if text == "":
        raise ServerConfigError("REQUEST_TIMEOUT_INVALID",
                                "explicit value is blank")
    try:
        seconds = int(text)
    except (TypeError, ValueError):
        raise ServerConfigError(
            "REQUEST_TIMEOUT_INVALID",
            "not an integer: %r" % (raw,)) from None
    if not (REQUEST_TIMEOUT_LOW <= seconds <= REQUEST_TIMEOUT_HIGH):
        raise ServerConfigError(
            "REQUEST_TIMEOUT_INVALID",
            "value %d outside %d..%d" % (seconds, REQUEST_TIMEOUT_LOW,
                                         REQUEST_TIMEOUT_HIGH))
    return seconds


def _allowlist_from_env():
    raw = os.environ.get("GITHUB_REPO_ALLOWLIST", "").strip()
    if not raw:
        return None
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


class WebhookServer(ThreadingHTTPServer):
    # 显式 daemon 线程:慢/僵死连接不得阻塞进程退出与 shutdown。
    daemon_threads = True


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "mergepilot-gh-app/0.1"
    protocol_version = "HTTP/1.1"
    # StreamRequestHandler.timeout:setup() 会把它 settimeout 到 accepted
    # socket —— header 读取、body 读取与 413 排水全部受其约束。main() 启动
    # 时按环境配置覆写(默认 30s);测试直接设置类属性。
    timeout = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def log_message(self, fmt, *args):  # 绝不记录 body/签名/secret
        sys.stderr.write("[gh-app] %s - %s\n"
                         % (self.address_string(), fmt % args))

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _timeout_respond(self) -> None:
        """读取超时:408 + 强制关连接;不触 receiver/DB,不泄漏任何细节。"""
        self.close_connection = True
        self._respond(HTTP_REQUEST_TIMEOUT,
                      {"ok": False, "error": "request read timeout"})

    def do_GET(self):
        if self.path != "/healthz":
            self._respond(404, {"ok": False, "error": "not found"})
            return
        ok = healthz(connect_from_env)
        self._respond(HTTP_OK if ok else HTTP_UNAVAILABLE,
                      {"ok": ok} if ok else {"ok": False,
                                             "error": "database unavailable"})

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(HTTP_BAD_REQUEST, {"ok": False,
                                             "error": "bad content-length"})
            return
        if length > MAX_BODY_BYTES:
            # 超限:排水并丢弃 body(HTTP/1.1 keep-alive 下,不排水会让
            # 客户端阻塞在写体),然后 413 + 关连接(零业务 DB 写)。
            # 排水同样受 socket timeout 约束——慢速超限客户端 → 408。
            remaining = length
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except socket.timeout:
                self._timeout_respond()
                return
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return
            self.close_connection = True
            self._respond(HTTP_TOO_LARGE,
                          {"ok": False, "error": "body exceeds 2 MiB"})
            return
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except socket.timeout:
            self._timeout_respond()
            return
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        try:
            status, outcome, detail = handle_webhook(
                raw=raw,
                event_header=self.headers.get("X-GitHub-Event"),
                delivery_header=self.headers.get("X-GitHub-Delivery"),
                signature_header=self.headers.get("X-Hub-Signature-256"),
                secret=os.environ["GITHUB_WEBHOOK_SECRET"],
                connect=connect_from_env,
                allowlist=_allowlist_from_env())
            self._respond(status,
                          {"ok": status < 400, "outcome": outcome,
                           "detail": detail})
        except ReceiverError as exc:
            self._respond(exc.status, {"ok": False, "error": exc.code})
        except KeyError:
            self._respond(HTTP_UNAVAILABLE,
                          {"ok": False, "error": "receiver not configured"})
        except Exception:
            self._respond(HTTP_UNAVAILABLE,
                          {"ok": False, "error": "internal error"})


def main() -> int:
    for env in ("GITHUB_WEBHOOK_SECRET", "GITHUB_INGRESS_DSN"):
        if not os.environ.get(env):
            sys.stderr.write("[gh-app] missing required env %s\n" % env)
            return 3
    try:
        timeout_seconds = parse_request_timeout(
            os.environ.get("GH_APP_REQUEST_TIMEOUT_SECONDS"))
    except ServerConfigError as exc:
        sys.stderr.write("[gh-app] config error: %s\n" % exc)
        return 3
    WebhookHandler.timeout = timeout_seconds
    # §4 DSN 合同:结构化校验(缺 connect_timeout 补默认;非法值拒绝)。
    try:
        ensure_connect_timeout(os.environ["GITHUB_INGRESS_DSN"])
    except DsnConfigError as exc:
        sys.stderr.write("[gh-app] config error: %s\n" % exc)
        return 3
    bind = os.environ.get("GH_APP_BIND", "127.0.0.1")
    port = int(os.environ.get("GH_APP_PORT", "8090"))
    httpd = WebhookServer((bind, port), WebhookHandler)
    sys.stderr.write("[gh-app] listening on %s:%d (read timeout %ds)\n"
                     % (bind, port, timeout_seconds))
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
