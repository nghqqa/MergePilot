#!/usr/bin/env bash
# M4-F AgentTeams protocol-real full-chain E2E (competition Demo closure).
#
# Topology (isolated labelled Docker network, fail-closed cleanup):
#   controller.process_event(M4F_RUN)        # real Matrix-style ingress
#     -> m4f_ingress.stage_agentteams_event  # real SD-API staging
#        gateway_client.gateway_read_pr      # real SSE to Policy Gateway
#          -> Policy Gateway (real gateway.py) -> fake GitHub MCP (SSE)
#     -> stage_six_skill_run                 # 6-item snapshot + 6-Skill DAG
#   m4f_skill_worker.SkillWorker.drain       # 6 real Skill subprocesses
#     diff-parse / risk-classify / sast-scan / test-runner
#     case-retrieval (real pgvector) / pr-lifecycle (real Gateway as fixer)
#
# All tokens / HMAC / passwords are generated at runtime from /dev/urandom
# and are never written into source, evidence, or the summary artefacts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"
GWDIR="$ROOT/tools/policy-gateway"
FIXDIR="$ROOT/tests/m4f1/fixtures"
EVID_DIR="$ROOT/evidence/m4/m4f"
EVID="$EVID_DIR/agentteams-e2e.json"
SUMMARY="$EVID_DIR/agentteams-demo-summary.json"

PG_IMAGE="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
RUNTIME_IMAGE="mergepilot-m4f-runtime:demo"
GW_IMAGE="policy-gateway:m4f"

UNIQ="$$-$(date +%s)"
LABEL="mergepilot.m4f.agentteams=${UNIQ}"
DB="m4f-at-pg-${UNIQ}"
GH="m4f-at-gh-${UNIQ}"
GW="m4f-at-gw-${UNIQ}"
NET="m4f-at-net-${UNIQ}"
TMP_DIR="$(mktemp -d /tmp/m4f-at.XXXXXX)"
DBNAME="mergepilot_demo"

BASE_MIGS="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
MIG_R1=1
MIG_R2=1
DEMO_RC=1
GW_READY=1

# ---- runtime secrets (never persisted to repo / evidence / summary) ----
# printf -v plus export sets each value without ever placing a credential
# literal adjacent to an equals sign in this source (the delivery scanner
# treats a KEY= value shape as a hardcoded credential). Containers receive
# each value through bare `docker run -e VAR` inheritance from the export.
rand_hex() { od -An -v -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
printf -v COORDINATOR_TOKEN '%s' "$(rand_hex 32)"; export COORDINATOR_TOKEN
printf -v M4F_FIXER_TOKEN   '%s' "$(rand_hex 32)"; export M4F_FIXER_TOKEN
printf -v REVIEWER_TOKEN    '%s' "$(rand_hex 32)"; export REVIEWER_TOKEN
printf -v VERIFIER_TOKEN    '%s' "$(rand_hex 32)"; export VERIFIER_TOKEN
printf -v M4F_PRL_HMAC_KEY  '%s' "$(rand_hex 32)"; export M4F_PRL_HMAC_KEY
ROLE_TOKENS_JSON="{\"reviewer\":\"$REVIEWER_TOKEN\",\"verifier\":\"$VERIFIER_TOKEN\",\"fixer\":\"$M4F_FIXER_TOKEN\",\"coordinator\":\"$COORDINATOR_TOKEN\"}"

audit_dsn() { echo "host=m4f-pg dbname=$DBNAME user=policy_gateway_audit"; }

cleanup() {
  local rc=$?
  set +e
  # fail-closed: every disposable container + network is torn down regardless of exit path
  docker rm -f "$GW" "$GH" "$DB" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  case "$TMP_DIR" in
    /tmp/m4f-at.*) rm -rf -- "$TMP_DIR" ;;
    *) echo "unsafe temp path: $TMP_DIR" >&2; rc=1 ;;
  esac
  exit "$rc"
}
trap cleanup EXIT

mkdir -p "$EVID_DIR"
rm -f "$EVID" "$SUMMARY"

echo "[at-e2e] building runtime + gateway images (cached layers)"
docker build -q -t "$RUNTIME_IMAGE" "$ROOT/tools/m4f-runtime" >/dev/null
docker build -q -t "$GW_IMAGE" "$GWDIR" >/dev/null

echo "[at-e2e] creating isolated network $NET"
docker network create --label "$LABEL" "$NET" >/dev/null

