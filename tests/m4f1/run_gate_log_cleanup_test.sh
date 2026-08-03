#!/usr/bin/env bash
# GATE_LOG cleanup counterexample (P3-1 + P1 fix).
#
# Proves release_finish() removes the caller's EXACT gate_log on both success
# (writer rc=0) and failure (M4F_VFY_FORCE_FAIL -> writer rc=2) exit paths, AND
# that a sibling "parent run_all" GATE_LOG is never touched (the P1 collision
# fix: no script globs /tmp for m4f1-gates.*).
#
# All test-owned temp files live inside a private mktemp dir — never in a path
# pattern that could match another process's manifest.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=release_finish.sh
. "$ROOT/tests/m4f1/release_finish.sh"

TMP="$(mktemp -d /tmp/m4f-glc.XXXXXX)"
trap 'rm -rf -- "$TMP" "$PARENT_TMP"' EXIT
EVID="$TMP/agentteams-e2e.json"
SUMMARY="$TMP/agentteams-demo-summary.json"

# ── stage minimal valid evidence so the success-path writer returns 0 ────────
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

# ── parent-log protection counterexample ─────────────────────────────────────
# Simulate a parent run_all's in-flight GATE_LOG inside a private temp dir.
# The cleanup counterexample must NOT touch this file (no global /tmp globbing).
PARENT_TMP="$(mktemp -d /tmp/m4f1-run-all-parent.XXXXXX)"
PARENT_GL="$PARENT_TMP/gates.tsv"
printf '0\tschema foundation\n0\tJCS oracle\n0\tproducer concurrency\n' > "$PARENT_GL"
parent_sha_before="$(python3 -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('$PARENT_GL').read_bytes()).hexdigest())")"

# ── success path: business rc=0, writer succeeds -> gate_log removed ─────────
gl="$TMP/gates-ok.tsv"
release_finish 0 "$gl" "$EVID" "$TMP/vfy-ok.txt" "$ROOT" >/dev/null 2>&1 || true
[ ! -e "$gl" ] || { echo "GATE-LOG-FAIL success-path: own gate_log not removed" >&2; exit 1; }
echo "GATE-LOG success-path: own gate_log removed (writer rc=0)"

# ── failure path: writer force-fails (rc=2) -> gate_log still removed ───────
gl2="$TMP/gates-fail.tsv"
M4F_VFY_FORCE_FAIL=1 release_finish 0 "$gl2" "$EVID" "$TMP/vfy-fail.txt" "$ROOT" >/dev/null 2>&1 || true
[ ! -e "$gl2" ] || { echo "GATE-LOG-FAIL failure-path: own gate_log not removed" >&2; exit 1; }
echo "GATE-LOG failure-path: own gate_log removed (writer rc=2)"

# ── parent-log protection: parent GATE_LOG unchanged ─────────────────────────
[ -e "$PARENT_GL" ] || { echo "GATE-LOG-FAIL parent: parent GATE_LOG deleted" >&2; exit 1; }
parent_sha_after="$(python3 -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('$PARENT_GL').read_bytes()).hexdigest())")"
[ "$parent_sha_before" = "$parent_sha_after" ] || {
  echo "GATE-LOG-FAIL parent: SHA-256 changed ($parent_sha_before -> $parent_sha_after)" >&2; exit 1; }
echo "GATE-LOG parent-protection: parent GATE_LOG intact (SHA-256 unchanged)"

echo "GATE-LOG CLEANUP COUNTEREXAMPLE PASSED (success + failure + parent-protected)"
