#!/usr/bin/env bash
# M5-0C C0 HiClaw test-stack deployment (MergePilot-Test only).
# Execution order: mp_guard → ACTION parse → RUN_KEY validate → resource names → case dispatch.
# Image resolution happens ONLY inside the `up` branch (after RUN_KEY + collision checks).
# down/status/health never depend on image existence.
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

ROOT_WSL="/mnt/d/goai/mergepilot-os"
source "$ROOT_WSL/tools/test-env/mp_guard.sh"

ACTION="${1:-status}"

# Image constants (RepoDigest pinned, not drift-vulnerable :latest)
EMBEDDED_IMG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-embedded@sha256:5f8b42fd6c4160b40eb7c3b26c5617edc78fe24d2fcb00f918ff6d742aaa2d2c"
EMBEDDED_TAG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-embedded:v1.1.2"
MANAGER_IMG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-manager@sha256:488a919fb5cbdb76958d0301adaf3105b899b3c54d1597617f68cc58005b4666"
MANAGER_TAG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-manager:latest"
WORKER_IMG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-worker@sha256:90d2ded54621df1744e54decc9663e29a372bc4a8bb44be7c50376b05d33c1f9"
WORKER_TAG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-worker:latest"

# ── Resolve image to immutable ID via digest, with verified tag fallback. ──
# Returns "<method>\n<sha256-id>" on stdout (no global state), or returns rc=6.
# Uses Python JSON parse for strict RepoDigests array membership (not grep substring).
# Never returns a tag; never auto-pulls.
resolve_pinned_image() {
  local digest_ref="$1" tag_ref="$2"
  local img_id
  # Path 1: digest inspect direct
  if img_id=$(docker image inspect "$digest_ref" --format '{{.Id}}' 2>/dev/null) && [ -n "$img_id" ]; then
    case "$img_id" in sha256:*) ;; *) return 6 ;; esac
    printf 'digest_direct\n%s' "$img_id"
    return 0
  fi
  # Path 2: tag fallback with strict JSON array verification
  docker image inspect "$tag_ref" >/dev/null 2>&1 || return 6
  local rd_json img_tag_id
  rd_json=$(docker image inspect "$tag_ref" --format '{{json .RepoDigests}}' 2>/dev/null)
  img_tag_id=$(docker image inspect "$tag_ref" --format '{{.Id}}' 2>/dev/null)
  [ -n "$rd_json" ] && [ -n "$img_tag_id" ] || return 6
  case "$img_tag_id" in sha256:*) ;; *) return 6 ;; esac
  # Strict JSON array membership check via Python
  M5C_DIGEST="$digest_ref" M5C_RD_JSON="$rd_json" python3 -c "
import json, os, sys
try:
    arr = json.loads(os.environ['M5C_RD_JSON'])
except Exception:
    sys.exit(6)
if not isinstance(arr, list) or len(arr) == 0:
    sys.exit(6)
expected = os.environ['M5C_DIGEST']
# Exact string equality per array element (no substring, no partial)
if expected not in arr:
    sys.exit(6)
sys.exit(0)
" || return 6
  printf 'verified_tag_fallback\n%s' "$img_tag_id"
  return 0
}

# ── RUN_KEY validation (before any ACTION dispatch) ──
if [[ ${M5C_RUN_KEY+x} ]]; then
  RUN_KEY="$M5C_RUN_KEY"
  [ -z "$RUN_KEY" ] && { python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','all_passed':False,'error':'RUN_KEY empty'}))"; exit 4; }
else
  RUN_KEY="$$-$(python3 -c 'import secrets;print(secrets.token_hex(4))')"
fi
case "$RUN_KEY" in *..*) python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','all_passed':False,'error':'RUN_KEY ..'}))"; exit 4 ;; esac
printf '%s' "$RUN_KEY" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' || { python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','all_passed':False,'error':'RUN_KEY charset'}))"; exit 4; }

# ── Resource names (derived from validated RUN_KEY) ──
L_SCOPE="com.mergepilot.scope=test"
L_PHASE="com.mergepilot.phase=m5-0c"
L_RUN="com.mergepilot.run_key=$RUN_KEY"
NET="m5c-net-$RUN_KEY"
CTRL="m5c-controller-$RUN_KEY"
MGR="m5c-manager-$RUN_KEY"
WRK="m5c-worker-$RUN_KEY"
VOL="m5c-data-$RUN_KEY"

