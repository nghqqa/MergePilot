#!/usr/bin/env bash
# run_all.sh -- MergePilot M4-B (diff-parse + risk-classify) gate runner.
#
# Runs every M4-B hard gate and writes:
#   evidence/m4/m4b/test-output-r1.txt  (pytest round 1, tests/m4b)
#   evidence/m4/m4b/test-output-r2.txt  (pytest round 2 -- two consecutive stable runs)
#   evidence/m4/m4b/verification.txt    (static/clean/git gates + scans + M4-A regression)
#
# Exit 0 only if every gate passes. Credential + identifier scanning is delegated
# to tests/skills/scan_delivery.py (reused from M4-A, single source of patterns),
# which exits 1 on any hit, so a real leak fails the gate.
#
# M4-A regression: runs the existing tests/skills suite two rounds IN ISOLATION
# (venv + PYTHONDONTWRITEBYTECODE=1 + -p no:cacheprovider) and asserts 75/75 each
# round. It does NOT run tests/skills/run_all.sh, so evidence/m4/m4a is never
# touched or overwritten.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# remove stale build artifacts from any manual runs (NOT delivery files; a later
# gate verifies the final state is clean)
find skills/diff_parse skills/risk_classify tests/m4b -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null
find skills/diff_parse skills/risk_classify tests/m4b -name '*.pyc' -delete 2>/dev/null

# Prefer the isolated out-of-repo venv with the exact pinned versions; fall back
# to PATH python if it is absent.
VENV_PY="/d/goai/m4a-venv/Scripts/python.exe"
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$VENV_PY" ]; then PYTHON="$VENV_PY"; else PYTHON="python"; fi
fi
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1

EVID="evidence/m4/m4b"
mkdir -p "$EVID"
R1="$EVID/test-output-r1.txt"
R2="$EVID/test-output-r2.txt"
VERIFY="$EVID/verification.txt"
: > "$VERIFY"

DELIVERY="skills/diff_parse skills/risk_classify tests/m4b evidence/m4/m4b"

FAIL=0
note() { printf '%s\n' "$1" | tee -a "$VERIFY"; }
gate() { [ "$2" = "1" ] && note "[PASS] $1" || { note "[FAIL] $1"; FAIL=1; }; }

note "=== MergePilot M4-B verification ==="
note "date: $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
note "python: $($PYTHON -c 'import sys;print(sys.executable)') ($($PYTHON --version 2>&1))"
note "PYTHON=$PYTHON"

# --- dependency import check -------------------------------------------------
DEPS_OK=1
$PYTHON <<'PY' >> "$VERIFY" 2>&1 || DEPS_OK=0
import importlib, sys
for m in ("jsonschema", "pytest"):
    try:
        importlib.import_module(m)
        print("importable:", m)
    except Exception as exc:  # noqa: BLE001
        print("NOT importable:", m, exc); sys.exit(1)
PY
gate "deps importable (jsonschema, pytest)" "$DEPS_OK"
$PYTHON - <<'PY' >> "$VERIFY" 2>&1
import importlib.metadata as md
print("jsonschema", md.version("jsonschema"))
print("pytest", md.version("pytest"))
PY

# --- py_compile (to temp dir; leaves no .pyc in repo) ------------------------
PYC_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || PYC_OK=0
import glob, os, py_compile, shutil, tempfile
files = sum((glob.glob(base + '/**/*.py', recursive=True) for base in
            ("skills/diff_parse", "skills/risk_classify", "tests/m4b")), [])
tmp = tempfile.mkdtemp()
try:
    for f in files:
        py_compile.compile(f, cfile=os.path.join(tmp, os.path.basename(f) + '.pyc'), doraise=True)
    print("py_compile OK: %d files" % len(files))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
gate "py_compile (all M4-B .py)" "$PYC_OK"

