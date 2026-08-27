#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WSL-side Matrix transport for the CLI prerequisite membership probe.

When the configured ``matrix_homeserver`` is not reachable from the Windows
host (WSL2 NAT / hardened iptables), the prerequisite membership probe and
the later lifecycle ``matrix_members_provider`` must observe the SAME
vantage: an HTTP request executed INSIDE the authorized distro.

Security contract:
- The Authorization header (and any secret) travels ONLY via the stdin JSON
  document piped to the in-distro runner — never argv, logs or diagnostics.
- The runner refuses any URL whose scheme/netloc does not match the
  allowlist derived from the validated config (anti-SSRF, prefix-free).
- Timeout is fail-closed: a dead/slow homeserver yields an exception, never
  a fabricated member list.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import urllib.parse
from typing import Optional, Callable

AUTHORIZED_DISTRO = "MergePilot-Test"
MATRIX_HOST_PORT_DEFAULT = 6167
_RUNNER_TIMEOUT_S = 12

_RUNNER = (
    "import sys,json,urllib.request,urllib.parse,urllib.error\n"
    "req=json.loads(sys.stdin.readline())\n"
    "u=urllib.parse.urlparse(req['url'])\n"
    "assert u.scheme=='http' and u.netloc==req['allow_netloc'], 'SSRF'\n"
    "assert u.path.startswith('/_matrix/'), 'PATH'\n"
    "r=urllib.request.Request(req['url'],method=req['method'])\n"
    "for k,v in (req.get('headers') or {}).items(): r.add_header(k,v)\n"
    "data=(req['body'].encode() if isinstance(req.get('body'),str) else None)\n"
    "try:\n"
    "    with urllib.request.urlopen(r,data=data,timeout=req.get('timeout',10)) as resp:\n"
    "        print(json.dumps({'status':resp.status,'body':resp.read().decode('utf-8','replace')}))\n"
    "except urllib.error.HTTPError as e:\n"
    "    print(json.dumps({'status':e.code,'body':e.read().decode('utf-8','replace')}))\n"
)


class MatrixTransportUnavailable(RuntimeError):
    """Raised (fail-closed) when the WSL-side matrix request cannot be made."""


def _runner_payload(method: str, url: str, headers: dict, body, allow_netloc: str) -> str:
    return json.dumps({"method": method, "url": url, "headers": headers or {},
                       "body": body, "allow_netloc": allow_netloc,
                       "timeout": 10})


def validate_matrix_target(url: str, allow_netloc: str) -> None:
    """Anti-SSRF pre-check on the caller side (mirrors the in-distro runner).

    Raises MatrixTransportUnavailable when the target is not the configured
    homeserver — no subprocess is spawned for a disallowed URL.
    """
    u = urllib.parse.urlparse(url or "")
    if u.scheme != "http" or u.netloc != allow_netloc:
        raise MatrixTransportUnavailable(
            "MATRIX_TRANSPORT_TARGET_REJECTED: url outside configured homeserver")
    if not u.path.startswith("/_matrix/"):
        raise MatrixTransportUnavailable(
            "MATRIX_TRANSPORT_TARGET_REJECTED: path outside /_matrix/")


