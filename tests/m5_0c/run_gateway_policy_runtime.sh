#!/usr/bin/env bash
# M5-0C real Policy Gateway runtime gate (runs inside MergePilot-Test).
# P2 hardening: unset-vs-empty RUN_KEY, `..` rejection, full collision check
# (network + DB + fake-MCP + Gateway), cleanup ownership flags (never deletes
# resources it didn't create), safe JSON via json.dumps, fail-closed residue=0/0.
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

ROOT_WSL="/mnt/d/goai/mergepilot-os"
source "$ROOT_WSL/tools/test-env/mp_guard.sh"   # rc=2 off-test

# safe fail-closed JSON (json.dumps — no printf string interpolation)
emit_fail() {  # $1=error $2=state $3=exit_code
  python3 -c 'import json,sys;print(json.dumps({"gate":"m5-0c-gateway-policy","all_passed":False,"error":sys.argv[1],"client_output_state":sys.argv[2],"client_rc":None,"residue":{"containers":0,"networks":0}}))' "$1" "${2:-rejected}"
  exit "${3:-1}"
}
emit_fail_coll() {  # $1=coll_type $2=coll_name
  python3 -c 'import json,sys;print(json.dumps({"gate":"m5-0c-gateway-policy","all_passed":False,"error":"collision: "+sys.argv[1]+" "+sys.argv[2]+" exists","client_output_state":"rejected","client_rc":None,"collision_type":sys.argv[1],"collision_name":sys.argv[2],"residue":{"containers":0,"networks":0}}))' "$1" "$2"
  exit 5
}

# ── RUN_KEY: unset→auto; explicit-empty→rc=4 ──
if [[ ${M5C_RUN_KEY+x} ]]; then
  RUN_KEY="$M5C_RUN_KEY"
  [ -z "$RUN_KEY" ] && emit_fail "RUN_KEY explicitly empty (rejected)" "rejected" 4
else
  RUN_KEY="$$-$(python3 -c 'import secrets;print(secrets.token_hex(4))')"
fi
# reject '..' substring (regex below allows individual dots)
case "$RUN_KEY" in *..*) emit_fail "RUN_KEY contains '..' (rejected)" "rejected" 4 ;; esac
# charset+length: ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ (rejects / \ space ; ctrl etc)
printf '%s' "$RUN_KEY" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' \
  || emit_fail "RUN_KEY invalid charset/length (rejected)" "rejected" 4

DBDIR="$ROOT_WSL/tools/audit-db"; GWDIR="$ROOT_WSL/tools/policy-gateway"
LABEL="com.mergepilot.m5_0c_gate=$RUN_KEY"
NET="m5c-net-$RUN_KEY"; DB="m5c-pg-$RUN_KEY"; GH="m5c-fakegh-$RUN_KEY"; GW="m5c-gateway-$RUN_KEY"
PGALIAS="m5c-pg-$RUN_KEY"; GHALIAS="m5c-fakegh-$RUN_KEY"; GWALIAS="m5c-gateway-$RUN_KEY"
DBNAME="m5c_audit"; PG_IMAGE="pgvector/pgvector:pg16"; GW_IMAGE="policy-gateway:m5-0c"
POLICY="$ROOT_WSL/config/m5-0c/real-github-policy.yaml"
CLIENT_SCRIPT="${M5C_CLIENT_SCRIPT:-tests/m5_0c/gateway_policy_client.py}"
RESULT_FILE="/tmp/m5c_gate_result-$RUN_KEY.json"; FINAL_FILE="/tmp/m5c_gate_final-$RUN_KEY.json"

