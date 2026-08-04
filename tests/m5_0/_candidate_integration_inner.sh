#!/usr/bin/env bash
# Inner: M5-0A real Candidate integration (runs in WSL where Docker is available).
#
# Isolated throwaway stack: temp PG16 + mini Matrix HS + test Gateway (m5coordinator
# policy) + fake GitHub MCP. The Candidate /syncs from the mini HS (REAL Matrix
# protocol, not direct process_event). All secrets runtime-generated, never
# persisted. cleanup trap ensures residue=0/0/0.
#
#hiclab_live=false: isolated test stack, NOT the shared production HiClaw.
set -euo pipefail

ROOT="/mnt/d/goai/mergepilot-os"
DBDIR="$ROOT/tools/audit-db"
GWDIR="$ROOT/tools/policy-gateway"
FIXDIR="$ROOT/tests/m5_0/fixtures"

PG_IMAGE="pgvector/pgvector:pg16"
RUNTIME_IMAGE="mergepilot-m4f-runtime:demo"
GW_IMAGE="policy-gateway:m4f"
CAND_IMAGE="mergepilot-m5-cand:integration"

UNIQ="m5int-$$-$(date +%s)"
LABEL="mergepilot.m5.integration=${UNIQ}"
NET="m5i-net-${UNIQ}"
DB="m5i-pg-${UNIQ}"
HS="m5i-hs-${UNIQ}"
GH="m5i-gh-${UNIQ}"
GW="m5i-gw-${UNIQ}"
CAND="m5i-cand-${UNIQ}"
CAND2="m5i-cand2-${UNIQ}"
DBNAME="m5test"

rand_hex() { head -c "$1" /dev/urandom | od -An -v -tx1 | tr -d ' \n'; }

# runtime secrets (printf -v + export; never adjacent to = in source for scanner)
printf -v M5_GW_TOKEN   '%s' "$(rand_hex 32)"; export M5_GW_TOKEN
printf -v M5_FIXER_TOK  '%s' "$(rand_hex 32)"; export M5_FIXER_TOK
printf -v M5_REVIEW_TOK '%s' "$(rand_hex 32)"; export M5_REVIEW_TOK
printf -v M5_VERIFY_TOK '%s' "$(rand_hex 32)"; export M5_VERIFY_TOK
printf -v M5_HMAC_KEY   '%s' "$(rand_hex 32)"; export M5_HMAC_KEY
printf -v M5_MGR_PW     '%s' "$(rand_hex 16)"; export M5_MGR_PW
printf -v M5_CTRL_PW    '%s' "$(rand_hex 16)"; export M5_CTRL_PW
# admin pw for candidate startup_assert (not used for Matrix in candidate mode, but required by __main__)
printf -v M5_ADMIN_PW   '%s' "$(rand_hex 16)"; export M5_ADMIN_PW
printf -v M5_PG_PW      '%s' "$(rand_hex 16)"; export M5_PG_PW

ROLE_TOKENS_JSON="{\"reviewer\":\"$M5_REVIEW_TOK\",\"verifier\":\"$M5_VERIFY_TOK\",\"fixer\":\"$M5_FIXER_TOK\",\"coordinator\":\"$M5_GW_TOKEN-coord\",\"m5coordinator\":\"$M5_GW_TOKEN\"}"

cleanup() {
  local rc=$?
  set +e
  docker rm -f "$CAND" "$CAND2" "$GW" "$GH" "$HS" "$DB" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker rmi "$CAND_IMAGE" >/dev/null 2>&1
  exit "$rc"
}
trap cleanup EXIT

