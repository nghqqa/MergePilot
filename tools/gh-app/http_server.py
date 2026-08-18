#!/usr/bin/env python3
"""http_server.py — GitHub webhook HTTP 入口 + healthz(M8-GH-1,stdlib-only)。

    POST /webhook  → receiver.handle_webhook(HMAC 验签 → INSERT-only)
    GET  /healthz  → SELECT 1 连接活性(无需表权限)

环境契约(秘密零日志/零 argv):
    GITHUB_WEBHOOK_SECRET  HMAC 密钥(必填)
    GITHUB_INGRESS_DSN     INSERT-only 角色连接串(必填)
    GITHUB_REPO_ALLOWLIST  逗号分隔 repo 列表(可选;receiver 侧早期 IGNORE)
    GH_APP_BIND / GH_APP_PORT  监听地址(默认 127.0.0.1:8090;隔离栈内由
                                编排注入 0.0.0.0)
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from receiver import (HTTP_ACCEPTED, HTTP_BAD_REQUEST, HTTP_OK,
                      HTTP_TOO_LARGE, HTTP_UNAVAILABLE,
                      handle_webhook, healthz, MAX_BODY_BYTES,
                      connect_from_env, ReceiverError)


def _allowlist_from_env():
    raw = os.environ.get("GITHUB_REPO_ALLOWLIST", "").strip()
    if not raw:
        return None
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "mergepilot-gh-app/0.1"
    protocol_version = "HTTP/1.1"

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
            # 超限:不读 body,直接 413(零 DB 写)。
            self._respond(HTTP_TOO_LARGE,
                          {"ok": False, "error": "body exceeds 2 MiB"})
            return
        raw = self.rfile.read(length) if length > 0 else b""
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
    bind = os.environ.get("GH_APP_BIND", "127.0.0.1")
    port = int(os.environ.get("GH_APP_PORT", "8090"))
    httpd = ThreadingHTTPServer((bind, port), WebhookHandler)
    sys.stderr.write("[gh-app] listening on %s:%d\n" % (bind, port))
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