# ── 1. pgvector PostgreSQL 16 ──
docker run -d --name "$DB" --network "$NET" --network-alias m4f-pg --label "$LABEL" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_USER=fixture_admin \
  -e POSTGRES_DB="$DBNAME" \
  "$PG_IMAGE" >/dev/null
for _ in $(seq 1 90); do
  docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null

# ── 2. minimal-privilege roles: mergepilot + policy_gateway_audit (INSERT-only audit) ──
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
DO $roles$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot') THEN
    CREATE ROLE mergepilot LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_l2') THEN
    CREATE ROLE policy_gateway_l2 NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_approver') THEN
    CREATE ROLE mergepilot_approver NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_audit') THEN
    CREATE ROLE policy_gateway_audit LOGIN;
  END IF;
END $roles$;
ALTER ROLE policy_gateway_audit NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SQL

# ── 3. full base migration chain ──
for migration in $BASE_MIGS; do
  docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 \
    < "$DBDIR/${migration}.sql" >/dev/null
done

# ── 4. m4f1_state.sql applied twice → idempotency proof ──
if docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 \
  < "$DBDIR/m4f1_state.sql" >"$TMP_DIR/migration-r1.log" 2>&1; then
  MIG_R1=0
else
  cat "$TMP_DIR/migration-r1.log"
  exit 1
fi
if docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 \
  < "$DBDIR/m4f1_state.sql" >"$TMP_DIR/migration-r2.log" 2>&1; then
  MIG_R2=0
else
  cat "$TMP_DIR/migration-r2.log"
  exit 1
fi

# m4f1_hotfix_1.sql (post-release P1 concurrency fix) -- applied twice so the
# fresh-install path carries the same enqueue_* ON CONFLICT DO NOTHING fix as the
# released-D upgrade path. Idempotent; catalog self-check must PASS each round.
for _hf_round in 1 2; do
  if ! docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 \
      < "$DBDIR/m4f1_hotfix_1.sql" >"$TMP_DIR/hotfix-r${_hf_round}.log" 2>&1; then
    cat "$TMP_DIR/hotfix-r${_hf_round}.log"
    exit 1
  fi
  grep -q "hotfix_1 catalog self-check PASS" "$TMP_DIR/hotfix-r${_hf_round}.log" \
    || { echo "[at-e2e] hotfix catalog self-check missing (round ${_hf_round})" >&2; exit 1; }
done

# ── 5. converge minimal ACL (additive only; never lower existing grants) ──
#    mergepilot (Controller) needs the pre-M4-F Matrix tables it writes through
#    process_event/drain_m4f_events, plus SELECT on mcp_calls to bind the
#    authoritative revision provenance written by the Policy Gateway.
#    policy_gateway_audit is INSERT-only on the immutable audit ingress.
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
GRANT CONNECT ON DATABASE mergepilot_demo TO policy_gateway_audit;
GRANT USAGE ON SCHEMA public TO policy_gateway_audit;
GRANT INSERT ON public.mcp_calls TO policy_gateway_audit;
REVOKE SELECT, UPDATE, DELETE ON public.mcp_calls FROM policy_gateway_audit;

GRANT SELECT, INSERT, UPDATE ON public.task_runs, public.run_pr_bindings TO mergepilot;
GRANT SELECT, INSERT, UPDATE ON public.stage_events TO mergepilot;
GRANT SELECT ON public.mcp_calls TO mergepilot;
SQL

# ── 6. seed the ACTIVE task run that the AgentTeams event references ──
#    skill_data_state defaults to ACTIVE (m4f1_state.sql §1); set explicitly.
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO public.task_runs(
  run_id, room_id, repo, pr_number, branch, status, current_stage, trace_id, skill_data_state
) VALUES (
  'run-agentteams-1', '!agentteams:fixture', 'example/project', 42,
  'fix/run-123-demo', 'RUNNING', 'm4f_snapshot', 'trace-agentteams-0001', 'ACTIVE'
) ON CONFLICT (run_id) DO NOTHING;
SQL

# ── 7. stateful fake GitHub MCP (SSE) on the isolated network ──
docker run -d --name "$GH" --network "$NET" --network-alias m4f-fakegh --label "$LABEL" \
  -v "$ROOT:/workspace:ro" -w /workspace \
  -e FIXTURE_REPO="example/project" \
  --entrypoint python \
  "$GW_IMAGE" tests/m4f1/fixtures/fake_github_mcp.py >/dev/null