# --- JSON Schema Draft 2020-12 meta-validation ------------------------------
SCHEMA_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || SCHEMA_OK=0
import glob, json
from jsonschema import Draft202012Validator
for f in sorted(glob.glob('skills/diff_parse/schema/*.json') +
                glob.glob('skills/risk_classify/schema/*.json')):
    schema = json.load(open(f, encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    print("schema meta-valid:", f)
PY
gate "jsonschema Draft 2020-12 meta-validation" "$SCHEMA_OK"

# --- bundled ruleset validates against rules.schema.json ---------------------
RULES_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || RULES_OK=0
import json
from jsonschema import Draft202012Validator
schema = json.load(open('skills/risk_classify/schema/rules.schema.json', encoding='utf-8'))
ruleset = json.load(open('skills/risk_classify/rules/risk-rules.v1.json', encoding='utf-8'))
Draft202012Validator(schema).validate(ruleset)
print("ruleset valid; rules_version=%s rule_count=%d" %
      (ruleset['rules_version'], len(ruleset['rules'])))
PY
gate "bundled ruleset conforms to rules.schema.json" "$RULES_OK"

# --- EXPECTED_PASS (single source: conftest.py) ------------------------------
EXPECTED_PASS=$($PYTHON - <<'PY'
import re
t = open("tests/m4b/conftest.py", encoding="utf-8").read()
print(re.search(r"EXPECTED_PASS = ([0-9]+)", t).group(1))
PY
)
note "EXPECTED_PASS (tests/m4b/conftest.py) = $EXPECTED_PASS"

# --- two consecutive stable pytest rounds (tests/m4b) ------------------------
PT_OK=1
$PYTHON -m pytest tests/m4b -v -p no:cacheprovider > "$R1" 2>&1; RC1=$?
$PYTHON -m pytest tests/m4b -v -p no:cacheprovider > "$R2" 2>&1; RC2=$?
P1=$(grep -oE '[0-9]+ passed' "$R1" | grep -oE '^[0-9]+' || echo 0)
F1=$(grep -oE '[0-9]+ failed' "$R1" | grep -oE '^[0-9]+' || echo 0)
P2=$(grep -oE '[0-9]+ passed' "$R2" | grep -oE '^[0-9]+' || echo 0)
F2=$(grep -oE '[0-9]+ failed' "$R2" | grep -oE '^[0-9]+' || echo 0)
note "m4b round1: passed=$P1 failed=$F1 rc=$RC1"
note "m4b round2: passed=$P2 failed=$F2 rc=$RC2"
note "m4b round1 tail: $(tail -n 1 "$R1")"
note "m4b round2 tail: $(tail -n 1 "$R2")"
[ "$P1" = "$EXPECTED_PASS" ] && [ "$F1" = "0" ] && [ "$RC1" = "0" ] && \
[ "$P2" = "$EXPECTED_PASS" ] && [ "$F2" = "0" ] && [ "$RC2" = "0" ] && PT_OK=1 || PT_OK=0
gate "m4b pytest two consecutive rounds == EXPECTED_PASS, 0 failed, rc 0" "$PT_OK"

# --- M4-A regression (isolated; never touches evidence/m4/m4a) ---------------
M4A_EXPECTED=$($PYTHON - <<'PY'
import re
t = open("tests/skills/conftest.py", encoding="utf-8").read()
print(re.search(r"EXPECTED_PASS = ([0-9]+)", t).group(1))
PY
)
note "M4-A EXPECTED_PASS (tests/skills/conftest.py) = $M4A_EXPECTED"
M4A_OK=1
MA1=$($PYTHON -m pytest tests/skills -p no:cacheprovider -q 2>&1)
MA2=$($PYTHON -m pytest tests/skills -p no:cacheprovider -q 2>&1)
MA1P=$(printf '%s' "$MA1" | grep -oE '[0-9]+ passed' | grep -oE '^[0-9]+' || echo 0)
MA1F=$(printf '%s' "$MA1" | grep -oE '[0-9]+ failed' | grep -oE '^[0-9]+' || echo 0)
MA2P=$(printf '%s' "$MA2" | grep -oE '[0-9]+ passed' | grep -oE '^[0-9]+' || echo 0)
MA2F=$(printf '%s' "$MA2" | grep -oE '[0-9]+ failed' | grep -oE '^[0-9]+' || echo 0)
note "m4a regression round1: passed=$MA1P failed=$MA1F"
note "m4a regression round2: passed=$MA2P failed=$MA2F"
note "m4a regression round1 tail: $(printf '%s' "$MA1" | tail -n 1)"
note "m4a regression round2 tail: $(printf '%s' "$MA2" | tail -n 1)"
[ "$MA1P" = "$M4A_EXPECTED" ] && [ "$MA1F" = "0" ] && \
[ "$MA2P" = "$M4A_EXPECTED" ] && [ "$MA2F" = "0" ] && M4A_OK=1 || M4A_OK=0
gate "m4a regression 75/75 x2, 0 failed (evidence/m4/m4a untouched)" "$M4A_OK"

# --- M3 untouched (expected zero diff on M3 files this round) ----------------
M3_CHANGED=$(git status --short -- tools/policy-gateway tools/workflow-controller tools/audit-db tools/approve.sh skills/sast-scan skills/gh-mcp tools/rag config/souls 2>/dev/null | wc -l | tr -d ' ')
note "M3-area changed entries: $M3_CHANGED"
gate "M3 files unchanged (zero diff)" "$([ "$M3_CHANGED" = "0" ] && echo 1 || echo 0)"

# --- narrow-scope authorization: only skills/common/runtime/cli.py changed ---
# Round-2 audit authorized a single fix in the M4-A common runtime (route every
# error emission through _finalize). Verify the blast radius is exactly that one
# file (no other skills/common, tests/skills or evidence/m4/m4a touched).
COMMON_CHANGED=$(git diff --name-only -- skills/common/ 2>/dev/null | tr -d '\r' | sort)
note "skills/common changed files:"; printf '%s\n' "$COMMON_CHANGED" >> "$VERIFY"
COMMON_OK=$(printf '%s' "$COMMON_CHANGED" | grep -vx 'skills/common/runtime/cli.py' | wc -l | tr -d ' ')
gate "skills/common change is cli.py-only (narrow authorization)" "$([ "$COMMON_OK" = "0" ] && [ -n "$COMMON_CHANGED" ] && echo 1 || echo 0)"
SKILLS_TESTS_CHANGED=$(git status --short -- tests/skills evidence/m4/m4a 2>/dev/null | wc -l | tr -d ' ')
gate "tests/skills + evidence/m4/m4a untouched" "$([ "$SKILLS_TESTS_CHANGED" = "0" ] && echo 1 || echo 0)"
# scan the modified common runtime file for credential/AI markers too
$PYTHON tests/skills/scan_delivery.py skills/common/runtime/cli.py >> "$VERIFY" 2>&1
COMMON_SCAN_RC=$?
gate "cli.py delivery scan: 0 credential/AI hits" "$([ "$COMMON_SCAN_RC" = "0" ] && echo 1 || echo 0)"

# --- git diff --check (tracked) + --no-index --check (new files) -------------
GIT_DC=$(git diff --check > /tmp/.m4b_gdc 2>&1; echo $?)
cat /tmp/.m4b_gdc >> "$VERIFY"; rm -f /tmp/.m4b_gdc
gate "git diff --check (tracked whitespace)" "$([ "$GIT_DC" = "0" ] && echo 1 || echo 0)"

NDC_OK=1
while IFS= read -r f; do
    # .diff fixtures are DATA (unified-diff markup): blank context lines are a
    # single space and Makefile diffs legitimately contain tabs, so they are
    # excluded from the code-style whitespace check. Only source files are checked.
    case "$f" in *.diff) continue;; esac
    out=$(git diff --no-index --check -- /dev/null "$f" 2>&1)
    ws=$(printf '%s' "$out" | grep -E 'trailing whitespace|space before tab|indent spaces' || true)
    if [ -n "$ws" ]; then
        note "whitespace-error in $f:"; printf '%s\n' "$ws" | tee -a "$VERIFY" >/dev/null
        NDC_OK=0
    fi
done < <(git ls-files --others --exclude-standard $DELIVERY)
gate "git diff --no-index --check (each new M4-B source file; .diff data excluded)" "$NDC_OK"

# --- trailing-whitespace scan (source files only; .diff data excluded) -------
TW_OK=1
TW_HITS=$(grep -rnE ' +$' --include='*.py' --include='*.sh' --include='*.json' --include='*.md' $DELIVERY 2>/dev/null | wc -l | tr -d ' ')
note "trailing_whitespace_hits=$TW_HITS (source files only)"
[ "$TW_HITS" = "0" ] || TW_OK=0
gate "no trailing whitespace (source files)" "$TW_OK"

# --- delivery scan: credentials + AI identifiers (exits 1 on any hit) -------
$PYTHON tests/skills/scan_delivery.py $DELIVERY "$VERIFY" 2>&1 | tee -a "$VERIFY"
SCAN_RC=${PIPESTATUS[0]}
gate "delivery scan: 0 credential/AI hits" "$([ "$SCAN_RC" = "0" ] && echo 1 || echo 0)"

# --- residual build artifacts ------------------------------------------------
RES_OK=1
RES_HITS=$(find evidence/m4/m4b -type d \( -name '__pycache__' -o -name '.pytest_cache' \) 2>/dev/null | wc -l | tr -d ' ')
PYC_HITS=$(find $DELIVERY -name '*.pyc' 2>/dev/null | wc -l | tr -d ' ')
note "residual_cache_dirs=$RES_HITS residual_pyc=$PYC_HITS"
[ "$RES_HITS" = "0" ] && [ "$PYC_HITS" = "0" ] || RES_OK=0
gate "no __pycache__/.pytest_cache/.pyc residual" "$RES_OK"

# --- git baseline state ------------------------------------------------------
note "--- git state ---"
note "git status --short:"; git status --short >> "$VERIFY" 2>&1
note "HEAD=$(git rev-parse HEAD)"
note "origin/main=$(git rev-parse origin/main 2>&1)"
note "ahead/behind=$(git rev-list --left-right --count HEAD...origin/main 2>&1)"
note "m4a-runtime-closed^{}=$(git rev-parse 'refs/tags/m4a-runtime-closed^{commit}' 2>&1)"
note "m3c-closed^{}=$(git rev-parse 'refs/tags/m3c-closed^{commit}' 2>&1)"
note "tag_count=$(git tag | wc -l | tr -d ' ')"

# --- self-scan of verification.txt (before summary) --------------------------
$PYTHON tests/skills/scan_delivery.py "$VERIFY" >> "$VERIFY" 2>&1
SELF_RC=$?
gate "verification.txt self-scan: 0 hits" "$([ "$SELF_RC" = "0" ] && echo 1 || echo 0)"

note ""
note "=== SUMMARY ==="
if [ "$FAIL" = "0" ]; then note "ALL GATES PASSED"; else note "SOME GATES FAILED (see above)"; fi

# --- normalize generated evidence text to LF --------------------------------
for f in "$R1" "$R2" "$VERIFY"; do
    [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

exit $FAIL