# ── ACTION dispatch ──
case "$ACTION" in
up)
  # === IMAGE RESOLUTION (inside up only) ===
  # All 3 must resolve before creating any network/container/volume.
  # resolve_pinned_image prints "<method>\n<sha256-id>"; parse both from stdout.
  _r="$(resolve_pinned_image "$EMBEDDED_IMG" "$EMBEDDED_TAG")" || {
    python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'resolve embedded failed'}))"; exit 6; }
  EMB_METHOD="${_r%%$'\n'*}"; EMBEDDED_ID="${_r#*$'\n'}"
  _r="$(resolve_pinned_image "$MANAGER_IMG" "$MANAGER_TAG")" || {
    python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'resolve manager failed'}))"; exit 6; }
  MANAGER_ID="${_r#*$'\n'}"
  _r="$(resolve_pinned_image "$WORKER_IMG" "$WORKER_TAG")" || {
    python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'resolve worker failed'}))"; exit 6; }
  WORKER_ID="${_r#*$'\n'}"
  unset _r

  # === IDEMPOTENCY CHECK — all 5 resources must exist with correct labels + image IDs ===
  NET_EXISTS=0; CTRL_EXISTS=0; MGR_EXISTS=0; WRK_EXISTS=0; VOL_EXISTS=0
  docker network inspect "$NET" >/dev/null 2>&1 && NET_EXISTS=1
  docker inspect "$CTRL" >/dev/null 2>&1 && CTRL_EXISTS=1
  docker inspect "$MGR" >/dev/null 2>&1 && MGR_EXISTS=1
  docker inspect "$WRK" >/dev/null 2>&1 && WRK_EXISTS=1
  docker volume inspect "$VOL" >/dev/null 2>&1 && VOL_EXISTS=1
  _EXIST="$NET_EXISTS$CTRL_EXISTS$MGR_EXISTS$WRK_EXISTS$VOL_EXISTS"
  ALL_EXIST=0
  [ "$_EXIST" = "11111" ] && ALL_EXIST=1

  if [ "$ALL_EXIST" = 1 ]; then
    # Every applicable resource must carry scope=test, phase=m5-0c, run_key=$RUN_KEY;
    # each container .Image must equal its resolved ID. Manager/worker may be exited
    # but their container objects must exist with correct identity.
    M5C_RK="$RUN_KEY" M5C_NET="$NET" M5C_CTRL="$CTRL" M5C_MGR="$MGR" M5C_WRK="$WRK" M5C_VOL="$VOL" \
    M5C_EMB_ID="$EMBEDDED_ID" M5C_MGR_ID="$MANAGER_ID" M5C_WRK_ID="$WORKER_ID" M5C_METHOD="$EMB_METHOD" \
    python3 <<'PYEOF'
import json, os, subprocess, sys
def q(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""
def clbl(c, k): return q(["docker","inspect",c,"--format",'{{index .Config.Labels "%s"}}' % k])
def nlbl(n, k): return q(["docker","network","inspect",n,"--format",'{{index .Labels "%s"}}' % k])
def vlbl(v, k): return q(["docker","volume","inspect",v,"--format",'{{index .Labels "%s"}}' % k])
def cimg(c): return q(["docker","inspect",c,"--format","{{.Image}}"])
e = os.environ; rk = e["M5C_RK"]
def labels_ok(obj, fn):
    return (fn(obj,"com.mergepilot.scope") == "test"
            and fn(obj,"com.mergepilot.phase") == "m5-0c"
            and fn(obj,"com.mergepilot.run_key") == rk)
ok = (labels_ok(e["M5C_CTRL"], clbl) and labels_ok(e["M5C_MGR"], clbl) and labels_ok(e["M5C_WRK"], clbl)
      and labels_ok(e["M5C_NET"], nlbl) and labels_ok(e["M5C_VOL"], vlbl)
      and cimg(e["M5C_CTRL"]) == e["M5C_EMB_ID"]
      and cimg(e["M5C_MGR"]) == e["M5C_MGR_ID"]
      and cimg(e["M5C_WRK"]) == e["M5C_WRK_ID"])
if ok:
    print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"up","all_passed":True,
                      "status":"already_up","idempotent":True,"resolution_method":e["M5C_METHOD"]})); sys.exit(0)
print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"up","all_passed":False,
                  "error":"collision: mismatched label/image","status":"collision"})); sys.exit(5)
