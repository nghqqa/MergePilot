#!/usr/bin/env bash
# run_all.sh -- MergePilot M4-D PRLifecycle release gate.
# Writes evidence/m4/m4d/{test-output-r1,test-output-r2,verification}.txt.
# Exit 0 only when unit, regression, static, boundary, and real fixture E2E
# evidence gates all pass.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

find skills/pr_lifecycle tests/m4d -type d \
  \( -name '__pycache__' -o -name '.pytest_cache' \) \
  -prune -exec rm -rf {} + 2>/dev/null
find skills/pr_lifecycle tests/m4d -name '*.pyc' -delete 2>/dev/null

VENV_PY="/d/goai/m4a-venv/Scripts/python.exe"
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$VENV_PY" ]; then PYTHON="$VENV_PY"; else PYTHON="python"; fi
fi
PROD_VENV_PY="/d/goai/m4d-venv/Scripts/python.exe"
if [ -z "${PROD_PYTHON:-}" ]; then
  if [ -x "$PROD_VENV_PY" ]; then
    PROD_PYTHON="$PROD_VENV_PY"
  else
    PROD_PYTHON=""
  fi
fi
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1

EVID="evidence/m4/m4d"
mkdir -p "$EVID"
R1="$EVID/test-output-r1.txt"
R2="$EVID/test-output-r2.txt"
VERIFY="$EVID/verification.txt"
: > "$VERIFY"
FAIL=0

DELIVERY="skills/pr_lifecycle tests/m4d evidence/m4/m4d"
SCAN_DELIVERY="$DELIVERY docs/M4-D-PRLifecycle设计冻结.md"

note() { printf '%s\n' "$1" | tee -a "$VERIFY"; }
gate() {
  if [ "$2" = "1" ]; then
    note "[PASS] $1"
  else
    note "[FAIL] $1"
    FAIL=1
  fi
}

note "=== MergePilot M4-D verification ==="
note "date: $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
note "python: $($PYTHON -c 'import sys;print(sys.executable)') ($($PYTHON --version 2>&1))"
if [ -n "$PROD_PYTHON" ]; then
  note "production adapter python: $($PROD_PYTHON -c 'import sys;print(sys.executable)') ($($PROD_PYTHON --version 2>&1))"
else
  note "production adapter python: MISSING"
fi

# Compile to an external temp directory so the delivery remains cache-free.
PYC_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || PYC_OK=0
import glob
import os
import py_compile
import shutil
import tempfile

files = (
    glob.glob("skills/pr_lifecycle/**/*.py", recursive=True)
    + glob.glob("tests/m4d/**/*.py", recursive=True)
)
tmp = tempfile.mkdtemp(prefix="m4d-pyc-")
try:
    for index, path in enumerate(files):
        py_compile.compile(
            path,
            cfile=os.path.join(tmp, "%04d.pyc" % index),
            doraise=True,
        )
    print("py_compile OK: %d files" % len(files))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
gate "py_compile (all M4-D .py)" "$PYC_OK"

SCHEMA_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || SCHEMA_OK=0
import glob
import json
from jsonschema import Draft202012Validator

for path in sorted(glob.glob("skills/pr_lifecycle/schema/*.json")):
    with open(path, encoding="utf-8") as fh:
        Draft202012Validator.check_schema(json.load(fh))
    print("meta-valid:", path)
PY
gate "Draft 2020-12 schema meta-validation" "$SCHEMA_OK"

REQ_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || REQ_OK=0
from pathlib import Path

