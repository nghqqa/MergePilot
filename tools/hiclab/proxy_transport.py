#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3B1.1 · Real Unix-socket HTTP transport for the Docker socket proxy.

This module implements the actual connection handling that the placeholder
``main()`` in docker_socket_proxy.py lacked:

  - accept a Unix socket connection
  - parse the HTTP request line + headers + query + body (Content-Length or
    chunked)
  - classify via classify_request (deny-by-default)
  - for nameprefix ops, do an AUTHORITATIVE upstream inspect to verify the
    target container actually belongs to this deployment (B11/name-spoof fix)
  - connect to the upstream (FakeUpstreamDaemon in tests, real dockerd in prod)
  - forward the (possibly transformed) request, with bounded body size
  - return the upstream status / headers / body to the client
  - handle 101 Upgrade / hijack by bidirectionally piping raw bytes
  - per-connection and per-request deadlines; fail-closed on ANY error

NO real Docker / HiClaw / AgentTeams is contacted by the tests; the handler
talks to whatever Unix socket path it is given as ``upstream_socket``.
"""
from __future__ import annotations

import errno
import json
import os
import select
import socket
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import docker_socket_proxy as dsp  # noqa: E402
import harden_policy as hp  # noqa: E402

# Per-request and per-connection deadlines (seconds).
CONN_DEADLINE = 30.0
REQ_HEADER_DEADLINE = 10.0
UPSTREAM_CONNECT_TIMEOUT = 5.0
UPSTREAM_RESPONSE_HEADER_TIMEOUT = 30.0
HIJACK_IDLE_TIMEOUT = 60.0

# Bounded reads. No unbounded io.readAll.
MAX_HEADER_BYTES = 64 * 1024          # headers must fit in 64 KiB
MAX_BODY_BYTES = dsp.MAX_BODY_BYTES   # 1 MiB request body (design D4.2)
MAX_RESPONSE_BYTES = dsp.MAX_RESPONSE_BYTES  # 16 MiB non-streaming response
MAX_INSPECT_BYTES = 1 * 1024 * 1024   # inspect body cap

HTTP_403 = b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
HTTP_502 = b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"


def _deny_resp(reason):
    body = json.dumps({"message": "denied: %s" % reason}).encode("utf-8")
    return HTTP_403 % (len(body), body)


def _bad_gateway(reason):
    body = json.dumps({"message": "bad gateway: %s" % reason}).encode("utf-8")
    return HTTP_502 % (len(body), body)


# ---------------------------------------------------------------------------
# HTTP request parsing (subset sufficient for Docker Engine API)
# ---------------------------------------------------------------------------


class ParsedRequest:
    __slots__ = ("method", "target", "path", "query", "version",
                 "headers", "body", "raw_target")

    def __init__(self):
        self.method = ""
        self.target = ""        # the request-target (path?query)
        self.raw_target = ""
        self.path = ""
        self.query = {}
        self.version = "HTTP/1.1"
        self.headers = {}       # lowercased keys
        self.body = b""

    @property
    def content_length(self):
        cl = self.headers.get("content-length")
        if cl is None:
            return None
        try:
            return int(cl)
        except ValueError:
            return -1  # malformed

    @property
    def upgrade(self):
        return self.headers.get("upgrade")

    @property
    def transfer_encoding(self):
        return (self.headers.get("transfer-encoding") or "").lower()


def _recv_until(sock, delimiter, max_bytes, deadline):
    """Read from ``sock`` until ``delimiter`` seen, deadline, or max_bytes.

    Returns (data_including_delimiter, leftover_after_delimiter, timed_out).
    On socket error returns (b'', b'', False) — caller treats as EOF.
    """
    buf = b""
    while True:
        if delimiter in buf:
            head, _, rest = buf.partition(delimiter)
            return (head + delimiter, rest, False)
        if len(buf) >= max_bytes:
            return (b"", b"", False)  # too big; signal as EOF
        now = time.monotonic()
        if now >= deadline:
            return (buf, b"", True)
        timeout = max(0.0, min(deadline - now, 1.0))
        try:
            r, _, _ = select.select([sock], [], [], timeout)
        except (OSError, ValueError):
            return (b"", b"", False)
        if not r:
            continue  # loop re-checks deadline
        try:
            chunk = sock.recv(4096)
        except OSError:
            return (b"", b"", False)
        if not chunk:
            return (buf, b"", False)  # EOF
        buf += chunk


def _read_n(sock, n, deadline):
    """Read exactly ``n`` bytes (for Content-Length bodies). Bounded."""
    buf = b""
    while len(buf) < n:
        now = time.monotonic()
        if now >= deadline:
            return None  # timed out
        timeout = max(0.0, min(deadline - now, 1.0))
        try:
            r, _, _ = select.select([sock], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not r:
            continue
        try:
            chunk = sock.recv(min(n - len(buf), 65536))
        except OSError:
            return None
        if not chunk:
            return None  # EOF before n bytes
        buf += chunk
    return buf


def _read_chunked(sock, deadline, max_bytes):
    """Read a chunked-transfer body up to max_bytes. Returns bytes or None."""
    body = b""
    while True:
        line, leftover, timed = _recv_until(sock, b"\r\n", 256, deadline)
        if not line:
            return None
        # strip any leftover we already consumed
        try:
            size_str = line.rstrip(b"\r\n").split(b";")[0]
            size = int(size_str, 16)
        except ValueError:
            return None
        if size == 0:
            # read trailing CRLF (after the chunk; trailers omitted)
            _trailer, _, _ = _recv_until(sock, b"\r\n", 256, deadline)
            return body
        if len(body) + size > max_bytes:
            return None
        # combine leftover + sock reads
        need = size + 2  # data + CRLF
        if len(leftover) >= need:
            data = leftover[:size]
            # the CRLF is leftover[size:size+2]; rest stays in socket — but
            # we can't push back, so read it into nothing (assume consumed)
        else:
            data_part = leftover + (_read_n(sock, size - len(leftover),
                                            deadline) or b"")
            if len(data_part) < size:
                return None
            data = data_part[:size]
            # consume the trailing CRLF
            crlf_need = 2
            crlf_have = len(data_part) - size
            if crlf_have < crlf_need:
                _read_n(sock, crlf_need - crlf_have, deadline)
        body += data


def parse_request(sock, deadline):
    """Parse one HTTP request from ``sock``. Returns ParsedRequest or None.

    Reads request line + headers (up to MAX_HEADER_BYTES), then the body
    (Content-Length bounded by MAX_BODY_BYTES, or chunked). On any parse
    failure / oversize / timeout, returns None (caller fails-closed).
    """
    head_blob, leftover, timed = _recv_until(sock, b"\r\n\r\n",
                                             MAX_HEADER_BYTES, deadline)
    if not head_blob:
        return None
    try:
        text = head_blob.decode("latin-1")
    except UnicodeDecodeError:
        return None
    lines = text.split("\r\n")
    if not lines:
        return None
    req_line = lines[0]
    parts = req_line.split(" ", 2)
    if len(parts) < 3:
        return None
    req = ParsedRequest()
    req.method, req.raw_target, req.version = parts[0], parts[1], parts[2]
    req.method = req.method.upper()
    req.target = req.raw_target
    # parse path + query
    parsed = urllib.parse.urlparse(req.raw_target)
    req.path = parsed.path or "/"
    qs = urllib.parse.parse_qs(parsed.query) if parsed.query else {}
    req.query = {k: v[0] if v else "" for k, v in qs.items()}
    # headers
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            return None
        k, _, v = line.partition(":")
        req.headers[k.strip().lower()] = v.strip()

    # body
    cl = req.content_length
    if cl is not None:
        if cl < 0 or cl > MAX_BODY_BYTES:
            return None  # malformed or oversized
        # consume leftover first
        if len(leftover) >= cl:
            req.body = leftover[:cl]
        else:
            need = cl - len(leftover)
            more = _read_n(sock, need, deadline)
            if more is None:
                return None
            req.body = leftover + more
    elif "chunked" in req.transfer_encoding:
        # leftover + chunked; we ignore leftover for chunked simplicity
        body = _read_chunked(sock, deadline, MAX_BODY_BYTES)
        if body is None:
            return None
        req.body = body
    else:
        req.body = b""  # no body
    return req


# ---------------------------------------------------------------------------
# Upstream connection + inspect (authoritative name/label verification)
# ---------------------------------------------------------------------------


def _connect_upstream(upstream_socket, timeout=UPSTREAM_CONNECT_TIMEOUT,
                      connect_fn=None):
    """Connect to the upstream Unix socket with a deadline. Returns socket or None.

    Production: uses socket.socket(AF_UNIX).connect(upstream_socket). The
    ``connect_fn`` parameter is for test injection (Windows hosts lacking
    AF_UNIX use a broker that produces an already-connected socket); when
    provided, it is called as ``connect_fn(upstream_socket, timeout)`` and
    must return a connected socket or None.
    """
    if connect_fn is not None:
        try:
            return connect_fn(upstream_socket, timeout)
        except OSError:
            return None
    if not hasattr(socket, "AF_UNIX"):
        return None  # fail-closed: cannot reach a real Unix-socket upstream
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(upstream_socket)
        return sock
    except OSError:
        sock.close()
        return None


def _decode_chunked_simple(data, max_bytes):
    """Decode a chunked HTTP body into payload bytes (simple parser).

    Used by upstream_inspect which needs the decoded body for JSON parsing.
    Reads ``size\r\n<data>\r\n`` frames until a 0-length chunk or data ends.
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
        if len(out) + size > max_bytes:
            break
        out += data[pos:pos + size]
        pos += size + 2  # skip data + CRLF
    return out


