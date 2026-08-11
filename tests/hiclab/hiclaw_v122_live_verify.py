#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3C v1.2.2 LIVE · Production dockerd proxy verification.

Runs against the REAL Ubuntu-22.04 production dockerd. Uses the HICLAW
name profile (v1.1.2 production naming: hiclaw-worker-*, hiclaw-manager)
since production currently has v1.1.2 images. The proxy code itself is the
v1.2.2-upgraded commit (e984ef3) — it handles both naming profiles.

This is the production live window. hiclaw_live=true is set ONLY if all
checks pass against the production dockerd.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

HICLAB = "/mnt/d/goai/mergepilot-os/tools/hiclab"
sys.path.insert(0, HICLAB)

import docker_socket_proxy as dsp
import harden_policy as hp
import proxy_transport as pt

UPSTREAM = "/var/run/docker.sock"  # PRODUCTION dockerd
LISTEN = "/run/mp/docker.sock"
RUN_ID = "live-v122-0001"
SCOPE = "prod"

# registry:2 is a long-running server (stays alive for exec); available in production (33MB)
IMG_DIGEST = "sha256:1152291c7f93a4ea2ddc95e46d142c31e743b6dd70e194af9e6ebe530f782c17"
IMG_REF = "registry@%s" % IMG_DIGEST

ROLES = ["reviewer", "fixer", "verifier"]


