#!/usr/bin/env bash
# MergePilot JCS Profile v1: fixed-oracle, ingress, and idempotent-migration gate.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"
TESTDIR="$ROOT/tests/m4f1"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
UNIQ="$$-$(date +%s)"
DB="m4f1-cj-${UNIQ}"
LABEL="m4f1-cj-${UNIQ}"
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
TMP_ROOT="$(mktemp -d)" || { echo "mktemp failed" >&2; exit 1; }
EXPECTED_OUT="$TMP_ROOT/expected.txt"
PG_OUT="$TMP_ROOT/pg.txt"
COMPARE_OUT="$TMP_ROOT/compare.txt"
SQL_FILE="$TMP_ROOT/canonical.sql"
rc=1

cleanup() {
  local exit_rc=$?
  local containers=1 networks=1 temp_dirs=1 final_rc
  trap - EXIT
  set +e
  docker rm -f "$DB" >/dev/null 2>&1
  containers="$(docker ps -aq --filter "label=$LABEL" | wc -l)" || containers=1
  networks="$(docker network ls -q --filter "label=$LABEL" | wc -l)" || networks=1
  case "$TMP_ROOT" in
    /tmp/*) rm -rf -- "$TMP_ROOT" ;;
    *) echo "unsafe temp path: $TMP_ROOT" >&2 ;;
  esac
  if [ ! -e "$TMP_ROOT" ]; then temp_dirs=0; fi
  final_rc=$exit_rc
  if [ "$final_rc" -eq 0 ] && [ "$rc" -ne 0 ]; then final_rc=$rc; fi
  if [ "$containers" -ne 0 ] || [ "$networks" -ne 0 ] || [ "$temp_dirs" -ne 0 ]; then
    final_rc=1
  fi
  echo "RESIDUE containers=$containers networks=$networks temp_dirs=$temp_dirs"
  exit "$final_rc"
}
trap cleanup EXIT

PYTHONDONTWRITEBYTECODE=1 python3 -B "$TESTDIR/verify_rfc8785.py" >"$EXPECTED_OUT"
echo "fixed oracle generated"

docker run -d --name "$DB" --label "$LABEL" \
  -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=app \
  "$IMG" >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT 1" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || { echo "postgres readiness failed" >&2; exit 1; }

docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 >/dev/null <<'EOSQL'
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') THEN
    CREATE ROLE policy_gateway_l2 NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') THEN
    CREATE ROLE mergepilot_approver NOLOGIN;
  END IF;
END
$roles$;
EOSQL

for migration in $BASE; do
  docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
    <"$DBDIR/${migration}.sql" >/dev/null 2>&1
done
echo "base chain rc=0"

for round in 1 2; do
  docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 \
    <"$DBDIR/m4f1_state.sql" >"$TMP_ROOT/m4f1-r${round}.out" 2>&1
  grep -q "self-check PASS" "$TMP_ROOT/m4f1-r${round}.out"
  echo "m4f1 round $round rc=0"
done

cat >"$SQL_FILE" <<'EOSQL'
\set ON_ERROR_STOP on
CREATE FUNCTION pg_temp.canon_row(p_id text, p_value jsonb)
RETURNS TABLE(test_id text, canonical_hex text, canonical_sha256 text)
LANGUAGE plpgsql AS $$
DECLARE v_canonical text;
BEGIN
  v_canonical := public.canonical_json(p_value);
  RETURN QUERY SELECT p_id,
    encode(convert_to(v_canonical, 'UTF8'), 'hex'),
    encode(public.digest(convert_to(v_canonical, 'UTF8'), 'sha256'), 'hex');
END $$;

CREATE FUNCTION pg_temp.try_sql(p_body text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v_result text;
BEGIN
  EXECUTE p_body INTO v_result;
  RETURN 'OK:' || v_result;
EXCEPTION WHEN OTHERS THEN
  RETURN 'ERR:' || SQLSTATE || ':' || SQLERRM;
END $$;

SELECT * FROM pg_temp.canon_row('V1', '{"b":1,"a":{"n":null}}'::jsonb);
SELECT * FROM pg_temp.canon_row('V2', '{"z":"é","a":"α"}'::jsonb);
SELECT * FROM pg_temp.canon_row('V3', '{"c":1.0,"b":1.50e2,"a":-0.0}'::jsonb);
SELECT * FROM pg_temp.canon_row('V4', '[1,null,{"b":2,"a":1}]'::jsonb);
SELECT * FROM pg_temp.canon_row('V5', jsonb_build_object(chr(57344),1,chr(120143),2));
SELECT * FROM pg_temp.canon_row('V6', jsonb_build_object('a'||chr(92)||'b','x'||chr(9)||'y'));
SELECT * FROM pg_temp.canon_row('V7', '{"n":1e2}'::jsonb);
SELECT * FROM pg_temp.canon_row('V8', '{"n":-0}'::jsonb);
SELECT * FROM pg_temp.canon_row('N1', '{"n":0.0000001}'::jsonb);
SELECT * FROM pg_temp.canon_row('N2', '{"n":0.000001}'::jsonb);
SELECT * FROM pg_temp.canon_row('N3', '{"n":0.1}'::jsonb);
SELECT * FROM pg_temp.canon_row('N4', '{"n":333333333.33333329}'::jsonb);
SELECT * FROM pg_temp.canon_row('S_BS', jsonb_build_object('k',chr(8)));
SELECT * FROM pg_temp.canon_row('S_TAB', jsonb_build_object('k',chr(9)));
SELECT * FROM pg_temp.canon_row('S_LF', jsonb_build_object('k',chr(10)));
SELECT * FROM pg_temp.canon_row('S_FF', jsonb_build_object('k',chr(12)));
SELECT * FROM pg_temp.canon_row('S_CR', jsonb_build_object('k',chr(13)));
SELECT * FROM pg_temp.canon_row('S_QUOTE', jsonb_build_object('k',chr(34)));
SELECT * FROM pg_temp.canon_row('S_BSLS', jsonb_build_object('k',chr(92)));
SELECT * FROM pg_temp.canon_row('S_CTRL', jsonb_build_object('k',chr(1)));
SELECT * FROM pg_temp.canon_row('LIT_BS_U_CANON', jsonb_build_object('k',chr(92)||'u0000'));
SELECT * FROM pg_temp.canon_row('R1', '{"z":1,"a":2,"m":3}'::jsonb);

SELECT 'V9', replace(pg_temp.try_sql($q$SELECT public.canonical_json('{"n":9007199254740993}'::jsonb)$q$), '|', '/'), '';
SELECT 'DUP_ROOT', pg_temp.try_sql($q$SELECT public.put_envelope(convert_to('{"a":1,"a":2}','UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'DUP_NESTED', pg_temp.try_sql($q$SELECT public.put_envelope(convert_to('{"o":{"a":1,"a":2}}','UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'DUP_ARRAY', pg_temp.try_sql($q$SELECT public.put_envelope(convert_to('[{"a":1,"a":2}]','UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'DUP_ESC_EQ', pg_temp.try_sql($q$SELECT public.put_envelope(convert_to('{"a":1,"\u0061":2}','UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'U0000_ESCAPE', pg_temp.try_sql($q$SELECT public.put_envelope(decode('225c753030303022','hex'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'RAW_NUL', pg_temp.try_sql($q$SELECT public.put_envelope(decode('7b2273223a2200227d','hex'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'SURRO_H', pg_temp.try_sql($q$SELECT public.put_envelope(decode('225c756438303022','hex'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'SURRO_L', pg_temp.try_sql($q$SELECT public.put_envelope(decode('225c756463303022','hex'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'BAD_JSON', pg_temp.try_sql($q$SELECT public.put_envelope(convert_to('{','UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'BAD_UTF8', pg_temp.try_sql($q$SELECT public.put_envelope(decode('ff','hex'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
SELECT 'LIT_BS_U', pg_temp.try_sql($q$SELECT public.put_envelope(decode('7b226b223a225c5c7530303030227d','hex'),'application/vnd.mergepilot.skill-request.v1+json')$q$), '';
EOSQL

docker exec -i "$DB" psql -X -qAt -F '|' -U mergepilot -d app -v ON_ERROR_STOP=1 \
  <"$SQL_FILE" >"$PG_OUT"

if PYTHONDONTWRITEBYTECODE=1 python3 -B "$TESTDIR/compare_jcs_results.py" \
  "$EXPECTED_OUT" "$PG_OUT" >"$COMPARE_OUT"; then
  cat "$COMPARE_OUT"
  rc=0
else
  cat "$COMPARE_OUT"
  echo "--- PostgreSQL rows ---"
  cat "$PG_OUT"
  exit 1
fi
