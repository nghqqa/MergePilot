#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3B2 · Isolated smoke test: real proxy ↔ real isolated dockerd.

Runs INSIDE MergePilot-Test WSL (as root) against the isolated dockerd at
/var/run/docker.sock. The proxy binds /run/mp/docker.sock; a controller stub
client talks to the proxy; we assert the proxy correctly:
  - forwards the 13 SOURCE_PROVEN endpoints to the real dockerd
  - transforms create bodies (restart=no, authoritative labels injected)
  - denies forbidden endpoints (logs/stats/events/version/etc)
  - enforces strict query (stop?t=10, delete?force=true)
  - enforces B11 archive path allowlist
  - fails-closed when upstream is unreachable

A small image (busybox) is used for the container lifecycle so the smoke does
NOT require pulling the full AgentTeams v1.2.2 controller image — the proxy's
contract is image-agnostic (it validates digests, not image contents).

Usage (from inside WSL as root):
    python3 /mnt/d/goai/mergepilot-os/tests/hiclab/d2b3b2_isolated_smoke.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

HICLAB = "/mnt/d/goai/mergepilot-os/tools/hiclab"
sys.path.insert(0, HICLAB)
sys.path.insert(0, "/mnt/d/goai/mergepilot-os/tests/hiclab")

import docker_socket_proxy as dsp
import harden_policy as hp
import proxy_transport as pt

UPSTREAM = "/var/run/docker.sock"        # real isolated dockerd
LISTEN = "/run/mp/docker.sock"           # proxy's filtered socket
RUN_ID = "b2smoke-0001"
SCOPE = "test"

# busybox digest (confirmed present in the isolated dockerd via
# `docker images --digests`). The proxy's image allowlist is digest-only.
BUSYBOX_DIGEST = "sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616"
# the create body uses Image = name@digest form (valid Docker reference)
BUSYBOX_IMG = "busybox@%s" % BUSYBOX_DIGEST