PYEOF
    exit $?
  fi

  # Partial resources → fail closed
  if [ "$_EXIST" != "00000" ]; then
    python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'partial resources exist (run down first)','status':'partial'}))"
    exit 5
  fi

  # === FRESH DEPLOY ===
  docker network create --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" "$NET" >/dev/null
  # Volume created explicitly WITH labels so idempotency can verify them (auto-create via
  # -v would yield an unlabeled volume).
  docker volume create --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" "$VOL" >/dev/null

  if [ -n "${M5C_SECRET_DIR:-}" ] && [ -f "${M5C_SECRET_DIR}/wrapper.sh" ]; then
    # C1 secret-file mode: controller reads HICLAW_REGISTRATION_TOKEN / HICLAW_MINIO_PASSWORD
    # via /secrets/wrapper.sh (read-only bind mount). No -e secret injection; Config.Env
    # carries only SECRETS_DIR=/secrets. Caller pre-populated $M5C_SECRET_DIR.
    docker run -d --name "$CTRL" --network "$NET" --network-alias "m5c-controller" \
      --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
      -v "$VOL:/data" -v "${M5C_SECRET_DIR}:/secrets:ro" \
      -e "SECRETS_DIR=/secrets" \
      --restart=no --entrypoint /secrets/wrapper.sh \
      "$EMBEDDED_ID" >/dev/null
  else
    # C0 legacy mode: per-RUN_KEY test creds via -e (backward compat for C0 18/18).
    C0_MINIO_PW="$(python3 -c 'import secrets;print("c0"+secrets.token_urlsafe(10))')"
    C0_REG_TOK="$(python3 -c 'import secrets;print("c0reg"+secrets.token_urlsafe(10))')"
    docker run -d --name "$CTRL" --network "$NET" --network-alias "m5c-controller" \
      --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
      -v "$VOL:/data" \
      -e "HICLAW_MINIO_PASSWORD=$C0_MINIO_PW" \
      -e "HICLAW_REGISTRATION_TOKEN=$C0_REG_TOK" \
      --restart=no --entrypoint supervisord \
      "$EMBEDDED_ID" -n -c /etc/supervisor/supervisord.conf >/dev/null
    unset C0_MINIO_PW C0_REG_TOK
  fi

  # Manager/Worker: resolved Image IDs (exit without creds — C0 expected)
  docker run -d --name "$MGR" --network "$NET" --network-alias "m5c-manager" \
    --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
    --restart=no "$MANAGER_ID" >/dev/null 2>&1 || true
  docker run -d --name "$WRK" --network "$NET" --network-alias "m5c-worker" \
    --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
    --restart=no "$WORKER_ID" >/dev/null 2>&1 || true

  # Wait for embedded services (tuwunel needs ~30s RocksDB init)
  echo "waiting for embedded services..." >&2
  for i in $(seq 1 90); do
    docker exec "$CTRL" curl -sf -o /dev/null http://localhost:6167/_matrix/client/versions 2>/dev/null && break
    sleep 1
  done
  sleep 5

  # Health collection (env-var safe python heredoc)
  M5C_RK="$RUN_KEY" M5C_CTRL="$CTRL" M5C_MGR="$MGR" M5C_WRK="$WRK" M5C_EMB_METHOD="$EMB_METHOD" \
  M5C_EMBEDDED_ID="$EMBEDDED_ID" python3 <<'PYEOF'
import json, os, subprocess, re
def dock(*args):
    r = subprocess.run(["docker"]+list(args), capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""
def dock_exec(cid, *cmd):
    r = subprocess.run(["docker","exec",cid]+list(cmd), capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""
def http_code(cid, url):
    return dock_exec(cid, "curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url).strip()
def safe_int(v):
    try: return int(str(v).strip().split('\n')[0].replace('<nil>','0'))
    except: return 0
ctrl=os.environ["M5C_CTRL"]; mgr=os.environ["M5C_MGR"]; wrk=os.environ["M5C_WRK"]; rk=os.environ["M5C_RK"]
matrix_code = http_code(ctrl, "http://localhost:6167/_matrix/client/versions")
minio_code = http_code(ctrl, "http://localhost:9000/minio/health/live")
element_code = http_code(ctrl, "http://localhost:8080/")
matrix_ok = matrix_code == "200"
minio_ok = minio_code == "200"
element_ok = element_code in ("200","301","302")
sup_ok = dock_exec(ctrl,"supervisorctl","status") != ""
ctrl_state = dock("inspect",ctrl,"--format","{{.State.Status}}") or "absent"
mgr_state = dock("inspect",mgr,"--format","{{.State.Status}}") or "absent"
wrk_state = dock("inspect",wrk,"--format","{{.State.Status}}") or "absent"
ctrl_sz = safe_int(dock("inspect",ctrl,"--format","{{.SizeRw}}"))
mgr_sz = safe_int(dock("inspect",mgr,"--format","{{.SizeRw}}"))
wrk_sz = safe_int(dock("inspect",wrk,"--format","{{.SizeRw}}"))
logs = subprocess.run(["docker","logs",ctrl], capture_output=True, text=True, timeout=15)
secret_hits = len(re.findall(r'ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{40}|-----BEGIN.*PRIVATE KEY-----', logs.stdout+logs.stderr))
all_ok = matrix_ok and minio_ok and element_ok and secret_hits == 0
print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"up","all_passed":all_ok,
  "resolution_method":os.environ.get("M5C_EMB_METHOD","unknown"),
  "resolved_image_id":os.environ.get("M5C_EMBEDDED_ID",""),
  "embedded":{"container":ctrl,"state":ctrl_state,
    "matrix_http":matrix_code,"matrix_6167":matrix_ok,
    "minio_http":minio_code,"minio_9000":minio_ok,
    "element_http":element_code,"element_8080":element_ok,
    "supervisorctl":sup_ok,"size_rw_bytes":ctrl_sz},
  "manager":{"container":mgr,"state":mgr_state,"image_ready":True,"identity_health":"deferred_C1","size_rw_bytes":mgr_sz},
  "worker":{"container":wrk,"state":wrk_state,"image_ready":True,"identity_health":"deferred_C1","size_rw_bytes":wrk_sz},
  "secret_scan_hits":secret_hits,"writable_max_bytes":max(ctrl_sz,mgr_sz,wrk_sz),
  "writable_exceeds_2gib":max(ctrl_sz,mgr_sz,wrk_sz)>2147483648}, indent=2))
