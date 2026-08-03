#!/usr/bin/env bash
# Release evidence fail-closed counterexamples.
#
# Drives the SAME release_finish() function that run_all.sh sources and proves,
# with no Docker, that the release gate is fail-closed under three scenarios:
#
#   * positive control   — valid evidence + matching digest + all-green gates
#                          -> release_finish returns 0 and verification.txt
#                          carries the exact "ALL GATES PASSED" record.
#   * case 1 (writer)    — stale green verification cleared at start, then the
#                          writer is forced to crash (M4F_VFY_FORCE_FAIL=1) ->
#                          release_finish returns 2, no verification.txt is
#                          left behind, stderr names the fault, no traceback.
#   * case 2 (mismatch)  — valid all_passed=true evidence whose stored digest
#                          is tampered to a different 64-hex value -> the run
#                          is fail-closed and verification.txt records
#                          "delivery_digest_check: MISMATCH", no traceback.
#
# Uses set -e so any setup/parse/assertion failure aborts the gate; expected
# non-zero commands are captured per-statement with `cmd || rc=$?` (errexit is
# never disabled globally).
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=release_finish.sh
. "$ROOT/tests/m4f1/release_finish.sh"

TMP="$(mktemp -d /tmp/m4f-neg.XXXXXX)"
trap 'rc=$?; rm -rf -- "$TMP"; exit "$rc"' EXIT
EVID="$TMP/agentteams-e2e.json"
VFY="$TMP/verification.txt"
GATES="$TMP/gates.tsv"
SUMMARY="$TMP/agentteams-demo-summary.json"
SCENARIOS_RUN=0
SCENARIOS_PASS=0

# Capture every writer invocation so tracebacks can be asserted-absent.
POS_OUT="$TMP/pos.out"; POS_ERR="$TMP/pos.err"
C1_OUT="$TMP/c1.out"; C1_ERR="$TMP/c1.err"
C2_OUT="$TMP/c2.out"; C2_ERR="$TMP/c2.err"

# The authoritative recomputed digest (independent of any stored value).
DIGEST="$(python3 "$ROOT/tests/m4f1/delivery_digest.py" "$ROOT" | sed -n '1p')"
printf '%s\n' "$DIGEST" | grep -Eq '^[0-9a-f]{64}$' \
  || { echo "setup: recomputed digest is not 64-hex: $DIGEST" >&2; exit 1; }

write_valid_evidence() {
  python3 - "$EVID" "$DIGEST" "$SUMMARY" <<'PY'
import json, pathlib, sys
evid, digest, summary = sys.argv[1], sys.argv[2], sys.argv[3]
obj = {
    "all_passed": True,
    "secret_leaks": 0,
    "residue": {"containers": 0, "networks": 0, "temp_dirs": 0},
    "runner": {"run_rc": 0, "migration_round_1_rc": 0, "migration_round_2_rc": 0},
    "delivery": {"digest": digest, "files": 1, "scope": "test"},
    "fixture": {"external_credentials": False},
    "jobs": [],
}
pathlib.Path(evid).write_text(json.dumps(obj), encoding="utf-8")
pathlib.Path(summary).write_text(
    json.dumps({"demo": {"topology": {"hiclaw_live": False}}}), encoding="utf-8"
)
PY
}

assert_no_traceback() { # label err-file
  if grep -q "Traceback" "$2"; then
    echo "ASSERT-FAIL $1: Python traceback present in $2" >&2
    return 1
  fi
}

# ───────────────────────── scenario: positive control ─────────────────────────
echo "=== scenario: positive control ==="
SCENARIOS_RUN=$((SCENARIOS_RUN + 1))
write_valid_evidence
printf '0\tcounterexample-business-gate\n' > "$GATES"
rm -f "$VFY"
pos_rc=0
release_finish 0 "$GATES" "$EVID" "$VFY" "$ROOT" >"$POS_OUT" 2>"$POS_ERR" || pos_rc=$?
[ "$pos_rc" -eq 0 ] || { echo "ASSERT-FAIL positive: release_finish rc=$pos_rc (want 0)" >&2; exit 1; }
[ -f "$VFY" ] || { echo "ASSERT-FAIL positive: verification.txt missing" >&2; exit 1; }
grep -q "^ALL GATES PASSED$" "$VFY" \
  || { echo "ASSERT-FAIL positive: verification.txt lacks ALL GATES PASSED" >&2; exit 1; }
grep -q "^delivery_digest_check: OK" "$VFY" \
  || { echo "ASSERT-FAIL positive: digest not OK in verification.txt" >&2; exit 1; }
