#!/usr/bin/env bash
# Minimal counterexample for the P3-1 GATE_LOG cleanup fix.
#
# Proves release_finish() removes the caller's gate_log temp file on BOTH exit
# paths -- a successful verification (writer rc=0) and a writer failure
# (M4F_VFY_FORCE_FAIL=1 -> write_verification rc=2). The cleanup is
# unconditional and runs before the rc-determining return, so neither path
# leaks an m4f1-gates.* manifest. Sources the SAME release_finish() that
# run_all.sh and run_release_evidence_negatives.sh use (the unified path).
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# clear stale manifests from prior runs so the residue assertion is authoritative
find /tmp -maxdepth 1 -name 'm4f1-gates.*' -delete 2>/dev/null || true
# shellcheck source=release_finish.sh
. "$ROOT/tests/m4f1/release_finish.sh"

TMP="$(mktemp -d /tmp/m4f-glc.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
EVID="$TMP/agentteams-e2e.json"
SUMMARY="$TMP/agentteams-demo-summary.json"

# minimal valid evidence whose delivery digest matches the real surface, so the
# success-path writer returns 0
DIGEST="$(python3 "$ROOT/tests/m4f1/delivery_digest.py" "$ROOT" | sed -n '1p')"
python3 - "$EVID" "$SUMMARY" "$DIGEST" <<'PY'
import json, pathlib, sys
evid, summary, digest = sys.argv[1], sys.argv[2], sys.argv[3]
obj = {
    "all_passed": True, "secret_leaks": 0,
    "residue": {"containers": 0, "networks": 0, "temp_dirs": 0},
    "runner": {"run_rc": 0, "migration_round_1_rc": 0, "migration_round_2_rc": 0},
    "delivery": {"digest": digest, "files": 1, "scope": "test"},
    "fixture": {"external_credentials": False}, "jobs": [],
}
pathlib.Path(evid).write_text(json.dumps(obj), encoding="utf-8")
pathlib.Path(summary).write_text(
    json.dumps({"demo": {"topology": {"hiclaw_live": False},
                         "delivery": {"digest": digest, "files": 1}}}), encoding="utf-8")
PY

# ── success path: business rc=0, writer succeeds -> gate_log removed ─────────
gl="$(mktemp /tmp/m4f1-gates.XXXXXX)"
release_finish 0 "$gl" "$EVID" "$TMP/vfy-ok.txt" "$ROOT" >/dev/null 2>&1 || true
[ ! -e "$gl" ] || { echo "GATE-LOG-FAIL success-path: manifest not removed" >&2; exit 1; }
echo "GATE-LOG success-path: manifest removed (writer rc=0)"

# ── failure path: writer force-fails (rc=2) -> gate_log still removed ───────
gl2="$(mktemp /tmp/m4f1-gates.XXXXXX)"
M4F_VFY_FORCE_FAIL=1 release_finish 0 "$gl2" "$EVID" "$TMP/vfy-fail.txt" "$ROOT" >/dev/null 2>&1 || true
[ ! -e "$gl2" ] || { echo "GATE-LOG-FAIL failure-path: manifest not removed" >&2; exit 1; }
echo "GATE-LOG failure-path: manifest removed (writer rc=2)"

# ── residue assertion: no m4f1-gates.* left by this test ─────────────────────
left="$(find /tmp -maxdepth 1 -name 'm4f1-gates.*' 2>/dev/null | wc -l)"
[ "$left" -eq 0 ] || { echo "GATE-LOG-FAIL residue: $left m4f1-gates.* remain" >&2; exit 1; }

echo "GATE-LOG CLEANUP COUNTEREXAMPLE PASSED (success + failure, residue=0)"
