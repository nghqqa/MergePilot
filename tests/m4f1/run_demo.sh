#!/usr/bin/env bash
# Disposable Controller -> snapshot -> six-Skill M4-F competition Demo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"
EVID_DIR="$ROOT/evidence/m4/m4f"
EVID="$EVID_DIR/full-chain-e2e.json"
PG_IMAGE="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
RUNTIME_IMAGE="mergepilot-m4f-runtime:demo"
UNIQ="$$-$(date +%s)"
LABEL="mergepilot.m4f.demo=${UNIQ}"
DB="m4f-demo-pg-${UNIQ}"
NET="m4f-demo-net-${UNIQ}"
TMP_DIR="$(mktemp -d /tmp/m4f-demo.XXXXXX)"
BASE_MIGS="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
MIG_R1=1
MIG_R2=1
DEMO_RC=1
REVISION_CUT_RC=1
PURGE_RACE_RC=1

cleanup() {
  local rc=$?
  set +e
  docker rm -f "$DB" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  case "$TMP_DIR" in
    /tmp/m4f-demo.*) rm -rf -- "$TMP_DIR" ;;
    *) echo "unsafe temp path: $TMP_DIR" >&2; rc=1 ;;
  esac
  exit "$rc"
}
trap cleanup EXIT

mkdir -p "$EVID_DIR"
rm -f "$EVID"

docker build -q -t "$RUNTIME_IMAGE" "$ROOT/tools/m4f-runtime" >/dev/null
docker network create --label "$LABEL" "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --network-alias m4f-pg \
  --label "$LABEL" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_USER=fixture_admin \
  -e POSTGRES_DB=mergepilot_demo \
  "$PG_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$DB" pg_isready -U fixture_admin -d mergepilot_demo >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$DB" pg_isready -U fixture_admin -d mergepilot_demo >/dev/null

docker exec -i "$DB" psql -U fixture_admin -d mergepilot_demo -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot') THEN
    CREATE ROLE mergepilot LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_l2') THEN
    CREATE ROLE policy_gateway_l2 NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_approver') THEN
    CREATE ROLE mergepilot_approver NOLOGIN;
  END IF;
END $roles$;
SQL

for migration in $BASE_MIGS; do
  docker exec -i "$DB" psql -U fixture_admin -d mergepilot_demo -v ON_ERROR_STOP=1 \
    < "$DBDIR/${migration}.sql" >/dev/null
done

if docker exec -i "$DB" psql -U fixture_admin -d mergepilot_demo -v ON_ERROR_STOP=1 \
  < "$DBDIR/m4f1_state.sql" >"$TMP_DIR/migration-r1.log" 2>&1; then
  MIG_R1=0
else
  cat "$TMP_DIR/migration-r1.log"
  exit 1
fi
if docker exec -i "$DB" psql -U fixture_admin -d mergepilot_demo -v ON_ERROR_STOP=1 \
  < "$DBDIR/m4f1_state.sql" >"$TMP_DIR/migration-r2.log" 2>&1; then
  MIG_R2=0
else
  cat "$TMP_DIR/migration-r2.log"
  exit 1
fi

# The disposable base chain is installed by fixture_admin, while production's
# pre-M4-F Controller tables are owned/writable by mergepilot. Reproduce only
# those existing Controller privileges; M4-F tables remain SD-API-only.
docker exec -i "$DB" psql -U fixture_admin -d mergepilot_demo -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
GRANT SELECT, INSERT, UPDATE ON public.task_runs, public.run_pr_bindings TO mergepilot;
SQL

docker exec -i "$DB" psql -U fixture_admin -d mergepilot_demo -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO public.task_runs(
  run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id
) VALUES (
  'run-123','!demo:fixture','example/project',42,'fix/run-123-demo',
  'RUNNING','m4f_snapshot','trace-m4f-demo-0001'
);
INSERT INTO public.run_pr_bindings(
  binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha
) VALUES (
  'bnd-demo-run-123','run-123','example/project',42,
  'fix/run-123-demo','main',repeat('2',40)
);
INSERT INTO public.mcp_calls(
  request_id,correlation_id,phase,ts,caller_agent,tool,decision,
  run_id,target_repo,git_sha,result_status
) VALUES (
  'demo-base-read-result','demo-base-read-correlation','RESULT',now(),
  'coordinator','github.get_commit','ALLOW','run-123','example/project',
  repeat('1',40),'OK'
);
SQL

