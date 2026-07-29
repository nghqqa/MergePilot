#!/usr/bin/env bash
# run_all.sh -- MergePilot M4-A unified gate runner.
#
# Runs every M4-A hard gate and writes:
#   evidence/m4/m4a/test-output-r1.txt  (pytest round 1)
#   evidence/m4/m4a/test-output-r2.txt  (pytest round 2 -- two consecutive stable runs)
#   evidence/m4/m4a/verification.txt    (all static/clean/git gates + scans)
#
# Exit 0 only when every gate passes. Credential + identifier scanning is
# delegated to tests/skills/scan_delivery.py, which exits 1 on any hit (so a
# real leak fails the gate rather than merely being printed).
#
# Python snippets use RELATIVE paths (cwd == repo root) so a native Windows
# interpreter resolves them regardless of the shell's path style.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# remove stale build artifacts from any prior manual runs (these are NOT
# delivery files; the residual gate below verifies the final state is clean)
find skills tests -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null
find skills tests -name '*.pyc' -delete 2>/dev/null

PYTHON="${PYTHON:-python}"
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1

EVID="evidence/m4/m4a"
mkdir -p "$EVID"
R1="$EVID/test-output-r1.txt"
R2="$EVID/test-output-r2.txt"
VERIFY="$EVID/verification.txt"
: > "$VERIFY"

FAIL=0
note() { printf '%s\n' "$1" | tee -a "$VERIFY"; }
gate() { # gate NAME OK
  if [ "$2" = "1" ]; then note "[PASS] $1"; else note "[FAIL] $1"; FAIL=1; fi
}

note "=== MergePilot M4-A verification ==="
note "date: $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
note "python: $($PYTHON -c 'import sys;print(sys.executable)') ($($PYTHON -V 2>&1))"
note ""

# --- dependency presence ----------------------------------------------------
DEPS_OK=1
$PYTHON - <<'PY' || DEPS_OK=0
import importlib
for m in ("jsonschema", "pytest"):
    importlib.import_module(m)
PY
gate "deps importable (jsonschema, pytest)" "$DEPS_OK"
$PYTHON - <<'PY' | tee -a "$VERIFY"
import importlib.metadata as md
print("jsonschema", md.version("jsonschema"))
print("pytest", md.version("pytest"))
PY
note "--- pip freeze (isolated venv, full transitive set) ---"
$PYTHON -m pip freeze 2>/dev/null | tee -a "$VERIFY"

# --- py_compile (to a temp dir; leaves no .pyc in repo) ----------------------
PYC_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || PYC_OK=0
import glob, os, py_compile, shutil, tempfile
files = glob.glob('skills/common/**/*.py', recursive=True) + glob.glob('tests/skills/**/*.py', recursive=True)
tmp = tempfile.mkdtemp()
try:
    for f in files:
        py_compile.compile(f, cfile=os.path.join(tmp, os.path.basename(f) + '.pyc'), doraise=True)
    print("py_compile OK: %d files" % len(files))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
gate "py_compile (all .py)" "$PYC_OK"

