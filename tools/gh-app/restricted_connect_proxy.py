#!/usr/bin/env python3
"""restricted_connect_proxy.py — 受限链式 CONNECT 代理(M8-GH-4B3 §3)。

冻结合同:
  * 只接受 CONNECT;普通 HTTP 方法一律 405 并关闭。
  * 目标必须**字节级精确**为 ``api.github.com:443``——大小写变体、
    尾点、userinfo(@)、scheme、路径、IP literal、其他端口全部 403。
  * 不自行解析 DNS、不自行直连公网;唯一上游为显式配置的
    **IP literal + 冻结端口 17890**(上游地址必须为 IP,禁止 hostname)。
  * 双向流复制带连接/读取/空闲超时。
  * SIGTERM:停止接受新连接,收割存量连接后退出。
  * 不记录 Authorization、请求正文、token 或上游响应正文。
  * 健康检查仅进程/TCP/配置自检;真实 GitHub 可达性属授权 E2E preflight。

部署形态:gh-proxy-r(仅 Reporter)与 gh-proxy-b(仅 MCP bridge),
两个实例来自同一镜像;环境变量区分上游与监听端口。
"""

from __future__ import annotations

import os
import re
import select
import signal
import socket
import sys
import threading
import time
from typing import Optional, Tuple

ALLOWED_TARGET = "api.github.com:443"
FROZEN_UPSTREAM_PORT = 17890
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
IDLE_TIMEOUT_SECONDS = 120.0
CHUNK = 65536

_IP_LITERAL_RE = re.compile(
    r"^(?:\d{1,3}\.){3}\d{1,3}$"
    r"|^\[(?:[0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}\]$")