def wsl_matrix_request(method: str, url: str, headers: dict = None,
                       body: str = None, *, allow_netloc: str,
                       distro: str = AUTHORIZED_DISTRO,
                       exec_fn=None, timeout: int = _RUNNER_TIMEOUT_S):
    """Run one Matrix client request inside the distro. Returns (status, body_str)."""
    validate_matrix_target(url, allow_netloc)
    payload = _runner_payload(method, url, headers or {}, body, allow_netloc)
    argv = ["wsl.exe", "-u", "root", "-d", distro, "--",
            "python3", "-c", _RUNNER]
    stdin_bytes = (payload + "\n").encode("utf-8")
    if exec_fn is not None:
        try:
            cp = exec_fn(argv, input_bytes=stdin_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MatrixTransportUnavailable(
                "MATRIX_TRANSPORT_RUNNER_TIMEOUT") from None
        out = cp.stdout or ""
        rc = cp.returncode
    else:
        try:
            cp = subprocess.run(argv, input=stdin_bytes, capture_output=True,
                                text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MatrixTransportUnavailable(
                "MATRIX_TRANSPORT_RUNNER_TIMEOUT") from None
        out = cp.stdout or ""
        rc = cp.returncode
    # First JSON line = runner result; leading wsl.exe noise is tolerated.
    line = ""
    for cand in out.splitlines():
        cand = cand.strip()
        if cand.startswith("{"):
            line = cand
            break
    if rc != 0 and not line:
        raise MatrixTransportUnavailable("MATRIX_TRANSPORT_RUNNER_FAILED")
    try:
        parsed = json.loads(line) if line else {}
    except ValueError:
        raise MatrixTransportUnavailable("MATRIX_TRANSPORT_RUNNER_MALFORMED") from None
    return int(parsed.get("status", 0)), str(parsed.get("body", ""))


def homeserver_netloc(config: dict) -> str:
    u = urllib.parse.urlparse((config or {}).get("matrix_homeserver", ""))
    return u.netloc


def normalize_homeserver_for_wsl_host(config: dict) -> str:
    """Rewrite the configured homeserver URL to the WSL-host vantage.

    The pinned container IP (e.g. 172.25.0.2:6167) is reachable from the
    distro itself but NOT from the Windows host; the WSL-host python3 CAN
    reach it directly. Same scheme/host/port, executed in-distro.
    """
    hs = (config or {}).get("matrix_homeserver", "").rstrip("/")
    u = urllib.parse.urlparse(hs)
    return hs  # host/port preserved; vantage change only (runner side)


class WslMatrixTransport:
    """Callable transport compatible with fetch_matrix_joined_mxids(transport=…)."""

    def __init__(self, config: dict, distro: str = AUTHORIZED_DISTRO,
                 exec_fn=None):
        self.allow_netloc = homeserver_netloc(config)
        self.distro = distro
        self._exec_fn = exec_fn

    def __call__(self, method: str, url: str, headers: dict = None,
                 body=None, timeout: int = 10):
        # fetch_matrix_joined_mxids calls transport("GET", url, headers=…, body=None)
        validate_matrix_target(url, self.allow_netloc)
        return wsl_matrix_request(method, url, headers=headers, body=body,
                                  allow_netloc=self.allow_netloc,
                                  distro=self.distro, timeout=timeout,
                                  exec_fn=self._exec_fn)


_current = None


def set_current_transport(t):
    global _current
    _current = t


def current_transport():
    return _current


def ensure_matrix_transport(config: dict, host_probe_fn=None,
                            smoke_exec_fn=None) -> Optional:
    """Return None when the host can reach the homeserver directly; otherwise
    a WSL transport. ``host_probe_fn`` defaults to a urllib versions-probe.
    The chosen transport is cached so the initial prerequisite probe and the
    later lifecycle membership provider share one vantage."""
    global _current
    if _current is not None:
        return _current

    def _default_probe():
        hs = (config or {}).get("matrix_homeserver", "").rstrip("/")
        rq = urllib.request.Request(hs + "/_matrix/client/versions")
        # never route localhost through an ambient system proxy
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(rq, timeout=4) as r:
            r.read()

    probe = host_probe_fn or _default_probe
    try:
        probe()
        return None                      # host vantage works: keep None
    except Exception:
        pass
    try:
        _current = WslMatrixTransport(config, exec_fn=smoke_exec_fn)
        # fail-closed smoke test: an unreachable homeserver must NOT yield a
        # usable transport (it would fake an empty member list downstream).
        st, _ = _current("GET", (config or {}).get("matrix_homeserver", "").rstrip("/")
                         + "/_matrix/client/versions")
        if st == 0:
            raise MatrixTransportUnavailable("smoke failed")
        return _current
    except Exception:
        _current = None
        return None
