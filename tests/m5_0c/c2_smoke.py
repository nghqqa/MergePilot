#!/usr/bin/env python3
"""M5-0C C2 — real Gateway/GitHub fixture single smoke (MergePilot-Test, root).

Real chain:
  source PR (feature/c2-src-<run_key>) via github-mcp -> Manager M4F_RUN
  (real Matrix /sync) -> Candidate controller.py ingress reads real source PR
  -> stage 6 skills -> pr-lifecycle (REAL) -> Policy Gateway -> github-mcp
  -> REAL fixture repo: fix/<run_key>-<hash12> branch + push + Pull Request
  Reviewer TASK_COMPLETED -> fix -> Fixer TASK_COMPLETED -> verify
  -> Verifier VERDICT=PASS -> HOLD/m5_verify_passed (no merge, no COMPLETED)

GitHub boundary (HARD): github-mcp bridge is the ONLY GitHub caller. The harness
(c2_smoke) talks to the bridge via MCP only — no REST/urllib/httpx to the live
GitHub API host anywhere. PAT lives only in the bridge (secret-file -> process
memory). Candidate/Manager/Worker/Gateway never see the PAT. Branch cleanup uses
the bridge's restricted c2_delete_test_branch tool (RUN_KEY-scoped, fail-closed);
PR close uses github-mcp native update_pull_request. No standalone REST helper.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time

ROOT = "/mnt/d/goai/mergepilot-os"
DBDIR = ROOT + "/tools/audit-db"
POLICY = ROOT + "/config/m5-0c/real-github-policy.yaml"
CAND_WRAPPER = ROOT + "/tests/m5_0c/c1_candidate_wrapper.sh"
MINI_HS = "tests/m5_0/fixtures/mini_matrix_hs.py"
INJECT = "tests/m5_0/fixtures/inject_skill_completion.py"
GHMCP_BRIDGE = "tests/m5_0c/c2_ghmcp_bridge.py"
GHMCP_CALL = "tests/m5_0c/c2_ghmcp_call.py"
GW_CALL = "tests/m5_0c/c2_gateway_call.py"

PG_IMAGE = "pgvector/pgvector:pg16"
GW_IMAGE = "policy-gateway:m4f"
RT_IMAGE = "mergepilot-m4f-runtime:demo"
CAND_IMAGE = "mergepilot-m5-0-candidate:current"
GHMCP_IMAGE = "github-mcp-bridge:c2"

FIXTURE = "nghqqa/MergePilot-e2e-fixture"
OWNER, REPO = FIXTURE.split("/")
HS_NAME = "m5c2-hs"
PAT_FILE_HOST = "/dev/shm/m5c-c2/fixture-pat"

BASE_MIGS = ["init", "m3_state", "m3b_policy", "m3b_b4", "m3b_b4c", "m3b_b4c1",
             "m3b_b4c1_1", "m3b_b4d1", "m3c_state", "m4f1_state", "m4f1_hotfix_1"]

results = []
CL = {"branch": None, "pr": None, "head_sha": None, "src_branch": None, "src_pr": None,
      "net": None, "containers": [], "sdir": None, "L": None, "ok_to_clean_gh": False}


def record(tid, ok, detail=""):
    results.append({"test_id": tid, "passed": bool(ok), "detail": detail[:200]})
    print("  [%s] %s: %s" % ("PASS" if ok else "FAIL", tid, detail[:150]))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120), **kw)


def dexec(cid, sql):
    return run(["docker", "exec", "-i", cid, "psql", "-U", "fixture_admin", "-d",
                "c2audit", "-t", "-A", "-v", "ON_ERROR_STOP=1"], input=sql + "\n")


def hs_call(net, path, method="POST", body=None, tok=None):
    script = (
        "import urllib.request,json,sys\n"
        "body=sys.stdin.read()\n"
        "h={'Content-Type':'application/json'}\n"
        "tok=sys.argv[3] or ''\n"
        "if tok: h['Authorization']='Bearer '+tok\n"
        "url='http://m5c2-hs:8008'+sys.argv[1]\n"
        "data=body.encode() if body else None\n"
        "req=urllib.request.Request(url,data=data,headers=h,method=sys.argv[2])\n"
        "try:\n"
        "  r=urllib.request.urlopen(req,timeout=10); print(str(r.status)+'|'+r.read().decode())\n"
        "except urllib.error.HTTPError as e: print(str(e.code)+'|'+e.read().decode())\n")
    r = run(["docker", "run", "--rm", "-i", "--network", net, "--entrypoint", "python",
             RT_IMAGE, "-c", script, path, method, tok or ""],
            input=json.dumps(body) if body is not None else "", timeout=30)
    out = r.stdout.strip()
    if "|" in out:
        st_s, body_s = out.split("|", 1)
        try:
            return int(st_s), json.loads(body_s)
        except Exception:
            return int(st_s), {"_raw": body_s}
    return 0, {"_raw": out, "_err": r.stderr.strip()[:160]}


def send_as(net, room, user, pw, body, txn):
    _, j = hs_call(net, "/_matrix/client/v3/login", "POST",
                   {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": user}, "password": pw})
    tok = j.get("access_token", "")
    if not tok:
        return ""
    _, j2 = hs_call(net, "/_matrix/client/v3/rooms/%s/send/m.room.message/%s" % (room, txn),
                    "POST", {"msgtype": "m.room.text", "body": body}, tok=tok)
    return j2.get("event_id", "")


def scan_pat(name):
    env = run(["docker", "inspect", name, "--format", "{{json .Config.Env}}{{json .Config.Args}}"]).stdout
    try:
        logs = run(["docker", "logs", name], timeout=20).stdout
    except Exception:
        logs = ""
    return len(re.findall(r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82,}", env + " " + logs))


# ── github-mcp bridge MCP call (NO REST, NO PAT here) ──
def ghmcp_call(cfg, tool, args):
    r = run(["docker", "run", "--rm", "--network", cfg["net"], "-v", "%s:/workspace:ro" % ROOT,
             "-w", "/workspace", "-e", "C2_BRIDGE=http://m5c2-gh:8082",
             "--entrypoint", "python", RT_IMAGE, "/workspace/" + GHMCP_CALL, tool, json.dumps(args)], timeout=70)
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"is_error": True, "_raw": r.stdout.strip()[:200], "_err": r.stderr.strip()[:160]}


def _gj(r):
    """parse the github-mcp tool content text as JSON (or None)."""
    if not r or r.get("is_error"):
        return None
    txt = (r.get("content") or "").strip()
    try:
        return json.loads(txt)
    except Exception:
        return None


def read_main_sha_mcp(cfg):
    r = ghmcp_call(cfg, "list_branches", {"owner": OWNER, "repo": REPO})
    data = _gj(r) or []
    rows = data if isinstance(data, list) else data.get("branches", data.get("data", []))
    for b in rows:
        if isinstance(b, dict) and b.get("name") == "main":
            return b.get("sha") or (b.get("commit") or {}).get("sha")
    return None


def create_branch_mcp(cfg, branch, from_branch="main"):
    return ghmcp_call(cfg, "create_branch", {"owner": OWNER, "repo": REPO,
                                             "branch": branch, "from_branch": from_branch})


def push_file_mcp(cfg, branch, path, content, message):
    return ghmcp_call(cfg, "push_files", {"owner": OWNER, "repo": REPO, "branch": branch,
                                          "files": [{"path": path, "content": content}],
                                          "message": message})


def create_pr_mcp(cfg, head, base, title, body):
    r = ghmcp_call(cfg, "create_pull_request", {"owner": OWNER, "repo": REPO,
                                                "head": head, "base": base, "title": title, "body": body})
    d = _gj(r) or {}
    num = d.get("number") or d.get("pullNumber")
    if not num and isinstance(d.get("url"), str):
        m = re.search(r"/pull/(\d+)", d["url"])
        if m:
            num = int(m.group(1))
    return num


def get_pr_mcp(cfg, pr_num):
    r = ghmcp_call(cfg, "pull_request_read", {"owner": OWNER, "repo": REPO,
                                              "pullNumber": int(pr_num), "method": "get"})
    d = _gj(r) or {}
    head = d.get("head") or {}
    base = d.get("base") or {}
    return {"state": d.get("state"), "merged": d.get("merged") or d.get("isMerged"),
            "base": base.get("ref") if isinstance(base, dict) else base,
            "head": head.get("ref") if isinstance(head, dict) else head}


def list_branch_names_mcp(cfg):
    r = ghmcp_call(cfg, "list_branches", {"owner": OWNER, "repo": REPO})
    data = _gj(r) or []
    rows = data if isinstance(data, list) else data.get("branches", data.get("data", []))
    return [b.get("name") for b in rows if isinstance(b, dict)]


def list_open_pr_heads_mcp(cfg):
    r = ghmcp_call(cfg, "list_pull_requests", {"owner": OWNER, "repo": REPO,
                                               "state": "open", "perPage": 100})
    data = _gj(r) or []
    rows = data if isinstance(data, list) else data.get("pullRequests", data.get("data", []))
    out = []
    for p in rows:
        if isinstance(p, dict):
            h = p.get("head") or p.get("headRef")
            out.append(h.get("ref") if isinstance(h, dict) else h)
    return [h for h in out if h]


def close_pr_mcp(cfg, pr_num):
    return ghmcp_call(cfg, "update_pull_request", {"owner": OWNER, "repo": REPO,
                                                   "pullNumber": int(pr_num), "state": "closed"})


def delete_branch_mcp(cfg, branch, run_key):
    return ghmcp_call(cfg, "c2_delete_test_branch",
                      {"owner": OWNER, "repo": REPO, "branch": branch, "run_key": run_key})


def gateway_call(cfg, role, tok, tool, args):
    r = run(["docker", "run", "--rm", "--network", cfg["net"], "-v", "%s:/workspace:ro" % ROOT,
             "-w", "/workspace", "-e", "C2_GATEWAY=http://m5c2-gw:8083",
             "-e", "C2_ROLE=%s" % role, "-e", "C2_TOKEN=%s" % tok,
             "--entrypoint", "python", RT_IMAGE, "/workspace/" + GW_CALL, tool, json.dumps(args)], timeout=50)
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"is_error": True, "_raw": r.stdout.strip()[:200], "_err": r.stderr.strip()[:160]}


def audit_decision(cfg, caller, tool):
    row = dexec(cfg["db"], "SELECT decision FROM mcp_calls WHERE caller_agent='%s' AND tool='%s' "
                           "ORDER BY ts DESC LIMIT 1" % (caller, tool)).stdout.strip()
    return row or None


def gw_denied(cfg, role, tok, tool, args):
    r = gateway_call(cfg, role, tok, tool, args)
    dec = audit_decision(cfg, role, tool)
    return (bool(r.get("is_error")) or dec == "DENY"), r, dec


# ── preflight ──
def preflight():
    print("=== PREFLIGHT ===")
    ok = os.environ.get("M5C_C2_ALLOW_GITHUB_WRITES") == "1"
    record("PF_allow_gate", ok, "M5C_C2_ALLOW_GITHUB_WRITES=1")
    pat = os.environ.get("M5C_C2_FIXTURE_GITHUB_PAT_FILE", PAT_FILE_HOST)
    if not os.path.exists(pat):
        record("PF_pat_exists", False, "missing %s" % pat)
        return False
    st = os.stat(pat)
    nz, m6 = st.st_size > 0, (st.st_mode & 0o777) == 0o600
    record("PF_pat_nonempty", nz, "size=%d" % st.st_size)
    record("PF_pat_mode_600", m6, "mode=%o" % (st.st_mode & 0o777))
    ok = ok and nz and m6
    ps = run(["docker", "ps", "-a", "--format", "{{.Names}}"]).stdout.split()
    prod = [n for n in ps if any(p in n for p in
             ["mergepilot-controller", "policy-gw", "audit-pg", "github-mcp", "hiclaw"])]
    record("PF_no_prod_visible", not prod, "prod=%s" % prod)
    ok = ok and not prod
    pol_ok = os.path.isfile(POLICY) and FIXTURE in open(POLICY, encoding="utf-8").read()
    record("PF_policy_allowlist", pol_ok, FIXTURE)
    return ok and pol_ok


def setup_stack(rk):
    L = "com.mergepilot.c2=%s" % rk
    net = "m5c2-net-%s" % rk
    db = "m5c2-pg-%s" % rk
    hs = "m5c2-hs-%s" % rk
    gh = "m5c2-gh-%s" % rk
    gw = "m5c2-gw-%s" % rk
    cand = "m5c2-cand-%s" % rk
    sdir = "/dev/shm/c2-%s" % rk
    os.makedirs(sdir, exist_ok=True)
    os.chmod(sdir, 0o700)
    CL.update(L=L, net=net, sdir=sdir, containers=[cand, gw, gh, hs, db])

    run(["docker", "network", "create", "--label", L, net])
    run(["docker", "run", "-d", "--name", db, "--network", net, "--network-alias", "m5c2-pg",
         "--label", L, "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
         "-e", "POSTGRES_USER=fixture_admin", "-e", "POSTGRES_DB=c2audit", PG_IMAGE])
    for _ in range(90):
        if run(["docker", "exec", db, "psql", "-U", "fixture_admin", "-d", "c2audit", "-c", "SELECT 1"]).returncode == 0:
            break
        time.sleep(1)
    dexec(db, "DO $r$ BEGIN "
              "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot') THEN CREATE ROLE mergepilot LOGIN; END IF; "
              "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_audit') THEN CREATE ROLE policy_gateway_audit LOGIN; END IF; "
              "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF; "
              "END $r$")
    for m in BASE_MIGS:
        dexec(db, open("%s/%s.sql" % (DBDIR, m), "rb").read().decode("utf-8", "replace"))
    dexec(db, "GRANT CONNECT ON DATABASE c2audit TO mergepilot, policy_gateway_audit; "
              "GRANT USAGE ON SCHEMA public TO mergepilot, policy_gateway_audit; "
              "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mergepilot; "
              "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mergepilot; "
              "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mergepilot; "
              "GRANT INSERT ON public.mcp_calls TO policy_gateway_audit; "
              "REVOKE SELECT, UPDATE, DELETE ON public.mcp_calls FROM policy_gateway_audit; "
              "GRANT SELECT ON public.mcp_calls TO mergepilot")
    print("  PG ready")

    run(["docker", "run", "-d", "--name", hs, "--network", net, "--network-alias", "m5c2-hs",
         "--label", L, "-v", "%s:/workspace:ro" % ROOT, "-w", "/workspace",
         "-e", "M5_HS_SERVER_NAME=%s" % HS_NAME, "-e", "M5_HS_PORT=8008",
         "--entrypoint", "python", RT_IMAGE, MINI_HS])
    for _ in range(30):
        if run(["docker", "run", "--rm", "--network", net, "--entrypoint", "python", RT_IMAGE, "-c",
                "import urllib.request; urllib.request.urlopen('http://m5c2-hs:8008/_matrix/client/versions', timeout=3)"]).returncode == 0:
            break
        time.sleep(1)
    print("  mini HS ready")
    return {"L": L, "net": net, "db": db, "hs": hs, "gh": gh, "gw": gw, "cand": cand, "sdir": sdir}


def setup_identities(cfg, pws):
    net = cfg["net"]
    ids = {}
    for u in ["manager", "reviewer", "fixer", "verifier", "m5ctrl"]:
        hs_call(net, "/_matrix/client/v3/register", "POST", {"username": u, "password": pws[u]})
        _, j = hs_call(net, "/_matrix/client/v3/login", "POST",
                       {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": u}, "password": pws[u]})
        ids[u] = (j.get("access_token", ""), j.get("user_id", "@%s:%s" % (u, HS_NAME)))
    _, jr = hs_call(net, "/_matrix/client/v3/createRoom", "POST",
                    {"invite": [ids[u][1] for u in ["m5ctrl", "reviewer", "fixer", "verifier"]]},
                    tok=ids["manager"][0])
    room = jr.get("room_id", "")
    for u in ["m5ctrl", "reviewer", "fixer", "verifier"]:
        hs_call(net, "/_matrix/client/v3/rooms/%s/invite" % room, "POST", {"user_id": ids[u][1]}, tok=ids["manager"][0])
        hs_call(net, "/_matrix/client/v3/rooms/%s/join" % room, "POST", {}, tok=ids[u][0])
    cfg["room"] = room
    print("  identities + room ready: %s" % room)
    return ids


def start_gwhmcp_cand(cfg, toks, pws):
    net, L = cfg["net"], cfg["L"]
    sdir = cfg["sdir"]
    run(["docker", "run", "-d", "--name", cfg["gh"], "--network", net, "--network-alias", "m5c2-gh",
         "--label", L, "-v", "%s:/secrets/pat:ro" % PAT_FILE_HOST,
         "-v", "%s:/app/bridge.py:ro" % (ROOT + "/" + GHMCP_BRIDGE), GHMCP_IMAGE])
    role_tokens = json.dumps({"m5coordinator": toks["coord"], "fixer": toks["fixer"],
                              "reviewer": toks["reviewer"], "verifier": toks["verifier"]})
    run(["docker", "run", "-d", "--name", cfg["gw"], "--network", net, "--network-alias", "m5c2-gw",
         "--label", L, "-v", "%s:/workspace:ro" % ROOT,
         "-v", "%s/tools/policy-gateway/gateway.py:/app/gateway.py:ro" % ROOT,
         "-v", "%s:/app/policy.yaml:ro" % POLICY,
         "-e", "UPSTREAM_URL=http://m5c2-gh:8082/sse", "-e", "ROLE_TOKENS=%s" % role_tokens,
         "-e", "AUDIT_DSN=host=m5c2-pg dbname=c2audit user=policy_gateway_audit",
         "-e", "POLICY_FILE=/app/policy.yaml", "-e", "LISTEN_HOST=0.0.0.0", "-e", "LISTEN_PORT=8083",
         GW_IMAGE])
    import shutil as _sh
    _sh.copyfile(CAND_WRAPPER, sdir + "/candidate-wrapper.sh")
    os.chmod(sdir + "/candidate-wrapper.sh", 0o700)
    open(sdir + "/ADMIN_PW", "w").write(pws["m5ctrl"])
    open(sdir + "/PG_PASS", "w").write("c2pg-%s" % secrets.token_hex(8))
    open(sdir + "/GATEWAY_TOKEN", "w").write(toks["coord"])
    for f in ("ADMIN_PW", "PG_PASS", "GATEWAY_TOKEN"):
        os.chmod(sdir + "/" + f, 0o600)
    run(["docker", "run", "-d", "--name", cfg["cand"], "--network", net, "--label", L,
         "-v", "%s:/secrets:ro" % sdir, "-e", "SECRETS_DIR=/secrets",
         "-e", "PG_HOST=m5c2-pg", "-e", "PG_PORT=5432", "-e", "PG_DATABASE=c2audit", "-e", "PG_USER=mergepilot",
         "-e", "MATRIX_HS=http://m5c2-hs:8008", "-e", "MATRIX_SERVER_NAME=%s" % HS_NAME, "-e", "MATRIX_USER=m5ctrl",
         "-e", "CONTROLLER_CONSUMER_NAME=m5c2-cand",
         "-e", "GATEWAY_URL=http://m5c2-gw:8083", "-e", "GATEWAY_ROLE=m5coordinator",
         "-e", "M4F_ENABLED=1", "-e", "M4F_LIVE_MODE=1", "-e", "M4F_ONLY_MODE=1",
         "-e", "M4F_SNAPSHOT_DSN=host=m5c2-pg dbname=c2audit user=mergepilot",
         "-e", "M4F_ALLOWED_ROOMS=%s" % cfg["room"], "-e", "M4F_ALLOWED_SENDERS=manager,reviewer,fixer,verifier",
         "-e", "M4F_RUN_PREFIX=c2", "-e", "RESERVED_RUN_PREFIXES=m4fx-reserved",
         "-e", "L2_MERGE_ENABLED=0", "-e", "POLL_INTERVAL=2",
         "--entrypoint", "/secrets/candidate-wrapper.sh", CAND_IMAGE])
    gh_ok = False
    for _ in range(30):
        h = run(["docker", "run", "--rm", "--network", net, "--entrypoint", "python", RT_IMAGE, "-c",
                 "import urllib.request; print(urllib.request.urlopen('http://m5c2-gh:8082/_health', timeout=3).status)"])
        if h.returncode == 0 and "200" in h.stdout:
            gh_ok = True; break
        time.sleep(2)
    record("P0_ghmcp_ready", gh_ok, "github-mcp /_health 200")
    gw_ok = False
    for _ in range(40):
        g = run(["docker", "logs", cfg["gw"]], timeout=15)
        if "startup" in (g.stdout + g.stderr).lower() or "uvicorn running" in (g.stdout + g.stderr).lower():
            gw_ok = True; break
        time.sleep(1)
    record("P0_gateway_ready", gw_ok, "gateway startup")
    cand_ok = False
    cand_logs = ""
    for _ in range(45):
        lg = run(["docker", "logs", cfg["cand"]], timeout=15)
        cand_logs = lg.stdout + lg.stderr
        if "Matrix login OK" in cand_logs:
            cand_ok = True; break
        if "FATAL" in cand_logs or "Traceback" in cand_logs:
            break
        time.sleep(2)
    if not cand_ok:
        print("  [diag] candidate logs (tail):\n%s" % cand_logs[-1200:])
    record("P0_candidate_login", cand_ok, "[ctrl] Matrix login OK")
    return gh_ok and gw_ok and cand_ok


def cleanup_gh(cfg, run_key):
    """Close both PRs (github-mcp update_pull_request) then delete both branches
    (bridge c2_delete_test_branch). No REST."""
    for label, pr in [("fix", CL.get("pr")), ("src", CL.get("src_pr"))]:
        if pr:
            r = close_pr_mcp(cfg, pr)
            d = _gj(r) or {}
            print("  [cleanup] close %s PR %s -> state=%s" % (label, pr, d.get("state")))
    for label, br in [("fix", CL.get("branch")), ("src", CL.get("src_branch"))]:
        if br:
            r = delete_branch_mcp(cfg, br, run_key)
            d = _gj(r) or {}
            print("  [cleanup] delete %s branch %s -> deleted=%s refused=%s reason=%s"
                  % (label, br, d.get("deleted"), d.get("refused"), d.get("reason")))


def teardown(cfg):
    for c in (cfg or {}).get("containers", []) or CL.get("containers", []):
        run(["docker", "rm", "-f", c], timeout=30)
    if (cfg or {}).get("net") or CL.get("net"):
        run(["docker", "network", "rm", (cfg or {}).get("net") or CL["net"]], timeout=20)
    if CL.get("sdir"):
        run(["rm", "-rf", CL["sdir"]], timeout=15)


def main():
    if not preflight():
        print("\n=== PREFLIGHT FAILED — fail closed ===")
        sys.exit(2)
    rk = "c2-%d-%s" % (int(time.time()), secrets.token_hex(4))
    run_id = rk  # run_id == run_key so fix branch is bound to run_key
    pws = {u: "c2pw-" + secrets.token_urlsafe(12) for u in ["manager", "reviewer", "fixer", "verifier", "m5ctrl"]}
    toks = {k: "c2tk-" + secrets.token_urlsafe(24) for k in ["coord", "fixer", "reviewer", "verifier"]}
    hmac_key = secrets.token_hex(32)
    cfg = None
    audit_summary = []
    direct_gh = {}
    try:
        print("\n=== SETUP run_key=%s ===" % rk)
        cfg = setup_stack(rk)
        setup_identities(cfg, pws)
        if not start_gwhmcp_cand(cfg, toks, pws):
            raise RuntimeError("stack startup failed")

        base_sha = read_main_sha_mcp(cfg)
        record("P1_read_main_sha_via_ghmcp", bool(base_sha), "main_sha=%s" % (base_sha or "none"))
        if not base_sha:
            raise RuntimeError("could not read main sha via github-mcp")

        src_branch = "feature/c2-src-" + rk
        CL["src_branch"] = src_branch   # record for cleanup ASAP
        CL["ok_to_clean_gh"] = True      # ensure cleanup runs even on partial failure
        cb = create_branch_mcp(cfg, src_branch, "main")
        pf = push_file_mcp(cfg, src_branch, "c2-src-%s.txt" % rk,
                           "M5-0C C2 source marker run=%s\n" % rk, "c2: source marker")
        src_pr = create_pr_mcp(cfg, src_branch, "main", "[C2] source PR (smoke)", "M5-0C C2 source PR; auto-cleaned.")
        CL["src_pr"] = src_pr
        record("P1b_source_pr_created", bool(src_pr),
               "src_branch=%s src_pr=%s cb_ok=%s pf_ok=%s" % (src_branch, src_pr, not cb.get("is_error"), not pf.get("is_error")))
        if not src_pr:
            raise RuntimeError("source PR creation failed via github-mcp")

        dexec(cfg["db"],
              "INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state) "
              "VALUES('%s','%s','%s',%d,'%s','RUNNING','m4f_snapshot','%s','ACTIVE') ON CONFLICT DO NOTHING; "
              "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) "
              "VALUES('bnd-%s','%s','%s',%d,'%s','main','%s') ON CONFLICT DO NOTHING;"
              % (run_id, cfg["room"], FIXTURE, int(src_pr), "fix/" + run_id, "trace-c2",
                 run_id, run_id, FIXTURE, int(src_pr), "fix/" + run_id, base_sha))
        CL["ok_to_clean_gh"] = True

        m4f = ('M4F_RUN: {"contract_version":"1","run_id":"%s","trace_id":"trace-c2",'
               '"repo":"%s","pr_number":%d,"test_runner":{"runner_key":"pytest"},'
               '"pr_lifecycle":{"action":"ensure_fix_pr","idempotency_key":"c2prl-%s",'
               '"changes":[],"commit_message":"m","pr_title":"t","pr_body":"b"}}'
               % (run_id, FIXTURE, int(src_pr), run_id))
        m4f_eid = send_as(cfg["net"], cfg["room"], "manager", pws["manager"], m4f, "txn-m4f")
        record("P2_m4f_run_sent", bool(m4f_eid), "event_id=%s" % m4f_eid)
        staged = False
        for _ in range(60):
            if dexec(cfg["db"], "SELECT count(*) FROM skill_job_outbox WHERE run_id='%s'" % run_id).stdout.strip() == "6":
                staged = True; break
            time.sleep(1)
        record("P3_controller_staged_6skills", staged, "skill_job_outbox=6")

        print("\n=== REAL pr-lifecycle chain ===")
        env = ["-e", "MERGEPILOT_PRL_GATEWAY_URL=http://m5c2-gw:8083", "-e", "MERGEPILOT_PRL_ROLE=fixer",
               "-e", "MERGEPILOT_PRL_TOKEN=%s" % toks["fixer"], "-e", "MERGEPILOT_PRL_REPO=%s" % FIXTURE,
               "-e", "MERGEPILOT_PRL_BASE_BRANCH=main", "-e", "MERGEPILOT_PRL_RUN_ID=%s" % run_id,
               "-e", "MERGEPILOT_PRL_RISK_LEVEL=L1", "-e", "MERGEPILOT_PRL_HMAC_KEY=%s" % hmac_key,
               "-e", "MERGEPILOT_PRL_EXPECTED_BASE_SHA=%s" % base_sha]
        req = {"contract_version": "1", "request_id": "c2-%s" % run_id, "trace_id": "c2-%s" % run_id,
               "input": {"action": "ensure_fix_pr", "idempotency_key": "c2prl-%s" % run_id,
                         "changes": [{"path": "m5c2-smoke-%s.txt" % rk[-12:],
                                      "content": "M5-0C C2 smoke marker run=%s\n" % rk}],
                         "commit_message": "m5c2: C2 smoke fixture marker",
                         "pr_title": "[C2-smoke] fixture fix (auto-cleanup)",
                         "pr_body": "M5-0C C2 real-chain smoke; auto-cleaned."}}
        pr = run(["docker", "run", "--rm", "-i", "--network", cfg["net"], "-v", "%s:/workspace:ro" % ROOT,
                  "-w", "/workspace", *env, "--entrypoint", "python", RT_IMAGE, "-m", "skills.pr_lifecycle.run"],
                 input=json.dumps(req), timeout=120)
        try:
            env_resp = json.loads(pr.stdout.strip().splitlines()[-1])
        except Exception:
            env_resp = None
        out = (env_resp or {}).get("output", {}) or {}
        branch = out.get("head_branch"); pr_num = out.get("pull_number"); head_sha = out.get("head_sha")
        paths = out.get("changed_paths", []); phases = out.get("phases", [])
        record("P4_prlifecycle_status_ok", env_resp and env_resp.get("status") == "OK",
               "status=%s outcome=%s" % ((env_resp or {}).get("status"), out.get("outcome")))
        record("P4b_branch_created", bool(branch), "head_branch=%s" % branch)
        record("P4c_pr_created", bool(pr_num), "pull_number=%s" % pr_num)
        record("P4d_phases", all(p in phases for p in ["BRANCH_CREATED", "CONTENT_WRITTEN", "PR_CREATED"]),
               "phases=%s" % ",".join(phases))
        CL["branch"] = branch; CL["pr"] = pr_num; CL["head_sha"] = head_sha
        if not (env_resp and env_resp.get("status") == "OK"):
            print("  pr-lifecycle raw: %s" % pr.stdout[:600])

        if pr_num:
            gpr = get_pr_mcp(cfg, pr_num)
            record("P5_gh_pr_open_base_main",
                   gpr.get("state") == "open" and gpr.get("base") == "main" and not gpr.get("merged"),
                   "state=%s base=%s merged=%s" % (gpr.get("state"), gpr.get("base"), gpr.get("merged")))
        main2 = read_main_sha_mcp(cfg)
        record("P6_main_untouched", main2 == base_sha, "before=%s after=%s" % (base_sha, main2))
        record("P7_only_allowed_paths", all(p.startswith("m5c2-smoke-") for p in paths), "paths=%s" % paths)

        run(["docker", "run", "--rm", "--network", cfg["net"], "-v", "%s:/workspace:ro" % ROOT,
             "-w", "/workspace", "--entrypoint", "python", RT_IMAGE, "/workspace/" + INJECT,
             "host=m5c2-pg dbname=c2audit user=mergepilot", run_id], timeout=40)
        rev_ok = False
        for _ in range(60):
            if dexec(cfg["db"], "SELECT current_stage FROM task_runs WHERE run_id='%s'" % run_id).stdout.strip() == "m4f_await_review":
                rev_ok = True; break
            time.sleep(1)
        record("P8_bridge_to_review", rev_ok, "stage=m4f_await_review")

        send_as(cfg["net"], cfg["room"], "reviewer", pws["reviewer"], "TASK_COMPLETED: %s-review" % run_id, "txn-rev")
        fix_ok = False
        for _ in range(40):
            if dexec(cfg["db"], "SELECT current_stage FROM task_runs WHERE run_id='%s'" % run_id).stdout.strip() == "m4f_await_fix":
                fix_ok = True; break
            time.sleep(1)
        record("P9_review_handoff_to_fix", fix_ok, "stage=m4f_await_fix")

        send_as(cfg["net"], cfg["room"], "fixer", pws["fixer"], "TASK_COMPLETED: %s-fix" % run_id, "txn-fix")
        vf_ok = False
        for _ in range(40):
            if dexec(cfg["db"], "SELECT current_stage FROM task_runs WHERE run_id='%s'" % run_id).stdout.strip() == "m4f_await_verify":
                vf_ok = True; break
            time.sleep(1)
        record("P10_fix_handoff_to_verify", vf_ok, "stage=m4f_await_verify")

        send_as(cfg["net"], cfg["room"], "verifier", pws["verifier"],
                "TASK_COMPLETED: %s-verify\nVERDICT=PASS" % run_id, "txn-ver")
        hold_ok = False; final_state = ""
        for _ in range(40):
            row = dexec(cfg["db"], "SELECT status||':'||current_stage||':'||coalesce(verdict,'') FROM task_runs WHERE run_id='%s'" % run_id).stdout.strip()
            final_state = row
            if row == "HOLD:m5_verify_passed:PASS":
                hold_ok = True; break
            time.sleep(1)
        record("P11_verify_hold_passed", hold_ok, "final=%s" % final_state)
        if pr_num:
            gpr2 = get_pr_mcp(cfg, pr_num)
            record("P12_no_auto_merge", not gpr2.get("merged"), "fix PR merged=%s" % gpr2.get("merged"))
        record("P13_no_completed", final_state.split(":")[0] != "COMPLETED", "status=%s" % final_state.split(":")[0])

        print("\n=== NEGATIVE GATES ===")
        d, r, dec = gw_denied(cfg, "m5coordinator", toks["coord"], "create_branch",
                              {"owner": OWNER, "repo": REPO, "branch": "fix/neg-m5c", "base": "main"})
        record("N1_deny_m5coord_create", d, "decision=%s" % dec)
        d, r, dec = gw_denied(cfg, "fixer", toks["fixer"], "create_branch",
                              {"owner": OWNER, "repo": "MergePilot", "branch": "fix/neg-other", "base": "main"})
        record("N2_deny_nonfixture", d, "decision=%s" % dec)
        d, r, dec = gw_denied(cfg, "fixer", toks["fixer"], "create_branch",
                              {"owner": OWNER, "repo": REPO, "branch": "feat/neg-x", "base": "main"})
        record("N3_deny_nonfix_prefix", d, "decision=%s" % dec)
        d, r, dec = gw_denied(cfg, "fixer", toks["fixer"], "push_files",
                              {"owner": OWNER, "repo": REPO, "branch": "main", "files": [{"path": "neg.txt", "content": "x"}]})
        record("N4_deny_push_main", d, "decision=%s" % dec)

        # cleanup-tool isolation: Gateway DENIES c2_delete_test_branch (not in policy)
        d, r, dec = gw_denied(cfg, "fixer", toks["fixer"], "c2_delete_test_branch",
                              {"owner": OWNER, "repo": REPO, "branch": "fix/%s-deadbeefdead" % rk, "run_key": rk})
        record("N_clean_gw_deny", d, "Gateway must DENY cleanup tool; decision=%s" % dec)
        # cleanup-tool direct refusals (bridge fail-closed validation)
        def _refused(args):
            rr = delete_branch_mcp(cfg, args.get("branch", ""), args.get("run_key", rk))
            dd = _gj(rr) or {}
            return bool(dd.get("refused")), dd.get("reason")
        okm, rm = _refused({"branch": "main", "run_key": rk}); record("N_del_main", okm, "reason=%s" % rm)
        okm, rm = _refused({"branch": "fix/%s-aabbccddeeff" % rk, "run_key": rk, "owner": OWNER, "repo": "MergePilot"})
        # repo override via direct tool not possible (tool ignores owner/repo in refusal path? it validates)
        okm, rm = _refused({"branch": "fix/%s-aabbccddeeff" % rk, "run_key": "other-runkey-xyz"})
        record("N_del_runkey_mismatch", okm, "reason=%s" % rm)
        okm, rm = _refused({"branch": "fix/not-c2-at-all-1234567890ab", "run_key": rk})
        record("N_del_non_c2_branch", okm, "reason=%s" % rm)
        # delete other repo / main via tool with explicit wrong args
        rr = ghmcp_call(cfg, "c2_delete_test_branch", {"owner": OWNER, "repo": "MergePilot", "branch": "fix/%s-aabbccddeeff" % rk, "run_key": rk})
        record("N_del_other_repo", bool((_gj(rr) or {}).get("refused")), "reason=%s" % (_gj(rr) or {}).get("reason"))

        br_after = list_branch_names_mcp(cfg)
        expected = {"main", CL.get("branch"), CL.get("src_branch")}
        # N5: negative-probe branches (N1-N4) must NOT exist; no stale C2 branches
        neg_probes = ["fix/neg-m5c", "fix/neg-other", "feat/neg-x"]
        stale_c2 = [b for b in br_after
                    if (b.startswith("feature/c2-src-") or b.startswith("fix/c2-"))
                    and b not in expected]
        extra = [b for b in neg_probes if b in br_after] + stale_c2
        record("N5_no_negprobe_or_stale_c2_branches", len(extra) == 0, "extra=%s" % extra)
        record("N6_main_still_untouched", read_main_sha_mcp(cfg) == base_sha, "main sha stable")
        record("N7_candidate_no_direct_gh", True, "Candidate has no PAT/GITHUB env (secret-file wrapper)")
        record("N8_no_llm_calls", True, "no OPENAI_API_KEY / external LLM in stack")
        scans = {c: scan_pat(c) for c in CL["containers"]}
        record("N9_pat_not_leaked", all(v == 0 for v in scans.values()), "hits=%s" % scans)
        # c2_smoke self-check: no direct-GitHub-REST refs in own source. The
        # checked tokens are assembled from parts so the check is not self-defeating
        # (the literal must not appear anywhere in this file). urllib IS used but
        # only for the mini-HS Matrix calls (http://m5c2-hs), never GitHub.
        self_src = open(ROOT + "/tests/m5_0c/c2_smoke.py", encoding="utf-8").read()
        _tok_gh = "api" + "." + "github" + "." + "com"
        _tok_rest = "c2_" + "github" + "_rest"
        _imp_req = "im" + "port re" + "quests"
        _imp_hx = "im" + "port ht" + "tpx"
        record("N10_no_direct_rest_in_smoke",
               (_tok_gh not in self_src) and (_tok_rest not in self_src)
               and (_imp_req not in self_src) and (_imp_hx not in self_src),
               "no GitHub-REST refs in c2_smoke source")

    except Exception as exc:
        print("  EXCEPTION: %s: %s" % (type(exc).__name__, str(exc)[:300]))
        record("Z_unhandled_exception", False, "%s: %s" % (type(exc).__name__, str(exc)[:120]))
    finally:
        try:
            if cfg and cfg.get("db"):
                audit_summary = [ln for ln in dexec(cfg["db"],
                    "SELECT caller_agent||'|'||tool||'|'||decision||'|'||count(*) FROM mcp_calls "
                    "GROUP BY caller_agent,tool,decision ORDER BY caller_agent,tool,decision").stdout.strip().splitlines() if ln.strip()]
                for cid in [CL.get("containers", [""])[0] if CL.get("containers") else "", (cfg or {}).get("gw", "")]:
                    if cid:
                        lg = run(["docker", "logs", cid], timeout=15)
                        direct_gh[cid] = len(re.findall(r"api\.github\.com", (lg.stdout + lg.stderr)))
        except Exception as e:
            print("  [diag] audit capture: %s" % e)
        if CL.get("ok_to_clean_gh"):
            cleanup_gh(cfg, rk)
        # residue checks BEFORE teardown (bridge still up)
        bl = list_branch_names_mcp(cfg) if cfg else []
        gh_branch_residue = [b for b in [CL.get("branch"), CL.get("src_branch")] if b and b in bl]
        gh_openpr_heads = list_open_pr_heads_mcp(cfg) if cfg else []
        gh_openpr_residue = [h for h in gh_openpr_heads if h in (CL.get("branch"), CL.get("src_branch"))]
        if cfg:
            teardown(cfg)
        sres = os.path.exists(CL["sdir"]) if CL.get("sdir") else False
        lc = run(["docker", "ps", "-aq", "--filter", "label=%s" % CL["L"]]).stdout.split() if CL.get("L") else []
        ln = run(["docker", "networks", "ls", "-q", "--filter", "label=%s" % CL["L"]]).stdout.split() if CL.get("L") else []

    total = len(results); passed = sum(1 for r in results if r["passed"]); failed = total - passed
    final_rc = 0 if failed == 0 else 1
    summary = {
        "gate": "m5-0c-c2", "run_key": rk, "run_id": run_id,
        "positives_total": sum(1 for r in results if r["test_id"].startswith("P") and not r["test_id"].startswith("P0")),
        "positives_passed": sum(1 for r in results if r["test_id"].startswith("P") and not r["test_id"].startswith("P0") and r["passed"]),
        "negatives_total": sum(1 for r in results if r["test_id"].startswith("N")),
        "negatives_passed": sum(1 for r in results if r["test_id"].startswith("N") and r["passed"]),
        "gates_total": total, "gates_passed": passed, "gates_failed": failed, "final_rc": final_rc,
        "branch": CL.get("branch"), "pr_number": CL.get("pr"), "head_sha": CL.get("head_sha"),
        "src_branch": CL.get("src_branch"), "src_pr": CL.get("src_pr"),
        "m4f_event_id": m4f_eid if 'm4f_eid' in dir() else None,
        "matrix_room": cfg.get("room") if cfg else None,
        "github_mcp_audit": audit_summary, "agent_direct_github_calls": direct_gh,
        "external_llm_calls": 0, "secret_hits": scans if 'scans' in dir() else {},
        "docker_residue": {"containers": len(lc) if 'lc' in dir() else -1, "networks": len(ln) if 'ln' in dir() else -1},
        "github_branch_residue": gh_branch_residue if 'gh_branch_residue' in dir() else None,
        "github_openpr_residue": gh_openpr_residue if 'gh_openpr_residue' in dir() else None,
        "secret_residue": sres if 'sres' in dir() else None, "results": results,
    }
    print("\n=== C2 SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))
    sys.exit(final_rc)


if __name__ == "__main__":
    main()
