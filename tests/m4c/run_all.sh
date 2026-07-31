#!/usr/bin/env bash
# run_all.sh -- MergePilot M4-C (sast-scan + test-runner) gate runner.
# Writes evidence/m4/m4c/{test-output-r1,test-output-r2,verification}.txt.
# Exit 0 only if every gate passes. Reuses tests/skills/scan_delivery.py.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
find skills/sast_scan skills/test_runner tests/m4c -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null
find skills/sast_scan skills/test_runner tests/m4c -name '*.pyc' -delete 2>/dev/null

VENV_PY="/d/goai/m4a-venv/Scripts/python.exe"
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$VENV_PY" ]; then PYTHON="$VENV_PY"; else PYTHON="python"; fi
fi
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1

EVID="evidence/m4/m4c"; mkdir -p "$EVID"
R1="$EVID/test-output-r1.txt"; R2="$EVID/test-output-r2.txt"; VERIFY="$EVID/verification.txt"
: > "$VERIFY"
DELIVERY="skills/sast_scan skills/test_runner tests/m4c evidence/m4/m4c"
SCAN_DELIVERY="$DELIVERY"
FAIL=0
note() { printf '%s\n' "$1" | tee -a "$VERIFY"; }
gate() { [ "$2" = "1" ] && note "[PASS] $1" || { note "[FAIL] $1"; FAIL=1; }; }

note "=== MergePilot M4-C verification ==="
note "date: $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
note "python: $($PYTHON -c 'import sys;print(sys.executable)') ($($PYTHON --version 2>&1))"

$PYTHON - <<'PY' >> "$VERIFY" 2>&1
import importlib.metadata as md
print("jsonschema", md.version("jsonschema")); print("pytest", md.version("pytest"))
PY

# py_compile
PYC_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || PYC_OK=0
import glob, os, py_compile, shutil, tempfile
fs = sum((glob.glob(b+'/**/*.py', recursive=True) for b in ("skills/sast_scan","skills/test_runner","tests/m4c")), [])
tmp = tempfile.mkdtemp()
try:
    [py_compile.compile(f, cfile=os.path.join(tmp, os.path.basename(f)+'.c'), doraise=True) for f in fs]
    print("py_compile OK: %d files" % len(fs))
finally: shutil.rmtree(tmp, ignore_errors=True)
PY
gate "py_compile (all M4-C .py)" "$PYC_OK"

# schema meta-validation + ruleset/profiles conformance
SCHEMA_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || SCHEMA_OK=0
import glob, json
from jsonschema import Draft202012Validator
for f in sorted(glob.glob('skills/sast_scan/schema/*.json') + glob.glob('skills/test_runner/schema/*.json')):
    Draft202012Validator.check_schema(json.load(open(f, encoding='utf-8'))); print("meta-valid:", f)
rs = json.load(open('skills/sast_scan/rules/sast-rules.v1.json', encoding='utf-8'))
Draft202012Validator(json.load(open('skills/sast_scan/schema/rules.schema.json', encoding='utf-8'))).validate(rs)
print("sast ruleset valid; rules_version=%s" % rs['rules_version'])
pf = json.load(open('skills/test_runner/config/runner-profiles.v1.json', encoding='utf-8'))
Draft202012Validator(json.load(open('skills/test_runner/schema/runner-profiles.schema.json', encoding='utf-8'))).validate(pf)
print("runner profiles valid; profiles_version=%s profiles=%d" % (pf['profiles_version'], len(pf['profiles'])))
PY
gate "schema meta-validation + ruleset/profiles conformance" "$SCHEMA_OK"

EXPECTED_PASS=$($PYTHON -c 'import re;print(re.search(r"EXPECTED_PASS = ([0-9]+)",open("tests/m4c/conftest.py",encoding="utf-8").read()).group(1))')
note "EXPECTED_PASS (tests/m4c/conftest.py) = $EXPECTED_PASS"

$PYTHON -m pytest tests/m4c -v -p no:cacheprovider > "$R1" 2>&1; RC1=$?
$PYTHON -m pytest tests/m4c -v -p no:cacheprovider > "$R2" 2>&1; RC2=$?
P1=$(grep -oE '[0-9]+ passed' "$R1"|grep -oE '^[0-9]+'||echo 0); F1=$(grep -oE '[0-9]+ failed' "$R1"|grep -oE '^[0-9]+'||echo 0)
P2=$(grep -oE '[0-9]+ passed' "$R2"|grep -oE '^[0-9]+'||echo 0); F2=$(grep -oE '[0-9]+ failed' "$R2"|grep -oE '^[0-9]+'||echo 0)
note "m4c round1: passed=$P1 failed=$F1 rc=$RC1"; note "m4c round2: passed=$P2 failed=$F2 rc=$RC2"
note "m4c r1 tail: $(tail -n1 "$R1")"; note "m4c r2 tail: $(tail -n1 "$R2")"
[ "$P1" = "$EXPECTED_PASS" ]&&[ "$F1" = "0" ]&&[ "$RC1" = "0" ]&&[ "$P2" = "$EXPECTED_PASS" ]&&[ "$F2" = "0" ]&&[ "$RC2" = "0" ] && PT=1 || PT=0
gate "m4c pytest two rounds == EXPECTED_PASS, 0 failed, rc 0" "$PT"