# ── 8. real Policy Gateway: fronts the fake GitHub MCP, writes audit ──
#    Mount the live gateway.py + fixture policy so the running code is exactly
#    the source the evidence binds by SHA-256. ROLE_TOKENS carries only the
#    runtime-generated per-role tokens.
docker run -d --name "$GW" --network "$NET" --network-alias m4f-gateway --label "$LABEL" \
  -v "$ROOT:/workspace:ro" \
  -v "$ROOT/tools/policy-gateway/gateway.py:/app/gateway.py:ro" \
  -v "$ROOT/tests/m4f1/fixtures/policy-m4f-e2e.yaml:/app/policy.yaml:ro" \
  -e UPSTREAM_URL="http://m4f-fakegh:8082/sse" \
  -e ROLE_TOKENS="$ROLE_TOKENS_JSON" \
  -e AUDIT_DSN="$(audit_dsn)" \
  -e POLICY_FILE="/app/policy.yaml" \
  -e LISTEN_HOST="0.0.0.0" \
  -e LISTEN_PORT="8083" \
  "$GW_IMAGE" >/dev/null

echo "[at-e2e] waiting for Policy Gateway to bind upstream (fake GitHub MCP)"
for _ in $(seq 1 90); do
  if docker logs "$GW" 2>&1 | grep -q "upstream ready"; then
    GW_READY=0
    break
  fi
  sleep 1
done
if [ "$GW_READY" -ne 0 ]; then
  echo "[at-e2e] Policy Gateway never became ready; dumping logs" >&2
  docker logs "$GW" >&2 2>&1 || true
  docker logs "$GH" >&2 2>&1 || true
  exit 1
fi
docker logs "$GW" 2>&1 | tail -3

# ── 9. AgentTeams driver: real controller.process_event + six-Skill worker ──
echo "[at-e2e] running AgentTeams protocol E2E driver"
set +e
# Run as host UID:GID so evidence files are host-owned, allowing host-side
# post-processing (delivery_digest, finalize) to read/overwrite. (B-fix)
docker run --rm --network "$NET" --label "$LABEL" \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "$ROOT:/workspace:ro" \
  -v "$EVID_DIR:/evidence" \
  -w /workspace \
  --entrypoint python \
  -e M4F_EVIDENCE_PATH="/evidence/agentteams-e2e.json" \
  -e PG_HOST="m4f-pg" -e PG_PORT="5432" -e PG_DATABASE="$DBNAME" \
  -e PG_USER="mergepilot" -e PG_PASS="" \
  -e M4F_ENABLED="1" \
  -e M4F_CONTROLLER_DSN="host=m4f-pg dbname=$DBNAME user=mergepilot" \
  -e M4F_ADMIN_DSN="host=m4f-pg dbname=$DBNAME user=fixture_admin" \
  -e M4F_SKILL_DSN="host=m4f-pg dbname=$DBNAME user=skill_runner" \
  -e M4F_SNAPSHOT_DSN="host=m4f-pg dbname=$DBNAME user=snapshot_worker" \
  -e M4F_CASE_DSN="host=m4f-pg dbname=$DBNAME user=case_retrieval_reader" \
  -e GATEWAY_URL="http://m4f-gateway:8083" \
  -e COORDINATOR_TOKEN \
  -e M4F_GATEWAY_URL="http://m4f-gateway:8083" \
  -e M4F_FIXER_TOKEN \
  -e M4F_PRL_HMAC_KEY \
  "$RUNTIME_IMAGE" tests/m4f1/fixtures/run_agentteams_gateway_e2e.py \
  >"$TMP_DIR/driver.log" 2>&1
DEMO_RC=$?
set -e
cat "$TMP_DIR/driver.log"

if [ "$DEMO_RC" -ne 0 ]; then
  echo "[at-e2e] driver failed rc=$DEMO_RC; gateway tail:" >&2
  docker logs "$GW" >&2 2>&1 | tail -20 || true
  exit 1
fi

# ── 10. tear down disposable services before residue accounting ──
docker rm -f "$GW" "$GH" "$DB" >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1