# CLIENT_SCRIPT path validation (before any docker)
case "$CLIENT_SCRIPT" in
  /*)  emit_fail "CLIENT_SCRIPT absolute (rejected)" "rejected" 3 ;;
  *..*) emit_fail "CLIENT_SCRIPT '..' (rejected)" "rejected" 3 ;;
esac
[ -f "$ROOT_WSL/$CLIENT_SCRIPT" ] || emit_fail "CLIENT_SCRIPT not found" "rejected" 3

# ── FULL collision check: all 4 resources BEFORE creating anything ──
docker network inspect "$NET" >/dev/null 2>&1 && emit_fail_coll "network" "$NET"
docker inspect "$DB" >/dev/null 2>&1 && emit_fail_coll "container" "$DB"
docker inspect "$GH" >/dev/null 2>&1 && emit_fail_coll "container" "$GH"
docker inspect "$GW" >/dev/null 2>&1 && emit_fail_coll "container" "$GW"

# ── ownership flags: cleanup only deletes what THIS run created ──
CREATED_NET=0; CREATED_DB=0; CREATED_GH=0; CREATED_GW=0
cleanup() {
  set +e
  [ "$CREATED_GW" = 1 ] && docker rm -f "$GW" >/dev/null 2>&1
  [ "$CREATED_GH" = 1 ] && docker rm -f "$GH" >/dev/null 2>&1
  [ "$CREATED_DB" = 1 ] && docker rm -f "$DB" >/dev/null 2>&1
  [ "$CREATED_NET" = 1 ] && docker network rm "$NET" >/dev/null 2>&1
}
cleanup_temp() { set +e; rm -f "$RESULT_FILE" "$FINAL_FILE" >/dev/null 2>&1; }
trap 'rc=$?; cleanup; cleanup_temp; exit $rc' EXIT

docker network create --label "$LABEL" "$NET" >/dev/null && CREATED_NET=1

# tokens
export M5C_M5COORDINATOR_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
export M5C_FIXER_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
export M5C_REVIEWER_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
export M5C_VERIFIER_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
ROLE_TOKENS_JSON=$(python3 <<PY
import json, os
print(json.dumps({"m5coordinator":os.environ["M5C_M5COORDINATOR_TOKEN"],"fixer":os.environ["M5C_FIXER_TOKEN"],"reviewer":os.environ["M5C_REVIEWER_TOKEN"],"verifier":os.environ["M5C_VERIFIER_TOKEN"]}))
PY
)

# 1. audit PG
docker run -d --name "$DB" --network "$NET" --network-alias "$PGALIAS" --label "$LABEL" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=fixture_admin -e POSTGRES_DB="$DBNAME" "$PG_IMAGE" >/dev/null && CREATED_DB=1
for _ in $(seq 1 90); do docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_audit') THEN CREATE ROLE policy_gateway_audit LOGIN; END IF; END $r$;
SQL
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/m3b_policy.sql" >/dev/null
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 -c "ALTER TABLE public.mcp_calls ADD COLUMN IF NOT EXISTS execution_id TEXT;" >/dev/null
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<SQL >/dev/null
GRANT CONNECT ON DATABASE $DBNAME TO policy_gateway_audit; GRANT USAGE ON SCHEMA public TO policy_gateway_audit;
GRANT INSERT ON public.mcp_calls TO policy_gateway_audit; REVOKE SELECT,UPDATE,DELETE ON public.mcp_calls FROM policy_gateway_audit;
GRANT SELECT ON public.mcp_calls TO fixture_admin;
SQL
AUDIT_DSN="host=$PGALIAS dbname=$DBNAME user=policy_gateway_audit"

# 2. image
docker build -q -t "$GW_IMAGE" "$GWDIR" >/dev/null

# 3. fake MCP
docker run -d --name "$GH" --network "$NET" --network-alias "$GHALIAS" --label "$LABEL" \
  -v "$ROOT_WSL:/workspace:ro" -w /workspace --entrypoint python "$GW_IMAGE" tests/m5_0c/fake_github_mcp_counted.py >/dev/null && CREATED_GH=1

# 4. gateway
docker run -d --name "$GW" --network "$NET" --network-alias "$GWALIAS" --label "$LABEL" \
  -v "$ROOT_WSL:/workspace:ro" -v "$ROOT_WSL/tools/policy-gateway/gateway.py:/app/gateway.py:ro" -v "$POLICY:/app/policy.yaml:ro" \
  -e UPSTREAM_URL="http://$GHALIAS:8082/sse" -e ROLE_TOKENS="$ROLE_TOKENS_JSON" -e AUDIT_DSN="$AUDIT_DSN" \
  -e POLICY_FILE="/app/policy.yaml" -e LISTEN_HOST="0.0.0.0" -e LISTEN_PORT="8083" "$GW_IMAGE" >/dev/null && CREATED_GW=1

READY=0
for _ in $(seq 1 40); do docker logs "$GW" 2>&1 | grep -qa 'upstream ready\|Uvicorn running\|Application startup' && { READY=1; break; }; sleep 1; done
[ "$READY" = "1" ] || { cleanup; python3 -c 'import json;print(json.dumps({"gate":"m5-0c-gateway-policy","all_passed":False,"error":"gateway not ready","client_output_state":"no_client","client_rc":None,"residue":{"containers":0,"networks":0}}))'; exit 1; }

# 5. client (reliable rc capture)
set +e
docker run --rm --network "$NET" --label "$LABEL" -v "$ROOT_WSL:/workspace:ro" -w /workspace --entrypoint python \
  -e M5C_GATEWAY="http://$GWALIAS:8083" -e M5C_FAKE="http://$GHALIAS:8082" -e M5C_AUDIT_DSN="host=$PGALIAS dbname=$DBNAME user=fixture_admin" \
  -e M5C_M5COORDINATOR_TOKEN -e M5C_FIXER_TOKEN -e M5C_REVIEWER_TOKEN -e M5C_VERIFIER_TOKEN -e M5C_NEGATIVE_MODE \
  "$GW_IMAGE" "$CLIENT_SCRIPT" > "$RESULT_FILE"
CLIENT_RC=$?
set -e

cleanup; sleep 1
LEFT_C=$(docker ps -aq --filter "label=$LABEL" | wc -l | tr -d ' ')
LEFT_N=$(docker network ls -q --filter "label=$LABEL" | wc -l | tr -d ' ')

FINAL_JSON=$(M5C_RUN_KEY_OUT="$RUN_KEY" python3 - "$CLIENT_RC" "$LEFT_C" "$LEFT_N" "$RESULT_FILE" <<'PY'
import json, os, sys
crc, lc, ln, rf = sys.argv[1:5]
state, payload, client, err = "empty", None, None, None
if not os.path.exists(rf) or os.path.getsize(rf) == 0:
    state, err = "empty", "client produced no output"
else:
    try:
        client = json.loads(open(rf, encoding="utf-8", errors="replace").read())
        if isinstance(client, dict):
            state = "valid_json"; payload = bool(client.get("all_passed"))
        else:
            state, err = "invalid_json", "client JSON not an object"; client = None
    except Exception as e:
        state, err = "invalid_json", "client output not valid JSON: %s" % e
all_passed = state == "valid_json" and payload is True and int(crc) == 0 and int(lc) == 0 and int(ln) == 0
print(json.dumps({"gate":"m5-0c-gateway-policy","run_key":os.environ.get("M5C_RUN_KEY_OUT",""),"all_passed":all_passed,"error":err,
    "client_rc":int(crc),"client_output_state":state,"client_payload_all_passed":payload,
    "scenarios":client.get("scenarios") if client else None,"passed":client.get("passed") if client else None,
    "failed":client.get("failed") if client else None,"residue":{"containers":int(lc),"networks":int(ln)},
    "results":client.get("results") if client else None}, indent=2))
PY
)
printf '%s\n' "$FINAL_JSON" > "$FINAL_FILE"
printf '%s\n' "$FINAL_JSON"
FINAL_RC=$(python3 -c 'import json;d=json.load(open("'"$FINAL_FILE"'"));print(0 if d["all_passed"] else 1)')
cleanup_temp
exit "$FINAL_RC"