# M4-A + M4-B regression
M4A=$($PYTHON -m pytest tests/skills -p no:cacheprovider -q 2>&1); M4B=$($PYTHON -m pytest tests/m4b -p no:cacheprovider -q 2>&1)
M4AP=$(printf '%s' "$M4A"|grep -oE '[0-9]+ passed'|grep -oE '^[0-9]+'||echo 0); M4BP=$(printf '%s' "$M4B"|grep -oE '[0-9]+ passed'|grep -oE '^[0-9]+'||echo 0)
note "m4a regression: passed=$M4AP (expect 75)"; note "m4b regression: passed=$M4BP (expect 96)"
gate "m4a regression 75/75" "$([ "$M4AP" = "75" ]&&echo 1||echo 0)"
gate "m4b regression 96/96" "$([ "$M4BP" = "96" ]&&echo 1||echo 0)"

# untouched boundaries
M3C=$(git status --short -- tools/ skills/sast-scan skills/gh-mcp config/souls evidence/m4/m4a evidence/m4/m4b 2>/dev/null|wc -l|tr -d ' ')
note "M3/M4-A/M4-B + skills/sast-scan changed entries: $M3C"
gate "M3 + M4-A + M4-B + skills/sast-scan untouched" "$([ "$M3C" = "0" ]&&echo 1||echo 0)"
COMMONC=$(git status --short -- skills/common 2>/dev/null|wc -l|tr -d ' ')
note "skills/common changed entries: $COMMONC"
gate "skills/common untouched" "$([ "$COMMONC" = "0" ]&&echo 1||echo 0)"

git diff --check > /tmp/.m4c_gdc 2>&1; GDC=$?; cat /tmp/.m4c_gdc >> "$VERIFY"; rm -f /tmp/.m4c_gdc
gate "git diff --check (tracked whitespace)" "$([ "$GDC" = "0" ]&&echo 1||echo 0)"
NDC_OK=1
while IFS= read -r f; do case "$f" in *.diff|*test-output-*|*verification.txt|*container-e2e.*) continue;; esac
  out=$(git diff --no-index --check -- /dev/null "$f" 2>&1)
  ws=$(printf '%s' "$out"|grep -E 'trailing whitespace|space before tab|indent' || true)
  [ -n "$ws" ] && { note "ws-error in $f"; NDC_OK=0; }
done < <(git ls-files --others --exclude-standard $DELIVERY)
gate "git diff --no-index --check (new source files; .diff excluded)" "$NDC_OK"
TW=$(grep -rnE ' +$' --include='*.py' --include='*.sh' --include='*.json' --include='*.md' $DELIVERY 2>/dev/null|wc -l|tr -d ' ')
note "trailing_whitespace_hits=$TW (source files)"; gate "no trailing whitespace" "$([ "$TW" = "0" ]&&echo 1||echo 0)"

$PYTHON tests/skills/scan_delivery.py $SCAN_DELIVERY >> "$VERIFY" 2>&1
gate "delivery scan: 0 hits" "$([ "$?" = "0" ]&&echo 1||echo 0)"
DOC_SCAN_OK=1
$PYTHON tests/skills/scan_delivery.py THIRD_PARTY.md \
  "docs/复赛路线图.md" "docs/附录B-Skill清单.md" "docs/项目状态.md" \
  >> "$VERIFY" 2>&1 || DOC_SCAN_OK=0
gate "M4-C docs scan: 0 hits" "$DOC_SCAN_OK"
RES=$(find evidence/m4/m4c -type d \( -name '__pycache__' -o -name '.pytest_cache' \) 2>/dev/null|wc -l|tr -d ' ')
PYC=$(find $DELIVERY -name '*.pyc' 2>/dev/null|wc -l|tr -d ' ')
note "residual_cache=$RES pyc=$PYC"; gate "no cache/pyc residue" "$([ "$RES" = "0" ]&&[ "$PYC" = "0" ]&&echo 1||echo 0)"

note "--- git state ---"; git status --short >> "$VERIFY" 2>&1
note "HEAD=$(git rev-parse HEAD) origin=$(git rev-parse origin/main) ab=$(git rev-list --left-right --count HEAD...origin/main)"
note "tags=$(git tag|wc -l|tr -d ' ') m4b^{}=$(git rev-parse 'refs/tags/m4b-diff-risk-closed^{commit}')"

# container E2E: structured JSON evidence with per-scenario checks.
note "--- container executor E2E ---"
E2E_JSON="$EVID/container-e2e.json"
IMAGE_JSON="$EVID/image-build.json"
if [ ! -f "$E2E_JSON" ] || [ ! -f "$IMAGE_JSON" ]; then
  note "container E2E/build evidence missing / release-blocking"
  gate "container E2E and image build evidence present" "0"
