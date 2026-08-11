#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3B1.1 · Real-transport test stubs for the Docker socket proxy.

These stubs use REAL filesystem Unix sockets (socket.AF_UNIX bound to a temp
path) — NOT socket.socketpair. The full production path runs:

    ControllerStubClient
      → proxy listening socket (real .sock path)
      → proxy_transport.handle_connection (production code)
      → FakeUpstreamDaemon listening socket (real .sock path)
      → proxy_transport.forward_and_relay (production code)
      → ControllerStubClient

No real Docker / HiClaw / AgentTeams is contacted. AF_UNIX filesystem sockets
work on POSIX and on Windows 10 1803+ (the test host supports them).

Fixtures:
  - FakeUpstreamDaemon     : real Unix-socket HTTP server; records requests;
                              programmable responses (status/headers/body,
                              chunked, 101 hijack, disconnect, slow)
  - ControllerStubClient   : sends real HTTP/1.1 requests over a Unix socket
  - ProxyHarness           : binds the proxy listening socket, spawns the
                              production handler thread per connection, wires
                              the FakeUpstreamDaemon as upstream
  - InspectStub            : programmable inspect-body factory
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time

HICLAB_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "hiclab"))
if HICLAB_DIR not in sys.path:
    sys.path.insert(0, HICLAB_DIR)

import docker_socket_proxy as dsp  # noqa: E402
import proxy_transport as pt  # noqa: E402

# Transport selection: POSIX hosts have socket.AF_UNIX (real filesystem Unix
# socket, matching production). Windows Python (miniconda 3.9) lacks AF_UNIX
# but supports socket.socketpair() over AF_INET — we use that as the Windows
# transport. BOTH paths run the identical production handler code
# (proxy_transport.handle_connection); only the socket family differs.
HAS_AF_UNIX = hasattr(socket, "AF_UNIX")


# ---------------------------------------------------------------------------
# TransportBackend: dual-mode socket plumbing (AF_UNIX filesystem on POSIX,
# socketpair-brokered on Windows). Both run the SAME production handler.
# ---------------------------------------------------------------------------


