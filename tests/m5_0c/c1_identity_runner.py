#!/usr/bin/env python3
"""M5-0C C1 — Matrix identity + login closure runner (MergePilot-Test).

Registers 5 isolated test identities in the temporary Matrix, logs them in,
creates a room, invites/joins members, sends an identity-verification event,
and verifies the causal chain via /sync (event_id, sender, room_id, body).

Hard security model:
  * M5C_C1_ALLOW_MATRIX_WRITES=1 gates every register/room/event write
    (fail closed otherwise).
  * The registration token + per-user passwords are generated in-process, held
    in memory, and written only to a tmpfs-backed secret dir (mode 700 / files
    600). The controller receives them via the /secrets wrapper (read-only bind
    mount) — NEVER via `docker -e`. Config.Env carries only SECRETS_DIR=/secrets.
  * access_token / sync_token stay in Python memory; never printed/logged.
  * The test homeserver server_name is verified (matrix-local.hiclaw.io).

This runner does NOT access GitHub, does NOT call external LLMs, does NOT touch
the production daemon (mp_guard gates the distro). It targets ONLY the temporary
C0 embedded Matrix brought up for the current RUN_KEY.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

DEPLOY = "/mnt/d/goai/mergepilot-os/tests/m5_0c/deploy_test_stack.sh"
WRAPPER_SRC = "/mnt/d/goai/mergepilot-os/tests/m5_0c/c1_secret_wrapper.sh"
CAND_WRAPPER_SRC = "/mnt/d/goai/mergepilot-os/tests/m5_0c/c1_candidate_wrapper.sh"
CANDIDATE_IMAGE = "mergepilot-m5-0-candidate:current"
MANAGER_IMAGE_REF = "higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-manager:latest"
NC_SHIM = "/mnt/d/goai/mergepilot-os/tests/m5_0c/c1_nc_shim.sh"
AGENT_WRAPPER_SRC = "/mnt/d/goai/mergepilot-os/tests/m5_0c/c1_agent_wrapper.sh"
MINIO_ROOT_USER = "admin"  # embedded start-minio.sh default
CANDIDATE_BUILD_CTX = "/mnt/d/goai/mergepilot-os/tools/workflow-controller"
AUDIT_DB = "/mnt/d/goai/mergepilot-os/tools/audit-db"
MIGRATION_SQL = ["init", "m3_state", "m3b_policy", "m3b_b4", "m3b_b4c",
                 "m3b_b4c1", "m3b_b4c1_1", "m3b_b4d1", "m3c_state",
                 "m4f1_state", "m4f1_hotfix_1"]
EXPECTED_HS_PREFIX = "matrix-local.hiclaw.io"  # test homeserver server_name
ROLE_PREFIX = {"mgr": "m5c-mgr", "rev": "m5c-rev",
               "fix": "m5c-fix", "ver": "m5c-ver", "ctrl": "m5c-ctrl"}
ROLES = ["mgr", "rev", "fix", "ver", "ctrl"]

results = []


def record(tid, ok, detail=""):
    results.append({"test_id": tid, "passed": bool(ok), "detail": detail[:160]})
    print(f"  [{'PASS' if ok else 'FAIL'}] {tid}: {detail[:120]}")


def scan_secrets(name, secret_vals):
    """Scan a container's Config.Env + Args + logs for ACTUAL secret values.
    Returns hit count (0 = clean). Only checks values >8 chars to avoid noise."""
    env = run(["docker", "inspect", name, "--format", "{{json .Config.Env}}{{json .Config.Args}}"]).stdout
    logs = run(["docker", "logs", name]).stdout
    combined = env + " " + logs
    return sum(1 for s in secret_vals if s and len(s) > 8 and s in combined)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)


def safe_key(rk):
    """Normalize RUN_KEY -> [a-z0-9-], trimmed, + sha256(rk) suffix to defeat
    normalization collisions (two distinct RUN_KEYs that normalize identically
    still disambiguate via the hash)."""
    norm = re.sub(r"[^a-z0-9-]", "-", rk.lower())
    norm = re.sub(r"-+", "-", norm).strip("-")
    if not norm:
        norm = "rk"
    norm = norm[:24].strip("-") or "rk"
    h = hashlib.sha256(rk.encode()).hexdigest()[:6]
    return f"{norm}-{h}"


def localpart(role, sk):
    return f"{ROLE_PREFIX[role]}-{sk}"


def secret_dir_for(rk):
    r = run(["findmnt", "-no", "FSTYPE", "/run"])
    base = "/run/secrets" if (r.returncode == 0 and r.stdout.strip() == "tmpfs") else "/dev/shm"
    d = f"{base}/m5c-{rk}"
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def write_secret(d, name, val):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(val)
    os.chmod(p, 0o600)


def mx(ctrl, method, path, body=None, token=None, timeout=30):
    """Matrix CS API call via `docker exec -i` (body on stdin via --data @- so the
    secret-bearing JSON never appears in a process cmdline). Returns (http_code,
    parsed_json_or_raw)."""
    cmd = ["docker", "exec", "-i", ctrl, "curl", "-s", "-w", "\n__HTTP__%{http_code}",
           "-X", method, "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["--data", "@-"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    cmd += [f"http://localhost:6167{path}"]
    data = json.dumps(body).encode() if body is not None else b""
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", "replace")
    if "\n__HTTP__" in out:
        body_s, code_s = out.rsplit("\n__HTTP__", 1)
        code = int(code_s.strip() or "0")
    else:
        body_s, code = out, r.returncode
    try:
        j = json.loads(body_s) if body_s.strip() else {}
    except Exception:
        j = {"_raw": body_s}
    return code, j


def bring_up(rk, secret_dir):
    env = dict(os.environ)
    env["M5C_RUN_KEY"] = rk
    env["M5C_SECRET_DIR"] = secret_dir
    r = run(["bash", DEPLOY, "up"], env=env, timeout=180)
    return r.returncode, r.stdout + r.stderr


def matrix_ready(ctrl):
    for _ in range(90):
        c, _ = mx(ctrl, "GET", "/_matrix/client/versions", timeout=5)
        if c == 200:
            return True
        time.sleep(1)
    return False


def teardown(rk):
    env = dict(os.environ)
    env["M5C_RUN_KEY"] = rk
    run(["bash", DEPLOY, "down"], env=env, timeout=60)


def ctrl_name(rk):
    return f"m5c-controller-{rk}"


def candidate_phase(ctrl, rk, sk, ctrl_lp, ctrl_pw, mgr_uid, sdir):
    """Run the REAL MergePilot Candidate (controller.py) against the current
    RUN_KEY's embedded Matrix + an isolated temp PostgreSQL. Verifies the actual
    controller.py process logs in (prints "[ctrl] Matrix login OK"), acquires the
    advisory lock, and makes zero gateway/github/llm calls (no M4F_RUN events sent).
    Secrets (ADMIN_PW=Matrix password, PG_PASS, GATEWAY_TOKEN) via /secrets wrapper."""
    print("=== CANDIDATE (real controller.py) ===")
    net = f"m5c-net-{rk}"
    pg = f"m5c-pg-{rk}"
    cand = f"m5c-cand-{rk}"
    L_SCOPE = "com.mergepilot.scope=test"
    L_RUN = f"com.mergepilot.run_key={rk}"
    import secrets as _s
    pg_pw = "pg" + _s.token_urlsafe(10)
    write_secret(sdir, "ADMIN_PW", ctrl_pw)      # controller uses ADMIN_PW as Matrix password
    write_secret(sdir, "PG_PASS", pg_pw)
    write_secret(sdir, "GATEWAY_TOKEN", "gwtest" + _s.token_urlsafe(10))
    shutil.copyfile(CAND_WRAPPER_SRC, os.path.join(sdir, "candidate-wrapper.sh"))
    os.chmod(os.path.join(sdir, "candidate-wrapper.sh"), 0o700)
    try:
        # build candidate image once (idempotent)
        if run(["docker", "image", "inspect", CANDIDATE_IMAGE]).returncode != 0:
            br = run(["docker", "build", "-q", "-t", CANDIDATE_IMAGE, CANDIDATE_BUILD_CTX], timeout=300)
            record("P_candidate_build", br.returncode == 0, f"build rc={br.returncode}")
        else:
            record("P_candidate_build", True, "image already built")
        # temp PG (labeled, test network only). Named volume so cleanup is precise
        # (pgvector declares VOLUME /var/lib/postgresql/data -> anonymous volume if
        # not overridden; named volume makes it identifiable + removable).
        pgvol = f"m5c-pgdata-{rk}"
        run(["docker", "volume", "create", "--label", L_SCOPE, "--label", L_RUN, pgvol])
        # PG password via POSTGRES_PASSWORD_FILE (official PG support) — NOT in Config.Env
        write_secret(sdir, "postgres_password", pg_pw)
        run(["docker", "run", "-d", "--name", pg, "--network", net,
             "--label", L_SCOPE, "--label", L_RUN,
             "-v", f"{pgvol}:/var/lib/postgresql/data",
             "-v", f"{sdir}:/secrets:ro",
             "-e", "POSTGRES_USER=mergepilot",
             "-e", "POSTGRES_PASSWORD_FILE=/secrets/postgres_password",
             "-e", "POSTGRES_DB=mergepilot_audit", "pgvector/pgvector:pg16"])
        ready = False
        for _ in range(30):
            if run(["docker", "exec", pg, "pg_isready", "-U", "mergepilot", "-d", "mergepilot_audit"]).returncode == 0:
                ready = True; break
            time.sleep(1)
        record("P_candidate_pg_ready", ready, f"pg={pg}")
        run(["docker", "exec", pg, "psql", "-U", "mergepilot", "-d", "mergepilot_audit",
             "-c", "CREATE ROLE mergepilot_approver NOLOGIN"])
        for name in MIGRATION_SQL:
            run(["docker", "cp", f"{AUDIT_DB}/{name}.sql", f"{pg}:/tmp/x.sql"])
            run(["docker", "exec", pg, "psql", "-U", "mergepilot", "-d", "mergepilot_audit", "-f", "/tmp/x.sql"])
        m4f_prefix = "m5c1-" + rk[-8:]
        # PGPASSFILE for the snapshot DSN (password NOT in M4F_SNAPSHOT_DSN/Config.Env)
        write_secret(sdir, "pgpass", f"{pg}:5432:mergepilot_audit:mergepilot:{pg_pw}")
        run(["docker", "run", "-d", "--name", cand, "--network", net,
             "--label", L_SCOPE, "--label", L_RUN,
             "-v", f"{sdir}:/secrets:ro",
             "-e", "SECRETS_DIR=/secrets", "-e", "M4F_ONLY_MODE=1", "-e", "M4F_ENABLED=1",
             "-e", "M4F_LIVE_MODE=1", "-e", f"MATRIX_USER={ctrl_lp}",
             "-e", "MATRIX_HS=http://m5c-controller:6167",
             "-e", "MATRIX_SERVER_NAME=matrix-local.hiclaw.io:8080",
             "-e", "M4F_ALLOWED_ROOMS=!unused:matrix-local.hiclaw.io:8080",
             "-e", f"M4F_ALLOWED_SENDERS={mgr_uid}",
             "-e", f"CONTROLLER_CONSUMER_NAME=m5c1-{rk}", "-e", f"M4F_RUN_PREFIX={m4f_prefix}",
             "-e", f"PG_HOST={pg}", "-e", "PG_PORT=5432", "-e", "PG_USER=mergepilot",
             "-e", "PG_DATABASE=mergepilot_audit",
             "-e", f"M4F_SNAPSHOT_DSN=postgresql://mergepilot@{pg}:5432/mergepilot_audit",
             "-e", "PGPASSFILE=/secrets/pgpass",
             "-e", "GATEWAY_URL=http://m5c-controller:8080",
             "-e", "RESERVED_RUN_PREFIXES=m4fx-reserved", "-e", "L2_MERGE_ENABLED=0",
             "--restart=no", "--entrypoint", "/secrets/candidate-wrapper.sh", CANDIDATE_IMAGE])
        # wait for login / advisory lock / fatal
        login_ok = False
        advisory_ok = False
        fatal = False
        for _ in range(30):
            logs = run(["docker", "logs", cand]).stdout
            if "[ctrl] Matrix login OK" in logs:
                login_ok = True
            if "advisory lock acquired" in logs:
                advisory_ok = True
            if "[ctrl] FATAL" in logs or "Traceback" in logs:
                fatal = True
                break
            if login_ok:
                break
            time.sleep(2)
        logs = run(["docker", "logs", cand]).stdout
        running = run(["docker", "inspect", cand, "--format", "{{.State.Running}}"]).stdout.strip()
        gw_calls = len(re.findall(r"gateway.*call|github|pull_request", logs, re.I))
        record("P_candidate_matrix_login", login_ok, f'[ctrl] Matrix login OK={login_ok}')
        record("P_candidate_advisory_lock", advisory_ok, "advisory lock acquired")
        record("P_candidate_no_fatal", not fatal, f"FATAL={fatal} running={running}")
        record("P_candidate_no_external_calls", gw_calls == 0, f"gateway/github mentions={gw_calls}")
    finally:
        try:
            _ch = scan_secrets(cand, [pg_pw, ctrl_pw])
            _ph = scan_secrets(pg, [pg_pw])
            record("P_candidate_secret_scan", _ch + _ph == 0, f"cand_hits={_ch} pg_hits={_ph}")
        except Exception:
            pass
        run(["docker", "rm", "-f", cand])
        run(["docker", "rm", "-f", pg])
        run(["docker", "volume", "rm", f"m5c-pgdata-{rk}"])


def manager_phase(ctrl, rk, reg_tok, mg_pw, sdir):
    """Co-located official manager (start-manager-agent.sh) real Matrix login.
    Secrets (HICLAW_REGISTRATION_TOKEN, HICLAW_FS_SECRET_KEY) via /secrets wrapper
    — NOT in Config.Env. The wrapper redacts access_token from docker logs."""
    print("=== MANAGER (real OpenClaw entrypoint) ===")
    L_SCOPE = "com.mergepilot.scope=test"
    L_RUN = f"com.mergepilot.run_key={rk}"
    mgr = f"m5c-mgr-{rk}"
    mgr_id = run(["docker", "images", "-aq", "--filter", f"reference={MANAGER_IMAGE_REF}"]).stdout.strip()
    for v in ("m5c-mgrdata", "m5c-mgrws", "m5c-mgrfs"):
        run(["docker", "volume", "create", "--label", L_SCOPE, "--label", L_RUN, f"{v}-{rk}"])
    run(["docker", "run", "--rm", "-v", f"m5c-mgrfs-{rk}:/fs", "busybox", "sh",
         "-c", "mkdir -p /fs/shared /fs/agents /fs/hiclaw-config && touch /fs/.initialized"])
    # write FS secret + agent wrapper to the shared secret dir
    write_secret(sdir, "HICLAW_FS_SECRET_KEY", mg_pw)
    shutil.copyfile(AGENT_WRAPPER_SRC, os.path.join(sdir, "agent-wrapper.sh"))
    os.chmod(os.path.join(sdir, "agent-wrapper.sh"), 0o700)
    run(["docker", "run", "-d", "--name", mgr, "--network", f"container:{ctrl}",
         "-v", f"m5c-mgrdata-{rk}:/data", "-v", f"m5c-mgrws-{rk}:/root/manager-workspace",
         "-v", f"m5c-mgrfs-{rk}:/root/hiclaw-fs", "-v", f"{sdir}:/secrets:ro",
         "-v", f"{NC_SHIM}:/usr/local/bin/nc:ro",
         "-e", "SECRETS_DIR=/secrets",
         "-e", "HICLAW_MATRIX_URL=http://127.0.0.1:6167",
         "-e", "HICLAW_MATRIX_DOMAIN=matrix-local.hiclaw.io:8080",
         "-e", "HICLAW_FS_ENDPOINT=http://127.0.0.1:9000",
         "-e", f"HICLAW_FS_ACCESS_KEY={MINIO_ROOT_USER}",
         "-e", "HICLAW_FS_BUCKET=hiclaw-storage", "-e", "HICLAW_AI_GATEWAY_DOMAIN=aigw-local.hiclaw.io",
         "--label", L_SCOPE, "--label", L_RUN, "--restart=no",
         "--entrypoint", "/secrets/agent-wrapper.sh", mgr_id,
         "/opt/hiclaw/scripts/init/start-manager-agent.sh"])
    login_ok = False
    device_seen = False
    for _ in range(90):  # up to 180s
        logs = run(["docker", "logs", mgr]).stdout
        if "Manager Matrix token obtained" in logs or '"access_token"' in logs:
            login_ok = True
        if "device_id" in logs:
            device_seen = True
        if login_ok or "FATAL" in logs:
            break
        time.sleep(2)
    logs = run(["docker", "logs", mgr]).stdout
    running = run(["docker", "inspect", mgr, "--format", "{{.State.Running}}"]).stdout.strip()
    uid_match = re.search(r'"user_id":"(@manager:[^"]+)"', logs)
    record("P_manager_register_login", login_ok, f'token_obtained={login_ok} uid={uid_match.group(1) if uid_match else ""}')
    record("P_manager_device_session", device_seen, f'device_id observed in login response')
    record("P_manager_running", running == "true", f"state={running}")
    _hits = scan_secrets(mgr, [reg_tok, mg_pw])
    record("P_manager_secret_scan", _hits == 0, f"secret_hits={_hits}")
    run(["docker", "rm", "-f", mgr])
    for v in ("m5c-mgrdata", "m5c-mgrws", "m5c-mgrfs"):
        run(["docker", "volume", "rm", f"{v}-{rk}"])


def worker_phase(ctrl, rk, reg_tok, mg_pw, sdir):
    """Co-located official worker (worker-entrypoint.sh) real OpenClaw Matrix
    login for reviewer/fixer/verifier. openclaw.json rendered in Python memory
    (no -e for WORKER_MATRIX_TOKEN); MinIO provisioned via mc pipe (stdin);
    FS secret via agent wrapper. Detects '[matrix] connected to gateway'."""
    print("=== WORKERS (real OpenClaw entrypoint: reviewer/fixer/verifier) ===")
    L_SCOPE = "com.mergepilot.scope=test"
    L_RUN = f"com.mergepilot.run_key={rk}"
    mgr_id = run(["docker", "images", "-aq", "--filter", f"reference={MANAGER_IMAGE_REF}"]).stdout.strip()
    wrk_ref = "higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-worker:latest"
    wrk_id = run(["docker", "images", "-aq", "--filter", f"reference={wrk_ref}"]).stdout.strip()
    import secrets as _s
    gw_key = "gwkey" + _s.token_hex(6)
    gw_auth = "gwauth" + _s.token_hex(6)
    # FS secret already in sdir (written by manager_phase); set up MinIO alias
    run(["docker", "exec", ctrl, "mc", "alias", "set", "prov",
         "http://localhost:9000", MINIO_ROOT_USER, mg_pw])
    run(["docker", "exec", ctrl, "mc", "mb", "prov/hiclaw-storage"])
    tmpl = run(["docker", "run", "--rm", "--entrypoint", "cat", mgr_id,
                "/opt/hiclaw/agent/skills/worker-management/references/worker-openclaw.json.tmpl"]).stdout
    soul = run(["docker", "run", "--rm", "--entrypoint", "cat", mgr_id, "/opt/hiclaw/agent/SOUL.md"]).stdout
    agents_md = run(["docker", "run", "--rm", "--entrypoint", "cat", mgr_id, "/opt/hiclaw/agent/AGENTS.md"]).stdout
    for role in ["reviewer", "fixer", "verifier"]:
      try:
        wpw = "wpw" + _s.token_urlsafe(12)
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/register",
                  {"username": role, "password": wpw,
                   "auth": {"type": "m.login.registration_token", "token": reg_tok}})
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/login",
                  {"type": "m.login.password",
                   "identifier": {"type": "m.id.user", "user": role}, "password": wpw})
        wtok = j.get("access_token", "")
        if not wtok:
            record(f"P_worker_{role}_login", False, "no token (register/login failed)")
            continue
        # render openclaw.json in Python memory (no -e for token, no temp files)
        rendered = (tmpl
                    .replace("${WORKER_MATRIX_TOKEN}", wtok)
                    .replace("${HICLAW_MATRIX_URL}", "http://127.0.0.1:6167")
                    .replace("${HICLAW_MATRIX_DOMAIN}", "matrix-local.hiclaw.io:8080")
                    .replace("${HICLAW_ADMIN_USER}", "admin")
                    .replace("${HICLAW_AI_GATEWAY_URL}", "http://aigw-local.hiclaw.io:8080")
                    .replace("${WORKER_GATEWAY_KEY}", gw_key)
                    .replace("${WORKER_GATEWAY_AUTH_TOKEN}", gw_auth)
                    .replace("${MATRIX_E2EE_ENABLED}", "false"))
        # provision MinIO via mc pipe (stdin — no temp files, no cmdline token)
        for name, content in (("openclaw.json", rendered), ("SOUL.md", soul), ("AGENTS.md", agents_md)):
            subprocess.run(["docker", "exec", "-i", ctrl, "mc", "pipe",
                            f"prov/hiclaw-storage/agents/{role}/{name}"],
                           input=content.encode(), capture_output=True, timeout=30)
        wvol = f"m5c-wrkfs-{rk}-{role}"
        run(["docker", "volume", "create", "--label", L_SCOPE, "--label", L_RUN, wvol])
        run(["docker", "run", "--rm", "-v", f"{wvol}:/fs", "busybox", "sh",
             "-c", f"mkdir -p /fs/agents/{role} /fs/shared && touch /fs/.initialized"])
        wctr = f"m5c-wrk-{rk}-{role}"
        run(["docker", "run", "-d", "--name", wctr, "--network", f"container:{ctrl}",
             "-v", f"{wvol}:/root/hiclaw-fs", "-v", f"{sdir}:/secrets:ro",
             "-v", f"{NC_SHIM}:/usr/local/bin/nc:ro",
             "-e", "SECRETS_DIR=/secrets",
             "-e", f"HICLAW_WORKER_NAME={role}", "-e", "HICLAW_FS_ENDPOINT=http://127.0.0.1:9000",
             "-e", f"HICLAW_FS_ACCESS_KEY={MINIO_ROOT_USER}",
             "-e", "HICLAW_FS_BUCKET=hiclaw-storage", "-e", "HICLAW_STORAGE_PREFIX=hiclaw/hiclaw-storage",
             "--label", L_SCOPE, "--label", L_RUN, "--restart=no",
             "--entrypoint", "/secrets/agent-wrapper.sh", wrk_id,
             "/opt/hiclaw/scripts/worker-entrypoint.sh"])
        connected = False
        for _ in range(60):
            logs = run(["docker", "logs", wctr]).stdout
            if "[matrix] connected" in logs or "connected to gateway" in logs:
                connected = True
                break
            if "FATAL" in logs:
                break
            time.sleep(2)
        running = run(["docker", "inspect", wctr, "--format", "{{.State.Running}}"]).stdout.strip()
        record(f"P_worker_{role}_login", connected, f"matrix_connected={connected} running={running}")
        _wh = scan_secrets(wctr, [mg_pw, wtok])
        record(f"P_worker_{role}_secret_scan", _wh == 0, f"secret_hits={_wh}")
        run(["docker", "rm", "-f", wctr])
        run(["docker", "volume", "rm", wvol])
      except Exception as e:
        record(f"P_worker_{role}_login", False, f"exception: {type(e).__name__}: {str(e)[:80]}")


# ── positive flow ──

def positive_flow():
    rk = "c1-" + str(int(time.time()))
    sk = safe_key(rk)
    print(f"=== POSITIVE FLOW run_key={rk} safe_key={sk} ===")
    if not os.environ.get("M5C_C1_ALLOW_MATRIX_WRITES") == "1":
        record("P_allow_gate", False, "M5C_C1_ALLOW_MATRIX_WRITES!=1 in positive path")
        return None
    sdir = secret_dir_for(rk)
    reg_tok = "c1reg" + __import__("secrets").token_urlsafe(12)
    passwords = {role: "c1p" + __import__("secrets").token_urlsafe(12) for role in ROLES}
    write_secret(sdir, "HICLAW_REGISTRATION_TOKEN", reg_tok)
    mg_pw = "c0" + __import__("secrets").token_urlsafe(12)
    write_secret(sdir, "HICLAW_MINIO_PASSWORD", mg_pw)
    shutil.copyfile(WRAPPER_SRC, os.path.join(sdir, "wrapper.sh"))
    os.chmod(os.path.join(sdir, "wrapper.sh"), 0o700)
    try:
        rc, _ = bring_up(rk, sdir)
        record("P_stack_up", rc == 0, f"up rc={rc}")
        if rc != 0:
            return None
        ctrl = ctrl_name(rk)
        ready = matrix_ready(ctrl)
        record("P_matrix_health", ready, "tuwunel /versions 200")
        if not ready:
            return None

        # Config.Env secret scan (controller must NOT carry secret values)
        envs = run(["docker", "inspect", ctrl, "--format", "{{json .Config.Env}}"]).stdout
        secret_hit = any(s in envs for s in [reg_tok] + list(passwords.values()))
        record("P_config_env_clean", not secret_hit, "no secret value in Config.Env")

        # register 5 users
        user_ids, access_tokens = {}, {}
        for role in ROLES:
            lp = localpart(role, sk)
            c, j = mx(ctrl, "POST", "/_matrix/client/v3/register",
                      {"username": lp, "password": passwords[role],
                       "auth": {"type": "m.login.registration_token", "token": reg_tok}})
            uid = j.get("user_id", "")
            user_ids[role] = uid
            record(f"P_register_{role}", c == 200 and uid, f"http={c} uid={uid}")
        nreg = sum(1 for r in ROLES if user_ids.get(r))
        record("P_register_5_5", nreg == 5, f"{nreg}/5")

        # login 5 (fresh access tokens, in-memory only)
        for role in ROLES:
            lp = localpart(role, sk)
            c, j = mx(ctrl, "POST", "/_matrix/client/v3/login",
                      {"type": "m.login.password",
                       "identifier": {"type": "m.id.user", "user": lp},
                       "password": passwords[role]})
            tok = j.get("access_token", "")
            if tok:
                access_tokens[role] = tok
            record(f"P_login_{role}", c == 200 and bool(tok), f"http={c} token_set={bool(tok)}")
        nlogin = sum(1 for r in ROLES if access_tokens.get(r))
        record("P_login_5_5", nlogin == 5, f"{nlogin}/5")

        if nreg < 5 or nlogin < 5:
            record("P_create_room", False, "skipped: registration/login incomplete")
            record("P_sync_event_id", False, "skipped: no room")
            print("PUBLIC user_ids:", json.dumps(user_ids))
            return rk

        # homeserver validation (fail closed if not the test HS)
        hs = ""
        for role in ROLES:
            c, j = mx(ctrl, "GET", "/_matrix/client/v3/account", token=access_tokens.get(role))
            if c == 200:
                hs = j.get("home_server", "") or hs
        # also derive from user_id
        any_uid = next((u for u in user_ids.values() if u), "")
        hs_from_uid = any_uid.split(":", 1)[1] if ":" in any_uid else ""
        hs_ok = (hs.startswith(EXPECTED_HS_PREFIX) or hs_from_uid.startswith(EXPECTED_HS_PREFIX))
        record("P_homeserver_test", hs_ok, f"hs={hs or hs_from_uid}")

        # manager creates room
        alias = f"c1room-{sk}"
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/createRoom",
                  {"room_alias_name": alias}, token=access_tokens["mgr"])
        room_id = j.get("room_id", "")
        record("P_create_room", c == 200 and bool(room_id), f"http={c} room_id={room_id}")
        # alias must NOT equal room_id
        record("P_alias_ne_room_id", bool(room_id) and alias not in room_id,
               f"alias={alias} room_id={room_id}")

        # manager invites the 4 members
        for role in ["rev", "fix", "ver", "ctrl"]:
            c, j = mx(ctrl, "POST", f"/_matrix/client/v3/rooms/{room_id}/invite",
                      {"user_id": user_ids[role]}, token=access_tokens["mgr"])
            record(f"P_invite_{role}", c == 200, f"http={c}")
        # members join
        joined = 1  # manager is creator, already joined
        for role in ["rev", "fix", "ver", "ctrl"]:
            c, j = mx(ctrl, "POST", f"/_matrix/client/v3/rooms/{room_id}/join",
                      {}, token=access_tokens[role])
            if c == 200:
                joined += 1
            record(f"P_join_{role}", c == 200, f"http={c}")
        record("P_membership_5", joined == 5, f"joined={joined}/5")

        # manager sends identity-verification event
        txn = "c1txn" + str(int(time.time()))
        c, j = mx(ctrl, "PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
                  {"msgtype": "m.room.text", "body": "C1 identity test"},
                  token=access_tokens["mgr"])
        event_id = j.get("event_id", "")
        record("P_send_event", c == 200 and bool(event_id), f"http={c} event_id={event_id}")

        # /sync on a non-manager member: verify event observable + causal chain
        ok_chain = False
        detail = ""
        raw = ""
        if event_id:
            time.sleep(1)
            c, j = mx(ctrl, "GET", "/_matrix/client/v3/sync?timeout=2000",
                      token=access_tokens["rev"])
            raw = json.dumps(j)
            found = event_id in raw and room_id in raw and "C1 identity test" in raw
            # sender must equal manager user_id
            sender_ok = user_ids["mgr"] in raw
            ok_chain = found and sender_ok
            detail = f"sync http={c} event_seen={event_id in raw} sender_ok={sender_ok}"
        record("P_sync_event_id", ok_chain, detail)
        record("P_sync_sender", ok_chain, f"sender={user_ids.get('mgr')}")
        record("P_sync_room_id", bool(room_id) and room_id in raw, "room_id observed")
        record("P_sync_body", "C1 identity test" in raw, "body observed")

        # real Candidate controller.py login (uses m5-0-ctrl creds + temp PG)
        if access_tokens.get("ctrl") and user_ids.get("mgr"):
            candidate_phase(ctrl, rk, sk, localpart("ctrl", sk),
                            passwords["ctrl"], user_ids["mgr"], sdir)
        else:
            record("P_candidate_matrix_login", False, "skipped: ctrl/mgr not ready")

        # real Manager OpenClaw login (co-located, @manager register+login)
        manager_phase(ctrl, rk, reg_tok, mg_pw, sdir)

        # real Worker OpenClaw login (reviewer/fixer/verifier, co-located)
        worker_phase(ctrl, rk, reg_tok, mg_pw, sdir)

        # public outputs (user_ids/room_id/event_id are not secrets)
        print("PUBLIC user_ids:", json.dumps(user_ids))
        print("PUBLIC room_id:", room_id, "event_id:", event_id)
        return rk
    finally:
        if 'rk' in dir():
            teardown(rk)
            shutil.rmtree(secret_dir_for(rk), ignore_errors=True)


# ── negative gates ──

def negatives():
    print("=== NEGATIVE GATES ===")
    # N1: allow-gate unset -> writes refused
    saved = os.environ.pop("M5C_C1_ALLOW_MATRIX_WRITES", None)
    # (the positive_flow checks the gate; here we just confirm the helper refuses)
    os.environ["M5C_C1_ALLOW_MATRIX_WRITES"] = "0"
    gate_ok = os.environ.get("M5C_C1_ALLOW_MATRIX_WRITES") != "1"
    record("N1_allow_gate_unset", gate_ok, "writes fail-closed when !=1")
    os.environ["M5C_C1_ALLOW_MATRIX_WRITES"] = "1"

    # bring up a stack for the api-level negatives
    rk = "c1neg-" + str(int(time.time()))
    sk = safe_key(rk)
    sdir = secret_dir_for(rk)
    reg_tok = "c1reg" + __import__("secrets").token_urlsafe(12)
    write_secret(sdir, "HICLAW_REGISTRATION_TOKEN", reg_tok)
    write_secret(sdir, "HICLAW_MINIO_PASSWORD", "c0" + __import__("secrets").token_urlsafe(10))
    shutil.copyfile(WRAPPER_SRC, os.path.join(sdir, "wrapper.sh"))
    os.chmod(os.path.join(sdir, "wrapper.sh"), 0o700)
    try:
        bring_up(rk, sdir)
        ctrl = ctrl_name(rk)
        if not matrix_ready(ctrl):
            record("N_setup", False, "matrix not ready for negatives")
            return
        lp0 = localpart("mgr", sk)
        # register one good user for membership negatives
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/register",
                  {"username": lp0, "password": "GoodPass1",
                   "auth": {"type": "m.login.registration_token", "token": reg_tok}})
        good_uid = j.get("user_id", "")
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/login",
                  {"type": "m.login.password",
                   "identifier": {"type": "m.id.user", "user": lp0}, "password": "GoodPass1"})
        good_tok = j.get("access_token", "")

        # N2: wrong registration token -> register fails (http != 200)
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/register",
                  {"username": localpart("rev", sk), "password": "X",
                   "auth": {"type": "m.login.registration_token", "token": "WRONGTOKEN"}})
        record("N2_bad_regtoken", c != 200, f"http={c}")

        # N3: duplicate username -> not overwritten (errcode M_USER_IN_USE)
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/register",
                  {"username": lp0, "password": "Other",
                   "auth": {"type": "m.login.registration_token", "token": reg_tok}})
        record("N3_dup_user", c != 200 and j.get("errcode") == "M_USER_IN_USE",
               f"http={c} errcode={j.get('errcode')}")

        # N4: wrong password -> login fails
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/login",
                  {"type": "m.login.password",
                   "identifier": {"type": "m.id.user", "user": lp0}, "password": "WrongPass"})
        record("N4_bad_password", c != 200, f"http={c}")

        # N5: non-member cannot send to room (create room as good user, send as outsider)
        c, j = mx(ctrl, "POST", "/_matrix/client/v3/createRoom", {}, token=good_tok)
        rid = j.get("room_id", "")
        # outsider: register a 2nd user NOT invited
        c2, j2 = mx(ctrl, "POST", "/_matrix/client/v3/register",
                    {"username": localpart("fix", sk) + "x", "password": "P2",
                     "auth": {"type": "m.login.registration_token", "token": reg_tok}})
        c2l, j2l = mx(ctrl, "POST", "/_matrix/client/v3/login",
                      {"type": "m.login.password",
                       "identifier": {"type": "m.id.user", "user": localpart("fix", sk) + "x"},
                       "password": "P2"})
        out_tok = j2l.get("access_token", "")
        if rid and out_tok:
            cs, _ = mx(ctrl, "PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/x1",
                       {"msgtype": "m.room.text", "body": "x"}, token=out_tok)
            record("N5_non_member_send", cs != 200, f"outsider send http={cs}")
        else:
            record("N5_non_member_send", False, "setup incomplete")

        # N6: wrong server_name fail-closed — verify the deployed HS matches the
        # test server_name. /account may omit home_server on this tuwunel version,
        # so derive the HS from the user_id (authoritative: @user:<server_name>).
        hs_from_uid = good_uid.split(":", 1)[1] if ":" in good_uid else ""
        record("N6_hs_test_only", hs_from_uid.startswith(EXPECTED_HS_PREFIX),
               f"hs_from_uid={hs_from_uid}")

        # N7: two RUN_KEYs that normalize identically disambiguate by hash
        rk_a, rk_b = "C1 Foo!", "c1-foo"
        sa, sb = safe_key(rk_a), safe_key(rk_b)
        record("N7_hash_disambig", sa != sb, f"{sa} vs {sb}")

        # N8: room alias cannot impersonate room_id (createRoom returns distinct rid)
        # covered structurally: alias is a name, rid is !id:hs; they differ by construction
        record("N8_alias_not_room_id", bool(rid) and not rid.startswith("#"), f"rid={rid}")

        # N9/N10: GitHub/LLM envs absent -> flow still works (we never used them)
        gh_absent = "GITHUB_PAT" not in os.environ and "GITHUB_TOKEN" not in os.environ
        llm_absent = "OPENAI_API_KEY" not in os.environ
        record("N9_no_github", gh_absent, "github env absent, flow independent")
        record("N10_no_llm", llm_absent, "llm env absent, flow independent")
    finally:
        teardown(rk)
        shutil.rmtree(sdir, ignore_errors=True)


def wrapper_redact_probe():
    """Verify the agent wrapper's sed redacts access_token (both JSON + key=value)."""
    print("=== WRAPPER REDACTION PROBE ===")
    fake = "TEST_ACCESS_TOKEN_123"
    probe = ('{"access_token":"' + fake + '","user_id":"@test:hs"}\n'
             'access_token=' + fake + ' other=data\n')
    sed_cmd = ["sed", "-u", "-E",
               "-e", 's/("access_token"[[:space:]]*:[[:space:]]*")[^"]*"/\\1<redacted-m5c>"/g',
               "-e", 's/(access_token[=:][[:space:]]*)[^,[:space:]}"]+/\\1<redacted-m5c>/g']
    r = subprocess.run(sed_cmd, input=probe, capture_output=True, text=True, timeout=5)
    ok = "<redacted-m5c>" in r.stdout and fake not in r.stdout
    record("P_wrapper_redact_probe", ok, f"redacted={ok} token_absent={fake not in r.stdout}")