set +e
docker run --rm --network "$NET" --label "$LABEL" \
  -v "$ROOT:/workspace:ro" \
  -v "$EVID_DIR:/evidence" \
  -e M4F_DB_HOST=m4f-pg \
  -e M4F_DB_NAME=mergepilot_demo \
  -e "M4F_ADMIN_DSN=host=m4f-pg dbname=mergepilot_demo user=fixture_admin" \
  -e "M4F_CONTROLLER_DSN=host=m4f-pg dbname=mergepilot_demo user=mergepilot" \
  -e "M4F_SNAPSHOT_DSN=host=m4f-pg dbname=mergepilot_demo user=snapshot_worker" \
  -e "M4F_SKILL_DSN=host=m4f-pg dbname=mergepilot_demo user=skill_runner" \
  "$RUNTIME_IMAGE" --evidence /evidence/full-chain-e2e.json \
  >"$TMP_DIR/demo.log" 2>&1
DEMO_RC=$?
set -e
cat "$TMP_DIR/demo.log"

set +e
docker run --rm --network "$NET" --label "$LABEL" --entrypoint python \
  -v "$ROOT:/workspace:ro" \
  -e "M4F_ADMIN_DSN=host=m4f-pg dbname=mergepilot_demo user=fixture_admin" \
  -e "M4F_CONTROLLER_DSN=host=m4f-pg dbname=mergepilot_demo user=mergepilot" \
  "$RUNTIME_IMAGE" tests/m4f1/fixtures/run_revision_cut.py \
  >"$TMP_DIR/revision-cut.log" 2>&1
REVISION_CUT_RC=$?
set -e
cat "$TMP_DIR/revision-cut.log"

set +e
docker run --rm --network "$NET" --label "$LABEL" --entrypoint python \
  -v "$ROOT:/workspace:ro" \
  -e "M4F_ADMIN_DSN=host=m4f-pg dbname=mergepilot_demo user=fixture_admin" \
  -e "M4F_CONTROLLER_DSN=host=m4f-pg dbname=mergepilot_demo user=mergepilot" \
  -e "M4F_SNAPSHOT_DSN=host=m4f-pg dbname=mergepilot_demo user=snapshot_worker" \
  -e "M4F_SKILL_DSN=host=m4f-pg dbname=mergepilot_demo user=skill_runner" \
  -e "M4F_PURGE_DSN=host=m4f-pg dbname=mergepilot_demo user=purge_operator" \
  "$RUNTIME_IMAGE" tests/m4f1/fixtures/run_complete_purge_race.py \
  >"$TMP_DIR/complete-purge-race.log" 2>&1
PURGE_RACE_RC=$?
set -e
cat "$TMP_DIR/complete-purge-race.log"

docker rm -f "$DB" >/dev/null
docker network rm "$NET" >/dev/null
case "$TMP_DIR" in
  /tmp/m4f-demo.*) rm -rf -- "$TMP_DIR" ;;
  *) echo "unsafe temp path: $TMP_DIR" >&2; exit 1 ;;
esac

if [ ! -f "$EVID" ]; then
  echo "demo evidence was not generated (demo rc=$DEMO_RC)" >&2
  exit 1
fi

CONTAINERS="$(docker ps -aq --filter "label=$LABEL" | wc -l | tr -d ' ')"
NETWORKS="$(docker network ls -q --filter "label=$LABEL" | wc -l | tr -d ' ')"
TEMP_DIRS="$(find /tmp -maxdepth 1 -type d -name 'm4f-demo.*' | wc -l | tr -d ' ')"

python3 "$ROOT/tests/m4f1/finalize_demo_evidence.py" "$EVID" \
  --demo-rc "$DEMO_RC" --containers "$CONTAINERS" --networks "$NETWORKS" \
  --temp-dirs "$TEMP_DIRS" --migration-r1 "$MIG_R1" --migration-r2 "$MIG_R2" \
  --revision-cut "$REVISION_CUT_RC" --purge-race "$PURGE_RACE_RC"

python3 - "$EVID" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
assert d["all_passed"] is True
assert len(d["jobs"]) == 6
assert d["checks"]["test_runner_passed"] is True
assert d["checks"]["pr_lifecycle_created"] is True
assert d["runner"]["revision_cut_rc"] == 0
assert d["runner"]["complete_purge_race_rc"] == 0
assert d["residue"] == {"containers":0,"networks":0,"temp_dirs":0}
print("M4-F DEMO ALL PASSED: jobs=6 residue=0/0/0")
PY

trap - EXIT
exit 0