class _Broker:
    """A tiny connection broker for the Windows (no-AF_UNIX) fallback.

    A broker holds a queue of pre-created socketpair server-ends. ``accept``
    pops one; the client side is obtained via ``connect``. This emulates a
    listening socket using socketpairs.
    """

    def __init__(self):
        self._q = []
        self._lock = threading.Lock()
        self._closed = False

    def offer(self, server_end):
        with self._lock:
            if self._closed:
                try:
                    server_end.close()
                except OSError:
                    pass
                return
            self._q.append(server_end)

    def accept(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._q:
                    return self._q.pop(0)
            time.sleep(0.01)
        raise OSError("broker accept timeout")

    def connect(self):
        """Create a socketpair, offer the server end, return the client end."""
        a, b = socket.socketpair()
        self.offer(b)
        return a

    def close(self):
        with self._lock:
            self._closed = True
            for s in self._q:
                try:
                    s.close()
                except OSError:
                    pass
            self._q = []


class TransportBackend:
    """Abstracts the socket family so the production handler runs on both
    POSIX (AF_UNIX filesystem) and Windows (socketpair broker).

    ``listen()`` returns a 'listener' object with ``accept()``; ``connect()``
    returns a connected client socket. On POSIX these use AF_UNIX; on Windows
    they use a socketpair broker (identical bytes-on-the-wire semantics for
    the HTTP the handler parses).
    """

    def __init__(self, label):
        self.label = label  # for debugging / audit
        self._broker = None if HAS_AF_UNIX else _Broker()
        self._listener_sock = None
        self._path = None

    def listen(self, path_hint):
        if HAS_AF_UNIX:
            self._path = path_hint
            try:
                os.unlink(path_hint)
            except OSError:
                pass
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(path_hint)
            s.listen(16)
            try:
                os.chmod(path_hint, 0o666)
            except OSError:
                pass
            self._listener_sock = s
            return s
        else:
            # Windows: no real listener; accept() pulls from the broker
            return self._broker

    def accept(self, timeout=10.0):
        """Accept one connection. POSIX: real accept(); Windows: broker.pop."""
        if HAS_AF_UNIX:
            if self._listener_sock is None:
                raise OSError("not listening")
            return self._listener_sock.accept()
        else:
            conn = self._broker.accept(timeout)
            return (conn, None)  # mirror accept() tuple shape

    def connect(self, path=None):
        if HAS_AF_UNIX:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            return s
        else:
            return self._broker.connect()

    def close(self):
        if self._listener_sock is not None:
            try:
                self._listener_sock.close()
            except OSError:
                pass
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
        if self._broker is not None:
            self._broker.close()


# ---------------------------------------------------------------------------
# FakeUpstreamDaemon — real Unix-socket HTTP server
# ---------------------------------------------------------------------------


class FakeUpstreamDaemon:
    """Listens via the TransportBackend (AF_UNIX on POSIX, broker on Windows);
    records requests; serves responses. The handler code is identical in both
    modes — only the socket family differs.

    Each accepted connection is handled in a thread that reads one HTTP
    request, records it, and replies with the next queued response (or a
    default 200/{}). Supports: hijack responses (101), chunked responses,
    immediate disconnect, and slow responses (for timeout tests).
    """

    def __init__(self, backend, sock_path):
        self.backend = backend
        self.sock_path = sock_path
        self.requests = []          # recorded requests (parsed)
        self.responses = []         # queued (status, headers, body, mode)
        self.default_response = (200, {"Content-Type": "application/json"},
                                 b"{}", "plain")
        self._lock = threading.Lock()
        self._running = False
        self._listener = None       # real socket (POSIX) or _Broker (Windows)
        self._thread = None

    def queue_response(self, status=200, body=b"{}", content_type="application/json",
                       extra_headers=None, mode="plain"):
        """Queue a response. mode: plain/chunked/hijack/disconnect/slow."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        headers = {"Content-Type": content_type}
        if extra_headers:
            headers.update(extra_headers)
        with self._lock:
            self.responses.append((status, headers, body, mode))

    def _next_response(self):
        with self._lock:
            if self.responses:
                return self.responses.pop(0)
            return self.default_response

    def start(self):
        if self._running:
            return
        self._listener = self.backend.listen(self.sock_path)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        # On POSIX, close the listener socket to unblock accept()
        if HAS_AF_UNIX and self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        # On Windows, close the broker
        if not HAS_AF_UNIX:
            self.backend.close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _accept_one(self):
        """Accept one connection via the backend (POSIX real accept / Windows broker)."""
        conn, _addr = self.backend.accept()
        return conn

    def _accept_loop(self):
        while self._running:
            try:
                conn = self._accept_one()
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True)
            t.start()

    def _handle(self, conn):
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
            if b"\r\n\r\n" not in buf:
                return
            head, _, rest = buf.partition(b"\r\n\r\n")
            head_text = head.decode("latin-1")
            lines = head_text.split("\r\n")
            req_line = lines[0] if lines else ""
            parts = req_line.split(" ", 2)
            method = parts[0] if len(parts) > 0 else ""
            path = parts[1] if len(parts) > 1 else ""
            cl = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        cl = 0
                    break
            body = rest[:cl]
            if len(body) < cl:
                try:
                    more = conn.recv(cl - len(body))
                    if more:
                        body += more
                except OSError:
                    pass
            try:
                parsed_body = json.loads(body) if body else None
            except (ValueError, UnicodeDecodeError):
                parsed_body = None
            with self._lock:
                self.requests.append({
                    "method": method, "path": path,
                    "body": parsed_body, "raw_body": body,
                })
            status, headers, body_bytes, mode = self._next_response()
            if mode == "disconnect":
                return
            if mode == "slow":
                time.sleep(30.0)
            if mode == "hijack":
                resp = (b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
                conn.sendall(resp)
                self._hijack_echo(conn)
                return
            reason = "OK" if 200 <= status < 300 else "ERROR"
            resp_line = "HTTP/1.1 %d %s\r\n" % (status, reason)
            headers = dict(headers)
            if mode == "chunked":
                headers["Transfer-Encoding"] = "chunked"
                headers.pop("Content-Length", None)
            else:
                headers["Content-Length"] = str(len(body_bytes))
            out = resp_line
            for k, v in headers.items():
                out += "%s: %s\r\n" % (k, v)
            out += "Connection: close\r\n\r\n"
            conn.sendall(out.encode("latin-1"))
            if mode == "chunked":
                conn.sendall(("%x\r\n" % len(body_bytes)).encode("ascii"))
                conn.sendall(body_bytes + b"\r\n")
                conn.sendall(b"0\r\n\r\n")
            else:
                if body_bytes:
                    conn.sendall(body_bytes)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _hijack_echo(self, conn):
        import select
        conn.settimeout(None)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                r, _, _ = select.select([conn], [], [], 1.0)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            try:
                conn.sendall(data)
            except OSError:
                return


# ---------------------------------------------------------------------------
# InspectStub — programmable inspect responses
# ---------------------------------------------------------------------------


class InspectStub:
    """Programmable inspect-body factory.

    D2B-3B1.2: by default the body carries the four authoritative labels
    (scope/run_id/agent/hardened) derived from the container name + the
    provided scope/run_id, so a "default" fixture is a VALID managed
    container. Tests that need a label-free or wrong-label fixture must
    explicitly pass ``labels={}`` (or wrong values) — there is no implicit
    label-free "valid" mode.
    """

    # Default authoritative scope/run_id match ProxyHarness defaults.
    DEFAULT_SCOPE = "test"
    DEFAULT_RUN_ID = "test-run-01"

    @staticmethod
    def body(name, labels=None, running=True, image="sha256:abc",
             scope=None, run_id=None, hardened="1", no_config=False,
             no_labels=False):
        """Build an inspect body.

        - ``labels`` (dict): if provided, used verbatim (overrides defaults;
          pass ``{}`` for an explicit label-free body).
        - ``scope``/``run_id``/``hardened``: override individual authoritative
          values (default to the harness's values).
        - ``no_config``: omit the Config key entirely (malformed body).
        - ``no_labels``: omit Config.Labels (Config present but no Labels).
        """
        import sys as _sys
        _hiclab = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..",
            "tools", "hiclab"))
        if _hiclab not in _sys.path:
            _sys.path.insert(0, _hiclab)
        import harden_policy as _hp
        agent = _hp.derive_agent_strict(name) or "manager"
        if labels is not None:
            cfg_labels = labels
        else:
            cfg_labels = {
                "com.mergepilot.scope": scope or InspectStub.DEFAULT_SCOPE,
                "com.mergepilot.run_id": run_id or InspectStub.DEFAULT_RUN_ID,
                "com.mergepilot.agent": agent,
                "com.mergepilot.hardened": hardened,
            }
        body = {
            "Name": name,
            "State": {"Running": running},
        }
        if no_config:
            return body  # no Config key -> inspect will DENY
        cfg = {"Image": image}
        if not no_labels:
            cfg["Labels"] = cfg_labels
        body["Config"] = cfg
        return body


def _decode_chunked(data):
    """Decode an HTTP chunked-transfer-encoded body into payload bytes.

    Parses ``size\r\n<data>\r\n`` frames until a 0-length chunk. Tolerant of
    trailing garbage. Returns the concatenated payload.
    """
    out = b""
    pos = 0
    while pos < len(data):
        nl = data.find(b"\r\n", pos)
        if nl == -1:
            break
        size_line = data[pos:nl]
        pos = nl + 2
        try:
            size = int(size_line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += data[pos:pos + size]
        pos += size + 2  # skip data + CRLF
    return out


# ---------------------------------------------------------------------------
# ControllerStubClient — real HTTP/1.1 over the transport backend
# ---------------------------------------------------------------------------


class ControllerStubClient:
    """Sends real HTTP/1.1 requests via the TransportBackend to the proxy."""

    def __init__(self, backend, proxy_path=None):
        self.backend = backend
        self.proxy_path = proxy_path

    def _round_trip(self, method, target, body=None, headers=None,
                    upgrade=False, raw_body=None):
        try:
            sock = self.backend.connect(self.proxy_path)
        except OSError as e:
            return (0, b"", e)
        sock.settimeout(10.0)
        if raw_body is not None:
            body_bytes = raw_body if isinstance(raw_body, bytes) else bytes(raw_body)
        elif body is not None:
            if isinstance(body, (dict, list)):
                body_bytes = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body_bytes = body.encode("utf-8")
            else:
                body_bytes = bytes(body)
        else:
            body_bytes = b""
        req = "%s %s HTTP/1.1\r\n" % (method, target)
        req += "Host: localhost\r\n"
        if body_bytes:
            req += "Content-Length: %d\r\n" % len(body_bytes)
            req += "Content-Type: application/json\r\n"
        if upgrade:
            req += "Connection: Upgrade\r\nUpgrade: tcp\r\n"
        else:
            req += "Connection: close\r\n"
        if headers:
            for k, v in headers.items():
                req += "%s: %s\r\n" % (k, v)
        req += "\r\n"
        try:
            sock.sendall(req.encode("latin-1") + body_bytes)
            resp = b""
            while b"\r\n\r\n" not in resp and len(resp) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b"\r\n\r\n" not in resp:
                return (0, resp, None)
            head, _, rbody = resp.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            try:
                status = int(status_line.split(" ")[1])
            except (IndexError, ValueError):
                status = 0
            if status == 101:
                sock.sendall(b"ping")
                import select as _sel
                _sel.select([sock], [], [], 2.0)
                try:
                    echoed = sock.recv(4096)
                except OSError:
                    echoed = b""
                return (status, head + b"\r\n\r\n" + rbody + echoed, None)
            # Determine body framing: Content-Length, chunked, or read-to-EOF
            cl = None
            is_chunked = False
            for line in head.decode("latin-1").split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        cl = None
                elif line.lower().startswith("transfer-encoding:") and \
                        "chunked" in line.lower():
                    is_chunked = True
            if is_chunked:
                # read until terminating 0-length chunk + EOF
                while b"\r\n0\r\n" not in rbody and b"\n0\r\n\r\n" not in rbody:
                    try:
                        chunk = sock.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    rbody += chunk
                # decode chunked body into payload bytes
                decoded = _decode_chunked(rbody)
                return (status, decoded, None)
            if cl is not None:
                while len(rbody) < cl:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    rbody += chunk
                return (status, rbody[:cl], None)
            # No Content-Length, not chunked: read until EOF
            while True:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                rbody += chunk
            return (status, rbody, None)
        except OSError as e:
            return (0, b"", e)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def get(self, target, headers=None):
        return self._round_trip("GET", target, headers=headers)

    def post(self, target, body=None, headers=None, upgrade=False,
             raw_body=None):
        return self._round_trip("POST", target, body=body, headers=headers,
                                upgrade=upgrade, raw_body=raw_body)

    def put(self, target, body=None, headers=None, raw_body=None):
        return self._round_trip("PUT", target, body=body, headers=headers,
                                raw_body=raw_body)

    def delete(self, target, headers=None):
        return self._round_trip("DELETE", target, headers=headers)

    def head(self, target, headers=None):
        return self._round_trip("HEAD", target, headers=headers)


# ---------------------------------------------------------------------------
# ProxyHarness — end-to-end production handler wiring via TransportBackend
# ---------------------------------------------------------------------------


class ProxyHarness:
    """End-to-end fixture running the production handler:

        ControllerStubClient
          → proxy listener (TransportBackend)
          → proxy_transport.handle_connection (production)
          → FakeUpstreamDaemon (TransportBackend)
          → production forward/relay
          → ControllerStubClient

    On POSIX this uses real AF_UNIX filesystem sockets (production-identical
    transport). On Windows it uses a socketpair broker (identical production
    handler code, different socket family). Neither path skips the handler.
    """

    def __init__(self, image_allowlist=('sha256:' + 'a' * 64,),
                 run_id='test-run-01', scope='test', name_profile='agentteams',
                 bind_allowlist=('/data',), upstream_reachable=True):
        self.image_allowlist = frozenset(image_allowlist)
        self.run_id = run_id
        self.scope = scope
        self.name_profile = name_profile
        self.bind_allowlist = frozenset(bind_allowlist)
        self.upstream_reachable = upstream_reachable
        self._td = None
        self.upstream_backend = TransportBackend("upstream")
        self.proxy_backend = TransportBackend("proxy")
        self.daemon = None
        self.server = None
        self.client = None
        self.proxy_sock_path = None
        self.upstream_sock_path = None
        self._proxy_listener = None
        self._accept_thread = None
        self._stop = False

    def __enter__(self):
        self._td = tempfile.mkdtemp(prefix="d2b3b1-")
        self.upstream_sock_path = os.path.join(self._td, "upstream.sock")
        self.proxy_sock_path = os.path.join(self._td, "proxy.sock")
        # Upstream daemon
        self.daemon = FakeUpstreamDaemon(self.upstream_backend,
                                         self.upstream_sock_path)
        if self.upstream_reachable:
            self.daemon.start()
        # Proxy config + server
        config = dsp.ProxyConfig(
            run_id=self.run_id, scope=self.scope,
            name_profile=self.name_profile,
            image_allowlist=self.image_allowlist,
            bind_allowlist=self.bind_allowlist,
            upstream_socket=self.upstream_sock_path,
            listen_socket=self.proxy_sock_path,
        )
        self.server = dsp.ProxyServer(config)
        # Bind proxy listener via backend
        self._proxy_listener = self.proxy_backend.listen(self.proxy_sock_path)
        self._stop = False
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self.client = ControllerStubClient(self.proxy_backend,
                                           self.proxy_sock_path)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop = True
        # Close proxy listener to unblock accept()
        if HAS_AF_UNIX and self._proxy_listener is not None:
            try:
                self._proxy_listener.close()
            except OSError:
                pass
        if not HAS_AF_UNIX:
            self.proxy_backend.close()
        if self._accept_thread:
            self._accept_thread.join(timeout=2.0)
        if self.daemon:
            self.daemon.stop()
        if HAS_AF_UNIX:
            self.upstream_backend.close()
            self.proxy_backend.close()
        try:
            if self.proxy_sock_path and os.path.exists(self.proxy_sock_path):
                os.unlink(self.proxy_sock_path)
        except OSError:
            pass
        import shutil
        if self._td and os.path.isdir(self._td):
            shutil.rmtree(self._td, ignore_errors=True)

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _addr = self.proxy_backend.accept()
            except OSError:
                break
            conn.settimeout(None)
            t = threading.Thread(
                target=self._safe_handle, args=(conn,), daemon=True)
            t.start()

    def _safe_handle(self, conn):
        try:
            pt.handle_connection(
                conn, self.server.config.upstream_socket,
                self.server.config, self.server.exec_registry,
                connect_fn=self._make_connect_fn())
        except Exception:
            pass  # handler is fail-closed internally

    def _make_connect_fn(self):
        """Build a connect_fn for the production handler.

        On POSIX this returns None (handler uses real AF_UNIX connect to the
        upstream filesystem socket). On Windows (no AF_UNIX) it returns a
        closure that pulls a fresh socketpair server-end from the upstream
        broker and returns the client-end — emulating a connect to the
        upstream. Both paths exercise the IDENTICAL production handler code.

        If the upstream is unreachable (``upstream_reachable=False``), the
        closure returns None — mirroring a real dockerd that refuses
        connection, so the handler fails-closed with 502.
        """
        if HAS_AF_UNIX:
            return None  # production path: real AF_UNIX connect
        if not self.upstream_reachable:
            def _connect_unreachable(upstream_socket, timeout):
                return None
            return _connect_unreachable
        backend = self.upstream_backend

        def _connect(upstream_socket, timeout):
            # upstream_socket is ignored on Windows; broker provides the conn
            return backend.connect()

        return _connect

    @property
    def upstream_request_count(self):
        return len(self.daemon.requests) if self.daemon else 0