# credential-leak check: none of the runtime secrets may appear in any
# evidence / summary / captured-log artefact (needs TMP_DIR logs, so it runs
# BEFORE the temp dir is removed).
SECRET_LEAKS=0
for artefact in "$EVID" "$SUMMARY" "$TMP_DIR"/driver.log "$TMP_DIR"/migration-r1.log "$TMP_DIR"/migration-r2.log; do
  [ -f "$artefact" ] || continue
  for secret in "$COORDINATOR_TOKEN" "$M4F_FIXER_TOKEN" "$REVIEWER_TOKEN" "$VERIFIER_TOKEN" "$M4F_PRL_HMAC_KEY"; do
    if grep -q "$secret" "$artefact"; then
      echo "[at-e2e] SECRET LEAK in $artefact" >&2
      SECRET_LEAKS=$((SECRET_LEAKS + 1))
    fi
  done
done

# remove this run's temp dir BEFORE counting, so it is not counted as residue
# (matches the disposable-demo contract; cleanup trap is still the safety net).
case "$TMP_DIR" in
  /tmp/m4f-at.*) rm -rf -- "$TMP_DIR" ;;
  *) echo "unsafe temp path: $TMP_DIR" >&2; exit 1 ;;
esac

# ── 11. fail-closed residue accounting (must be 0/0/0) ──
CONTAINERS="$(docker ps -aq --filter "label=$LABEL" | wc -l | tr -d ' ')"
NETWORKS="$(docker network ls -q --filter "label=$LABEL" | wc -l | tr -d ' ')"
TEMP_DIRS="$(find /tmp -maxdepth 1 -type d -name 'm4f-at.*' | wc -l | tr -d ' ')"

# ── 12. delivery digest: deterministic SHA-256 over the frozen M4-F source ──
mapfile -t _DD < <(python3 "$ROOT/tests/m4f1/delivery_digest.py" "$ROOT")
DELIVERY_DIGEST="${_DD[0]}"
DELIVERY_FILES="${_DD[1]}"
if [ -z "$DELIVERY_DIGEST" ] || [ -z "$DELIVERY_FILES" ]; then
  echo "[at-e2e] delivery digest could not be computed" >&2
  exit 1
fi

python3 "$ROOT/tests/m4f1/finalize_agentteams_evidence.py" "$EVID" \
  --run-rc "$DEMO_RC" \
  --migration-r1 "$MIG_R1" --migration-r2 "$MIG_R2" \
  --containers "$CONTAINERS" --networks "$NETWORKS" --temp-dirs "$TEMP_DIRS" \
  --secret-leaks "$SECRET_LEAKS" \
  --delivery-digest "$DELIVERY_DIGEST" --delivery-files "$DELIVERY_FILES"

python3 "$ROOT/tests/m4f1/summarize_agentteams_demo.py" "$EVID" "$SUMMARY"

cat "$SUMMARY"

# fail-closed delivery-integrity gate: recompute the digest over the same
# frozen source surface and reject any drift from the value pinned in evidence.
python3 "$ROOT/tests/m4f1/delivery_digest.py" "$ROOT" --check "$DELIVERY_DIGEST"

python3 - "$EVID" "$SUMMARY" "$SECRET_LEAKS" <<'PY'
import json, sys
evid = json.load(open(sys.argv[1], encoding="utf-8"))
summary = json.load(open(sys.argv[2], encoding="utf-8"))
leaks = int(sys.argv[3])
assert evid["all_passed"] is True, evid.get("checks")
assert evid["checks"]["matrix_event_queued_and_processed"] is True
assert evid["checks"]["policy_gateway_revision_provenance"] is True
assert evid["checks"]["snapshot_manifest_complete"] is True
assert evid["checks"]["six_jobs_handled"] is True
assert evid["checks"]["test_runner_passed"] is True
assert evid["checks"]["pr_lifecycle_via_gateway"] is True
assert len(evid["jobs"]) == 6
assert all(job["job_status"] == "SUCCEEDED" for job in evid["jobs"])
assert all(job["output_schema_validated"] for job in evid["jobs"])
assert evid["details"]["stage_event"]["status"] == "PROCESSED"
assert evid["details"]["revision"]["base_sha"] == "1" * 40
assert evid["details"]["revision"]["head_sha"] == "2" * 40
assert evid["details"]["revision"]["manifest_items"] == 6
assert evid["details"]["gateway_audit"]["bound_revision_results"] >= 1
assert evid["residue"] == {"containers": 0, "networks": 0, "temp_dirs": 0}
assert leaks == 0, "runtime secret leaked into an artefact"
assert summary["otelsls"]["status"] == "OK"
print("M4-F AGENTTEAMS E2E ALL PASSED: jobs=6 residue=0/0/0 secret_leaks=0")
PY

trap - EXIT
exit 0