def docker(*args):
    """Run a docker CLI command (root), return (rc, stdout)."""
    r = subprocess.run(["docker"] + list(args),
                       capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def http_roundtrip(sock_path, method, target, body=None, upgrade=False):
    """Send one HTTP request to the proxy socket, return (status, body_str)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15.0)
    s.connect(sock_path)
    if body is not None and not isinstance(body, (bytes, bytearray)):
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) \
            else str(body).encode()
    elif body is None:
        body = b""
    req = "%s %s HTTP/1.1\r\nHost: localhost\r\n" % (method, target)
    if body:
        req += "Content-Length: %d\r\nContent-Type: application/json\r\n" % len(body)
    req += "Upgrade: tcp\r\nConnection: Upgrade\r\n\r\n" if upgrade \
        else "Connection: close\r\n\r\n"
    s.sendall(req.encode() + body)
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        c = s.recv(4096)
        if not c:
            break
        buf += c
    if b"\r\n\r\n" not in buf:
        s.close()
        return (0, buf.decode("latin-1", "replace"))
    head, _, rbody = buf.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ")[1])
    except (IndexError, ValueError):
        status = 0
    cl = 0
    for line in head.decode("latin-1").split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try:
                cl = int(line.split(":", 1)[1].strip())
            except ValueError:
                cl = 0
            break
    while len(rbody) < cl:
        c = s.recv(4096)
        if not c:
            break
        rbody += c
    s.close()
    return (status, rbody[:cl].decode("utf-8", "replace"))


def main():
    results = []
    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(("  PASS " if cond else "  FAIL ") + name + ("  " + detail if detail and not cond else ""))

    # record pre-smoke volume count (orphan volumes from prior runs are not ours)
    rc, out = docker("volume", "ls", "--format", "{{.Name}}")
    pre_volumes = set(n for n in out.strip().split("\n") if n)

    # ---------- start the proxy ----------
    print("=== starting proxy (root, /run/mp/docker.sock → /var/run/docker.sock) ===")
    # clean any stale socket/marker
    for p in (LISTEN, "/etc/hiclab/proxy-deployed"):
        try:
            os.unlink(p)
        except OSError:
            pass
    os.makedirs("/run/mp", exist_ok=True)
    os.makedirs("/etc/hiclab", exist_ok=True)

    config = dsp.ProxyConfig(
        run_id=RUN_ID, scope=SCOPE, name_profile="agentteams",
        image_allowlist=(BUSYBOX_DIGEST,),
        upstream_socket=UPSTREAM, listen_socket=LISTEN,
    )
    server = dsp.ProxyServer(config)
    ok, reason = server.startup_self_check()
    check("proxy self-check", ok, reason)
    if not ok:
        return _finish(results, False)

    server._sock = dsp.bind_listening_socket(LISTEN)
    import threading
    stop = [False]
    def accept_loop():
        while not stop[0]:
            try:
                conn, _ = server._sock.accept()
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(
                target=lambda c=conn: pt.handle_connection(
                    c, UPSTREAM, config, server.exec_registry),
                daemon=True).start()
    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    check("proxy listening socket exists", os.path.exists(LISTEN))
    check("proxy socket mode 0600", (os.lstat(LISTEN).st_mode & 0o777) == 0o600)

    # marker
    ok = server.arm_marker()
    check("marker written", ok)
    if ok:
        with open("/etc/hiclab/proxy-deployed", "rb") as f:
            mc = f.read()
        check("marker has pid line", b"pid=" in mc)
        check("marker has digest line", b"digest=" in mc)

    # ---------- POSITIVE ----------
    print("\n=== POSITIVE smoke ===")

    # 1. /_ping
    st, _b = http_roundtrip(LISTEN, "GET", "/_ping")
    check("GET /_ping → 200", st == 200, "got %d" % st)

    # 2. create worker (transform must inject restart=no + labels)
    st, body = http_roundtrip(LISTEN, "POST",
        "/containers/create?name=agentteams-worker-fixer",
        body={"Image": BUSYBOX_IMG, "Cmd": ["sleep", "60"]})
    check("create agentteams-worker-fixer → 201", st == 201, "got %d %s" % (st, body[:120]))
    worker_id = ""
    if st == 201:
        try:
            worker_id = json.loads(body).get("Id", "")
        except Exception:
            pass

    # side-channel: verify the REAL container got restart=no + labels
    rc, out = docker("inspect", "agentteams-worker-fixer",
                     "--format", "{{.HostConfig.RestartPolicy.Name}}|{{json .Config.Labels}}")
    if rc == 0:
        parts = out.strip().split("|", 1)
        check("real container restart=no", parts[0] == "no", "got %r" % parts[0])
        try:
            labels = json.loads(parts[1]) if len(parts) > 1 else {}
        except Exception:
            labels = {}
        check("real container label hardened=1", labels.get("com.mergepilot.hardened") == "1")
        check("real container label agent=fixer", labels.get("com.mergepilot.agent") == "fixer")
        check("real container label run_id", labels.get("com.mergepilot.run_id") == RUN_ID)
        check("real container label scope", labels.get("com.mergepilot.scope") == SCOPE)
    else:
        check("real container inspect (side-channel)", False, out[:120])

    # 3. start worker
    st, _b = http_roundtrip(LISTEN, "POST", "/containers/agentteams-worker-fixer/start")
    check("start worker → 204", st in (204, 304), "got %d" % st)

    # 4. inspect worker (through proxy)
    st, body = http_roundtrip(LISTEN, "GET", "/containers/agentteams-worker-fixer/json")
    check("inspect worker → 200", st == 200, "got %d" % st)

    # 5. stop?t=10
    st, _b = http_roundtrip(LISTEN, "POST", "/containers/agentteams-worker-fixer/stop?t=10")
    check("stop?t=10 → 204", st in (204, 304), "got %d" % st)

    # 6. image inspect (allowlisted digest)
    st, _b = http_roundtrip(LISTEN, "GET", "/images/%s/json" % BUSYBOX_IMG)
    check("image inspect (allowlisted) → 200", st == 200, "got %d" % st)

    # 7. delete?force=true (cleanup the worker)
    st, _b = http_roundtrip(LISTEN, "DELETE", "/containers/agentteams-worker-fixer?force=true")
    check("delete?force=true → 204", st in (204, 304, 200), "got %d" % st)

    # ---------- NEGATIVE ----------
    print("\n=== NEGATIVE smoke (each must 403, no upstream side-effect) ===")
    for name, method, target, body in [
        ("GET /version", "GET", "/version", None),
        ("GET /info", "GET", "/info", None),
        ("GET /events", "GET", "/events", None),
        ("GET /containers/x/logs", "GET", "/containers/agentteams-worker-fixer/logs", None),
        ("GET /containers/x/stats", "GET", "/containers/agentteams-worker-fixer/stats", None),
        ("GET /containers/x/changes", "GET", "/containers/agentteams-worker-fixer/changes", None),
        ("POST /containers/x/wait", "POST", "/containers/agentteams-worker-fixer/wait", None),
        ("archive /etc", "PUT", "/containers/agentteams-worker-fixer/archive?path=/etc", b"x"),
        ("archive /", "PUT", "/containers/agentteams-worker-fixer/archive?path=/", b"x"),
        ("archive ..", "PUT", "/containers/agentteams-worker-fixer/archive?path=/a/../..", b"x"),
        ("stop wrong query", "POST", "/containers/agentteams-worker-fixer/stop?t=5", None),
        ("stop no query", "POST", "/containers/agentteams-worker-fixer/stop", None),
        ("delete wrong query", "DELETE", "/containers/agentteams-worker-fixer?force=false", None),
        ("delete no query", "DELETE", "/containers/agentteams-worker-fixer", None),
        ("image wrong digest", "GET", "/images/sha256:deadbeef/json", None),
        ("wrong name", "POST", "/containers/create?name=evil-container", {"Image": BUSYBOX_IMG}),
        ("build", "POST", "/build", None),
        ("networks create", "POST", "/networks/create", None),
    ]:
        st, _b = http_roundtrip(LISTEN, method, target, body=body)
        check(name + " → 403", st == 403, "got %d" % st)

    # verify no stray containers created by the negative tests
    rc, out = docker("ps", "-a", "--format", "{{.Names}}")
    stray = [n for n in out.strip().split("\n") if n and n != "agentteams-worker-fixer"]
    check("no stray containers from negatives", not stray,
          "stray=%r" % stray)

    # ---------- FAIL-CLOSED ----------
    print("\n=== FAIL-CLOSED (upstream unavailable) ===")
    # point a fresh proxy at a nonexistent upstream
    bad_listen = "/run/mp/bad.sock"
    try:
        os.unlink(bad_listen)
    except OSError:
        pass
    bad_cfg = dsp.ProxyConfig(run_id="bad", scope="test",
                              image_allowlist=(BUSYBOX_DIGEST,),
                              upstream_socket="/run/nonexistent.sock",
                              listen_socket=bad_listen)
    bad_server = dsp.ProxyServer(bad_cfg)
    bad_server._sock = dsp.bind_listening_socket(bad_listen)
    stop2 = [False]
    def bad_loop():
        while not stop2[0]:
            try:
                conn, _ = bad_server._sock.accept()
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(
                target=lambda c=conn: pt.handle_connection(
                    c, "/run/nonexistent.sock", bad_cfg, bad_server.exec_registry),
                daemon=True).start()
    bt = threading.Thread(target=bad_loop, daemon=True)
    bt.start()
    time.sleep(0.3)
    st, _b = http_roundtrip(bad_listen, "GET", "/_ping")
    check("upstream unavailable → 502", st == 502, "got %d" % st)
    stop2[0] = True
    try:
        bad_server._sock.close()
    except OSError:
        pass
    try:
        os.unlink(bad_listen)
    except OSError:
        pass

    # ---------- CLEANUP + RESIDUE ----------
    print("\n=== CLEANUP + RESIDUE ===")
    stop[0] = True
    try:
        server._sock.close()
    except OSError:
        pass
    server.shutdown()
    time.sleep(0.5)

    # containers
    rc, out = docker("ps", "-a", "--format", "{{.Names}}")
    containers = [n for n in out.strip().split("\n") if n]
    check("container residue=0", not containers, "left=%r" % containers)
    # volumes (only count NEW volumes created by this smoke run; orphan
    # volumes from prior test runs are pre-existing and not our residue)
    rc, out = docker("volume", "ls", "--format", "{{.Name}}")
    post_volumes = set(n for n in out.strip().split("\n") if n)
    new_volumes = post_volumes - pre_volumes
    check("volume residue=0 (new only)", not new_volumes, "new=%r" % new_volumes)
    # socket
    check("proxy socket removed", not os.path.exists(LISTEN))
    # marker
    check("marker removed", not os.path.exists("/etc/hiclab/proxy-deployed"))

    return _finish(results, all(r[1] for r in results))


def _finish(results, all_ok):
    passed = sum(1 for r in results if r[1])
    failed = sum(1 for r in results if not r[1])
    print("\n=== SUMMARY: %d passed, %d failed ===" % (passed, failed))
    # write evidence (atomic) — only the result JSON, bound to commit
    subprocess.run(["git", "config", "--global", "--add", "safe.directory",
                    "/mnt/d/goai/mergepilot-os"], capture_output=True)
    commit = subprocess.check_output(
        ["git", "-C", "/mnt/d/goai/mergepilot-os", "rev-parse", "HEAD"]
    ).decode().strip()
    evidence = {
        "kind": "d2b3b2-isolated-smoke",
        "source_commit": commit,
        "passed": passed,
        "failed": failed,
        "all_ok": bool(all_ok),
        "checks": [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in results],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ev_path = "/mnt/d/goai/mergepilot-os/evidence/m5/0d/d2b3b2-isolated-smoke.json"
    os.makedirs(os.path.dirname(ev_path), exist_ok=True)
    tmp = ev_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(evidence, f, indent=2)
    os.replace(tmp, ev_path)
    print("evidence written: %s (source_commit=%s)" % (ev_path, commit[:12]))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
