#!/usr/bin/env bash
# M4-F1 release gate: frozen schema, all SD APIs, concurrency, host runtime,
# revision cut, six-Skill full-chain Demo, AgentTeams protocol E2E, release
# evidence counterexamples, and an auto-generated release verification record.
#
# Each gate's name and rc is recorded; on any failure the remaining gates still
# run so the verification record carries complete failure evidence, and the
# final rc is non-zero. An EXIT trap always writes the verification record and
# is fail-closed: a verification-writer / digest / JSON failure fails the run
# even when every business gate passed. The verification target is cleared at
# start so a writer crash can never leave a stale green record.
set -uo pipefail
# host-Python gates (release evidence negatives, finalize, summarize,
# delivery_digest, write_verification) must not litter __pycache__ on the repo
# between the gates and the hygiene scan.
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/tools/test-env/mp_guard.sh"  # fail-closed: MergePilot-Test daemon only
RUNTIME_IMAGE="mergepilot-m4f-runtime:demo"
EVID="$ROOT/evidence/m4/m4f/agentteams-e2e.json"
VERIFICATION="$ROOT/evidence/m4/m4f/verification.txt"
# Per-run private temp dir: GATE_LOG is allocated inside it so the cleanup trap
# can rm the EXACT dir without globbing /tmp (which would collide with other
# run_all instances or with the gate-log cleanup counterexample sub-test).
RUN_TMP="$(mktemp -d /tmp/m4f1-run-all.XXXXXX)"
GATE_LOG="$RUN_TMP/gates.tsv"
OVERALL_RC=0

# clear any stale verification from a previous run before any gate runs
rm -f "$VERIFICATION"

. "$ROOT/tests/m4f1/release_finish.sh"

run_gate() {
  local name="$1"
  shift
  echo "=== M4-F1 gate: ${name} ==="
  local rc=0
  "$@" || rc=$?
  printf '%s\t%s\n' "$rc" "$name" >> "$GATE_LOG"
  if [ "$rc" -ne 0 ]; then
    OVERALL_RC="$rc"
    echo "=== M4-F1 gate FAIL: ${name} (rc=${rc}) ===" >&2
  else
    echo "=== M4-F1 gate PASS: ${name} ==="
  fi
}

finish() {
  local rc=$?
  set +e
  release_finish "$rc" "$GATE_LOG" "$EVID" "$VERIFICATION" "$ROOT"
  local frc=$?
  rm -rf -- "$RUN_TMP"
  exit "$frc"
}
trap finish EXIT

run_gate "schema foundation and exact ACL" \
  bash "$ROOT/tests/m4f1/run_schema_foundation.sh"
run_gate "MergePilot JCS Profile fixed oracle" \
  bash "$ROOT/tests/m4f1/run_canonical_json.sh"
run_gate "producer SD APIs" \
  bash "$ROOT/tests/m4f1/run_producer_api.sh"
run_gate "producer two-connection concurrency" \
  bash "$ROOT/tests/m4f1/run_producer_concurrency.sh"
run_gate "claim/heartbeat/fail state machines" \
  bash "$ROOT/tests/m4f1/run_worker_state.sh"
run_gate "atomic completion APIs" \
  bash "$ROOT/tests/m4f1/run_complete_api.sh"
run_gate "purge and reference counting" \
  bash "$ROOT/tests/m4f1/run_purge_api.sh"

run_gate "build host runtime fixture" \
  docker build -q -t "$RUNTIME_IMAGE" "$ROOT/tools/m4f-runtime"
run_gate "release evidence negatives (writer fail-closed + stale cleared)" \
  bash "$ROOT/tests/m4f1/run_release_evidence_negatives.sh"
run_gate "release evidence unit tests" \
  docker run --rm -v "$ROOT:/workspace:ro" --entrypoint python \
    "$RUNTIME_IMAGE" -m pytest -q tests/m4f1/test_release_evidence.py
run_gate "gate-log cleanup counterexample (P3-1, success + failure)" \
  bash "$ROOT/tests/m4f1/run_gate_log_cleanup_test.sh"
run_gate "host Skill worker unit tests" \
  docker run --rm -v "$ROOT:/workspace:ro" --entrypoint python \
    "$RUNTIME_IMAGE" -m pytest -q tests/m4f1/test_runtime.py
run_gate "text/cache/credential/attribution hygiene" \
  docker run --rm -v "$ROOT:/workspace:ro" --entrypoint python \
    "$RUNTIME_IMAGE" tests/m4f1/check_hygiene.py
run_gate "M4-F tracked whitespace" git -C "$ROOT" diff --check -- \
  "docs/项目状态.md" "tools/workflow-controller/controller.py"
run_gate "six-Skill full-chain Demo, revision cut, complete/purge race" \
  bash "$ROOT/tests/m4f1/run_demo.sh"
run_gate "AgentTeams protocol E2E (real Gateway + six Skills + PRLifecycle)" \
  bash "$ROOT/tests/m4f1/run_agentteams_gateway_e2e.sh"
run_gate "M4-A~E legacy functional regression (authoritative platforms)" \
  bash "$ROOT/tests/m4f1/run_legacy_functional_regression.sh"

# ── fail-closed GATE_LOG integrity assertion (exactly 17 gates, no dup) ──────
# Prevents evidence corruption (e.g. a sub-gate colliding with this GATE_LOG)
# from silently reducing verification.txt below the real gate count.
python3 - "$GATE_LOG" <<'PYINTEGRITY' || { echo "[m4f1] GATE_LOG integrity FAIL" >&2; OVERALL_RC=1; }
expected = [
    "schema foundation and exact ACL",
    "MergePilot JCS Profile fixed oracle",
    "producer SD APIs",
    "producer two-connection concurrency",
    "claim/heartbeat/fail state machines",
    "atomic completion APIs",
    "purge and reference counting",
    "build host runtime fixture",
    "release evidence negatives (writer fail-closed + stale cleared)",
    "release evidence unit tests",
    "gate-log cleanup counterexample (P3-1, success + failure)",
    "host Skill worker unit tests",
    "text/cache/credential/attribution hygiene",
    "M4-F tracked whitespace",
    "six-Skill full-chain Demo, revision cut, complete/purge race",
    "AgentTeams protocol E2E (real Gateway + six Skills + PRLifecycle)",
    "M4-A~E legacy functional regression (authoritative platforms)",
]
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
names = [ln.split("\t", 1)[1] if "\t" in ln else ln for ln in lines]
if len(lines) != 17:
    print("INTEGRITY-FAIL: %d gate lines (expected 17)" % len(lines), file=sys.stderr)
    sys.exit(1)
if len(set(names)) != 17:
    print("INTEGRITY-FAIL: duplicate gate names in GATE_LOG", file=sys.stderr)
    sys.exit(1)
missing = [e for e in expected if e not in names]
if missing:
    print("INTEGRITY-FAIL: missing gates %s" % missing, file=sys.stderr)
    sys.exit(1)
print("GATE_LOG integrity OK: 17/17 gates recorded, no duplicates")
PYINTEGRITY

if [ "$OVERALL_RC" -ne 0 ]; then
  echo "M4-F1 GATES FAILED rc=$OVERALL_RC" >&2
fi
exit "$OVERALL_RC"
