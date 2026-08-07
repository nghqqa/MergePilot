#!/usr/bin/env bash
# M5-0C C0 HiClaw test-stack deployment (MergePilot-Test only).
# ALL fixes: P1 tuwunel (named volume for RocksDB Direct I/O),
#            P1 health (HTTP status code checks),
#            P2 JSON (safe_int, env-var python heredoc),
#            P2 MinIO (HICLAW_MINIO_PASSWORD ≥8 chars per RUN_KEY).
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

ROOT_WSL="/mnt/d/goai/mergepilot-os"
source "$ROOT_WSL/tools/test-env/mp_guard.sh"

ACTION="${1:-status}"
EMBEDDED_IMG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-embedded@sha256:5f8b42fd6c4160b40eb7c3b26c5617edc78fe24d2fcb00f918ff6d742aaa2d2c"
MANAGER_IMG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-manager@sha256:488a919fb5cbdb76958d0301adaf3105b899b3c54d1597617f68cc58005b4666"
WORKER_IMG="higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-worker@sha256:90d2ded54621df1744e54decc9663e29a372bc4a8bb44be7c50376b05d33c1f9"

# RUN_KEY validation
if [[ ${M5C_RUN_KEY+x} ]]; then
  RUN_KEY="$M5C_RUN_KEY"
  [ -z "$RUN_KEY" ] && { python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','all_passed':False,'error':'RUN_KEY empty'}))"; exit 4; }
else
  RUN_KEY="$$-$(python3 -c 'import secrets;print(secrets.token_hex(4))')"
fi
case "$RUN_KEY" in *..*) python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','all_passed':False,'error':'RUN_KEY ..'}))"; exit 4 ;; esac
printf '%s' "$RUN_KEY" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' || { python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','all_passed':False,'error':'RUN_KEY charset'}))"; exit 4; }

L_SCOPE="com.mergepilot.scope=test"
L_PHASE="com.mergepilot.phase=m5-0c"
L_RUN="com.mergepilot.run_key=$RUN_KEY"
NET="m5c-net-$RUN_KEY"
CTRL="m5c-controller-$RUN_KEY"
MGR="m5c-manager-$RUN_KEY"
WRK="m5c-worker-$RUN_KEY"
VOL="m5c-data-$RUN_KEY"

case "$ACTION" in
up)
  # Image check (always)
  for img in "$EMBEDDED_IMG" "$MANAGER_IMG" "$WORKER_IMG"; do
    docker image inspect "$img" >/dev/null 2>&1 || { python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'image missing'}))"; exit 6; }
  done

  # Idempotency: if ALL resources exist with correct labels → already_up
  # If resources exist but labels/digest wrong → collision fail-closed rc=5
  NET_EXISTS=0; CTRL_EXISTS=0; MGR_EXISTS=0; WRK_EXISTS=0; VOL_EXISTS=0
  docker network inspect "$NET" >/dev/null 2>&1 && NET_EXISTS=1
  docker inspect "$CTRL" >/dev/null 2>&1 && CTRL_EXISTS=1
  docker inspect "$MGR" >/dev/null 2>&1 && MGR_EXISTS=1
  docker inspect "$WRK" >/dev/null 2>&1 && WRK_EXISTS=1
  docker volume inspect "$VOL" >/dev/null 2>&1 && VOL_EXISTS=1
  ALL_EXIST=0
  [ "$NET_EXISTS" = 1 ] && [ "$CTRL_EXISTS" = 1 ] && [ "$VOL_EXISTS" = 1 ] && ALL_EXIST=1

  if [ "$ALL_EXIST" = 1 ]; then
    # Verify labels match THIS run_key
    CTRL_LABEL=$(docker inspect "$CTRL" --format '{{index .Config.Labels "com.mergepilot.run_key"}}' 2>/dev/null)
    NET_LABEL=$(docker network inspect "$NET" --format '{{index .Labels "com.mergepilot.run_key"}}' 2>/dev/null)
    CTRL_IMG=$(docker inspect "$CTRL" --format '{{.Image}}' 2>/dev/null)
    EMBEDDED_ID=$(docker image inspect "$EMBEDDED_IMG" --format '{{.Id}}' 2>/dev/null)
    LABELS_OK=0
    [ "$CTRL_LABEL" = "$RUN_KEY" ] && [ "$NET_LABEL" = "$RUN_KEY" ] && [ "$CTRL_IMG" = "$EMBEDDED_ID" ] && LABELS_OK=1

    if [ "$LABELS_OK" = 1 ]; then
      # Idempotent: all resources match → return already_up
      python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':True,'status':'already_up','idempotent':True}))"
      exit 0
    else
      # Resources exist but labels/digest mismatch → fail closed
      python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'collision: resources exist with mismatched labels/digest','status':'collision'}))"
      exit 5
    fi
  fi

  # Partial resources exist → fail closed
  if [ "$NET_EXISTS" = 1 ] || [ "$CTRL_EXISTS" = 1 ] || [ "$VOL_EXISTS" = 1 ]; then
    python3 -c "import json;print(json.dumps({'gate':'m5-0c-c0','run_key':'$RUN_KEY','action':'up','all_passed':False,'error':'partial resources exist (run down first)','status':'partial'}))"
    exit 5
  fi

  # Fresh deploy
  docker network create --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" "$NET" >/dev/null

  # Per-RUN_KEY test credentials (runtime-generated, NOT production, NOT persisted)
  C0_MINIO_PW="$(python3 -c 'import secrets;print("c0"+secrets.token_urlsafe(10))')"
  C0_REG_TOK="$(python3 -c 'import secrets;print("c0reg"+secrets.token_urlsafe(10))')"

  # Embedded: main supervisord.conf (supervisorctl socket) + NAMED VOLUME for /data
  # (ext4 supports RocksDB Direct I/O; tmpfs does NOT → tuwunel FATAL)
  # HICLAW_REGISTRATION_TOKEN (not CONDUWUIT_ — start-tuwunel.sh maps it)
  # HICLAW_MINIO_PASSWORD (≥8 chars for MinIO policy)
  docker run -d --name "$CTRL" --network "$NET" --network-alias "m5c-controller" \
    --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
    -v "$VOL:/data" \
    -e "HICLAW_MINIO_PASSWORD=$C0_MINIO_PW" \
    -e "HICLAW_REGISTRATION_TOKEN=$C0_REG_TOK" \
    --restart=no --entrypoint supervisord \
    "$EMBEDDED_IMG" -n -c /etc/supervisor/supervisord.conf >/dev/null
  unset C0_MINIO_PW C0_REG_TOK

  # Manager/Worker: image readiness (exit without creds — C0 expected)
  docker run -d --name "$MGR" --network "$NET" --network-alias "m5c-manager" \
    --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
    --restart=no "$MANAGER_IMG" >/dev/null 2>&1 || true
  docker run -d --name "$WRK" --network "$NET" --network-alias "m5c-worker" \
    --label "$L_SCOPE" --label "$L_PHASE" --label "$L_RUN" \
    --restart=no "$WORKER_IMG" >/dev/null 2>&1 || true

  # Wait for ALL embedded services (tuwunel needs ~30s for RocksDB init)
  echo "waiting for embedded services..." >&2
  for i in $(seq 1 90); do
    docker exec "$CTRL" curl -sf -o /dev/null http://localhost:6167/_matrix/client/versions 2>/dev/null && break
    sleep 1
  done
  sleep 5

  # Collect health via python (env-var safe, no bash interpolation in strings)
  M5C_RK="$RUN_KEY" M5C_CTRL="$CTRL" M5C_MGR="$MGR" M5C_WRK="$WRK" python3 <<'PYEOF'