def upstream_inspect(upstream_socket, container_name, deadline, connect_fn=None):
    """Perform an authoritative GET /containers/{name}/json against upstream.

    Returns the parsed inspect dict, or None on any failure (not found,
    non-200, malformed JSON, oversize, timeout). The proxy uses this to
    verify a target container REALLY belongs to this deployment before
    allowing start/stop/delete/exec/archive on it (defends against the
    controller fabricating a name that happens to match the regex).
    """
    usock = _connect_upstream(upstream_socket, connect_fn=connect_fn)
    if usock is None:
        return None
    try:
        path = "/containers/%s/json" % urllib.parse.quote(container_name, safe="")
        req = ("GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
               % path).encode("latin-1")
        usock.sendall(req)
        head, leftover, _ = _recv_until(usock, b"\r\n\r\n", MAX_HEADER_BYTES,
                                        deadline)
        if not head:
            return None
        lines = head.decode("latin-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if len(status_parts) < 2 or status_parts[1] != "200":
            return None
        # Parse framing: dockerd may use Content-Length OR Transfer-Encoding:
        # chunked for inspect responses. Handle both.
        cl = None
        is_chunked = False
        for line in lines[1:]:
            ll = line.lower()
            if ll.startswith("content-length:"):
                try:
                    cl = int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
            elif ll.startswith("transfer-encoding:") and "chunked" in ll:
                is_chunked = True
        if is_chunked:
            # decode chunked body (leftover + further reads until EOF/0-chunk)
            raw = leftover
            # read more until we see the terminating 0-length chunk OR upstream
            # closes (Connection: close). Use recv loop that collects partial.
            while b"\r\n0\r\n" not in raw and len(raw) < MAX_INSPECT_BYTES:
                now = time.monotonic()
                if now >= deadline:
                    break
                timeout = max(0.0, min(deadline - now, 1.0))
                try:
                    r, _, _ = select.select([usock], [], [], timeout)
                except (OSError, ValueError):
                    break
                if not r:
                    continue
                try:
                    chunk = usock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break  # EOF (Connection: close)
                raw += chunk
            body = _decode_chunked_simple(raw, MAX_INSPECT_BYTES)
        elif cl is not None:
            if cl > MAX_INSPECT_BYTES:
                return None
            body = leftover
            if len(body) < cl:
                more = _read_n(usock, cl - len(body), deadline)
                if more is None:
                    return None
                body += more
            body = body[:cl]
        else:
            # No Content-Length, not chunked: read until EOF
            body = leftover
            while len(body) < MAX_INSPECT_BYTES:
                more = _read_n(usock, 4096, deadline)
                if not more:
                    break
                body += more
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
    finally:
        try:
            usock.close()
        except OSError:
            pass


def _inspect_authoritative(inspect_body, config, container_name):
    """Verify the upstream inspect body authorizes ``container_name``.

    D2B-3B1.2: ALL of the following must hold (fail-closed on any miss):
      - inspect_body is a dict (legal JSON object)
      - Name == container_name (exact)
      - Config exists and is a dict
      - Config.Labels exists and is a dict
      - com.mergepilot.scope   == config.scope
      - com.mergepilot.run_id  == config.run_id
      - com.mergepilot.hardened == "1"
      - com.mergepilot.agent   == derive_agent_strict(container_name)

    Missing ANY authoritative label -> DENY. There is no "labels optional"
    compatibility mode: every container the proxy allows MUST carry the four
    authoritative labels (injected by the create transform). Legacy label-free
    containers must be cleaned up or recreated; they are not auto-trusted.
    """
    if not isinstance(inspect_body, dict):
        return False
    name = inspect_body.get("Name", "")
    # Docker prefixes container names with "/" in the inspect Name field
    # (e.g. "/agentteams-worker-fixer"). Normalize by stripping a leading "/".
    if isinstance(name, str) and name.startswith("/"):
        name = name[1:]
    if not isinstance(name, str) or name != container_name:
        return False
    cfg = inspect_body.get("Config")
    if not isinstance(cfg, dict):
        return False
    labels = cfg.get("Labels")
    if not isinstance(labels, dict):
        return False
    # Exact-match all four authoritative labels (no generic allowlist).
    if labels.get("com.mergepilot.scope") != config.scope:
        return False
    if labels.get("com.mergepilot.run_id") != config.run_id:
        return False
    if labels.get("com.mergepilot.hardened") != "1":
        return False
    expected_agent = hp.derive_agent_strict(container_name)
    if expected_agent is None:
        return False  # unknown container name -> DENY
    if labels.get("com.mergepilot.agent") != expected_agent:
        return False
    return True


# ---------------------------------------------------------------------------
# Exec-create response parsing (register exec_id only on success)
# ---------------------------------------------------------------------------


def parse_exec_create_response(status, body_bytes):
    """From an upstream POST /containers/{name}/exec response, extract the
    exec ID. Returns the ID string, or None if the response is not a success
    (non-2xx), malformed, oversized, or missing the Id field.
    """
    if status < 200 or status >= 300:
        return None
    if not body_bytes or len(body_bytes) > 4096:
        return None
    try:
        obj = json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    eid = obj.get("Id")
    if not isinstance(eid, str) or not eid:
        return None
    # sanity: exec IDs are opaque hex-ish; reject obvious garbage
    if len(eid) > 128 or "\x00" in eid or "/" in eid:
        return None
    return eid


# ---------------------------------------------------------------------------
# Forwarding: issue the (possibly transformed) request upstream + relay reply
# ---------------------------------------------------------------------------


def _build_upstream_request(method, target, headers, body):
    """Re-serialize the request to send upstream. Drops hop-by-hop headers."""
    out = ("%s %s HTTP/1.1\r\n" % (method, target)).encode("latin-1")
    out += b"Host: localhost\r\n"
    out += b"Connection: close\r\n"
    if body:
        out += ("Content-Length: %d\r\n" % len(body)).encode("latin-1")
        ct = headers.get("content-type")
        if ct:
            out += ("Content-Type: %s\r\n" % ct).encode("latin-1")
    out += b"\r\n"
    if body:
        out += body
    return out


def forward_and_relay(client_sock, upstream_socket, req, decision,
                      conn_deadline, exec_registry=None,
                      response_body_sink=None, connect_fn=None):
    """Forward ``req`` upstream and relay the response back to the client.

    Handles:
      - bounded request body
      - upstream connect/parse/size failure -> 502 fail-closed
      - 101 Upgrade -> bidirectional hijack pipe
      - normal response -> status + headers + body relay
    Returns the upstream status code (for exec-create registration) or -1.

    If ``response_body_sink`` is a callable, the full response body (for
    non-streaming, non-hijack responses) is passed to it as bytes — used by
    the exec-create path to extract the exec Id for registry registration.

    ``connect_fn`` is for test injection (Windows broker); production leaves
    it None and uses real AF_UNIX connect.
    """
    # Build the upstream target: use the original request-target EXCEPT for
    # transform decisions, where we rewrite the body (path/method unchanged).
    body_to_send = req.body
    if decision.action == "transform" and decision.body is not None:
        # decision.body is the JSON dict post-transform; re-serialize
        try:
            body_to_send = json.dumps(decision.body).encode("utf-8")
        except (TypeError, ValueError):
            client_sock.sendall(_deny_resp("transform serialization failed"))
            return -1

    usock = _connect_upstream(upstream_socket, connect_fn=connect_fn)
    if usock is None:
        client_sock.sendall(_bad_gateway("upstream connect failed"))
        return -1

    try:
        up_req = _build_upstream_request(req.method, req.raw_target,
                                         req.headers, body_to_send)
        usock.sendall(up_req)

        # Read upstream response head
        head, leftover, _ = _recv_until(usock, b"\r\n\r\n", MAX_HEADER_BYTES,
                                        conn_deadline)
        if not head:
            client_sock.sendall(_bad_gateway("upstream no response"))
            return -1
        head_text = head.decode("latin-1")
        lines = head_text.split("\r\n")
        status_parts = lines[0].split(" ", 2)
        try:
            status = int(status_parts[1]) if len(status_parts) > 1 else 0
        except ValueError:
            status = 0

        # 101 Switching Protocols -> hijack: relay head + leftover, then pipe
        # bidirectionally until either side closes.
        if status == 101:
            client_sock.sendall(head + leftover)
            _hijack_pipe(client_sock, usock, conn_deadline)
            return 101

        # Parse Content-Length / chunked for the response body
        resp_cl = None
        resp_te = ""
        for line in lines[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            kl = k.strip().lower()
            if kl == "content-length":
                try:
                    resp_cl = int(v.strip())
                except ValueError:
                    resp_cl = None
            elif kl == "transfer-encoding":
                resp_te = v.strip().lower()

        captured_body = b""

        # Relay body, bounded. The body may have already started in ``leftover``
        # (bytes read after the \r\n\r\n head terminator) — incorporate it.
        if resp_cl is not None:
            client_sock.sendall(head)  # head only; body relayed below
            if resp_cl > MAX_RESPONSE_BYTES:
                return status  # stop relaying oversized
            body_data = leftover
            if len(body_data) < resp_cl:
                more = _read_n(usock, resp_cl - len(body_data), conn_deadline)
                if more:
                    body_data += more
            client_sock.sendall(body_data[:resp_cl])
            captured_body = body_data[:resp_cl]
        elif "chunked" in resp_te:
            client_sock.sendall(head)  # head only; chunked body relayed below
            captured_body = _relay_chunked_capture(
                usock, client_sock, conn_deadline, MAX_RESPONSE_BYTES,
                initial_buf=leftover)
        else:
            # No content-length, not chunked: relay head + leftover + until close
            client_sock.sendall(head + leftover)
            captured_body = _relay_until_close_capture(
                usock, client_sock, conn_deadline, MAX_RESPONSE_BYTES)

        if response_body_sink is not None and captured_body:
            try:
                response_body_sink(status, captured_body)
            except Exception:
                pass
        return status
    except OSError:
        return -1
    finally:
        try:
            usock.close()
        except OSError:
            pass


def _relay_chunked(usock, client_sock, deadline, max_bytes):
    return _relay_chunked_capture(usock, client_sock, deadline, max_bytes)


def _relay_until_close(usock, client_sock, deadline, max_bytes):
    return _relay_until_close_capture(usock, client_sock, deadline, max_bytes)


def _relay_chunked_capture(usock, client_sock, deadline, max_bytes,
                           initial_buf=b""):
    """Relay a chunked response body, bounded. Returns the decoded body bytes.

    ``initial_buf`` carries any body bytes that already arrived with the
    response head (bytes read after the \r\n\r\n terminator). Reads
    size-line + chunk-data + CRLF repeatedly. Captures the decoded payload
    (without chunk framing) for sinks like exec-create Id parsing.
    """
    total = 0
    captured = b""
    pending = bytes(initial_buf)  # bytes already buffered but not yet parsed
    while True:
        # Ensure pending contains at least the size line (up to CRLF)
        while b"\r\n" not in pending:
            now = time.monotonic()
            if now >= deadline:
                return captured
            try:
                r, _, _ = select.select([usock], [], [], min(deadline - now, 1.0))
            except (OSError, ValueError):
                return captured
            if not r:
                continue
            try:
                chunk = usock.recv(4096)
            except OSError:
                return captured
            if not chunk:
                return captured
            pending += chunk
        line, _, pending = pending.partition(b"\r\n")
        line = line + b"\r\n"
        client_sock.sendall(line)
        try:
            size = int(line.rstrip(b"\r\n").split(b";")[0], 16)
        except ValueError:
            return captured
        if size == 0:
            # terminating chunk: drain the trailing CRLF (and any trailers)
            # relay what we have left in pending (the final CRLF)
            if pending:
                client_sock.sendall(pending)
            # also read+relay until upstream closes (trailers / final CRLF)
            try:
                rest = _read_n(usock, 2, deadline)
                if rest:
                    client_sock.sendall(rest)
            except OSError:
                pass
            return captured
        total += size
        if total > max_bytes:
            return captured
        # need size bytes of data + 2 bytes CRLF
        need = size + 2
        while len(pending) < need:
            now = time.monotonic()
            if now >= deadline:
                return captured
            try:
                r, _, _ = select.select([usock], [], [], min(deadline - now, 1.0))
            except (OSError, ValueError):
                return captured
            if not r:
                continue
            try:
                chunk = usock.recv(4096)
            except OSError:
                return captured
            if not chunk:
                return captured
            pending += chunk
        buf = pending[:need]
        pending = pending[need:]
        payload = buf[:size]
        client_sock.sendall(buf)
        captured += payload


def _relay_until_close_capture(usock, client_sock, deadline, max_bytes):
    """Relay bytes from usock to client until EOF/deadline/limit. Returns bytes."""
    total = 0
    captured = b""
    while True:
        now = time.monotonic()
        if now >= deadline:
            return captured
        timeout = max(0.0, min(deadline - now, 1.0))
        try:
            r, _, _ = select.select([usock], [], [], timeout)
        except (OSError, ValueError):
            return captured
        if not r:
            continue
        try:
            chunk = usock.recv(65536)
        except OSError:
            return captured
        if not chunk:
            return captured
        total += len(chunk)
        if total > max_bytes:
            send_chunk = chunk[:max_bytes - (total - len(chunk))]
            client_sock.sendall(send_chunk)
            captured += send_chunk
            return captured
        client_sock.sendall(chunk)
        captured += chunk


def _hijack_pipe(client_sock, usock, deadline):
    """Bidirectionally pipe bytes between client and upstream (101 hijack).

    Runs until either side closes or HIJACK_IDLE_TIMEOUT of inactivity.
    """
    last_activity = time.monotonic()
    socks = [client_sock, usock]
    while True:
        now = time.monotonic()
        if now - last_activity > HIJACK_IDLE_TIMEOUT:
            return
        timeout = max(0.0, HIJACK_IDLE_TIMEOUT - (now - last_activity))
        try:
            r, _, _ = select.select(socks, [], [], min(timeout, 1.0))
        except (OSError, ValueError):
            return
        if not r:
            continue
        for s in r:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            last_activity = time.monotonic()
            other = usock if s is client_sock else client_sock
            try:
                other.sendall(data)
            except OSError:
                return


# ---------------------------------------------------------------------------
# Connection handler (the real per-connection entry point)
# ---------------------------------------------------------------------------


def handle_connection(client_sock, upstream_socket, config, exec_registry,
                      conn_deadline=None, connect_fn=None):
    """Handle one client connection: parse, classify, forward, relay.

    Fail-closed: any error -> 403/502 and close. Never degrades to passthrough.
    ``connect_fn`` is for test injection (Windows broker); production leaves
    it None and uses real AF_UNIX connect.
    """
    if conn_deadline is None:
        conn_deadline = time.monotonic() + CONN_DEADLINE

    try:
        req = parse_request(client_sock, min(conn_deadline,
                                             time.monotonic() + REQ_HEADER_DEADLINE))
        if req is None:
            client_sock.sendall(_deny_resp("request parse failed"))
            return

        target_header = req.upgrade
        decision = dsp.classify_request(
            req.method, req.raw_target, config, exec_registry,
            body=_try_json(req.body), query=req.query,
            target_header=target_header)

        if decision.action == "deny":
            client_sock.sendall(_deny_resp(decision.reason))
            return

        if decision.action == "transform":
            # Re-run deny evaluation on the parsed body (defense in depth)
            body_obj = _try_json(req.body)
            if body_obj is None:
                client_sock.sendall(_deny_resp("transform: body not JSON"))
                return
            # D2B-3B2 fix: the container name comes from the ?name= query
            # parameter (decision.name), NOT from the request body's Name
            # field (which the controller leaves empty). Set it here so
            # apply_hardening_v2 can derive the agent correctly via
            # derive_agent_strict(decision.name). Without this, the injected
            # com.mergepilot.agent label would be wrong, causing subsequent
            # authoritative inspects to fail.
            body_obj["Name"] = decision.name
            deny_reason = hp.evaluate_deny(body_obj, config)
            if deny_reason:
                client_sock.sendall(_deny_resp(deny_reason))
                return
            decision.body = hp.apply_hardening_v2(
                body_obj, _kind_for(decision), config.hardening_config())

        # AUTHORITATIVE INSPECT for nameprefix ops (req 8 + D2B-3B1.2).
        # Covers ALL decisions that target a named container:
        #   - start/stop/json/archive (reason "containers/<op>")
        #   - delete (reason "delete container")
        #   - exec-create (reason "exec create") — D2B-3B1.2: exec-create now
        #     MUST pass authoritative inspect before the exec-create request
        #     is forwarded upstream. If inspect fails, the proxy returns 403
        #     and the FakeUpstreamDaemon receives ONLY the inspect (never the
        #     exec-create). This closes the gap where exec could target a
        #     container the proxy never verified.
        _needs_inspect = (
            decision.action == "allow"
            and decision.name
            and (decision.reason.startswith("containers/")
                 or decision.reason == "delete container"
                 or decision.reason == "exec create")
        )
        if _needs_inspect:
            inspect_body = upstream_inspect(upstream_socket, decision.name,
                                            conn_deadline, connect_fn=connect_fn)
            if not _inspect_authoritative(inspect_body, config, decision.name):
                client_sock.sendall(_deny_resp(
                    "authoritative inspect failed for %s" % decision.name[:32]))
                return

        # Exec-create response capture: parse the Id from the upstream response
        # and register it so subsequent /exec/{id}/start|json can be authorized.
        exec_sink = None
        if decision.reason == "exec create" and exec_registry is not None:
            def _sink(status, body_bytes, _name=decision.name, _reg=exec_registry):
                eid = parse_exec_create_response(status, body_bytes)
                if eid:
                    _reg.register(eid, _name)
            exec_sink = _sink

        # Forward + relay
        status = forward_and_relay(client_sock, upstream_socket, req, decision,
                                   conn_deadline, exec_registry,
                                   response_body_sink=exec_sink,
                                   connect_fn=connect_fn)
    except Exception:
        # fail-closed: never leak an exception to the client as a 500 passthrough
        try:
            client_sock.sendall(_deny_resp("internal error (fail-closed)"))
        except OSError:
            pass
    finally:
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client_sock.close()
        except OSError:
            pass


def _try_json(body_bytes):
    if not body_bytes:
        return None
    try:
        return json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError):
        return None


def _kind_for(decision):
    """Derive the worker/manager kind for transform from the decision name.

    D2B-3B1.2: uses the unified ``derive_agent_strict`` so transform and
    inspect share one derivation path. Returns "worker" for any worker name,
    "manager" for manager names; falls back to "worker" if indeterminate
    (classify already rejected unknown names, so this is defensive).
    """
    agent = hp.derive_agent_strict(decision.name or "")
    if agent == "manager":
        return "manager"
    return "worker"