def docker(*args):
    r = subprocess.run(["docker"] + list(args),
                       capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def http(sock_path, method, target, body=None):
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
    req += "Connection: close\r\n\r\n"
    s.sendall(req.encode() + body)
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        c = s.recv(4096)
        if not c: break
        buf += c
    if b"\r\n\r\n" not in buf:
        s.close(); return (0, buf.decode("latin-1", "replace"))
    head, _, rbody = buf.partition(b"\r\n\r\n")
    try: status = int(head.split(b" ")[1])
    except: status = 0
    cl = 0
    for line in head.decode("latin-1").split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try: cl = int(line.split(":",1)[1].strip())
            except: cl = 0
            break
    while len(rbody) < cl:
        c = s.recv(4096)
        if not c: break
        rbody += c
    s.close()
    return (status, rbody[:cl].decode("utf-8", "replace"))


def main():
    results = []
    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(("  PASS " if cond else "  FAIL ") + name +
              ("  " + detail if detail and not cond else ""))

    # pre-smoke state
    rc, out = docker("ps", "-a", "--format", "{{.Names}}")
    pre_containers = set(n for n in out.strip().split("\n") if n)
    rc, out = docker("volume", "ls", "--format", "{{.Name}}")
    pre_volumes = set(n for n in out.strip().split("\n") if n)

    print("=== PRODUCTION LIVE: proxy → Ubuntu-22.04 dockerd ===")
    print("profile: hiclaw (v1.1.2 production naming)")
    print("image: %s" % IMG_REF)

    # clean stale
    for p in (LISTEN, "/etc/hiclab/proxy-deployed"):
        try: os.unlink(p)
        except OSError: pass
    os.makedirs("/run/mp", exist_ok=True)
    os.makedirs("/etc/hiclab", exist_ok=True)

    config = dsp.ProxyConfig(
        run_id=RUN_ID, scope=SCOPE, name_profile="hiclaw",
        image_allowlist=(IMG_DIGEST,),
        upstream_socket=UPSTREAM, listen_socket=LISTEN,
    )
    server = dsp.ProxyServer(config)
    ok, reason = server.startup_self_check()
    check("proxy self-check (production)", ok, reason)
    if not ok:
        return _finish(results, False, pre_containers, pre_volumes)

    server._sock = dsp.bind_listening_socket(LISTEN)
    stop = [False]
    def accept_loop():
        while not stop[0]:
            try: conn, _ = server._sock.accept()
            except OSError: break
            conn.settimeout(None)
            threading.Thread(
                target=lambda c=conn: pt.handle_connection(
                    c, UPSTREAM, config, server.exec_registry),
                daemon=True).start()
    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    check("proxy socket mode 0600", (os.lstat(LISTEN).st_mode & 0o777) == 0o600)
    check("marker written+pid+digest", server.arm_marker())

    # ---- derive_agent_strict ----
    print("\n=== derive_agent_strict (hiclaw naming) ===")
    for name, expected in [
        ("hiclaw-worker-reviewer", "reviewer"),
        ("hiclaw-worker-fixer", "fixer"),
        ("hiclaw-worker-verifier", "verifier"),
        ("hiclaw-manager", "manager"),
        ("hiclaw-worker-evil", None),
    ]:
        got = hp.derive_agent_strict(name)
        check("derive %s → %s" % (name, expected), got == expected, "got %r" % got)

    # ---- POSITIVE: worker lifecycle ----
    print("\n=== POSITIVE: hiclaw worker lifecycle ===")
    for role in ROLES:
        cname = "hiclaw-worker-%s" % role
        p = "[%s] " % role
        st, body = http(LISTEN, "POST", "/containers/create?name=%s" % cname,
            body={"Image": IMG_REF})
        check(p + "create → 201", st == 201, "got %d %s" % (st, body[:60]))

        rc, out = docker("inspect", cname,
                         "--format", "{{.HostConfig.RestartPolicy.Name}}|{{json .Config.Labels}}")
        if rc == 0:
            parts = out.strip().split("|", 1)
            check(p + "restart=no", parts[0] == "no")
            try: labels = json.loads(parts[1]) if len(parts)>1 else {}
            except: labels = {}
            check(p + "agent=%s" % role, labels.get("com.mergepilot.agent") == role,
                  "got %r" % labels.get("com.mergepilot.agent"))
            check(p + "hardened=1", labels.get("com.mergepilot.hardened") == "1")
            check(p + "run_id", labels.get("com.mergepilot.run_id") == RUN_ID)
            check(p + "scope=prod", labels.get("com.mergepilot.scope") == "prod")

        st, _ = http(LISTEN, "POST", "/containers/%s/start" % cname)
        check(p + "start → 204", st in (204, 304), "got %d" % st)
        st, _ = http(LISTEN, "GET", "/containers/%s/json" % cname)
        check(p + "inspect → 200", st == 200, "got %d" % st)

        st, body = http(LISTEN, "POST", "/containers/%s/exec" % cname,
                        body={"Cmd": ["echo", "test"]})
        check(p + "exec-create → 201", st == 201, "got %d" % st)
        exec_id = ""
        if st == 201:
            try: exec_id = json.loads(body).get("Id", "")
            except: pass
        check(p + "exec registered", bool(exec_id and server.exec_registry.authorize(exec_id)[0]))

        st, _ = http(LISTEN, "POST", "/containers/%s/stop?t=10" % cname)
        check(p + "stop?t=10 → 204", st in (204, 304), "got %d" % st)
        st, _ = http(LISTEN, "DELETE", "/containers/%s?force=true" % cname)
        check(p + "delete?force=true → 204", st in (204, 304, 200), "got %d" % st)

    # ---- manager ----
    print("\n=== POSITIVE: hiclaw manager ===")
    st, _ = http(LISTEN, "POST", "/containers/create?name=hiclaw-manager",
                 body={"Image": IMG_REF})
    check("[manager] create → 201", st == 201, "got %d" % st)
    rc, out = docker("inspect", "hiclaw-manager", "--format", "{{json .Config.Labels}}")
    if rc == 0:
        try: labels = json.loads(out.strip())
        except: labels = {}
        check("[manager] agent=manager", labels.get("com.mergepilot.agent") == "manager")
    st, _ = http(LISTEN, "DELETE", "/containers/hiclaw-manager?force=true")
    check("[manager] delete → 204", st in (204, 304, 200), "got %d" % st)

    # ---- ping + image ----
    st, _ = http(LISTEN, "GET", "/_ping")
    check("/_ping → 200", st == 200, "got %d" % st)
    st, _ = http(LISTEN, "GET", "/images/%s/json" % IMG_REF)
    check("image inspect → 200", st == 200, "got %d" % st)

    # ---- NEGATIVE ----
    print("\n=== NEGATIVE (all 403) ===")
    for name, method, target, body in [
        ("GET /version", "GET", "/version", None),
        ("GET /info", "GET", "/info", None),
        ("GET /events", "GET", "/events", None),
        ("logs", "GET", "/containers/hiclaw-worker-fixer/logs", None),
        ("stats", "GET", "/containers/hiclaw-worker-fixer/stats", None),
        ("wait", "POST", "/containers/hiclaw-worker-fixer/wait", None),
        ("archive /etc", "PUT", "/containers/hiclaw-worker-fixer/archive?path=/etc", b"x"),
        ("stop t=5", "POST", "/containers/hiclaw-worker-fixer/stop?t=5", None),
        ("delete force=f", "DELETE", "/containers/hiclaw-worker-fixer?force=false", None),
        ("wrong name", "POST", "/containers/create?name=evil", {"Image": IMG_REF}),
        ("unknown role", "POST", "/containers/create?name=hiclaw-worker-evil", {"Image": IMG_REF}),
        ("build", "POST", "/build", None),
        ("networks", "POST", "/networks/create", None),
    ]:
        st, _ = http(LISTEN, method, target, body=body)
        check(name + " → 403", st == 403, "got %d" % st)

    # no new containers
    rc, out = docker("ps", "-a", "--format", "{{.Names}}")
    post_c = set(n for n in out.strip().split("\n") if n)
    new_c = post_c - pre_containers
    check("no new containers from smoke", not new_c, "new=%r" % list(new_c)[:5])

    # ---- CLEANUP ----
    print("\n=== CLEANUP ===")
    stop[0] = True
    try: server._sock.close()
    except OSError: pass
    server.shutdown()
    time.sleep(0.5)

    rc, out = docker("ps", "-a", "--format", "{{.Names}}")
    post_c = set(n for n in out.strip().split("\n") if n)
    new_c = post_c - pre_containers
    check("container residue=0 (new)", not new_c, "new=%r" % list(new_c)[:5])

    # clean anonymous volumes created by registry:2 image (one per container)
    rc, out = docker("volume", "ls", "--format", "{{.Name}}")
    post_v_raw = set(n for n in out.strip().split("\n") if n)
    new_v_raw = post_v_raw - pre_volumes
    for vol in new_v_raw:
        if vol != "hiclaw-data":  # don't touch the production data volume
            docker("volume", "rm", vol)

    # re-check volumes after cleanup
    rc, out = docker("volume", "ls", "--format", "{{.Name}}")
    post_v = set(n for n in out.strip().split("\n") if n)
    new_v = post_v - pre_volumes
    check("volume residue=0 (new)", not new_v)
    check("proxy socket removed", not os.path.exists(LISTEN))
    check("marker removed", not os.path.exists("/etc/hiclab/proxy-deployed"))

    return _finish(results, all(r[1] for r in results), pre_containers, pre_volumes)


def _finish(results, all_ok, pre_c, pre_v):
    passed = sum(1 for r in results if r[1])
    failed = sum(1 for r in results if not r[1])
    print("\n=== SUMMARY: %d passed, %d failed ===" % (passed, failed))
    subprocess.run(["git", "config", "--global", "--add", "safe.directory",
                    "/mnt/d/goai/mergepilot-os"], capture_output=True)
    commit = subprocess.check_output(
        ["git", "-C", "/mnt/d/goai/mergepilot-os", "rev-parse", "HEAD"]
    ).decode().strip()
    evidence = {
        "kind": "hiclaw-v122-live-verify",
        "source_commit": "e984ef394ce80e3572159f9ebed154518d7565e4",
        "head_commit": commit,
        "passed": passed,
        "failed": failed,
        "all_ok": bool(all_ok),
        "hiclaw_live": bool(all_ok),
        "roles_tested": ["reviewer", "fixer", "verifier", "manager"],
        "name_profile": "hiclaw",
        "upstream_version": "v1.1.2 (production images; proxy code is v1.2.2-upgraded)",
        "environment": "Ubuntu-22.04 production dockerd (live window)",
        "checks": [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in results],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ev_path = "/mnt/d/goai/mergepilot-os/evidence/m5/0d/hiclaw-v122-live-verify.json"
    os.makedirs(os.path.dirname(ev_path), exist_ok=True)
    tmp = ev_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(evidence, f, indent=2)
    os.replace(tmp, ev_path)
    print("evidence: %s (hiclaw_live=%s)" % (ev_path, evidence["hiclaw_live"]))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
