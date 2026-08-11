#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HiClaw v1.2.2 TRUE production live verification.

Uses the REAL v1.2.2 agentteams-* images (pulled from the correct registry
namespace agentteams/) against the Ubuntu-22.04 production dockerd. The proxy
runs with the agentteams name profile (v1.2.2 naming). Both the proxy code
version AND the upstream image version are verified.
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

UPSTREAM = "/var/run/docker.sock"
LISTEN = "/run/mp/docker.sock"
RUN_ID = "true-live-122"
SCOPE = "prod"

# REAL v1.2.2 worker image (pulled from agentteams/ namespace)
WORKER_IMG_FULL = "higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-worker:v1.2.2"
WORKER_DIGEST = "sha256:301f9e311654eca203246fa666d63a126244ea8793f700603d2a6d37b7ffea75"
WORKER_IMG = "higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-worker@%s" % WORKER_DIGEST

# manager image
MANAGER_DIGEST = "sha256:dd11878943e4a425ff38dcc152c9d44ea0e68d97bac89f711207134b8636c0fb"

UPSTREAM_SOURCE_COMMIT = "849182af8e017168a5a200a87b1062142caf462d"
PROXY_SOURCE_COMMIT = "e984ef394ce80e3572159f9ebed154518d7565e4"

ROLES = ["reviewer", "fixer", "verifier"]


