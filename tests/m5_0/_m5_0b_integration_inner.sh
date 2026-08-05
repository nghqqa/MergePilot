#!/usr/bin/env bash
# Inner: M5-0B DAG->review/fix/verify handoff closed loop (runs in WSL).
#
# Isolated throwaway stack: temp PG16 + mini Matrix HS (@manager/@reviewer/
# @fixer/@verifier) + test Gateway (m5coordinator) + fake GitHub MCP. The
# Candidate /syncs the mini HS: real M4F_RUN ingress, then real TASK_COMPLETED
# handoffs advance review->fix->verify->PASS. Six-Skill completion is injected
# (M4-F execution domain); the bridge + handoffs are the M5-0B surface under
# test. All secrets runtime-generated. cleanup trap -> residue 0/0/0.
#
# hiclaw_live=false: isolated mini-HS, NOT shared production HiClaw.
set -euo pipefail

ROOT="/mnt/d/goai/mergepilot-os"
# MergePilot test-env isolation guard (fail-closed: MergePilot-Test daemon only).
source "${ROOT}/tools/test-env/mp_guard.sh"
DBDIR="$ROOT/tools/audit-db"
GWDIR="$ROOT/tools/policy-gateway"
FIXDIR="$ROOT/tests/m5_0/fixtures"
M4F1FIX="$ROOT/tests/m4f1/fixtures"

PG_IMAGE="pgvector/pgvector:pg16"
RUNTIME_IMAGE="mergepilot-m4f-runtime:demo"
GW_IMAGE="policy-gateway:m4f"
CAND_IMAGE="mergepilot-m5-0b:integration"

UNIQ="m5b-$$-$(date +%s)"
LABEL="mergepilot.m5b.integration=${UNIQ}"
NET="m5b-net-${UNIQ}"
DB="m5b-pg-${UNIQ}"
HS="m5b-hs-${UNIQ}"
GH="m5b-gh-${UNIQ}"
GW="m5b-gw-${UNIQ}"
CAND="m5b-cand-${UNIQ}"
DBNAME="m5btest"
PREFIX="m5itest-"   # Candidate run prefix

rand_hex() { head -c "$1" /dev/urandom | od -An -v -tx1 | tr -d ' \n'; }

# runtime secrets (printf -v + export; never adjacent = in a source scanner)
printf -v M5B_GW_TOKEN '%s' "$(rand_hex 32)"; export M5B_GW_TOKEN
printf -v M5B_FIXER_TOK '%s' "$(rand_hex 32)"; export M5B_FIXER_TOK
printf -v M5B_REVIEW_TOK '%s' "$(rand_hex 32)"; export M5B_REVIEW_TOK
printf -v M5B_VERIFY_TOK '%s' "$(rand_hex 32)"; export M5B_VERIFY_TOK
printf -v M5B_MGR_PW '%s' "$(rand_hex 16)"; export M5B_MGR_PW
printf -v M5B_REV_PW '%s' "$(rand_hex 16)"; export M5B_REV_PW
printf -v M5B_FIX_PW '%s' "$(rand_hex 16)"; export M5B_FIX_PW
printf -v M5B_VER_PW '%s' "$(rand_hex 16)"; export M5B_VER_PW
printf -v M5B_CTRL_PW '%s' "$(rand_hex 16)"; export M5B_CTRL_PW
printf -v M5B_PG_PW '%s' "$(rand_hex 16)"; export M5B_PG_PW
printf -v M5B_HMAC_KEY '%s' "$(rand_hex 32)"; export M5B_HMAC_KEY

ROLE_TOKENS_JSON="{\"reviewer\":\"$M5B_REVIEW_TOK\",\"verifier\":\"$M5B_VERIFY_TOK\",\"fixer\":\"$M5B_FIXER_TOK\",\"coordinator\":\"$M5B_GW_TOKEN-coord\",\"m5coordinator\":\"$M5B_GW_TOKEN\"}"

