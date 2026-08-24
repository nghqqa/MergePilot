#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows-side loopback publication edge for the MergePilot preview.

The console-edge and gh-webhook publish their ports INSIDE the WSL
distro (PUBLISH_BIND backend). WSL2's own localhost forwarding is not
reliable (NAT mode + system proxies drop it — the preview.2 blocker),
so this process is the explicit Windows publication edge:

  - listens ONLY on 127.0.0.1 (Windows loopback), never 0.0.0.0;
  - forwards the two FIXED ports (8600 -> 8600, 8090 -> 8090) to the
    selected WSL distro's eth0 address — no arbitrary targets, no
    credentials, no open-proxy capability: destinations are compiled
    from the fixed port map and the single distro parameter;
  - re-derives the WSL IP when connections start failing (WSL VM IPs
    change across reboots), with a bounded refresh interval;
  - writes an identity file (pid/name/distro/token/purpose/ports) so
    Stop/Cleanup can verify process identity and the launch token
    before terminating — never killing an unrelated process.

The edge's own security posture is unchanged: console-edge still
enforces its Host allowlist (127.0.0.1/localhost), GET-only paths,
and no header forwarding — this is raw TCP passthrough, and those
inner guards see the client's original Host header.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time

LISTEN_HOST = "127.0.0.1"
# fixed port map: Windows loopback port -> distro-side port (same)
FORWARD_PORTS = ((8600, 8600), (8090, 8090))
CONNECT_TIMEOUT = 5.0
PUMP_BUFFER = 65536
IP_MAX_AGE = 30.0


def derive_wsl_ip(distro: str) -> str:
    """The distro's primary address (reachable from the Windows host
    through the WSL NAT). Fails closed with a stable message."""
    try:
        cp = subprocess.run(
            ["wsl.exe", "-d", distro, "--exec", "/bin/sh", "-c",
             "hostname -I"],
            capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("WSL_IP_DERIVE_FAILED: %s" % exc) from None
    out = cp.stdout.decode("utf-8", "replace").replace("\x00", "")
    for token in out.split():
        if not token.startswith("127."):
            return token
    raise RuntimeError("WSL_IP_DERIVE_FAILED: no address in %r" % out[:60])


class Forwarder:
    """Raw TCP 127.0.0.1:<port> -> <distro-ip>:<port> for the fixed
    port map. One accept thread per port; one pump thread per
    connection; both directions forwarded verbatim."""

    def __init__(self, distro: str):
        self._distro = distro
        self._ip = None
        self._ip_at = 0.0
        self._ip_lock = threading.Lock()

    def _current_ip(self, force: bool = False) -> str:
        with self._ip_lock:
            now = time.monotonic()
            if self._ip is None or force or now - self._ip_at > IP_MAX_AGE:
                self._ip = derive_wsl_ip(self._distro)
                self._ip_at = now
            return self._ip

    def _connect_upstream(self, target_port: int) -> socket.socket:
        ip = self._current_ip()
        try:
            return socket.create_connection(
                (ip, target_port), timeout=CONNECT_TIMEOUT)
        except OSError:
            # WSL IP may have rotated — re-derive once (bounded) and
            # retry before giving up on this connection
            ip = self._current_ip(force=True)
            return socket.create_connection(
                (ip, target_port), timeout=CONNECT_TIMEOUT)

    def _pump(self, client: socket.socket, target_port: int) -> None:
        try:
            upstream = self._connect_upstream(target_port)
        except OSError as exc:
            sys.stderr.write("forward connect to :%d failed: %s\n"
                             % (target_port, exc))
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            while True:
                readable, _, _ = select.select([client, upstream], [], [],
                                               60)
                if not readable:
                    return
                for src in readable:
                    data = src.recv(PUMP_BUFFER)
                    if not data:
                        return
                    dst = upstream if src is client else client
                    dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (client, upstream):
                try:
                    s.close()
                except OSError:
                    pass

    def serve(self) -> None:
        servers = []
        for listen_port, target_port in FORWARD_PORTS:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind((LISTEN_HOST, listen_port))
            except OSError as exc:
                raise RuntimeError(
                    "WINDOWS_LOOPBACK_BIND_FAILED: %s:%d (%s) — another "
                    "process owns the port or loopback is unavailable"
                    % (LISTEN_HOST, listen_port, exc)) from None
            srv.listen(16)
            servers.append((srv, target_port))
        sys.stderr.write("[forwarder] listening on %s for %s (distro=%s)"
                         "\n" % (LISTEN_HOST, FORWARD_PORTS, self._distro))
        sys.stderr.flush()
        while True:
            for srv, target_port in servers:
                srv.settimeout(0.25)
                try:
                    client, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    time.sleep(0.5)
                    continue
                threading.Thread(target=self._pump,
                                 args=(client, target_port),
                                 daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distro", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--identity-file", required=True)
    args = parser.parse_args()
    forwarder = Forwarder(args.distro)
    try:
        # pre-derive the IP so a dead distro fails BEFORE we grab the
        # loopback ports (fail-closed, no half-started edge)
        forwarder._current_ip()
    except RuntimeError as exc:
        sys.stderr.write("[forwarder] %s\n" % exc)
        return 3
    # bind ports first, then publish identity (the identity file only
    # ever describes a fully-listening edge)
    for listen_port, _ in FORWARD_PORTS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((LISTEN_HOST, listen_port))
        except OSError as exc:
            sys.stderr.write("[forwarder] WINDOWS_LOOPBACK_BIND_FAILED: "
                             "%s:%d (%s)\n" % (LISTEN_HOST, listen_port,
                                               exc))
            return 4
        finally:
            probe.close()
    identity = {
        "pid": os.getpid(),
        "name": "python",
        "distro": args.distro,
        "token": args.token,
        "purpose": "mergepilot-loopback-publication",
        "ports": [p for p, _ in FORWARD_PORTS],
        "listen_host": LISTEN_HOST,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(args.identity_file, "w", encoding="utf-8") as fh:
        json.dump(identity, fh)
    forwarder.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