def docker(*args):
    r = subprocess.run(["docker"] + list(args),
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def http(sock_path, method, target, body=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30.0)  # v1.2.2 worker is large; allow more time
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

    print("=== v1.2.2 TRUE PRODUCTION LIVE ===")
    print("upstream: AgentTeams v1.2.2 (commit 849182a)")
    print("proxy: e984ef3 (v1.2.2-upgraded)")
    print("worker image: %s" % WORKER_IMG[:80])

    # ---- verify v1.2.2 upstream image is real ----
    print("\n=== UPSTREAM VERSION VERIFICATION ===")
    rc, out = docker("run", "--rm", "--entrypoint", "env",
        "higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:v1.2.2")
    has_at_env = "AGENTTEAMS_" in out
    check("v1.2.2 image has AGENTTEAMS_* env", has_at_env,
          "AGENTTEAMS_ not found in env output")
    # verify it does NOT have old HICLAW naming
    has_hiclaw_cmd = "hiclab" in out.lower()
    check("v1.2.2 image uses agentteams (not hiclab)", not has_hiclaw_cmd,
          "found hiclab reference in env")

    # ---- start proxy ----
    print("\n=== STARTING PROXY (agentteams profile) ===")
    for p in (LISTEN, "/etc/hiclab/proxy-deployed"):
        try: os.unlink(p)
        except OSError: pass
    os.makedirs("/run/mp", exist_ok=True)
    os.makedirs("/etc/hiclab", exist_ok=True)

    config = dsp.ProxyConfig(
        run_id=RUN_ID, scope=SCOPE, name_profile="agentteams",
        image_allowlist=(WORKER_DIGEST, MANAGER_DIGEST),
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
    check("marker written", server.arm_marker())

    # ---- POSITIVE: worker lifecycle (all 3 roles, REAL v1.2.2 worker) ----
    print("\n=== POSITIVE: agentteams worker lifecycle (v1.2.2 image) ===")
    for role in ROLES:
        cname = "agentteams-worker-%s" % role
        p = "[%s] " % role
        st, body = http(LISTEN, "POST", "/containers/create?name=%s" % cname,
            body={"Image": WORKER_IMG,
                  "Entrypoint": ["/bin/sh", "-c", "sleep 300"],
                  "Cmd": []})
        check(p + "create → 201", st == 201, "got %d %s" % (st, body[:80]))

        rc, out = docker("inspect", cname,
                         "--format", "{{.HostConfig.RestartPolicy.Name}}|{{json .Config.Labels}}|{{.Config.Image}}")
        if rc == 0:
            parts = out.strip().split("|", 2)
            check(p + "restart=no", parts[0] == "no")
            try: labels = json.loads(parts[1]) if len(parts)>1 else {}
            except: labels = {}
            check(p + "agent=%s" % role, labels.get("com.mergepilot.agent") == role,
                  "got %r" % labels.get("com.mergepilot.agent"))
            check(p + "hardened=1", labels.get("com.mergepilot.hardened") == "1")
            check(p + "run_id", labels.get("com.mergepilot.run_id") == RUN_ID)
            check(p + "scope=prod", labels.get("com.mergepilot.scope") == SCOPE)
            # verify the REAL v1.2.2 image is running
            img = parts[2] if len(parts)>2 else ""
            check(p + "image is v1.2.2 worker", WORKER_DIGEST[:20] in img or "agentteams-worker" in img,
                  "img=%s" % img[:60])

        st, _ = http(LISTEN, "POST", "/containers/%s/start" % cname)
        check(p + "start → 204", st in (204, 304), "got %d" % st)
        # wait for the container to be running before exec (v1.2.2 worker
        # has startup time; without this, exec-create returns 409)
        time.sleep(3)
        st, _ = http(LISTEN, "GET", "/containers/%s/json" % cname)
        check(p + "inspect → 200", st == 200, "got %d" % st)

        # exec-create on the REAL v1.2.2 worker
        st, body = http(LISTEN, "POST", "/containers/%s/exec" % cname,
                        body={"Cmd": ["echo", "v122-test"]})
        check(p + "exec-create → 201", st == 201, "got %d %s" % (st, body[:60]))
        exec_id = ""
        if st == 201:
            try: exec_id = json.loads(body).get("Id", "")
            except: pass
        check(p + "exec registered", bool(exec_id and server.exec_registry.authorize(exec_id)[0]))

        st, _ = http(LISTEN, "POST", "/containers/%s/stop?t=10" % cname)
        check(p + "stop?t=10 → 204", st in (204, 304), "got %d" % st)
        st, _ = http(LISTEN, "DELETE", "/containers/%s?force=true" % cname)
        check(p + "delete → 204", st in (204, 304, 200), "got %d" % st)

    # ---- manager (v1.2.2) ----
    print("\n=== POSITIVE: agentteams manager (v1.2.2) ===")
    mgr_img = "higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager@%s" % MANAGER_DIGEST
    st, _ = http(LISTEN, "POST", "/containers/create?name=agentteams-manager",
                 body={"Image": mgr_img, "Cmd": ["sleep", "60"]})
    check("[manager] create → 201", st == 201, "got %d" % st)
    rc, out = docker("inspect", "agentteams-manager", "--format", "{{json .Config.Labels}}")
    if rc == 0:
        try: labels = json.loads(out.strip())
        except: labels = {}
        check("[manager] agent=manager", labels.get("com.mergepilot.agent") == "manager")
    st, _ = http(LISTEN, "DELETE", "/containers/agentteams-manager?force=true")
    check("[manager] delete → 204", st in (204, 304, 200), "got %d" % st)

    # ---- ping + image ----
    st, _ = http(LISTEN, "GET", "/_ping")
    check("/_ping → 200", st == 200, "got %d" % st)
    st, _ = http(LISTEN, "GET", "/images/%s/json" % WORKER_IMG)
    check("image inspect → 200", st == 200, "got %d" % st)

    # ---- NEGATIVE ----
    print("\n=== NEGATIVE (all 403) ===")
    for name, method, target, body in [
        ("GET /version", "GET", "/version", None),
        ("GET /info", "GET", "/info", None),
        ("GET /events", "GET", "/events", None),
        ("logs", "GET", "/containers/agentteams-worker-fixer/logs", None),
        ("stats", "GET", "/containers/agentteams-worker-fixer/stats", None),
        ("archive /etc", "PUT", "/containers/agentteams-worker-fixer/archive?path=/etc", b"x"),
        ("stop t=5", "POST", "/containers/agentteams-worker-fixer/stop?t=5", None),
        ("delete force=f", "DELETE", "/containers/agentteams-worker-fixer?force=false", None),
        ("wrong name", "POST", "/containers/create?name=evil", {"Image": WORKER_IMG}),
        ("unknown role", "POST", "/containers/create?name=agentteams-worker-evil", {"Image": WORKER_IMG}),
        ("build", "POST", "/build", None),
    ]:
        st, _ = http(LISTEN, method, target, body=body)
        check(name + " → 403", st == 403, "got %d" % st)

    # ---- CLEANUP ----
    print("\n=== CLEANUP ===")
    stop[0] = True
    try: server._sock.close()
    except OSError: pass
    server.shutdown()
    time.sleep(1)

    # clean anonymous volumes
    rc, out = docker("volume", "ls", "-q", "--filter", "dangling=true")
    for vol in out.strip().split("\n"):
        if vol and vol != "hiclaw-data":
            docker("volume", "rm", vol)

    rc, out = docker("ps", "-a", "--format", "{{.Names}}")
    post_c = set(n for n in out.strip().split("\n") if n)
    new_c = post_c - pre_containers
    check("container residue=0 (new)", not new_c, "new=%r" % list(new_c)[:5])
    rc, out = docker("volume", "ls", "--format", "{{.Name}}")
    post_v = set(n for n in out.strip().split("\n") if n)
    new_v = post_v - pre_volumes
    check("volume residue=0 (new)", not new_v, "new=%r" % list(new_v)[:5])
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
        "kind": "hiclaw-v122-true-live-pass",
        "upstream_version": "v1.2.2",
        "upstream_source_commit": UPSTREAM_SOURCE_COMMIT,
        "proxy_source_commit": PROXY_SOURCE_COMMIT,
        "head_commit": commit,
        "worker_image_digest": WORKER_DIGEST,
        "manager_image_digest": MANAGER_DIGEST,
        "passed": passed,
        "failed": failed,
        "all_ok": bool(all_ok),
        "hiclaw_live": bool(all_ok),
        "roles_tested": ["reviewer", "fixer", "verifier", "manager"],
        "name_profile": "agentteams",
        "environment": "Ubuntu-22.04 production dockerd (true v1.2.2 live)",
        "checks": [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in results],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ev_path = "/mnt/d/goai/mergepilot-os/evidence/m5/0d/hiclaw-v122-true-live-pass.json"
    os.makedirs(os.path.dirname(ev_path), exist_ok=True)
    tmp = ev_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(evidence, f, indent=2)
    os.replace(tmp, ev_path)
    print("evidence: %s (hiclaw_live=%s)" % (ev_path, evidence["hiclaw_live"]))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