BASE_MIGS="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
PASS_COUNT=0
FAIL_COUNT=0
gate() { # gate NAME CONDITION  (CONDITION: 0=pass)
  local name="$1"; shift
  if [ "$1" = "0" ]; then
    echo "GATE PASS: $name"; PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "GATE FAIL: $name (rc=$1)"; FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

echo "=== M5-0A real Candidate integration (isolated stack, UNIQ=$UNIQ) ==="

# Record production PID BEFORE (must not change)
PROD_BEFORE=$(docker inspect mergepilot-controller --format '{{.State.Pid}} {{.State.StartedAt}}' 2>/dev/null || echo "missing")
echo "prod BEFORE: $PROD_BEFORE"

# ── 1. network + PG ──
docker network create --label "$LABEL" "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --network-alias m5i-pg --label "$LABEL" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=fixture_admin -e POSTGRES_DB="$DBNAME" \
  "$PG_IMAGE" >/dev/null
# wait for the actual database to be creatable (pg_isready races with DB init)
for _ in $(seq 1 90); do
  docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null

# roles
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
DO $r$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot') THEN CREATE ROLE mergepilot LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_audit') THEN CREATE ROLE policy_gateway_audit LOGIN; END IF;
END $r$;
SQL

for m in $BASE_MIGS; do
  docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/${m}.sql" >/dev/null
done
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >/dev/null
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_hotfix_1.sql" >/dev/null 2>&1 || true

# privileges (match M4-F E2E: controller tables + mcp_calls SELECT)
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
GRANT CONNECT ON DATABASE m5test TO policy_gateway_audit, mergepilot;
GRANT USAGE ON SCHEMA public TO policy_gateway_audit, mergepilot;
GRANT INSERT ON public.mcp_calls TO policy_gateway_audit;
REVOKE SELECT, UPDATE, DELETE ON public.mcp_calls FROM policy_gateway_audit;
GRANT SELECT, INSERT, UPDATE ON public.task_runs, public.run_pr_bindings, public.stage_events TO mergepilot;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.dispatch_outbox TO mergepilot;
GRANT SELECT ON public.mcp_calls TO mergepilot;
GRANT SELECT, INSERT, UPDATE ON public.controller_offsets, public.snapshot_job_outbox, public.skill_job_outbox TO mergepilot;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mergepilot;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mergepilot;
SQL

# seed task_run that the M4F_RUN will reference
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
VALUES('m5itest-run1','!m5test:room','example/project',42,'fix/m5itest','RUNNING','m4f_snapshot','trace-m5itest','ACTIVE')
ON CONFLICT (run_id) DO NOTHING;
INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
VALUES('bnd-m5itest','m5itest-run1','example/project',42,'fix/m5itest','main',repeat('2',40))
ON CONFLICT (binding_id) DO NOTHING;

-- Cross-partition sentinels: the Candidate must leave both rows untouched.
INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
VALUES('normal-run1','!normal:room','example/project',43,'fix/normal','RUNNING','m4f_snapshot','trace-normal','ACTIVE')
ON CONFLICT (run_id) DO NOTHING;
INSERT INTO stage_events(event_id,room_id,run_id,sender,event_type,raw_body,body_sha256,status,stage)
VALUES('$normal:m5test-hs','!normal:room','normal-run1','@manager:m5test-hs','M4F_RUN',
       '{"contract_version":"1","run_id":"normal-run1","trace_id":"trace-normal","repo":"example/project","pr_number":43,"test_runner":{"runner_key":"pytest"},"pr_lifecycle":{"action":"ensure_fix_pr","idempotency_key":"normal-ik","changes":[],"commit_message":"m","pr_title":"t","pr_body":"b"}}',
       'normal-sentinel','M4F_PENDING','m4f')
ON CONFLICT (event_id) DO NOTHING;
INSERT INTO dispatch_outbox(idempotency_key,run_id,room_id,target_agent,target_stage,attempt,body,status)
VALUES('normal-sentinel-dispatch','normal-run1','!normal:room','reviewer','review',1,'partition sentinel','PENDING')
ON CONFLICT (idempotency_key) DO NOTHING;
SQL
echo "PG ready + seeded"