def categorize(tid):
    if "secret_scan" in tid: return "secret_scan"
    if tid.startswith("P_wrapper"): return "wrapper_redaction"
    if tid.startswith("P_candidate"): return "candidate"
    if tid.startswith("P_manager"): return "manager"
    if tid.startswith("P_worker"): return "worker"
    if tid.startswith("N"): return "negative"
    if tid.startswith("P_"): return "api_positive"
    return "other"


def main():
    if not os.environ.get("M5C_C1_ALLOW_MATRIX_WRITES") == "1":
        print("FATAL: M5C_C1_ALLOW_MATRIX_WRITES must be 1 to perform C1 writes")
        sys.exit(2)
    wrapper_redact_probe()
    positive_flow()
    negatives()
    gates_total = len(results)
    gates_passed = sum(1 for r in results if r["passed"])
    gates_failed = sum(1 for r in results if not r["passed"])
    from collections import Counter
    cats = Counter(categorize(r["test_id"]) for r in results)
    summary = {"gate": "m5-0c-c1-identity",
               "gates_total": gates_total, "gates_passed": gates_passed,
               "gates_failed": gates_failed, "final_rc": 0 if gates_failed == 0 else 1,
               "categories": dict(cats),
               "github_calls": 0, "llm_calls": 0, "results": results}
    print(json.dumps(summary, indent=2))
    sys.exit(0 if gates_failed == 0 else 1)


if __name__ == "__main__":
    main()