lines = {
    line.strip()
    for line in Path("skills/pr_lifecycle/requirements.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
expected = {"mcp==1.28.1", "httpx==0.28.1", "anyio==4.14.2"}
assert lines == expected, (lines, expected)
print("exact dependency pins:", ", ".join(sorted(lines)))
PY
gate "production adapter dependencies exactly pinned" "$REQ_OK"

PROD_OK=1
if [ -z "$PROD_PYTHON" ]; then
  PROD_OK=0
else
  $PROD_PYTHON - <<'PY' >> "$VERIFY" 2>&1 || PROD_OK=0
import importlib.metadata as metadata
import sys

assert sys.version_info >= (3, 10), sys.version
expected = {"mcp": "1.28.1", "httpx": "0.28.1", "anyio": "4.14.2"}
actual = {name: metadata.version(name) for name in expected}
assert actual == expected, (actual, expected)
print("production adapter runtime:", sys.version)
print("production dependency versions:", actual)
PY
fi
gate "production adapter Python >=3.10 and exact dependencies installed" "$PROD_OK"

EXPECTED_PASS=$($PYTHON -c 'import re;print(re.search(r"EXPECTED_PASS = ([0-9]+)",open("tests/m4d/conftest.py",encoding="utf-8").read()).group(1))')
note "EXPECTED_PASS (tests/m4d/conftest.py) = $EXPECTED_PASS"

$PYTHON -m pytest tests/m4d -v -p no:cacheprovider > "$R1" 2>&1
RC1=$?
$PYTHON -m pytest tests/m4d -v -p no:cacheprovider > "$R2" 2>&1
RC2=$?
P1=$(grep -oE '[0-9]+ passed' "$R1" | grep -oE '^[0-9]+' || echo 0)
F1=$(grep -oE '[0-9]+ failed' "$R1" | grep -oE '^[0-9]+' || echo 0)
P2=$(grep -oE '[0-9]+ passed' "$R2" | grep -oE '^[0-9]+' || echo 0)
F2=$(grep -oE '[0-9]+ failed' "$R2" | grep -oE '^[0-9]+' || echo 0)
note "m4d round1: passed=$P1 failed=$F1 rc=$RC1"
note "m4d round2: passed=$P2 failed=$F2 rc=$RC2"
note "m4d r1 tail: $(tail -n1 "$R1")"
note "m4d r2 tail: $(tail -n1 "$R2")"
if [ "$P1" = "$EXPECTED_PASS" ] && [ "$F1" = "0" ] && [ "$RC1" = "0" ] \
  && [ "$P2" = "$EXPECTED_PASS" ] && [ "$F2" = "0" ] && [ "$RC2" = "0" ]; then
  PT=1
else
  PT=0
fi
gate "m4d pytest two rounds == EXPECTED_PASS, 0 failed, rc 0" "$PT"

# Released milestone regression, two rounds each.
run_regression() {
  local label="$1" path="$2" expected="$3" round output passed rc ok=1
  for round in 1 2; do
    output=$($PYTHON -m pytest "$path" -q -p no:cacheprovider 2>&1)
    rc=$?
    passed=$(printf '%s' "$output" | grep -oE '[0-9]+ passed' | grep -oE '^[0-9]+' || echo 0)
    note "$label round$round: passed=$passed expected=$expected rc=$rc"
    [ "$rc" = "0" ] && [ "$passed" = "$expected" ] || ok=0
  done
  gate "$label regression $expected/$expected x2" "$ok"
}
run_regression "m4a" "tests/skills" "75"
run_regression "m4b" "tests/m4b" "96"
run_regression "m4c" "tests/m4c" "87"

# Existing release boundaries must remain untouched.
BOUNDARY_PATHS=(
  skills/common
  skills/diff_parse
  skills/risk_classify
  skills/sast_scan
  skills/test_runner
  skills/sast-scan
  skills/gh-mcp
  tests/skills
  tests/m4b
  tests/m4c
  evidence/m4/m4a
  evidence/m4/m4b
  evidence/m4/m4c
  tools
  config/souls
)
BOUNDARY_COUNT=$(git status --short -- "${BOUNDARY_PATHS[@]}" 2>/dev/null | wc -l | tr -d ' ')
note "protected boundary changed entries: $BOUNDARY_COUNT"
gate "M3 + M4-A/B/C + common protected boundaries untouched" \
  "$([ "$BOUNDARY_COUNT" = "0" ] && echo 1 || echo 0)"

git diff --check > /tmp/.m4d_gdc 2>&1
GDC=$?
cat /tmp/.m4d_gdc >> "$VERIFY"
rm -f /tmp/.m4d_gdc
gate "git diff --check (tracked whitespace)" "$([ "$GDC" = "0" ] && echo 1 || echo 0)"

NDC_OK=1
while IFS= read -r path; do
  case "$path" in
    *test-output-*|*verification.txt|*gateway-e2e.json) continue ;;
  esac
  output=$(git diff --no-index --check -- /dev/null "$path" 2>&1)
  ws=$(printf '%s' "$output" | grep -E 'trailing whitespace|space before tab|indent' || true)
  if [ -n "$ws" ]; then
    note "ws-error in $path"
    NDC_OK=0
  fi
done < <(git ls-files --others --exclude-standard $DELIVERY docs/M4-D-PRLifecycle设计冻结.md)
gate "git diff --no-index --check (new source files)" "$NDC_OK"

TW=$(grep -rnE ' +$' --include='*.py' --include='*.sh' --include='*.json' \
  --include='*.md' $DELIVERY docs/M4-D-PRLifecycle设计冻结.md 2>/dev/null \
  | wc -l | tr -d ' ')
note "trailing_whitespace_hits=$TW"
gate "no trailing whitespace" "$([ "$TW" = "0" ] && echo 1 || echo 0)"

$PYTHON tests/skills/scan_delivery.py $SCAN_DELIVERY >> "$VERIFY" 2>&1
gate "delivery/design scan: 0 hits" "$([ "$?" = "0" ] && echo 1 || echo 0)"

RES=$(find skills/pr_lifecycle tests/m4d evidence/m4/m4d -type d \
  \( -name '__pycache__' -o -name '.pytest_cache' \) 2>/dev/null \
  | wc -l | tr -d ' ')
PYC=$(find skills/pr_lifecycle tests/m4d evidence/m4/m4d -name '*.pyc' \
  2>/dev/null | wc -l | tr -d ' ')
note "residual_cache=$RES pyc=$PYC"
gate "no cache/pyc residue" "$([ "$RES" = "0" ] && [ "$PYC" = "0" ] && echo 1 || echo 0)"

# Real fixture E2E is release-blocking. The JSON is generated only by the
# production CLI chain; this gate validates structure and key safety outcomes.
E2E_JSON="$EVID/gateway-e2e.json"
if [ ! -f "$E2E_JSON" ]; then
  note "fixture GitHub E2E = NOT RUN / release-blocking"
  gate "fixture GitHub E2E structured evidence valid" "0"
else
  E2E_OK=1
  if ! $PYTHON - "$E2E_JSON" >> "$VERIFY" 2>&1 <<'PYE2E'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)