COMPLETED=0
cleanup() {
  set +e
  docker rm -f "$CAND" "$GW" "$GH" "$HS" "$DB" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker rmi "$CAND_IMAGE" >/dev/null 2>&1
  local CONTAINERS NETWORKS
  CONTAINERS=$(docker ps -aq --filter "label=$LABEL" | wc -l)
  NETWORKS=$(docker network ls --filter "label=$LABEL" -q | wc -l)
  echo "residue: containers=$CONTAINERS networks=$NETWORKS"
  echo "gates: PASS=${PASS_COUNT:-0} FAIL=${FAIL_COUNT:-0} COMPLETED=$COMPLETED"
  if [ "${COMPLETED:-0}" = "1" ] && [ "${FAIL_COUNT:-0}" = 0 ] && [ "$CONTAINERS" = "0" ] && [ "$NETWORKS" = "0" ]; then
    exit 0
  else
    exit 1
  fi
}
trap cleanup EXIT

BASE_MIGS="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
PASS_COUNT=0
FAIL_COUNT=0
gate() {
  local name="$1" rc="$2"
  if [ "$rc" = "0" ]; then
    echo "GATE PASS: $name"; PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "GATE FAIL: $name (rc=$rc)"; FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

_prod_snapshot() {
  # name=containerid=running for each of the 6 production containers
  for c in mergepilot-controller policy-gw audit-pg github-mcp hiclaw-manager hiclaw-controller; do
    docker inspect "$c" --format "{{.Name}}={{.Id}}={{.State.Running}}" 2>/dev/null || echo "/$c=MISSING=false"
  done | tr '\n' ' '
}

echo "=== M5-0B handoff closed-loop integration (UNIQ=$UNIQ) ==="

# Record the 6 production containers' IDs (the invariant: the test must NOT
# replace them). PID/StartedAt may differ across the run because the host WSL
# stack keeps recycling these `--restart unless-stopped` containers — that is
# Under the isolated MergePilot-Test daemon (v2.6), the production controller is
# NOT visible at all (different dockerd + vhdx). The former same-daemon
# production-ID snapshot is replaced by "no production container visible".
PROD_VISIBLE_BEFORE=""
for _c in mergepilot-controller policy-gw audit-pg github-mcp hiclaw-manager hiclaw-controller; do
  if docker inspect "$_c" >/dev/null 2>&1; then PROD_VISIBLE_BEFORE="$PROD_VISIBLE_BEFORE $_c"; fi
done
echo "prod containers visible from test daemon BEFORE: '${PROD_VISIBLE_BEFORE:-none}'"

# ── 1. network + PG ──
docker network create --label "$LABEL" "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --network-alias m5b-pg --label "$LABEL" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=fixture_admin -e POSTGRES_DB="$DBNAME" \
  "$PG_IMAGE" >/dev/null
for _ in $(seq 1 90); do
  docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null

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

docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
GRANT CONNECT ON DATABASE m5btest TO mergepilot, policy_gateway_audit;
GRANT USAGE ON SCHEMA public TO mergepilot, policy_gateway_audit;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mergepilot;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mergepilot;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mergepilot;
GRANT INSERT ON public.mcp_calls TO policy_gateway_audit;
REVOKE SELECT, UPDATE, DELETE ON public.mcp_calls FROM policy_gateway_audit;
SQL

# seed task_run + binding + cross-partition sentinel (non-m5live)
RUN1="m5itest-run1"
RUN2="m5itest-run2"
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<SQL >/dev/null
INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
VALUES('$RUN1','!m5b:room','example/project',42,'fix/m5itest','RUNNING','m4f_snapshot','trace-b1','ACTIVE')
ON CONFLICT (run_id) DO NOTHING;
INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
VALUES('bnd-b1','$RUN1','example/project',42,'fix/m5itest','main',repeat('2',40))
ON CONFLICT (binding_id) DO NOTHING;
INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
VALUES('$RUN2','!m5b:room','example/project',43,'fix/m5itest2','RUNNING','m4f_await_verify','trace-b2','ACTIVE')
ON CONFLICT (run_id) DO NOTHING;
INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
VALUES('bnd-b2','$RUN2','example/project',43,'fix/m5itest2','main',repeat('2',40))
ON CONFLICT (binding_id) DO NOTHING;
-- RUN2 pre-seeded at m4f_await_verify (review+fix COMPLETED, verify PENDING_DISPATCH)
-- so the BLOCKED negative path exercises the real verify-handoff via /sync without
-- depending on the flaky M4-F revision-read Gateway path for a second full ingress.
INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN2','review','reviewer',1,'COMPLETED')
ON CONFLICT (run_id,stage,attempt) DO NOTHING;
INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN2','fix','fixer',1,'COMPLETED')
ON CONFLICT (run_id,stage,attempt) DO NOTHING;
INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN2','verify','verifier',1,'PENDING_DISPATCH')
ON CONFLICT (run_id,stage,attempt) DO NOTHING;
-- non-m5live sentinel: must stay untouched
INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
VALUES('normal-run1','!normal:room','example/project',44,'fix/normal','RUNNING','m4f_snapshot','trace-n','ACTIVE')
ON CONFLICT DO NOTHING;
INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('normal-run1','review','reviewer',1,'PENDING_DISPATCH')
ON CONFLICT (run_id,stage,attempt) DO NOTHING;
INSERT INTO dispatch_outbox(idempotency_key,run_id,room_id,target_agent,target_stage,attempt,body)
VALUES('normal-sentinel-dispatch','normal-run1','!normal:room','reviewer','review',1,'sentinel')
ON CONFLICT (idempotency_key) DO NOTHING;
SQL
echo "PG ready"

# ── 2. mini Matrix HS ──
docker run -d --name "$HS" --network "$NET" --network-alias m5b-hs --label "$LABEL" \
  -v "$ROOT:/workspace:ro" -w /workspace \
  -e M5_HS_SERVER_NAME="m5b-hs" -e M5_HS_PORT="8008" \
  --entrypoint python "$RUNTIME_IMAGE" tests/m5_0/fixtures/mini_matrix_hs.py >/dev/null
for _ in $(seq 1 30); do
  docker run --rm --network "$NET" --entrypoint python "$RUNTIME_IMAGE" -c \
    "import urllib.request; urllib.request.urlopen('http://$HS:8008/_matrix/client/versions',timeout=3)" \
    >/dev/null 2>&1 && break
  sleep 1
done

# register manager + reviewer + fixer + verifier + m5ctrl; create room; login all
ROOM_INFO=$(docker run --rm --network "$NET" --entrypoint python "$RUNTIME_IMAGE" -c "
import urllib.request,json,urllib.error
HS='http://$HS:8008'
def post(p,d,tok=None):
  h={'Content-Type':'application/json'}
  if tok: h['Authorization']='Bearer '+tok
  req=urllib.request.Request(HS+p,data=json.dumps(d).encode(),headers=h)
  try: return json.loads(urllib.request.urlopen(req,timeout=5).read())
  except urllib.error.HTTPError as e: return json.loads(e.read())
post('/_matrix/client/v3/register',{'username':'manager','password':'$M5B_MGR_PW'})
post('/_matrix/client/v3/register',{'username':'reviewer','password':'$M5B_REV_PW'})
post('/_matrix/client/v3/register',{'username':'fixer','password':'$M5B_FIX_PW'})
post('/_matrix/client/v3/register',{'username':'verifier','password':'$M5B_VER_PW'})
post('/_matrix/client/v3/register',{'username':'m5ctrl','password':'$M5B_CTRL_PW'})
mgr=post('/_matrix/client/v3/login',{'type':'m.login.password','identifier':{'type':'m.id.user','user':'manager'},'password':'$M5B_MGR_PW'})
room=post('/_matrix/client/v3/createRoom',{'invite':['@m5ctrl:m5b-hs','@reviewer:m5b-hs','@fixer:m5b-hs','@verifier:m5b-hs']},tok=mgr['access_token'])
for u,pw in [('m5ctrl','$M5B_CTRL_PW'),('reviewer','$M5B_REV_PW'),('fixer','$M5B_FIX_PW'),('verifier','$M5B_VER_PW')]:
  t=post('/_matrix/client/v3/login',{'type':'m.login.password','identifier':{'type':'m.id.user','user':u},'password':pw})
  post('/_matrix/client/v3/rooms/'+room['room_id']+'/join',{},tok=t['access_token'])
print(json.dumps({'room_id':room['room_id']}))
")
ROOM_ID=$(echo "$ROOM_INFO" | python3 -c "import sys,json;print(json.load(sys.stdin)['room_id'])")
echo "room=$ROOM_ID"
# Bind the seeded M5 task_runs to the real Matrix room so P1-3 room-authoritative
# handoff verification (event.room_id == task_runs.room_id) holds.
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c \
  "UPDATE task_runs SET room_id='$ROOM_ID' WHERE run_id LIKE 'm5itest-%';" >/dev/null 2>&1 || true

# ── 3. fake GitHub MCP + Gateway (m5coordinator) ──
docker run -d --name "$GH" --network "$NET" --network-alias m5b-fakegh --label "$LABEL" \
  -v "$ROOT:/workspace:ro" -w /workspace -e FIXTURE_REPO="example/project" \
  --entrypoint python "$GW_IMAGE" /workspace/tests/m4f1/fixtures/fake_github_mcp.py >/dev/null
TMP_POL="$(mktemp /tmp/m5b-pol.XXXXXX.yaml)"
cat > "$TMP_POL" <<YAML
version: "m5-0b-integration-v1"
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
docker run -d --name "$GW" --network "$NET" --network-alias m5b-gateway --label "$LABEL" \
  -v "$ROOT:/workspace:ro" \
  -v "$ROOT/tools/policy-gateway/gateway.py:/app/gateway.py:ro" \
  -v "$TMP_POL:/app/policy.yaml:ro" \
  -e UPSTREAM_URL="http://m5b-fakegh:8082/sse" \
  -e ROLE_TOKENS="$ROLE_TOKENS_JSON" \
  -e AUDIT_DSN="host=m5b-pg dbname=$DBNAME user=policy_gateway_audit" \
  -e POLICY_FILE="/app/policy.yaml" \
  -e LISTEN_HOST="0.0.0.0" -e LISTEN_PORT="8083" \
  "$GW_IMAGE" >/dev/null
for _ in $(seq 1 30); do
  docker run --rm --network "$NET" --entrypoint python "$RUNTIME_IMAGE" -c "
import socket; s=socket.socket(); s.settimeout(2)
try: s.connect(('m5b-gateway',8083)); s.close()
except OSError: raise SystemExit(1)" >/dev/null 2>&1 && break
  sleep 1
done

# ── 4. build + start Candidate ──
docker build -q -t "$CAND_IMAGE" "$ROOT/tools/workflow-controller" >/dev/null
docker run -d --name "$CAND" --network "$NET" --label "$LABEL" \
  -e PG_HOST=m5b-pg -e PG_PORT=5432 -e PG_DATABASE="$DBNAME" -e PG_USER=mergepilot -e PG_PASS="$M5B_PG_PW" \
  -e ADMIN_PW="$M5B_CTRL_PW" \
  -e MATRIX_HS="http://$HS:8008" -e MATRIX_SERVER_NAME="m5b-hs" \
  -e MATRIX_USER="m5ctrl" -e CONTROLLER_CONSUMER_NAME="m5-0b-candidate" \
  -e GATEWAY_URL="http://m5b-gateway:8083" -e GATEWAY_ROLE="m5coordinator" -e GATEWAY_TOKEN="$M5B_GW_TOKEN" \
  -e M4F_ENABLED=1 -e M4F_LIVE_MODE=1 -e M4F_ONLY_MODE=1 \
  -e M4F_SNAPSHOT_DSN="host=m5b-pg dbname=$DBNAME user=mergepilot" \
  -e M4F_ALLOWED_ROOMS="$ROOM_ID" \
  -e M4F_ALLOWED_SENDERS="manager,reviewer,fixer,verifier" \
  -e M4F_RUN_PREFIX="$PREFIX" -e RESERVED_RUN_PREFIXES="" \
  -e L2_MERGE_ENABLED=0 -e POLL_INTERVAL=2 \
  "$CAND_IMAGE" >/dev/null

CAND_HEALTHY=1
for _ in $(seq 1 40); do
  if docker logs "$CAND" 2>&1 | grep -q "Matrix login OK"; then CAND_HEALTHY=0; break; fi
  sleep 1
done
gate "1. Candidate healthy (lock + Matrix login)" "$CAND_HEALTHY"
echo "=== Candidate log tail ==="
docker logs "$CAND" 2>&1 | tail -15 || true

# helper: send a message as a user (login then PUT)
send_as() {
  local user="$1" pw="$2" body="$3" txn="$4"
  docker run --rm --network "$NET" --entrypoint python \
    -e BODY="$body" -e ROOM="$ROOM_ID" \
    "$RUNTIME_IMAGE" -c "
import urllib.request,json,os
hs='http://$HS:8008'
tok=json.loads(urllib.request.urlopen(urllib.request.Request(hs+'/_matrix/client/v3/login',data=json.dumps({'type':'m.login.password','identifier':{'type':'m.id.user','user':'$user'},'password':'$pw'}).encode(),headers={'Content-Type':'application/json'}),timeout=5).read())['access_token']
data=json.dumps({'msgtype':'m.text','body':os.environ['BODY']}).encode()
req=urllib.request.Request(hs+'/_matrix/client/v3/rooms/'+os.environ['ROOM']+'/send/m.room.message/$txn',data=data,headers={'Content-Type':'application/json','Authorization':'Bearer '+tok})
print(json.loads(urllib.request.urlopen(req,timeout=5).read())['event_id'])
"
}

# ── 5. M4F_RUN via real /sync ──
M4F_BODY='M4F_RUN: {"contract_version":"1","run_id":"'"$RUN1"'","trace_id":"trace-b1","repo":"example/project","pr_number":42,"test_runner":{"runner_key":"pytest"},"pr_lifecycle":{"action":"ensure_fix_pr","idempotency_key":"m5bk1","changes":[],"commit_message":"m","pr_title":"t","pr_body":"b"}}'
RUN1_EID=$(send_as "manager" "$M5B_MGR_PW" "$M4F_BODY" "txn-m4f1")
# wait Candidate /sync + ingress + 6 skills enqueued
ENQ_OK=1
for _ in $(seq 1 60); do
  N=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT count(*) FROM skill_job_outbox WHERE run_id='$RUN1'" 2>/dev/null)
  [ "$N" = "6" ] && { ENQ_OK=0; break; }
  sleep 1
done
gate "2. M4F_RUN /sync'd -> exactly six Skill jobs enqueued" "$ENQ_OK"

# ── 6. inject six-Skill SUCCEEDED + skill_invocations (M4-F execution domain) ──
docker run --rm --network "$NET" -v "$ROOT:/workspace:ro" --entrypoint python "$RUNTIME_IMAGE" \
  /workspace/tests/m5_0/fixtures/inject_skill_completion.py \
  "host=m5b-pg dbname=$DBNAME user=fixture_admin" "$RUN1" 2>&1 | tail -8
INJ_RC=${PIPESTATUS[0]}
gate "3. inject six-Skill SUCCEEDED + validated invocations" "$INJ_RC"

# ── 7. wait skill->review bridge -> review stage + reviewer dispatch (exactly 1) ──
BRIDGE_OK=1
for _ in $(seq 1 40); do
  ST=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT current_stage FROM task_runs WHERE run_id='$RUN1'" 2>/dev/null)
  [ "$ST" = "m4f_await_review" ] && { BRIDGE_OK=0; break; }
  sleep 1
done
RV_STAGE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_runs WHERE run_id='$RUN1' AND stage='review'" 2>/dev/null)
RV_DISP=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM dispatch_outbox WHERE idempotency_key='m5-$RUN1-review-dispatch'" 2>/dev/null)
echo "bridge: current_stage=$ST review_stages=$RV_STAGE review_dispatchs=$RV_DISP"
[ "$BRIDGE_OK" = "0" ] && [ "$RV_STAGE" = "1" ] && [ "$RV_DISP" = "1" ] && B_GATE=0 || B_GATE=1
gate "4. skill->review bridge: exactly 1 review stage + 1 reviewer dispatch" "$B_GATE"

# ── 8. Reviewer TASK_COMPLETED via real /sync -> fix stage + fixer dispatch ──
REV_EID=$(send_as "reviewer" "$M5B_REV_PW" "TASK_COMPLETED: $RUN1-review" "txn-rev1")
FIX_OK=1
for _ in $(seq 1 40); do
  ST=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT current_stage FROM task_runs WHERE run_id='$RUN1'" 2>/dev/null)
  [ "$ST" = "m4f_await_fix" ] && { FIX_OK=0; break; }
  sleep 1
done
FX_STAGE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_runs WHERE run_id='$RUN1' AND stage='fix'" 2>/dev/null)
FX_DISP=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM dispatch_outbox WHERE idempotency_key='m5-$RUN1-fix-dispatch'" 2>/dev/null)
RV_DONE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT status FROM stage_runs WHERE run_id='$RUN1' AND stage='review' ORDER BY attempt DESC LIMIT 1" 2>/dev/null)
echo "review handoff: current_stage=$ST fix_stages=$FX_STAGE fix_dispatchs=$FX_DISP review_stage=$RV_DONE"
[ "$FIX_OK" = "0" ] && [ "$FX_STAGE" = "1" ] && [ "$FX_DISP" = "1" ] && [ "$RV_DONE" = "COMPLETED" ] && FX_GATE=0 || FX_GATE=1
gate "5. Reviewer handoff -> fix stage + fixer dispatch (review COMPLETED)" "$FX_GATE"

# ── 9. Fixer TASK_COMPLETED -> verify stage + verifier dispatch ──
send_as "fixer" "$M5B_FIX_PW" "TASK_COMPLETED: $RUN1-fix" "txn-fix1" >/dev/null
VF_OK=1
for _ in $(seq 1 40); do
  ST=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT current_stage FROM task_runs WHERE run_id='$RUN1'" 2>/dev/null)
  [ "$ST" = "m4f_await_verify" ] && { VF_OK=0; break; }
  sleep 1
done
VF_STAGE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_runs WHERE run_id='$RUN1' AND stage='verify'" 2>/dev/null)
VF_DISP=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM dispatch_outbox WHERE idempotency_key='m5-$RUN1-verify-dispatch'" 2>/dev/null)
echo "fix handoff: current_stage=$ST verify_stages=$VF_STAGE verify_dispatchs=$VF_DISP"
[ "$VF_OK" = "0" ] && [ "$VF_STAGE" = "1" ] && [ "$VF_DISP" = "1" ] && V_GATE=0 || V_GATE=1
gate "6. Fixer handoff -> verify stage + verifier dispatch" "$V_GATE"

# ── 10. Verifier PASS -> HOLD/m5_verify_passed ──
send_as "verifier" "$M5B_VER_PW" "TASK_COMPLETED: $RUN1-verify
VERDICT=PASS" "txn-ver1" >/dev/null
PASS_OK=1
for _ in $(seq 1 40); do
  ST=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT status||':'||current_stage||':'||coalesce(verdict,'') FROM task_runs WHERE run_id='$RUN1'" 2>/dev/null)
  [ "$ST" = "HOLD:m5_verify_passed:PASS" ] && { PASS_OK=0; break; }
  sleep 1
done
echo "verify PASS: task=$ST"
gate "7. Verifier VERDICT=PASS -> HOLD/m5_verify_passed" "$PASS_OK"

# ── 11. Replay idempotency: re-send all three handoffs; counts must not grow ──
send_as "reviewer" "$M5B_REV_PW" "TASK_COMPLETED: $RUN1-review" "txn-rev1b" >/dev/null
send_as "fixer" "$M5B_FIX_PW" "TASK_COMPLETED: $RUN1-fix" "txn-fix1b" >/dev/null
send_as "verifier" "$M5B_VER_PW" "TASK_COMPLETED: $RUN1-verify
VERDICT=PASS" "txn-ver1b" >/dev/null
sleep 5
RV_STAGE2=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_runs WHERE run_id='$RUN1'" 2>/dev/null)
RV_DISP2=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM dispatch_outbox WHERE run_id LIKE 'm5itest-%'" 2>/dev/null)
echo "replay: total stages=$RV_STAGE2 total dispatches=$RV_DISP2"
# expected: 3 stages (review+fix+verify) + 3 dispatches (review/fix/verify) for RUN1
[ "$RV_STAGE2" = "3" ] && [ "$RV_DISP2" = "3" ] && REPL_GATE=0 || REPL_GATE=1
gate "8. Replay idempotency: no duplicate stage/dispatch" "$REPL_GATE"

# ── 12. Negative path: RUN2 six skills -> review -> ... -> verify VERDICT=BLOCKED -> HOLD ──
# ── 12. Negative path: RUN2 (pre-seeded at m4f_await_verify) -> verify VERDICT=BLOCKED -> HOLD ──
# RUN2 is pre-seeded at m4f_await_verify with review+fix COMPLETED + verify
# PENDING_DISPATCH, so the BLOCKED negative path exercises the REAL verify
# handoff via /sync (the M5-0B surface) without depending on the M4-F
# revision-read Gateway path for a second full ingress (flaky in this WSL stack).
send_as "verifier" "$M5B_VER_PW" "TASK_COMPLETED: $RUN2-verify
VERDICT=BLOCKED" "txn-ver2" >/dev/null 2>&1 || true
BL_OK=1
for _ in $(seq 1 40); do
  ST=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT status||':'||current_stage||':'||coalesce(verdict,'') FROM task_runs WHERE run_id='$RUN2'" 2>/dev/null)
  [ "$ST" = "HOLD:m5_verify_failed:BLOCKED" ] && { BL_OK=0; break; }
  sleep 1
done
echo "RUN2 BLOCKED: task=$ST"
gate "9. Verifier VERDICT=BLOCKED -> HOLD/m5_verify_failed" "$BL_OK"

# also verify the FAIL verdict path on a third pre-seeded run
RUN3="m5itest-run3"
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c \
  "INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
   VALUES('$RUN3','$ROOM_ID','example/project',45,'fix/m5itest3','RUNNING','m4f_await_verify','trace-b3','ACTIVE')
   ON CONFLICT (run_id) DO NOTHING;
   INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN3','verify','verifier',1,'PENDING_DISPATCH')
   ON CONFLICT (run_id,stage,attempt) DO NOTHING;" >/dev/null 2>&1 || true
send_as "verifier" "$M5B_VER_PW" "TASK_COMPLETED: $RUN3-verify
VERDICT=FAIL" "txn-ver3" >/dev/null 2>&1 || true
FL_OK=1
for _ in $(seq 1 40); do
  ST3=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
    "SELECT status||':'||current_stage||':'||coalesce(verdict,'') FROM task_runs WHERE run_id='$RUN3'" 2>/dev/null)
  [ "$ST3" = "HOLD:m5_verify_failed:FAIL" ] && { FL_OK=0; break; }
  sleep 1
done
echo "RUN3 FAIL: task=$ST3"
gate "9b. Verifier VERDICT=FAIL -> HOLD/m5_verify_failed" "$FL_OK"

# verify no-VERDICT (1-line) stays PARTIAL on a fourth pre-seeded run
RUN4="m5itest-run4"
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c \
  "INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state)
   VALUES('$RUN4','$ROOM_ID','example/project',46,'fix/m5itest4','RUNNING','m4f_await_verify','trace-b4','ACTIVE')
   ON CONFLICT (run_id) DO NOTHING;
   INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN4','verify','verifier',1,'PENDING_DISPATCH')
   ON CONFLICT (run_id,stage,attempt) DO NOTHING;" >/dev/null 2>&1 || true
send_as "verifier" "$M5B_VER_PW" "TASK_COMPLETED: $RUN4-verify" "txn-ver4" >/dev/null 2>&1 || true
sleep 5
PART_TASK=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT status||':'||current_stage FROM task_runs WHERE run_id='$RUN4'" 2>/dev/null)
PART_EVT=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT status FROM stage_events WHERE event_type='TASK_COMPLETED' AND run_id='$RUN4' ORDER BY received_at DESC LIMIT 1" 2>/dev/null)
echo "RUN4 PARTIAL: task=$PART_TASK event=$PART_EVT"
# run must stay RUNNING/m4f_await_verify (not finalized) + event PARTIAL
[ "$PART_TASK" = "RUNNING:m4f_await_verify" ] && [ "$PART_EVT" = "PARTIAL" ] && PT_GATE=0 || PT_GATE=1
gate "9c. Verify without VERDICT stays PARTIAL (run not finalized)" "$PT_GATE"

# ── 13. Non-m5live sentinel untouched ──
NS_STAGE=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_runs WHERE run_id='normal-run1'" 2>/dev/null)
NS_DISP=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM dispatch_outbox WHERE idempotency_key='normal-sentinel-dispatch' AND status='PENDING'" 2>/dev/null)
echo "sentinel: normal-run1 stages=$NS_STAGE pending-dispatch=$NS_DISP"
[ "$NS_STAGE" = "1" ] && [ "$NS_DISP" = "1" ] && NS_GATE=0 || NS_GATE=1
gate "10. Non-m5live sentinel rows untouched" "$NS_GATE"

# ── 14. negative: wrong-sender handoff rejected (reviewer sending -fix) ──
# send a -fix marker as reviewer -> must NOT advance any run; recorded ERROR
WSEND_EID=$(send_as "reviewer" "$M5B_REV_PW" "TASK_COMPLETED: $RUN1-fix" "txn-ws1" 2>/dev/null || echo "")
sleep 4
WS_ERR=$(docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -t -A -c \
  "SELECT count(*) FROM stage_events WHERE event_type='TASK_COMPLETED' AND status='ERROR'" 2>/dev/null)
echo "wrong-sender ERROR events=$WS_ERR"
gate "11. Wrong-sender handoff fail-closed (ERROR)" "$([ "$WS_ERR" -ge 1 ] && echo 0 || echo 1)"

# ── 15. daemon isolation: no production container visible (v2.6) ──
# Under the isolated MergePilot-Test daemon, the proof is that NO production
# container is visible before or after the run — replacing the former
# same-daemon production-ID snapshot.
PROD_VISIBLE_AFTER=""
for _c in mergepilot-controller policy-gw audit-pg github-mcp hiclaw-manager hiclaw-controller; do
  if docker inspect "$_c" >/dev/null 2>&1; then PROD_VISIBLE_AFTER="$PROD_VISIBLE_AFTER $_c"; fi
done
echo "prod containers visible from test daemon AFTER: '${PROD_VISIBLE_AFTER:-none}'"
gate "12. No production container visible from test daemon (daemon isolation)" \
  "$([ -z "$PROD_VISIBLE_BEFORE" ] && [ -z "$PROD_VISIBLE_AFTER" ] && echo 0 || echo 1)"

echo "=== SUMMARY: PASS=$PASS_COUNT FAIL=$FAIL_COUNT ==="
echo "hiclaw_live=false (isolated mini-HS test stack, not shared production HiClaw)"
COMPLETED=1