class ProxyConfigError(Exception):
    """配置错误;detail 不含任何 secret(本模块无 secret)。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


def parse_connect_target(line: str) -> Tuple[Optional[str], Optional[int]]:
    """解析 CONNECT 请求行;目标必须字节级等于 ALLOWED_TARGET。

    返回 (host, port);拒绝时返回 (None, None)。"""
    parts = line.split()
    if len(parts) != 3 or parts[0] != "CONNECT":
        return None, None
    target = parts[1]
    if target != ALLOWED_TARGET:
        return None, None
    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        return None, None
    return host, int(port)


def _drain_headers(sock: socket.socket) -> Optional[str]:
    """读取到空行(头部结束);超时/超长返回 None。"""
    buf = b""
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    while b"\r\n\r\n" not in buf:
        if time.monotonic() > deadline or len(buf) > 16384:
            return None
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return None
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")


def _pipe_bidirectional(client: socket.socket,
                        upstream: socket.socket) -> None:
    """双向流复制,带空闲超时;任一侧关闭即结束。"""
    sockets = [client, upstream]
    last_active = time.monotonic()
    while sockets:
        if time.monotonic() - last_active > IDLE_TIMEOUT_SECONDS:
            break
        try:
            readable, _, _ = select.select(sockets, [], [], 5.0)
        except (OSError, ValueError):
            break
        if not readable:
            continue
        for src in readable:
            dst = upstream if src is client else client
            try:
                data = src.recv(CHUNK)
            except OSError:
                data = b""
            if not data:
                sockets.clear()
                break
            last_active = time.monotonic()
            try:
                dst.sendall(data)
            except OSError:
                sockets.clear()
                break


def handle_connection(client: socket.socket, upstream_ip: str,
                      upstream_port: int) -> None:
    """一个下游连接的完整生命周期。"""
    try:
        client.settimeout(READ_TIMEOUT_SECONDS)
        first = client.recv(4096)
        if not first:
            return
        # 只取首行(其余头部随 _drain_headers 一起消费)
        head = first.split(b"\r\n", 1)
        request_line = head[0].decode("utf-8", "replace")
        remainder = head[1] if len(head) > 1 else b""
        # 若首块未含完整头部,继续读
        while b"\r\n\r\n" not in first and remainder is not None:
            chunk = client.recv(4096)
            if not chunk:
                return
            first += chunk
            remainder = first.split(b"\r\n\r\n", 1)
            if len(remainder) == 2:
                remainder = remainder[1]
                break
        parts = request_line.split()
        if not parts or parts[0] != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n"
                           b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            return
        host, port = parse_connect_target(request_line)
        if host is None:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                           b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            return
        # 链式 CONNECT:向配置的上游(IP literal:17890)原样转发 CONNECT
        upstream = socket.create_connection((upstream_ip, upstream_port),
                                            timeout=CONNECT_TIMEOUT_SECONDS)
        try:
            upstream.sendall(("CONNECT %s:%d HTTP/1.1\r\n"
                              "Host: %s:%d\r\n\r\n"
                              % (host, port, host, port)).encode("ascii"))
            # 读取上游隧道建立响应(仅状态行;正文不解析不记录)
            status_line = b""
            while b"\r\n" not in status_line:
                chunk = upstream.recv(256)
                if not chunk:
                    break
                status_line += chunk
            if b" 200 " not in status_line:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                               b"Content-Length: 0\r\n"
                               b"Connection: close\r\n\r\n")
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n"
                           b"\r\n")
            _pipe_bidirectional(client, upstream)
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upstream.close()
    except OSError:
        pass
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()


def load_config(environ=None) -> dict:
    """严格配置:监听地址/端口 + 上游 IP literal + 冻结端口。"""
    env = os.environ if environ is None else environ
    listen_host = env.get("GH_PROXY_BIND", "0.0.0.0")
    listen_port_raw = env.get("GH_PROXY_PORT", "18090")
    upstream_ip = env.get("GH_PROXY_UPSTREAM_IP", "")
    upstream_port_raw = env.get("GH_PROXY_UPSTREAM_PORT",
                                str(FROZEN_UPSTREAM_PORT))
    if not listen_port_raw.isdigit() or not (1 <= int(listen_port_raw)
                                             <= 65535):
        raise ProxyConfigError("PROXY_CONFIG_INVALID",
                               "GH_PROXY_PORT must be 1..65535")
    if not upstream_port_raw.isdigit() or int(upstream_port_raw) \
            != FROZEN_UPSTREAM_PORT:
        raise ProxyConfigError("PROXY_CONFIG_INVALID",
                               "GH_PROXY_UPSTREAM_PORT must be exactly %d"
                               % FROZEN_UPSTREAM_PORT)
    if not _IP_LITERAL_RE.match(upstream_ip or ""):
        raise ProxyConfigError("PROXY_CONFIG_INVALID",
                               "GH_PROXY_UPSTREAM_IP must be an IP literal "
                               "(hostnames forbidden)")
    return {"bind": listen_host, "port": int(listen_port_raw),
            "upstream_ip": upstream_ip,
            "upstream_port": int(upstream_port_raw)}


def main() -> int:
    """独立进程入口。健康自检:配置合法 + 可绑定监听端口。"""
    try:
        config = load_config()
    except ProxyConfigError as exc:
        sys.stderr.write("[gh-proxy] %s: %s\n" % (exc.code, exc.detail))
        return 3
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((config["bind"], config["port"]))
    except OSError:
        sys.stderr.write("[gh-proxy] bind failed on %s:%d\n"
                         % (config["bind"], config["port"]))
        return 3
    server.listen(64)

    stopping = threading.Event()

    def _on_term(_signum, _frame):
        stopping.set()

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass

    sys.stderr.write("[gh-proxy] listening on %s:%d; upstream %s:%d "
                     "(target %s only)\n"
                     % (config["bind"], config["port"],
                        config["upstream_ip"], config["upstream_port"],
                        ALLOWED_TARGET))
    server.settimeout(1.0)
    threads = []
    while not stopping.is_set():
        try:
            client, _addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = threading.Thread(
            target=handle_connection,
            args=(client, config["upstream_ip"], config["upstream_port"]),
            daemon=True)
        t.start()
        threads.append(t)
        # 收割已结束线程,防列表无限增长
        threads = [t for t in threads if t.is_alive()]
    # SIGTERM:停止监听并等待存量连接(有界)
    server.close()
    deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
    for t in threads:
        t.join(max(0.1, deadline - time.monotonic()))
    sys.stderr.write("[gh-proxy] stopped\n")
    return 0


__all__ = [
    "ALLOWED_TARGET", "FROZEN_UPSTREAM_PORT", "ProxyConfigError",
    "parse_connect_target", "load_config", "handle_connection", "main",
]

if __name__ == "__main__":
    sys.exit(main())