PYEOF
  ;;

health)
  # Health depends ONLY on container + endpoints, never on image existence.
  CTRL_NAME="m5c-controller-$RUN_KEY"
  M5C_RK="$RUN_KEY" M5C_CTRL="$CTRL_NAME" python3 <<'PYEOF'
import json, os, subprocess, re, sys
def dock_exec(cid, *cmd):
    r = subprocess.run(["docker","exec",cid]+list(cmd), capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""
def http_code(cid, url):
    return dock_exec(cid, "curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url).strip()
ctrl=os.environ["M5C_CTRL"]; rk=os.environ["M5C_RK"]
exists = subprocess.run(["docker","inspect",ctrl], capture_output=True).returncode == 0
if not exists:
    print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"health","all_passed":False,"error":"controller not found"})); sys.exit(1)
matrix_code = http_code(ctrl,"http://localhost:6167/_matrix/client/versions")
minio_code = http_code(ctrl,"http://localhost:9000/minio/health/live")
element_code = http_code(ctrl,"http://localhost:8080/")
matrix_ok = matrix_code == "200"
minio_ok = minio_code == "200"
element_ok = element_code in ("200","301","302")
logs = subprocess.run(["docker","logs",ctrl], capture_output=True, text=True, timeout=15)
secret_hits = len(re.findall(r'ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{40}|-----BEGIN.*PRIVATE KEY-----', logs.stdout+logs.stderr))
all_ok = matrix_ok and minio_ok and element_ok and secret_hits == 0
print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"health","all_passed":all_ok,
  "matrix_http":matrix_code,"matrix_6167":matrix_ok,
  "minio_http":minio_code,"minio_9000":minio_ok,
  "element_http":element_code,"element_8080":element_ok,
  "secret_hits":secret_hits}, indent=2))
sys.exit(0 if all_ok else 1)
PYEOF
  ;;

down)
  # Down depends ONLY on RUN_KEY resource names + labels. Never on image existence.
  set +e
  docker rm -f "$WRK" >/dev/null 2>&1
  docker rm -f "$MGR" >/dev/null 2>&1
  docker rm -f "$CTRL" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker volume rm "$VOL" >/dev/null 2>&1
  set -e 2>/dev/null || true
  sleep 1
  LEFT_C=$(docker ps -aq --filter "label=$L_RUN" | wc -l | tr -d ' ')
  LEFT_N=$(docker network ls -q --filter "label=$L_RUN" | wc -l | tr -d ' ')
  M5C_RK="$RUN_KEY" M5C_LC="$LEFT_C" M5C_LN="$LEFT_N" python3 -c "
import json,os
lc=int(os.environ['M5C_LC']); ln=int(os.environ['M5C_LN'])
print(json.dumps({'gate':'m5-0c-c0','run_key':os.environ['M5C_RK'],'action':'down',
  'residue':{'containers':lc,'networks':ln},'all_passed':lc==0 and ln==0}, indent=2))
"
  [ "$LEFT_C" = 0 ] && [ "$LEFT_N" = 0 ] || exit 1
  ;;

status)
  # Status depends ONLY on labels. Never on image existence.
  echo "RUN_KEY=$RUN_KEY"
  docker ps -a --filter "label=$L_RUN" --format '{{.Names}} {{.Status}} {{.Size}}' 2>/dev/null
  echo "networks:"
  docker network ls --filter "label=$L_RUN" --format '{{.Name}}' 2>/dev/null
  echo "volumes:"
  docker volume ls -q --filter "name=$VOL" 2>/dev/null
  ;;

*) echo "usage: $0 {up|health|down|status}"; exit 64 ;;
esac