assert data.get("schema_version") == "1"
assert data.get("target_repository") == "nghqqa/MergePilot-e2e-fixture"
assert data.get("production_chain") == [
    "python -m skills.pr_lifecycle.run",
    "core.run",
    "PolicyGatewayAdapter",
    "Policy Gateway",
    "github-mcp",
    "GitHub fixture",
]
assert data.get("all_passed") is True
assert data.get("residue", {}).get("open_prs") == 0
assert data.get("residue", {}).get("fix_branches") == 0
assert data.get("residue", {}).get("db_rows") == 0

scenarios = {item["scenario"]: item for item in data.get("scenarios", [])}
required = {
    "fix_create",
    "fix_replay",
    "idempotency_conflict",
    "forbidden_path",
    "role_denial",
    "repo_denial",
    "ticket_denial",
    "close_once",
    "merge_once",
    "revert_modified",
    "revert_added_rejected",
}
assert set(scenarios) == required
for name, item in scenarios.items():
    assert item.get("passed") is True, name
    assert re.fullmatch(r"[0-9a-f]{64}", item.get("envelope_sha256", "")), name
    assert item.get("credential_hits") == 0, name

assert scenarios["fix_create"]["outcome"] == "CREATED"
assert scenarios["fix_replay"]["outcome"] == "EXISTING"
assert scenarios["idempotency_conflict"]["error_code"] == "DENIED"
assert scenarios["forbidden_path"]["error_code"] == "INVALID_INPUT"
assert scenarios["role_denial"]["error_code"] == "DENIED"
assert scenarios["repo_denial"]["error_code"] == "DENIED"
assert scenarios["ticket_denial"]["error_code"] == "DENIED"
assert scenarios["close_once"]["outcome"] == "CLOSED"
assert scenarios["merge_once"]["outcome"] in {"MERGED", "ALREADY_MERGED"}
assert scenarios["revert_modified"]["outcome"] == "CREATED"
assert scenarios["revert_added_rejected"]["error_code"] == "DENIED"
print("fixture E2E JSON validation: all required scenarios and residue gates passed")
PYE2E
  then
    E2E_OK=0
  fi
  gate "fixture GitHub E2E structured evidence valid" "$E2E_OK"
fi

note "--- git state ---"
git status --short >> "$VERIFY" 2>&1
note "HEAD=$(git rev-parse HEAD) origin=$(git rev-parse origin/main) ab=$(git rev-list --left-right --count HEAD...origin/main)"
note "tags=$(git tag | wc -l | tr -d ' ') m4c^{}=$(git rev-parse 'refs/tags/m4c-sast-test-closed^{commit}')"

$PYTHON tests/skills/scan_delivery.py "$VERIFY" >> "$VERIFY" 2>&1
gate "verification.txt self-scan: 0 hits" "$([ "$?" = "0" ] && echo 1 || echo 0)"

note ""
note "=== SUMMARY ==="
if [ "$FAIL" = "0" ]; then
  note "ALL GATES PASSED"
else
  note "SOME GATES FAILED (see above)"
fi

for path in "$R1" "$R2" "$VERIFY"; do
  [ -f "$path" ] && sed -i 's/\r$//' "$path"
done
exit "$FAIL"
