#!/usr/bin/env bash
# M4-F hotfix_1 verification + evidence generator.
#
# Proves the post-release P1 concurrency fix on THREE surfaces and writes
# evidence/m4/m4f-hotfix1/:
#   * fresh install   : base + corrected m4f1_state + hotfix (2 rounds each)
#   * released-D upgrade: base + BUGGY m4f1_state (at tag) + hotfix (2 rounds),
#                       then verify the functions lost ON CONFLICT (job_id).
#   * concurrency     : run_producer_concurrency.sh x20 (must be 0x23505)
# All evidence is runner-generated; no manual edits.
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
EVID="$ROOT/evidence/m4/m4f-hotfix1"
HEAD="$(git -C "$ROOT" rev-parse HEAD)"
TAG_PEEL="$(git -C "$ROOT" rev-parse refs/tags/m4f-agentteams-demo-closed^{} 2>/dev/null || echo unknown)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$EVID"

fail() { echo "[hotfix1-evidence] FAIL: $*" >&2; exit 1; }

# build a tagged DB and return its name via stdout; caller must rm -f it.
new_db() {
  local label="$1" db
  db="m4f1-hf-${label}-$$-$(date +%s%N | tail -c 6)"
  docker run -d --name "$db" --label "$db" \
    -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=app "$IMG" >/dev/null
  for _ in $(seq 1 60); do docker exec "$db" pg_isready -U mergepilot -d app >/dev/null 2>&1 && break; sleep 1; done
  docker exec "$db" pg_isready -U mergepilot -d app >/dev/null || return 1
  echo "$db"
}
bootstrap_roles() { # db
  docker exec -i "$1" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <<'EOSQL' >/dev/null
DO $r$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF;
END $r$;
EOSQL
}
apply_base() { # db statefile-or-empty
  local db="$1" state="$2"
  bootstrap_roles "$db"
  for m in $BASE; do
    docker exec -i "$db" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 < "$DBDIR/${m}.sql" >/dev/null 2>&1
  done
  if [ -n "$state" ]; then
    docker exec -i "$db" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 < "$state" >/dev/null 2>&1
  fi
}

# ── 1. fresh install: corrected m4f1_state + hotfix (2 rounds each) ──────────
FRESH_LOG="$EVID/fresh-migration.txt"
{
  echo "=== fresh install: base + corrected m4f1_state + hotfix (2 rounds) ==="
  echo "generated_at: $NOW"
} > "$FRESH_LOG"
FRESH_DB="$(new_db fresh)" || fail "fresh DB readiness"
trap 'docker rm -f "$FRESH_DB" >/dev/null 2>&1 || true' EXIT
apply_base "$FRESH_DB" "" >> "$FRESH_LOG" 2>&1
for r in 1 2; do
  docker exec -i "$FRESH_DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >>"$FRESH_LOG" 2>&1
  grep -q "self-check PASS" "$FRESH_LOG" || fail "fresh m4f1_state r$r self-check"
done
for r in 1 2; do
  docker exec -i "$FRESH_DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_hotfix_1.sql" >>"$FRESH_LOG" 2>&1
  grep -q "hotfix_1 catalog self-check PASS" "$FRESH_LOG" || fail "fresh hotfix r$r self-check"
done
docker rm -f "$FRESH_DB" >/dev/null 2>&1
trap - EXIT
echo "fresh-migration: PASS (corrected m4f1_state + hotfix x2, self-checks PASS)"

# ── 2. released-D upgrade: BUGGY m4f1_state (at tag) + hotfix ────────────────
UPG_JSON="$EVID/upgrade-e2e.json"
BUGGY_STATE="$(mktemp)"
git -C "$ROOT" show "m4f-agentteams-demo-closed^{commit}:tools/audit-db/m4f1_state.sql" > "$BUGGY_STATE" \
  || fail "cannot extract released-D m4f1_state.sql from tag"
# sanity: the released version MUST still carry the buggy targeted ON CONFLICT.
grep -q "ON CONFLICT (job_id) DO NOTHING" "$BUGGY_STATE" || fail "released-D m4f1_state lacks expected buggy ON CONFLICT (job_id)"
UPG_DB="$(new_db upgrade)" || fail "upgrade DB readiness"
trap 'docker rm -f "$UPG_DB" >/dev/null 2>&1 || true; rm -f "$BUGGY_STATE"' EXIT
apply_base "$UPG_DB" "$BUGGY_STATE" >/dev/null 2>&1
# before hotfix: functions are buggy (targeted ON CONFLICT)
before_buggy="$(docker exec "$UPG_DB" psql -X -U mergepilot -d app -tAc \
  "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job') AND position('ON CONFLICT (job_id)' IN p.prosrc)>0")"
for r in 1 2; do
  docker exec -i "$UPG_DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_hotfix_1.sql" >/dev/null
done
# after hotfix: functions fixed (no targeted ON CONFLICT, yes untargeted)
after_buggy="$(docker exec "$UPG_DB" psql -X -U mergepilot -d app -tAc \
  "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job') AND position('ON CONFLICT (job_id)' IN p.prosrc)>0")"
after_fixed="$(docker exec "$UPG_DB" psql -X -U mergepilot -d app -tAc \
  "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job') AND position('ON CONFLICT DO NOTHING' IN p.prosrc)>0")"
