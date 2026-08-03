#!/usr/bin/env bash
# Real two-connection producer races: bind and enqueue, same and conflicting payloads.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
UNIQ="$$-$(date +%s)"
DB="m4f1-pc-${UNIQ}"
LABEL="m4f1-pc-${UNIQ}"
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
TMP_ROOT="$(mktemp -d)" || exit 1
rc=1

cleanup() {
  local exit_rc=$? containers=1 networks=1 temp_dirs=1 final_rc
  trap - EXIT
  set +e
  docker rm -f "$DB" >/dev/null 2>&1
  containers="$(docker ps -aq --filter "label=$LABEL" | wc -l)" || containers=1
  networks="$(docker network ls -q --filter "label=$LABEL" | wc -l)" || networks=1
  case "$TMP_ROOT" in /tmp/*) rm -rf -- "$TMP_ROOT" ;; *) echo "unsafe temp path" >&2 ;; esac
  if [ ! -e "$TMP_ROOT" ]; then temp_dirs=0; fi
  final_rc=$exit_rc
  if [ "$final_rc" -eq 0 ] && [ "$rc" -ne 0 ]; then final_rc=$rc; fi
  if [ "$containers" -ne 0 ] || [ "$networks" -ne 0 ] || [ "$temp_dirs" -ne 0 ]; then final_rc=1; fi
  echo "RESIDUE containers=$containers networks=$networks temp_dirs=$temp_dirs"
  exit "$final_rc"
}
trap cleanup EXIT

docker run -d --name "$DB" --label "$LABEL" \
  -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=app "$IMG" >/dev/null
ready=0
for _ in $(seq 1 60); do
  docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT 1" >/dev/null 2>&1 && { ready=1; break; }
  sleep 1
done
[ "$ready" -eq 1 ] || { echo "postgres readiness failed" >&2; exit 1; }

docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 >/dev/null <<'EOSQL'
DO $roles$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF;
END $roles$;
EOSQL
for migration in $BASE; do
  docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
    <"$DBDIR/${migration}.sql" >/dev/null 2>&1
done
for round in 1 2; do
  docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
    <"$DBDIR/m4f1_state.sql" >"$TMP_ROOT/m4f1-r${round}.out" 2>&1
  grep -q "self-check PASS" "$TMP_ROOT/m4f1-r${round}.out"
done

cat >"$TMP_ROOT/setup.sql" <<'EOSQL'
\set ON_ERROR_STOP on
INSERT INTO public.task_runs(run_id) VALUES ('pc_bind_same'),('pc_bind_diff'),('pc_skill') ON CONFLICT DO NOTHING;
INSERT INTO public.run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES
  ('pc_rpb_same','pc_bind_same','o/r',101,'fix/a','main',repeat('a',40)),
  ('pc_rpb_diff','pc_bind_diff','o/r',102,'fix/b','main',repeat('a',40)) ON CONFLICT DO NOTHING;
INSERT INTO public.mcp_calls(request_id,correlation_id,phase,ts,caller_agent,tool,decision,run_id,target_repo,git_sha,result_status) VALUES
  ('pc_mc_same','pc_corr_same','RESULT',now(),'coordinator','github.get_commit','ALLOW','pc_bind_same','o/r',repeat('b',40),'OK'),
  ('pc_mc_d1','pc_corr_d1','RESULT',now(),'coordinator','github.get_commit','ALLOW','pc_bind_diff','o/r',repeat('b',40),'OK'),
  ('pc_mc_d2','pc_corr_d2','RESULT',now(),'coordinator','github.get_commit','ALLOW','pc_bind_diff','o/r',repeat('c',40),'OK')
  ON CONFLICT DO NOTHING;

DO $$
DECLARE manifest_digest text; request_digest text; request_id text; input_digest text;
BEGIN
  manifest_digest:=public.put_envelope(convert_to('{"fixture":"pc"}','UTF8'),
    'application/vnd.mergepilot.snapshot-manifest.v1+json');
  INSERT INTO public.run_snapshots(snapshot_id,run_id,repo,pr_number,base_sha,head_sha,manifest_digest)
    VALUES ('pc_snap','pc_skill','o/r',103,repeat('b',40),repeat('a',40),manifest_digest);

  input_digest:=encode(digest(convert_to(public.canonical_json('{"f":1}'::jsonb),'UTF8'),'sha256'),'hex');
  request_id:='req-'||left(encode(digest(public._canon_str('pc_trace')||public._canon_str('pc_skill')||
    public._canon_str('diff-parse')||public._canon_str('1')||public._canon_str(input_digest),'sha256'),'hex'),24);
  request_digest:=public.put_envelope(convert_to(jsonb_build_object(
    'contract_version','1','request_id',request_id,'trace_id','pc_trace','input',jsonb_build_object('f',1))::text,'UTF8'),
    'application/vnd.mergepilot.skill-request.v1+json');
  INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
    VALUES ('pc_snap',0,'diff-parse','1.0.0',request_digest);
  PERFORM public.enqueue_skill_job('pc_skill','pc_snap','pc_trace','diff-parse','1.0.0',1,request_digest,'{}');

  input_digest:=encode(digest(convert_to(public.canonical_json('{"f":2}'::jsonb),'UTF8'),'sha256'),'hex');
  request_id:='req-'||left(encode(digest(public._canon_str('pc_trace')||public._canon_str('pc_skill')||
    public._canon_str('risk-classify')||public._canon_str('1')||public._canon_str(input_digest),'sha256'),'hex'),24);
  request_digest:=public.put_envelope(convert_to(jsonb_build_object(
    'contract_version','1','request_id',request_id,'trace_id','pc_trace','input',jsonb_build_object('f',2))::text,'UTF8'),
    'application/vnd.mergepilot.skill-request.v1+json');
  INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
    VALUES ('pc_snap',1,'risk-classify','1.0.0',request_digest);
END $$;
EOSQL
docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
  <"$TMP_ROOT/setup.sql" >/dev/null

evidence_expr() {
  local call_id=$1
  printf "(SELECT encode(public.digest(public._canon_str(request_id)||public._canon_str(correlation_id)||public._canon_str(tool)||public._canon_str(target_repo)||public._canon_str(run_id)||public._canon_str(git_sha)||public._canon_str(result_status),'sha256'),'hex') FROM public.mcp_calls WHERE request_id='%s')" "$call_id"
}

run_pair() {
  local prefix=$1 file_a=$2 file_b=$3 pid_a pid_b pair_a pair_b
  set +e
  docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
    <"$file_a" >"$TMP_ROOT/${prefix}-a.out" 2>&1 & pid_a=$!
  docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
    <"$file_b" >"$TMP_ROOT/${prefix}-b.out" 2>&1 & pid_b=$!
  wait "$pid_a"; pair_a=$?
  wait "$pid_b"; pair_b=$?
  set -e
  printf '%s %s\n' "$pair_a" "$pair_b" >"$TMP_ROOT/${prefix}.rc"
}

same_evidence="$(evidence_expr pc_mc_same)"
for side in a b; do
  cat >"$TMP_ROOT/bind-same-${side}.sql" <<EOSQL
\set ON_ERROR_STOP on
SET statement_timeout='10s'; SET lock_timeout='5s';
BEGIN;
SELECT public.bind_revision('pc_bind_same','o/r',101,repeat('a',40),repeat('b',40),'pc_mc_same',$same_evidence);
SELECT pg_sleep(1);
COMMIT;
EOSQL
done
run_pair bind-same "$TMP_ROOT/bind-same-a.sql" "$TMP_ROOT/bind-same-b.sql"
read -r same_a same_b <"$TMP_ROOT/bind-same.rc"
[ "$same_a" -eq 0 ] && [ "$same_b" -eq 0 ]
[ "$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT count(*) FROM public.revision_bindings WHERE run_id='pc_bind_same'")" -eq 1 ]
echo "PC-BIND-SAME PASS rc=0/0 rows=1"

for side in a b; do
  call_id=pc_mc_d1; base_sha="$(printf 'b%.0s' {1..40})"
  [ "$side" = b ] && { call_id=pc_mc_d2; base_sha="$(printf 'c%.0s' {1..40})"; }
  diff_evidence="$(evidence_expr "$call_id")"
  cat >"$TMP_ROOT/bind-diff-${side}.sql" <<EOSQL
\set ON_ERROR_STOP on
\set VERBOSITY verbose
SET statement_timeout='10s'; SET lock_timeout='5s';
BEGIN;
SELECT public.bind_revision('pc_bind_diff','o/r',102,repeat('a',40),'$base_sha','$call_id',$diff_evidence);
SELECT pg_sleep(1);
COMMIT;
EOSQL
done
run_pair bind-diff "$TMP_ROOT/bind-diff-a.sql" "$TMP_ROOT/bind-diff-b.sql"
read -r diff_a diff_b <"$TMP_ROOT/bind-diff.rc"
if [ "$diff_a" -eq 0 ]; then loser="$TMP_ROOT/bind-diff-b.out"; [ "$diff_b" -ne 0 ];
else loser="$TMP_ROOT/bind-diff-a.out"; [ "$diff_b" -eq 0 ]; fi
grep -q 'P0001' "$loser"; grep -q 'revision binding conflict' "$loser"; ! grep -q '23505' "$loser"
[ "$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT count(*) FROM public.revision_bindings WHERE run_id='pc_bind_diff'")" -eq 1 ]
echo "PC-BIND-DIFF PASS rc=$diff_a/$diff_b rows=1 no_23505"

target_digest="$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT request_envelope_ref FROM public.snapshot_manifest_items WHERE snapshot_id='pc_snap' AND skill_name='risk-classify'")"
dep_job="$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT job_id FROM public.skill_job_outbox WHERE run_id='pc_skill' AND skill_name='diff-parse'")"
for side in a b; do
  deps="'{}'::text[]"; [ "$side" = b ] && deps="ARRAY['$dep_job']::text[]"
  cat >"$TMP_ROOT/enqueue-diff-${side}.sql" <<EOSQL
\set ON_ERROR_STOP on
\set VERBOSITY verbose
SET statement_timeout='10s'; SET lock_timeout='5s';
BEGIN;
SELECT public.enqueue_skill_job('pc_skill','pc_snap','pc_trace','risk-classify','1.0.0',1,'$target_digest',$deps);
SELECT pg_sleep(1);
COMMIT;
EOSQL
done
run_pair enqueue-diff "$TMP_ROOT/enqueue-diff-a.sql" "$TMP_ROOT/enqueue-diff-b.sql"
read -r enq_a enq_b <"$TMP_ROOT/enqueue-diff.rc"
if [ "$enq_a" -eq 0 ]; then loser="$TMP_ROOT/enqueue-diff-b.out"; [ "$enq_b" -ne 0 ];
else loser="$TMP_ROOT/enqueue-diff-a.out"; [ "$enq_b" -eq 0 ]; fi
grep -q 'P0001' "$loser"; grep -q 'dependency set conflict' "$loser"; ! grep -q '23505' "$loser"
[ "$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT count(*) FROM public.skill_job_outbox WHERE run_id='pc_skill' AND skill_name='risk-classify'")" -eq 1 ]
echo "PC-ENQUEUE-DIFF PASS rc=$enq_a/$enq_b rows=1 no_23505"

dep_count="$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT count(*) FROM public.skill_job_dependencies d JOIN public.skill_job_outbox j ON j.job_id=d.job_id WHERE j.run_id='pc_skill' AND j.skill_name='risk-classify'")"
same_deps="'{}'::text[]"; [ "$dep_count" -eq 1 ] && same_deps="ARRAY['$dep_job']::text[]"
for side in a b; do
  cat >"$TMP_ROOT/enqueue-same-${side}.sql" <<EOSQL
\set ON_ERROR_STOP on
SET statement_timeout='10s'; SET lock_timeout='5s';
BEGIN;
SELECT public.enqueue_skill_job('pc_skill','pc_snap','pc_trace','risk-classify','1.0.0',1,'$target_digest',$same_deps);
SELECT pg_sleep(1);
COMMIT;
EOSQL
done
run_pair enqueue-same "$TMP_ROOT/enqueue-same-a.sql" "$TMP_ROOT/enqueue-same-b.sql"
read -r enq_same_a enq_same_b <"$TMP_ROOT/enqueue-same.rc"
[ "$enq_same_a" -eq 0 ] && [ "$enq_same_b" -eq 0 ]
[ "$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT count(*) FROM public.skill_job_outbox WHERE run_id='pc_skill' AND skill_name='risk-classify'")" -eq 1 ]
echo "PC-ENQUEUE-SAME PASS rc=0/0 rows=1"

rc=0
echo "PRODUCER CONCURRENCY PASS"