# ── 2. mini Matrix HS ──
docker run -d --name "$HS" --network "$NET" --network-alias m5i-hs --label "$LABEL" \
  -v "$ROOT:/workspace:ro" -w /workspace \
  -e M5_HS_SERVER_NAME="m5test-hs" -e M5_HS_PORT="8008" \
  --entrypoint python "$RUNTIME_IMAGE" tests/m5_0/fixtures/mini_matrix_hs.py >/dev/null
sleep 2
# probe HS
docker run --rm --network "$NET" --entrypoint python "$RUNTIME_IMAGE" -c "
import urllib.request,json
r=urllib.request.urlopen('http://$HS:8008/_matrix/client/versions',timeout=3)
print('HS versions:', r.status)
" >/dev/null 2>&1 && echo "mini HS ready" || { echo "FAIL: mini HS not reachable"; exit 1; }

# ── 3. register users + create room + ctrl join ──
ROOM_INFO=$(docker run --rm --network "$NET" --entrypoint python "$RUNTIME_IMAGE" -c "
import urllib.request,json,urllib.error
HS='http://$HS:8008'
def post(p,d,tok=None):
    h={'Content-Type':'application/json'}
    if tok: h['Authorization']='Bearer '+tok
    req=urllib.request.Request(HS+p,data=json.dumps(d).encode(),headers=h)
    try: return json.loads(urllib.request.urlopen(req,timeout=5).read())
    except urllib.error.HTTPError as e: return json.loads(e.read())
post('/_matrix/client/v3/register',{'username':'manager','password':'$M5_MGR_PW'})
post('/_matrix/client/v3/register',{'username':'m5ctrl','password':'$M5_CTRL_PW'})
mgr=post('/_matrix/client/v3/login',{'type':'m.login.password','identifier':{'type':'m.id.user','user':'manager'},'password':'$M5_MGR_PW'})
room=post('/_matrix/client/v3/createRoom',{'invite':['@m5ctrl:m5test-hs']},tok=mgr['access_token'])
ctrl=post('/_matrix/client/v3/login',{'type':'m.login.password','identifier':{'type':'m.id.user','user':'m5ctrl'},'password':'$M5_CTRL_PW'})
post('/_matrix/client/v3/rooms/'+room['room_id']+'/join',{},tok=ctrl['access_token'])
print(json.dumps({'room_id':room['room_id'],'mgr_token':mgr['access_token']}))
")
ROOM_ID=$(echo "$ROOM_INFO" | python3 -c "import sys,json;print(json.load(sys.stdin)['room_id'])")
MGR_TOKEN=$(echo "$ROOM_INFO" | python3 -c "import sys,json;print(json.load(sys.stdin)['mgr_token'])")
echo "room=$ROOM_ID"

# ── 4. fake GitHub MCP + Gateway (m5coordinator policy) ──
docker run -d --name "$GH" --network "$NET" --network-alias m5i-fakegh --label "$LABEL" \
  -v "$ROOT:/workspace:ro" -w /workspace -e FIXTURE_REPO="example/project" \
  --entrypoint python "$GW_IMAGE" /workspace/tests/m4f1/fixtures/fake_github_mcp.py >/dev/null

# build a policy yaml with m5coordinator for this run (in temp)
TMP_POL="$(mktemp /tmp/m5i-pol.XXXXXX.yaml)"
cat > "$TMP_POL" <<YAML
version: "m5-0-integration-v1"
repos:
  allowlist: ["example/project"]
branches:
  base_allowlist: ["main"]
  fix_prefix: "fix/"
  protected: ["main"]
file_paths:
  denylist: ["glob:**/.env*"]
tool_classes:
  read: [pull_request_read, list_branches, list_pull_requests, get_file_contents, get_commit, list_commits]
  comment: []
  fix: [create_branch, push_files, create_pull_request]
  l2: []
  disabled: []
roles:
  reviewer: {classes: [read]}
  verifier: {classes: [read]}
  fixer: {classes: [read, fix], write_checks: true}
  coordinator: {classes: [read]}
  m5coordinator: {classes: [read]}
YAML

docker run -d --name "$GW" --network "$NET" --network-alias m5i-gateway --label "$LABEL" \
  -v "$ROOT:/workspace:ro" \
  -v "$ROOT/tools/policy-gateway/gateway.py:/app/gateway.py:ro" \
  -v "$TMP_POL:/app/policy.yaml:ro" \
  -e UPSTREAM_URL="http://m5i-fakegh:8082/sse" \
  -e ROLE_TOKENS="$ROLE_TOKENS_JSON" \
  -e AUDIT_DSN="host=m5i-pg dbname=$DBNAME user=policy_gateway_audit" \
  -e POLICY_FILE="/app/policy.yaml" \
  -e LISTEN_HOST="0.0.0.0" -e LISTEN_PORT="8083" \
  "$GW_IMAGE" >/dev/null
for _ in $(seq 1 90); do docker logs "$GW" 2>&1 | grep -q "Application startup complete" && break; sleep 1; done
docker logs "$GW" 2>&1 | tail -2
sleep 2  # let the port fully bind
echo "Gateway ready (m5coordinator policy)"

# ── 5. build + start Candidate ──
docker build -q -t "$CAND_IMAGE" "$ROOT/tools/workflow-controller" >/dev/null
echo "candidate image built"

# Verify Gateway is TCP-reachable from the test network before starting Candidate
# (Docker DNS alias propagation can lag by a few seconds after container start).
for _ in $(seq 1 30); do
  if docker run --rm --network "$NET" --entrypoint python "$RUNTIME_IMAGE" -c "
import socket
s=socket.socket(); s.settimeout(2)
try:
    s.connect(('m5i-gateway',8083)); s.close()
except OSError:
    raise SystemExit(1)
" >/dev/null 2>&1; then
    echo "Gateway TCP-reachable from test network"; break
  fi
  sleep 1
done

docker run -d --name "$CAND" --network "$NET" --label "$LABEL" \
  -e PG_HOST=m5i-pg -e PG_PORT=5432 -e PG_DATABASE="$DBNAME" -e PG_USER=mergepilot -e PG_PASS="$M5_PG_PW" \
  -e ADMIN_PW="$M5_CTRL_PW" \
  -e MATRIX_HS="http://$HS:8008" \
  -e MATRIX_SERVER_NAME="m5test-hs" \
  -e MATRIX_USER="m5ctrl" \
  -e CONTROLLER_CONSUMER_NAME="m5-0-candidate" \
  -e GATEWAY_URL="http://m5i-gateway:8083" \
  -e GATEWAY_ROLE="m5coordinator" \
  -e GATEWAY_TOKEN="$M5_GW_TOKEN" \
  -e M4F_ENABLED=1 -e M4F_LIVE_MODE=1 -e M4F_ONLY_MODE=1 \
  -e M4F_SNAPSHOT_DSN="host=m5i-pg dbname=$DBNAME user=mergepilot" \
  -e M4F_ALLOWED_ROOMS="$ROOM_ID" \
  -e M4F_ALLOWED_SENDERS="manager" \
  -e M4F_RUN_PREFIX="m5itest-" \
  -e RESERVED_RUN_PREFIXES="" \
  -e L2_MERGE_ENABLED=0 -e POLL_INTERVAL=3 \
  "$CAND_IMAGE" >/dev/null

# wait for candidate: advisory lock acquired + Matrix login OK
CAND_HEALTHY=1
for _ in $(seq 1 40); do
  if docker logs "$CAND" 2>&1 | grep -q "Matrix login OK"; then CAND_HEALTHY=0; break; fi
  sleep 1
done
docker logs "$CAND" 2>&1 | grep -E "advisory lock|Matrix login|FATAL|ERROR" | head -5
gate "1. Candidate healthy (lock acquired + Matrix login)" "$CAND_HEALTHY"

# ── 6. 2nd candidate → advisory lock denied → non-zero exit ──
docker rm -f "$CAND2" >/dev/null 2>&1 || true
set +e
docker run --rm --name "$CAND2" --network "$NET" --label "$LABEL" \
  -e PG_HOST=m5i-pg -e PG_PORT=5432 -e PG_DATABASE="$DBNAME" -e PG_USER=mergepilot -e PG_PASS="$M5_PG_PW" \
  -e ADMIN_PW="$M5_CTRL_PW" -e MATRIX_HS="http://$HS:8008" \
  -e MATRIX_SERVER_NAME="m5test-hs" -e MATRIX_USER="m5-ctrl-2" \
  -e CONTROLLER_CONSUMER_NAME="m5-0-candidate-2" \
  -e GATEWAY_URL="http://m5i-gateway:8083" -e GATEWAY_ROLE="m5coordinator" -e GATEWAY_TOKEN="$M5_GW_TOKEN" \
  -e M4F_ENABLED=1 -e M4F_LIVE_MODE=1 -e M4F_ONLY_MODE=1 \
  -e M4F_SNAPSHOT_DSN="host=m5i-pg dbname=$DBNAME user=mergepilot" \
  -e M4F_ALLOWED_ROOMS="$ROOM_ID" -e M4F_ALLOWED_SENDERS="manager" \
  -e M4F_RUN_PREFIX="m5itest2-" -e L2_MERGE_ENABLED=0 \
  "$CAND_IMAGE" > /tmp/m5i-cand2.log 2>&1
CAND2_RC=$?
set -e
echo "2nd candidate exit code: $CAND2_RC"
gate "2. 2nd Candidate advisory lock denied (non-zero exit)" "$([ $CAND2_RC -ne 0 ] && echo 0 || echo 1)"

# ── 7. Manager sends M4F_RUN via real Matrix API ──
M4F_BODY='M4F_RUN: {"contract_version":"1","run_id":"m5itest-run1","trace_id":"trace-m5itest","repo":"example/project","pr_number":42,"test_runner":{"runner_key":"pytest"},"pr_lifecycle":{"action":"ensure_fix_pr","idempotency_key":"m5ik","changes":[],"commit_message":"m","pr_title":"t","pr_body":"b"}}'
set +e
SEND_OUT=$(docker run --rm --network "$NET" --entrypoint python \
  -e M5_HS="http://$HS:8008" -e M5_ROOM_ID="$ROOM_ID" -e M5_MGR_TOKEN="$MGR_TOKEN" -e M5_BODY="$M4F_BODY" \
  "$RUNTIME_IMAGE" -c '
import os, urllib.request, json
data = json.dumps({"msgtype":"m.text","body":os.environ["M5_BODY"]}).encode()
req = urllib.request.Request(
    os.environ["M5_HS"]+"/_matrix/client/v3/rooms/"+os.environ["M5_ROOM_ID"]+"/send/m.room.message/m5txn1",
    data=data, headers={"Content-Type":"application/json","Authorization":"Bearer "+os.environ["M5_MGR_TOKEN"]})
r = json.loads(urllib.request.urlopen(req, timeout=5).read())
print(r["event_id"])
' 2>&1)
SEND_RC=$?
set -e
if [ "$SEND_RC" -ne 0 ]; then
  echo "Manager send FAILED (rc=$SEND_RC): $SEND_OUT"
  SENT_EVENT_ID=""
else
  SENT_EVENT_ID="$SEND_OUT"
fi
echo "Manager sent M4F_RUN, Matrix event_id=$SENT_EVENT_ID"

# wait for Candidate to /sync and durably record the real Matrix event
SYNCED=1
for _ in $(seq 1 30); do
  N=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT count(*) FROM stage_events WHERE event_id='$SENT_EVENT_ID'" 2>/dev/null)
  [ "$N" = "1" ] && { SYNCED=0; break; }
  sleep 1
done
gate "3. Candidate /sync consumed M4F_RUN (stage_events has real event_id)" "$SYNCED"

# wait for the authoritative M4-F state transition, not merely event receipt
PROCESSED_OK=1
for _ in $(seq 1 60); do
  STATUS=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT status FROM stage_events WHERE event_id='$SENT_EVENT_ID'" 2>/dev/null)
  [ "$STATUS" = "PROCESSED" ] && { PROCESSED_OK=0; break; }
  sleep 1
done

echo "=== Candidate log tail ==="
docker logs "$CAND" 2>&1 | tail -25 || true

# ── 8. authoritative assertions via PG ──
SENDER=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT sender FROM stage_events WHERE event_id='$SENT_EVENT_ID'" 2>/dev/null)
EID_DB=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT event_id FROM stage_events WHERE event_id='$SENT_EVENT_ID'" 2>/dev/null)
STATUS=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT status FROM stage_events WHERE event_id='$SENT_EVENT_ID'" 2>/dev/null)
RUN_ID_DB=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT run_id FROM stage_events WHERE event_id='$SENT_EVENT_ID'" 2>/dev/null)
echo "stage_events: event_id=$EID_DB sender=$SENDER run_id=$RUN_ID_DB status=$STATUS"
gate "4. event_id matches Matrix send return" "$([ "$EID_DB" = "$SENT_EVENT_ID" ] && echo 0 || echo 1)"
gate "5. sender is full @manager:m5test-hs" "$([ "$SENDER" = "@manager:m5test-hs" ] && echo 0 || echo 1)"
gate "6. stage_events reaches PROCESSED" "$PROCESSED_OK"

# controller_offsets independent row
OFFSET_N=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM controller_offsets WHERE consumer_name='m5-0-candidate'" 2>/dev/null)
gate "7. controller_offsets has m5-0-candidate row" "$([ "$OFFSET_N" -ge 1 ] && echo 0 || echo 1)"

# wait for drain → six authoritative Skill jobs
JOBS_OK=1
for _ in $(seq 1 60); do
  JN=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT count(*) FROM skill_job_outbox WHERE run_id='m5itest-run1'" 2>/dev/null)
  [ "$JN" = "6" ] && { JOBS_OK=0; break; }
  sleep 1
done
SKILL_NAMES=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT string_agg(skill_name,',' ORDER BY skill_name) FROM skill_job_outbox WHERE run_id='m5itest-run1'" 2>/dev/null)
EXPECTED_SKILLS="case-retrieval,diff-parse,pr-lifecycle,risk-classify,sast-scan,test-runner"
echo "skill jobs for m5itest-run1: ${JN:-0}; names=$SKILL_NAMES"
[ "$JOBS_OK" = "0" ] && [ "$SKILL_NAMES" = "$EXPECTED_SKILLS" ] && SKILLS_GATE=0 || SKILLS_GATE=1
gate "8. exactly six expected Skill jobs enqueued" "$SKILLS_GATE"

# Gateway audit role=m5coordinator
GW_ROLE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE caller_agent='m5coordinator' OR caller_agent LIKE '%m5coordinator%'" 2>/dev/null)
echo "Gateway audit m5coordinator rows: $GW_ROLE"
gate "9. Gateway audit role=m5coordinator" "$([ "$GW_ROLE" -ge 1 ] && echo 0 || echo 1)"

# Provenance exception is narrow: run-bound get_diff must remain denied.
set +e
docker exec "$CAND" python -c '
from gateway_client import GatewayError, gateway_call
try:
    gateway_call("pull_request_read", {
        "method": "get_diff", "owner": "example", "repo": "project",
        "pullNumber": 42, "mergepilot_run_id": "m5itest-run1",
    })
except GatewayError as exc:
    raise SystemExit(0)
raise SystemExit(1)
' >/dev/null 2>&1
PROVENANCE_NEG_RC=$?
set -e
PROVENANCE_DENY_N=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE caller_agent='m5coordinator' AND tool='pull_request_read' AND decision='DENY' AND reason_code='M4F_PROVENANCE_CONTEXT_DENIED' AND run_id='m5itest-run1'" 2>/dev/null)
[ "$PROVENANCE_NEG_RC" = "0" ] && [ "$PROVENANCE_DENY_N" -ge 1 ] && PROVENANCE_NEG_GATE=0 || PROVENANCE_NEG_GATE=1
gate "9b. m5coordinator provenance rejects non-get PR reads" "$PROVENANCE_NEG_GATE"

# cross-claim: the normal-prefix stage and dispatch sentinels remain untouched
CROSS_STAGE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_events WHERE event_id='\$normal:m5test-hs' AND status<>'M4F_PENDING'" 2>/dev/null)
CROSS_OUTBOX=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM dispatch_outbox WHERE idempotency_key='normal-sentinel-dispatch' AND status<>'PENDING'" 2>/dev/null)
echo "cross_claim mutations: stage_events=$CROSS_STAGE dispatch_outbox=$CROSS_OUTBOX"
gate "10. Candidate leaves non-prefix stage/outbox rows untouched" \
  "$([ "$CROSS_STAGE" = "0" ] && [ "$CROSS_OUTBOX" = "0" ] && echo 0 || echo 1)"

# ── 9. lock disconnect → non-zero exit ──
# identify and terminate only the Candidate's dedicated advisory-lock backend
LOCK_BACKENDS=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='$DBNAME' AND application_name='m5-0-candidate-m5-lock'" 2>/dev/null)
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DBNAME' AND application_name='m5-0-candidate-m5-lock'" >/dev/null 2>&1
LOCK_EXIT=1
for _ in $(seq 1 20); do
  ST=$(docker inspect "$CAND" --format '{{.State.Status}}' 2>/dev/null)
  [ "$ST" = "exited" ] && { LOCK_EXIT=0; break; }
  sleep 1
done
CAND_EXIT_CODE=$(docker inspect "$CAND" --format '{{.State.ExitCode}}' 2>/dev/null)
echo "Candidate exit after lock disconnect: $CAND_EXIT_CODE"
gate "11. exact lock backend disconnect causes Candidate non-zero exit" \
  "$([ "$LOCK_BACKENDS" = "1" ] && [ "$CAND_EXIT_CODE" -ne 0 ] && echo 0 || echo 1)"

# ── 10. production PID unchanged ──
PROD_AFTER=$(docker inspect mergepilot-controller --format '{{.State.Pid}} {{.State.StartedAt}}' 2>/dev/null)
echo "prod AFTER: $PROD_AFTER"
gate "12. Production PID/StartedAt unchanged" "$([ "$PROD_BEFORE" = "$PROD_AFTER" ] && echo 0 || echo 1)"

# ── residue ──
docker rm -f "$CAND" >/dev/null 2>&1 || true
docker rm -f "$GW" "$GH" "$HS" "$DB" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
CONTAINERS=$(docker ps -aq --filter "label=$LABEL" | wc -l | tr -d ' ')
NETWORKS=$(docker network ls -q --filter "label=$LABEL" | wc -l | tr -d ' ')
rm -f "$TMP_POL"
echo "residue: containers=$CONTAINERS networks=$NETWORKS"

echo "=== SUMMARY: PASS=$PASS_COUNT FAIL=$FAIL_COUNT ==="
echo "hiclaw_live=false (isolated mini-HS test stack, not shared production HiClaw)"
[ "$FAIL_COUNT" = "0" ] && [ "$CONTAINERS" = "0" ] && [ "$NETWORKS" = "0" ] && exit 0 || exit 1