else
  E2E_OK=1
  $PYTHON - "$E2E_JSON" "$IMAGE_JSON" >> "$VERIFY" 2>&1 || E2E_OK=0 <<'PYE2E'
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
b = json.load(open(sys.argv[2], encoding="utf-8"))
assert d.get("all_passed") is True, "all_passed is not True"
scen = {s["scenario"]: s for s in d.get("scenarios", [])}
assert set(scen) == {"pass", "timeout", "error", "tmpfs_quota"}, "scenario set"
assert len(d.get("scenarios", [])) == 4, "scenario count"
expected_side_effects = {"fs_tmp", "process_exec"}
digest_re = re.compile(r"^[0-9a-f]{64}$")

def common(s, name):
    assert s.get("executor") == "container", name + " executor"
    assert s.get("isolation") == "container", name + " isolation"
    assert s.get("network_policy") == "denied", name + " network"
    assert set(s.get("side_effects", [])) == expected_side_effects, name + " side effects"
    assert s.get("residual") == 0, name + " residual"
    assert s.get("artifacts") == [], name + " artifacts"
    assert digest_re.fullmatch(s.get("envelope_sha256", "")), name + " envelope digest"

# PASS scenario
p = scen.get("pass", {})
common(p, "pass")
assert p.get("status") == "OK", "pass status"
assert p.get("error_code") is None, "pass error_code"
assert p.get("verdict") == "PASS", "pass verdict"
assert p.get("exit_code") == 0, "pass exit_code"
assert p.get("rc") == 0, "pass rc"
# TIMEOUT scenario
t = scen.get("timeout", {})
common(t, "timeout")
assert t.get("status") == "ERROR", "timeout status"
assert t.get("error_code") == "TIMEOUT", "timeout error_code"
assert t.get("verdict") == "TIMEOUT", "timeout verdict"
assert t.get("rc") == 3, "timeout rc"
# ERROR scenario
e = scen.get("error", {})
common(e, "error")
assert e.get("status") == "ERROR", "error status"
assert e.get("error_code") == "INTERNAL_ERROR", "error error_code"
assert e.get("verdict") == "ERROR", "error verdict"
assert e.get("rc") == 1, "error rc"
# TMPFS_QUOTA scenario: writing >8 MiB should hit ENOSPC; test catches OSError -> PASS
q = scen.get("tmpfs_quota", {})
common(q, "tmpfs_quota")
assert q.get("verdict") == "PASS", "tmpfs_quota verdict"
assert q.get("status") == "OK", "tmpfs_quota status"
assert q.get("error_code") is None, "tmpfs_quota error_code"
assert q.get("exit_code") == 0, "tmpfs_quota exit_code"
assert q.get("rc") == 0, "tmpfs_quota rc"
assert q.get("quota_errno") == 28, "tmpfs_quota errno"
# image digest: must be repository@sha256:64hex
assert re.fullmatch(r"[A-Za-z0-9._/:\-]+@sha256:[0-9a-f]{64}", d.get("image_digest", "")), "image digest format"
image_ref = b["image"]["repository"] + "@" + b["image"]["registry_digest"]
assert d["image_digest"] == image_ref, "E2E/build image mismatch"
base_ref = b["base_image"]["reference"]
assert re.fullmatch(r"python:3\.9\.25-slim@sha256:[0-9a-f]{64}", base_ref), "base image digest"
dockerfile = open("skills/test_runner/Dockerfile", encoding="utf-8").read()
assert base_ref in dockerfile, "Dockerfile/base evidence mismatch"
expected_freeze = {
    "exceptiongroup==1.3.1", "iniconfig==2.1.0", "packaging==26.2",
    "pluggy==1.6.0", "Pygments==2.20.0", "pytest==8.4.2",
    "tomli==2.4.1", "typing_extensions==4.16.0",
}
assert set(b.get("pip_freeze", [])) == expected_freeze, "container pip freeze"
print("E2E JSON validation: all markers present and correct")
PYE2E
  if [ "$E2E_OK" = "1" ]; then
    note "container E2E = PASSED (4 scenarios via production CLI; ENOSPC=28; tmpfs /artifacts 8MiB; artifacts=[] by contract)"
  else
    note "container E2E = FAILED (JSON validation failed)"
  fi
  gate "container E2E and image build evidence valid" "$E2E_OK"
fi
note "container executor + WSL transport also covered by unit tests (exit classification, argv hardening, path mapping, cleanup, image-digest validation)"

$PYTHON tests/skills/scan_delivery.py "$VERIFY" >> "$VERIFY" 2>&1
gate "verification.txt self-scan: 0 hits" "$([ "$?" = "0" ]&&echo 1||echo 0)"
note ""; note "=== SUMMARY ==="
[ "$FAIL" = "0" ] && note "ALL GATES PASSED" || note "SOME GATES FAILED (see above)"
for f in "$R1" "$R2" "$VERIFY"; do [ -f "$f" ] && sed -i 's/\r$//' "$f"; done
exit $FAIL