assert_no_traceback "positive stderr" "$POS_ERR" || exit 1
SCENARIOS_PASS=$((SCENARIOS_PASS + 1))
echo "SCENARIO-PASS positive control (rc=0, ALL GATES PASSED, no traceback)"

# ───────────────────────── scenario: case 1 (writer failure) ─────────────────
echo "=== scenario: case 1 writer failure ==="
SCENARIOS_RUN=$((SCENARIOS_RUN + 1))
write_valid_evidence
printf '0\tcounterexample-business-gate\n' > "$GATES"
# pre-seed a stale green record from a previous run, then clear it exactly as
# run_all.sh does at start, so a writer crash cannot leave it behind.
printf 'final_rc: 0\nALL GATES PASSED\n' > "$VFY"
rm -f "$VFY"
c1_rc=0
M4F_VFY_FORCE_FAIL=1 release_finish 0 "$GATES" "$EVID" "$VFY" "$ROOT" >"$C1_OUT" 2>"$C1_ERR" || c1_rc=$?
[ "$c1_rc" -eq 2 ] \
  || { echo "ASSERT-FAIL case1: release_finish rc=$c1_rc (want 2)" >&2; exit 1; }
[ ! -e "$VFY" ] \
  || { echo "ASSERT-FAIL case1: stale verification.txt survived writer failure" >&2; exit 1; }
grep -q "M4F_VFY_FORCE_FAIL" "$C1_ERR" \
  || { echo "ASSERT-FAIL case1: stderr lacks the FORCE_FAIL fault marker" >&2; exit 1; }
assert_no_traceback "case1 stderr" "$C1_ERR" || exit 1
SCENARIOS_PASS=$((SCENARIOS_PASS + 1))
echo "SCENARIO-PASS case1 writer failure (rc=2, no stale, fault named, no traceback)"

# ───────────────────────── scenario: case 2 (digest mismatch) ─────────────────
echo "=== scenario: case 2 digest mismatch ==="
SCENARIOS_RUN=$((SCENARIOS_RUN + 1))
write_valid_evidence
printf '0\tcounterexample-business-gate\n' > "$GATES"
rm -f "$VFY"
# tamper ONLY the stored digest to a clearly different 64-hex value, then
# re-parse and assert the mismatch holds (real mismatch, not missing/invalid).
python3 - "$EVID" "$DIGEST" <<'PY'
import json, pathlib, sys
evid, real = sys.argv[1], sys.argv[2]
p = pathlib.Path(evid)
d = json.loads(p.read_text(encoding="utf-8"))
bad = ("0" * 64) if real != ("0" * 64) else ("1" * 64)
assert bad != real, "tampered digest must differ from recomputed"
d["delivery"]["digest"] = bad
p.write_text(json.dumps(d), encoding="utf-8")
# re-parse and assert the stored value is well-formed JSON and really differs
d2 = json.loads(p.read_text(encoding="utf-8"))
assert d2["delivery"]["digest"] == bad, "tampered digest not persisted"
assert d2["delivery"]["digest"] != real, "tampered digest equals recomputed"
PY
c2_rc=0
release_finish 0 "$GATES" "$EVID" "$VFY" "$ROOT" >"$C2_OUT" 2>"$C2_ERR" || c2_rc=$?
[ "$c2_rc" -ne 0 ] \
  || { echo "ASSERT-FAIL case2: release_finish rc=0 (fail-open on digest mismatch)" >&2; exit 1; }
[ -f "$VFY" ] \
  || { echo "ASSERT-FAIL case2: verification.txt missing" >&2; exit 1; }
grep -q "^delivery_digest_check: MISMATCH$" "$VFY" \
  || { echo "ASSERT-FAIL case2: verification.txt lacks 'delivery_digest_check: MISMATCH'" >&2; exit 1; }
assert_no_traceback "case2 stderr" "$C2_ERR" || exit 1
SCENARIOS_PASS=$((SCENARIOS_PASS + 1))
echo "SCENARIO-PASS case2 digest mismatch (rc=$c2_rc, MISMATCH recorded, no traceback)"

# ───────────────────────── scenarios-run/pass accounting ──────────────────────
echo "=== scenarios run=$SCENARIOS_RUN pass=$SCENARIOS_PASS ==="
if [ "$SCENARIOS_RUN" -ne 3 ] || [ "$SCENARIOS_PASS" -ne 3 ]; then
  echo "RELEASE EVIDENCE NEGATIVES FAILED: run=$SCENARIOS_RUN pass=$SCENARIOS_PASS" >&2
  exit 1
fi
echo "RELEASE EVIDENCE NEGATIVES ALL PASSED"
exit 0