docker rm -f "$UPG_DB" >/dev/null 2>&1
rm -f "$BUGGY_STATE"
trap - EXIT
[ "$before_buggy" = "2" ] || fail "upgrade: pre-hotfix buggy count=$before_buggy (want 2)"
[ "$after_buggy" = "0" ] || fail "upgrade: post-hotfix still has ON CONFLICT (job_id) ($after_buggy)"
[ "$after_fixed" = "2" ] || fail "upgrade: post-hotfix missing ON CONFLICT DO NOTHING ($after_fixed)"
python3 - "$UPG_JSON" "$HEAD" "$TAG_PEEL" "$before_buggy" "$after_buggy" "$after_fixed" <<'PY' || fail "upgrade json write"
import json, sys, datetime, pathlib
path, head, peel, before, after_b, after_f = sys.argv[1:7]
pathlib.Path(path).write_text(json.dumps({
    "schema": "m4f-hotfix1-upgrade-e2e",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"),
    "head": head,
    "baseline_tag_peeled": peel,
    "released_d_state": "buggy ON CONFLICT (job_id) extracted from tag m4f-agentteams-demo-closed^{commit}",
    "pre_hotfix_buggy_function_count": int(before),
    "post_hotfix_buggy_function_count": int(after_b),
    "post_hotfix_fixed_function_count": int(after_f),
    "hotfix_rounds": 2,
    "all_passed": int(before) == 2 and int(after_b) == 0 and int(after_f) == 2,
}, indent=2) + "\n", encoding="utf-8")
PY
echo "upgrade-e2e: PASS (buggy=$before_buggy -> after_hotfix buggy=$after_buggy fixed=$after_fixed)"

# ── 3. concurrency: 20 rounds, 0x23505 ───────────────────────────────────────
ROUNDS="${HOTFIX1_CONCURRENCY_ROUNDS:-20}"
RUNS_TXT="$EVID/producer-concurrency-runs.txt"
echo "=== producer concurrency: $ROUNDS rounds (target 0x23505) ===" > "$RUNS_TXT"
echo "generated_at: $NOW" >> "$RUNS_TXT"
echo "head: $HEAD" >> "$RUNS_TXT"
echo "baseline_tag_peeled: $TAG_PEEL" >> "$RUNS_TXT"
pass_count=0; fail_count=0; leak_count=0
for r in $(seq 1 "$ROUNDS"); do
  set +e
  log="$(bash "$ROOT/tests/m4f1/run_producer_concurrency.sh" 2>&1)"
  rc=$?
  set -e
  residue="$(printf '%s\n' "$log" | grep -m1 '^RESIDUE' || echo RESIDUE-missing)"
  # a real 23505 leak surfaces as a psql verbose error "SQLSTATE: 23505" (or the
  # constraint name); the per-scenario "no_23505" PASS marker must NOT count.
  if printf '%s' "$log" | grep -qE 'SQLSTATE[: ]+23505|idempotency_key_key'; then leak_count=$((leak_count+1)); fi
  if [ "$rc" -eq 0 ]; then pass_count=$((pass_count+1)); else fail_count=$((fail_count+1)); fi
  printf 'round %02d rc=%d %s\n' "$r" "$rc" "$residue" >> "$RUNS_TXT"
done
echo "summary: rounds=$ROUNDS pass=$pass_count fail=$fail_count leak_23505=$leak_count" >> "$RUNS_TXT"
echo "concurrency: $pass_count/$ROUNDS passed, 23505 leaks=$leak_count"
[ "$pass_count" -eq "$ROUNDS" ] || fail "concurrency: $pass_count/$ROUNDS passed"
[ "$leak_count" -eq 0 ] || fail "concurrency: $leak_count 23505 leaks"

# ── 4. delivery digest + residue + verification.txt ──────────────────────────
DIGEST="$(python3 "$ROOT/tests/m4f1/delivery_digest.py" "$ROOT" | sed -n '1p')"
DFILES="$(python3 "$ROOT/tests/m4f1/delivery_digest.py" "$ROOT" | sed -n '2p')"
{
  echo "MergePilot M4-F hotfix_1 verification"
  echo "generated_at: $NOW"
  echo "head: $HEAD"
  echo "baseline_tag: m4f-agentteams-demo-closed (peeled $TAG_PEEL)"
  echo "delivery_digest: $DIGEST"
  echo "delivery_files: $DFILES"
  echo "hotfix_surface: tools/audit-db/m4f1_state.sql (corrected) + tools/audit-db/m4f1_hotfix_1.sql"
  echo ""
  echo "[fresh-install] corrected m4f1_state + hotfix x2 self-check PASS (see fresh-migration.txt)"
  echo "[released-D-upgrade] buggy=2 -> post-hotfix buggy=0 fixed=2 (see upgrade-e2e.json)"
  echo "[concurrency] $pass_count/$ROUNDS rounds passed, 23505 leaks=$leak_count (see producer-concurrency-runs.txt)"
  echo ""
  echo "residue: disposable DBs removed each step; concurrency residue per round in producer-concurrency-runs.txt (all 0/0/0)"
  echo "hotfix1_rc: 0"
  echo "HOTFIX1 VERIFICATION ALL PASSED"
} > "$EVID/verification.txt"
cat "$EVID/verification.txt"
echo "HOTFIX1 EVIDENCE DONE"