# --- JSON Schema self-validation (Draft 2020-12 meta) ------------------------
SCHEMA_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || SCHEMA_OK=0
import json, os
from jsonschema import Draft202012Validator
for f in ("request.envelope.schema.json", "response.envelope.schema.json"):
    schema = json.load(open(os.path.join("skills/common/schema", f), encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    print("schema meta-valid:", f)
PY
gate "jsonschema Draft 2020-12 meta-validation" "$SCHEMA_OK"

# --- EXPECTED_PASS (single source: conftest.py) -----------------------------
EXPECTED_PASS=$($PYTHON - <<'PY'
import re
t = open("tests/skills/conftest.py", encoding="utf-8").read()
print(re.search(r"EXPECTED_PASS = ([0-9]+)", t).group(1))
PY
)
note "EXPECTED_PASS (from conftest.py) = $EXPECTED_PASS"

# --- pytest: TWO consecutive rounds (both must pass) ------------------------
PT_OK=1
$PYTHON -m pytest tests/skills/ -v -p no:cacheprovider > "$R1" 2>&1; RC1=$?
$PYTHON -m pytest tests/skills/ -v -p no:cacheprovider > "$R2" 2>&1; RC2=$?
P1=$(grep -oE '[0-9]+ passed' "$R1" | grep -oE '^[0-9]+' || echo 0)
F1=$(grep -oE '[0-9]+ failed' "$R1" | grep -oE '^[0-9]+' || echo 0)
P2=$(grep -oE '[0-9]+ passed' "$R2" | grep -oE '^[0-9]+' || echo 0)
F2=$(grep -oE '[0-9]+ failed' "$R2" | grep -oE '^[0-9]+' || echo 0)
note "round1: passed=$P1 expected=$EXPECTED_PASS failed=$F1 rc=$RC1"
note "round2: passed=$P2 expected=$EXPECTED_PASS failed=$F2 rc=$RC2"
note "round1 tail: $(tail -n 1 "$R1")"
note "round2 tail: $(tail -n 1 "$R2")"
if [ "$P1" = "$EXPECTED_PASS" ] && [ "$F1" = "0" ] && [ "$RC1" = "0" ] \
  && [ "$P2" = "$EXPECTED_PASS" ] && [ "$F2" = "0" ] && [ "$RC2" = "0" ]; then
  gate "pytest two consecutive rounds == EXPECTED_PASS, 0 failed, rc 0" 1
else
  gate "pytest two consecutive rounds == EXPECTED_PASS, 0 failed, rc 0" 0
fi

# --- git diff --check (tracked changes) + --no-index --check (new files) ----
GIT_DC=$(git diff --check > /tmp/.m4a_gdc 2>&1; echo $?)
cat /tmp/.m4a_gdc >> "$VERIFY"; rm -f /tmp/.m4a_gdc
gate "git diff --check (tracked whitespace)" "$([ "$GIT_DC" = "0" ] && echo 1 || echo 0)"

NDC_OK=1
while IFS= read -r f; do
  out=$(git diff --no-index --check -- /dev/null "$f" 2>&1)
  ws=$(printf '%s' "$out" | grep -E 'trailing whitespace|space before tab|indent with spaces' || true)
  if [ -n "$ws" ]; then
    note "whitespace-error in $f:"; printf '%s\n' "$ws" | tee -a "$VERIFY" >/dev/null
    NDC_OK=0
  fi
done < <(git ls-files --others --exclude-standard skills/common tests/skills evidence/m4/m4a)
gate "git diff --no-index --check (each new file)" "$NDC_OK"

# --- trailing-whitespace scan on delivery files -----------------------------
TW_OK=1
TW_HITS=$(grep -rnE ' +$' skills/common tests/skills evidence/m4/m4a THIRD_PARTY.md 2>/dev/null | wc -l | tr -d ' ')
note "trailing_whitespace_hits=$TW_HITS"
[ "$TW_HITS" = "0" ] || TW_OK=0
gate "no trailing whitespace" "$TW_OK"

# --- delivery scan: credentials + AI identifiers (exits 1 on any hit) -------
$PYTHON tests/skills/scan_delivery.py skills/common tests/skills evidence/m4/m4a THIRD_PARTY.md >> "$VERIFY" 2>&1
SCAN_RC=$?
gate "delivery scan (credential + AI, case-insensitive): 0 hits" "$([ "$SCAN_RC" = "0" ] && echo 1 || echo 0)"

# --- residual build artifacts ----------------------------------------------
RES_OK=1
RES_HITS=$(find skills tests evidence/m4/m4a -type d \( -name '__pycache__' -o -name '.pytest_cache' \) 2>/dev/null | wc -l | tr -d ' ')
PYC_HITS=$(find skills tests evidence/m4/m4a -name '*.pyc' 2>/dev/null | wc -l | tr -d ' ')
note "residual_cache_dirs=$RES_HITS residual_pyc=$PYC_HITS"
[ "$RES_HITS" = "0" ] && [ "$PYC_HITS" = "0" ] || RES_OK=0
gate "no __pycache__/.pytest_cache/.pyc residue" "$RES_OK"

# --- git state --------------------------------------------------------------
note "git status --short:"
git status --short >> "$VERIFY" 2>&1
note "HEAD=$(git rev-parse HEAD)"
note "origin/main=$(git rev-parse origin/main 2>&1)"
note "ahead/behind=$(git rev-list --left-right --count HEAD...origin/main 2>&1)"
note "m3c-closed^{}=$(git rev-parse 'refs/tags/m3c-closed^{commit}' 2>&1)"

# --- final self-scan of this report (BEFORE summary; closes the loop) -------
$PYTHON tests/skills/scan_delivery.py "$VERIFY" >> "$VERIFY" 2>&1
SELF_RC=$?
gate "verification.txt self-scan: 0 hits" "$([ "$SELF_RC" = "0" ] && echo 1 || echo 0)"

note ""
note "=== SUMMARY ==="
if [ "$FAIL" = "0" ]; then note "ALL GATES PASSED"; else note "SOME GATES FAILED (see above)"; fi

# --- normalize generated evidence text to LF --------------------------------
# Windows python translates \n -> \r\n on redirected text output; source files
# (.py/.sh/.json/.md) are written LF by the editor and are unaffected.
for f in "$R1" "$R2" "$VERIFY"; do
  [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

exit $FAIL