import json, os, subprocess, re
def dock(*args):
    r = subprocess.run(["docker"]+list(args), capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""
def dock_exec(cid, *cmd):
    r = subprocess.run(["docker","exec",cid]+list(cmd), capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""
def http_code(cid, url):
    out = dock_exec(cid, "curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url)
    return out.strip()
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
# secret scan
logs = subprocess.run(["docker","logs",ctrl], capture_output=True, text=True, timeout=15)
secret_hits = len(re.findall(r'ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{40}|-----BEGIN.*PRIVATE KEY-----', logs.stdout+logs.stderr))
all_ok = matrix_ok and minio_ok and element_ok and secret_hits == 0
print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"up","all_passed":all_ok,
  "embedded":{"container":ctrl,"state":ctrl_state,"matrix_6167":matrix_ok,"matrix_http":matrix_code,
    "minio_9000":minio_ok,"minio_http":minio_code,"element_8080":element_ok,"element_http":element_code,
    "supervisorctl":sup_ok,"size_rw_bytes":ctrl_sz},
  "manager":{"container":mgr,"state":mgr_state,"image_ready":True,"identity_health":"deferred_C1","size_rw_bytes":mgr_sz},
  "worker":{"container":wrk,"state":wrk_state,"image_ready":True,"identity_health":"deferred_C1","size_rw_bytes":wrk_sz},
  "secret_scan_hits":secret_hits,"writable_max_bytes":max(ctrl_sz,mgr_sz,wrk_sz),
  "writable_exceeds_2gib":max(ctrl_sz,mgr_sz,wrk_sz)>2147483648}, indent=2))
PYEOF
  ;;

health)
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
matrix_ok = http_code(ctrl,"http://localhost:6167/_matrix/client/versions") == "200"
minio_ok = http_code(ctrl,"http://localhost:9000/minio/health/live") == "200"
element_ok = http_code(ctrl,"http://localhost:8080/") in ("200","301","302")
logs = subprocess.run(["docker","logs",ctrl], capture_output=True, text=True, timeout=15)
secret_hits = len(re.findall(r'ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{40}|-----BEGIN.*PRIVATE KEY-----', logs.stdout+logs.stderr))
all_ok = matrix_ok and minio_ok and element_ok and secret_hits == 0
print(json.dumps({"gate":"m5-0c-c0","run_key":rk,"action":"health","all_passed":all_ok,
  "matrix_6167":matrix_ok,"minio_9000":minio_ok,"element_8080":element_ok,"secret_hits":secret_hits}, indent=2))
sys.exit(0 if all_ok else 1)
PYEOF
  ;;

down)
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
  echo "RUN_KEY=$RUN_KEY"
  docker ps -a --filter "label=$L_RUN" --format '{{.Names}} {{.Status}} {{.Size}}' 2>/dev/null
  echo "networks:"
  docker network ls --filter "label=$L_RUN" --format '{{.Name}}' 2>/dev/null
  echo "volumes:"
  docker volume ls -q --filter "name=$VOL" 2>/dev/null
  ;;

*) echo "usage: $0 {up|health|down|status}"; exit 64 ;;
esac
