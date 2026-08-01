#!/usr/bin/env bash
# MergePilot M4-E CaseRetrieval verification gate.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

find skills/case_retrieval tests/m4e -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null || true
find skills/case_retrieval tests/m4e -name '*.pyc' -delete 2>/dev/null || true

VENV_PY="/d/goai/m4a-venv/Scripts/python.exe"
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$VENV_PY" ]; then PYTHON="$VENV_PY"; else PYTHON="python"; fi
fi
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1

EVID="evidence/m4/m4e"
mkdir -p "$EVID"
R1="$EVID/test-output-r1.txt"
R2="$EVID/test-output-r2.txt"
VERIFY="$EVID/verification.txt"
: > "$VERIFY"

FAIL=0
note() { printf '%s\n' "$1" | tee -a "$VERIFY"; }
gate() {
  if [ "$2" = "1" ]; then note "[PASS] $1"; else note "[FAIL] $1"; FAIL=1; fi
}

note "=== MergePilot M4-E verification ==="
note "python: $($PYTHON --version 2>&1)"

PYC_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || PYC_OK=0
import glob, os, py_compile, shutil, tempfile
files = []
for base in ("skills/case_retrieval", "tests/m4e"):
    files.extend(glob.glob(base + "/**/*.py", recursive=True))
tmp = tempfile.mkdtemp()
try:
    for index, path in enumerate(files):
        py_compile.compile(path, cfile=os.path.join(tmp, "%d.pyc" % index), doraise=True)
    print("py_compile OK: %d files" % len(files))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
gate "py_compile" "$PYC_OK"

SCHEMA_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || SCHEMA_OK=0
import glob, json
from jsonschema import Draft202012Validator
for path in sorted(glob.glob("skills/case_retrieval/schema/*.json")):
    schema = json.load(open(path, encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    print("meta-valid:", path)
PY
gate "schema meta-validation" "$SCHEMA_OK"

E2E_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || E2E_OK=0
import hashlib, json, re
from pathlib import Path
d = json.load(open("evidence/m4/m4e/pgvector-e2e.json", encoding="utf-8"))
assert d["all_passed"] is True
assert d["production_chain"]["entry"] == "skills.case_retrieval.run.handle"
assert d["production_chain"]["status"] == "OK"
assert d["production_chain"]["repo_scope"] == "repo-alpha"
assert d["scopes"]["repo-alpha"]["knowledge_base_size"] == 3
assert d["scopes"]["repo-beta"]["knowledge_base_size"] == 3
for key in ("null_scope_excluded", "deterministic", "no_match", "stale_observed", "untrusted_observed"):
    assert d[key] is True
assert d["missing_scope_subcode"] == "CASE_RETR_SCOPE_MISSING"
assert d["schema_unsupported_subcode"] == "CASE_RETR_SCHEMA_UNSUPPORTED"
assert d["reader_role"] == {
    "superuser": False,
    "createrole": False,
    "createdb": False,
    "replication": False,
    "bypassrls": False,
    "default_readonly": "on",
}
assert all(d["write_denials"].values())
assert d["statement_timeout"]["pgcode"] == "57014"
assert d["statement_timeout"]["elapsed_ms"] < 1000
assert d["public_legacy"] == {"rows": 5, "null_scope": 5}
assert d["side_effects"] == [] and d["credential_hits"] == 0
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", d["versions"]["pgvector"])
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", d["versions"]["fastembed"])
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", d["versions"]["psycopg2"])
assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T.*Z", d["generated_at"])
digest = hashlib.sha256()
paths = []
for base in (Path("skills/case_retrieval"), Path("tests/m4e")):
    for path in base.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            paths.append(path)
for path in sorted(paths, key=lambda item: item.as_posix()):
    digest.update(path.as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
assert d["delivery_digest"] == digest.hexdigest()
print("structured pgvector E2E evidence valid")
PY
gate "structured pgvector E2E evidence" "$E2E_OK"

# Structured Docker fixture lifecycle evidence (guard / cleanup / residual /
# digest binding). Recomputes the delivery_digest and the pgvector-e2e.json
# SHA-256 and verifies they match what the orchestrator recorded.
DOCKER_FIX_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || DOCKER_FIX_OK=0
import hashlib, json, re
from datetime import datetime
from pathlib import Path
d = json.load(open("evidence/m4/m4e/docker-fixture-e2e.json", encoding="utf-8"))
assert d["all_passed"] is True
assert d["schema_version"] == "2"
assert d["status"] == "complete"
assert d["fixture_guard_passed"] is True
assert d["cleanup_ok"] is True
assert d["env_files_cleaned"] is True
assert d["container_residual"] == 0
assert d["network_residual"] == 0
assert d["core_e2e_all_passed"] is True
assert d["credential_hits"] == 0
assert re.match(r"^sha256:[0-9a-f]{64}$", d["image_id"] or "")
assert re.match(r"^[^@]+@sha256:[0-9a-f]{64}$", d["repo_digest"] or "")
assert d["image_reference"]
assert d["seeder_schema_version"] == "1"
assert d["database_name"] == "mergepilot_m4e_fixture"
datetime.fromisoformat((d["generated_at"] or "").replace("Z", "+00:00"))
# binding: recompute delivery_digest over skills/case_retrieval + tests/m4e
digest = hashlib.sha256()
for base in (Path("skills/case_retrieval"), Path("tests/m4e")):
    for p in sorted(base.rglob("*"), key=lambda v: v.relative_to(".").as_posix()):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            digest.update(p.relative_to(".").as_posix().encode()); digest.update(b"\0")
            digest.update(p.read_bytes()); digest.update(b"\0")
assert d["delivery_digest"] == digest.hexdigest(), "delivery_digest mismatch"
# binding: recompute pgvector-e2e.json SHA-256
pg_sha = hashlib.sha256(Path("evidence/m4/m4e/pgvector-e2e.json").read_bytes()).hexdigest()
assert d["pgvector_e2e_sha256"] == pg_sha, "pgvector_e2e_sha256 mismatch"
print("docker fixture lifecycle evidence valid (digest + sha256 bound)")
PY
gate "docker fixture lifecycle evidence" "$DOCKER_FIX_OK"

# Platform-aware expected counts loaded from conftest.py via runpy (NOT a
# regex on a hard-coded integer). conftest selects (EXPECTED_PASS, EXPECTED_SKIP)
# per os.name so Windows and POSIX each verify their own exact numbers.
read EXPECTED_PASS EXPECTED_SKIP < <($PYTHON -c "import runpy; m=runpy.run_path('tests/m4e/conftest.py'); print(m['EXPECTED_PASS'], m['EXPECTED_SKIP'])")
EXPECTED_PASS="${EXPECTED_PASS%$'\r'}"
EXPECTED_SKIP="${EXPECTED_SKIP%$'\r'}"
PLATFORM=$($PYTHON -c 'import os; print(os.name)')
PLATFORM="${PLATFORM%$'\r'}"
note "platform=$PLATFORM EXPECTED_PASS=$EXPECTED_PASS EXPECTED_SKIP=$EXPECTED_SKIP"

$PYTHON -m pytest tests/m4e -v -p no:cacheprovider > "$R1" 2>&1; RC1=$?
$PYTHON -m pytest tests/m4e -v -p no:cacheprovider > "$R2" 2>&1; RC2=$?
P1=$(grep -oE '[0-9]+ passed' "$R1" | grep -oE '^[0-9]+' || echo 0)
P2=$(grep -oE '[0-9]+ passed' "$R2" | grep -oE '^[0-9]+' || echo 0)
S1=$(grep -oE '[0-9]+ skipped' "$R1" | grep -oE '^[0-9]+' || echo 0)
S2=$(grep -oE '[0-9]+ skipped' "$R2" | grep -oE '^[0-9]+' || echo 0)
F1=$(grep -oE '[0-9]+ failed' "$R1" | grep -oE '^[0-9]+' || echo 0)
F2=$(grep -oE '[0-9]+ failed' "$R2" | grep -oE '^[0-9]+' || echo 0)
note "m4e round1: passed=$P1 skipped=$S1 failed=$F1 rc=$RC1"
note "m4e round2: passed=$P2 skipped=$S2 failed=$F2 rc=$RC2"
if [ "$P1" = "$EXPECTED_PASS" ] && [ "$P2" = "$EXPECTED_PASS" ] && [ "$S1" = "$EXPECTED_SKIP" ] && [ "$S2" = "$EXPECTED_SKIP" ] && [ "$F1" = 0 ] && [ "$F2" = 0 ] && [ "$RC1" = 0 ] && [ "$RC2" = 0 ]; then
  gate "M4-E two rounds" 1
else
  gate "M4-E two rounds" 0
fi

run_regression() {
  local path="$1" expected="$2" label="$3" round output passed rc
  for round in 1 2; do
    output=$($PYTHON -m pytest "$path" -q -p no:cacheprovider 2>&1); rc=$?
    passed=$(printf '%s' "$output" | grep -oE '[0-9]+ passed' | grep -oE '^[0-9]+' || echo 0)
    note "$label round$round: passed=$passed rc=$rc"
    if [ "$passed" != "$expected" ] || [ "$rc" != 0 ]; then FAIL=1; fi
  done
}
run_regression tests/skills 75 M4-A
run_regression tests/m4b 96 M4-B
run_regression tests/m4c 87 M4-C
run_regression tests/m4d 54 M4-D

PROTECTED=$(git status --short -- skills/common skills/diff_parse skills/risk_classify skills/sast_scan skills/test_runner skills/pr_lifecycle tests/skills tests/m4b tests/m4c tests/m4d evidence/m4/m4a evidence/m4/m4b evidence/m4/m4c evidence/m4/m4d tools config 2>/dev/null | wc -l | tr -d ' ')
if [ "$PROTECTED" = 0 ]; then gate "protected boundaries" 1; else gate "protected boundaries" 0; fi

git diff --check >> "$VERIFY" 2>&1; GDC=$?
if [ "$GDC" = 0 ]; then gate "tracked diff check" 1; else gate "tracked diff check" 0; fi

NDC_OK=1
while IFS= read -r path; do
  case "$path" in *test-output-*|*verification.txt) continue;; esac
  git diff --no-index --check -- /dev/null "$path" >/tmp/.m4e-check 2>&1 || true
  if grep -Eq 'trailing whitespace|space before tab|indent with spaces' /tmp/.m4e-check; then NDC_OK=0; fi
done < <(git ls-files --others --exclude-standard skills/case_retrieval tests/m4e evidence/m4/m4e)
rm -f /tmp/.m4e-check
gate "untracked diff check" "$NDC_OK"

for path in "$R1" "$R2" "$VERIFY"; do
  [ -f "$path" ] && sed -i 's/\r//g' "$path"
done
LF_OK=1
$PYTHON - <<'PY' >> "$VERIFY" 2>&1 || LF_OK=0
from pathlib import Path
bad = []
for root in ("skills/case_retrieval", "tests/m4e", "evidence/m4/m4e"):
    for path in Path(root).rglob("*"):
        if path.is_file() and b"\r" in path.read_bytes():
            bad.append(str(path))
if bad:
    raise SystemExit("CR bytes: " + ", ".join(bad))
print("LF-only files verified")
PY
gate "LF only" "$LF_OK"

$PYTHON tests/skills/scan_delivery.py skills/case_retrieval tests/m4e evidence/m4/m4e/README.md evidence/m4/m4e/pgvector-e2e.json >> "$VERIFY" 2>&1
gate "delivery credential/identifier scan" "$([ "$?" = 0 ] && echo 1 || echo 0)"

find skills/case_retrieval tests/m4e -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null || true
find skills/case_retrieval tests/m4e -name '*.pyc' -delete 2>/dev/null || true
RESIDUE=$(find skills/case_retrieval tests/m4e evidence/m4/m4e \( -name '__pycache__' -o -name '.pytest_cache' -o -name '*.pyc' \) 2>/dev/null | wc -l | tr -d ' ')
if [ "$RESIDUE" = 0 ]; then gate "no cache residue" 1; else gate "no cache residue" 0; fi

note "HEAD=$(git rev-parse HEAD)"
note "origin=$(git rev-parse origin/main)"
note "ab=$(git rev-list --left-right --count origin/main...HEAD)"
note "tags=$(git tag | wc -l | tr -d ' ')"

$PYTHON tests/skills/scan_delivery.py "$VERIFY" >/dev/null 2>&1
gate "verification self-scan" "$([ "$?" = 0 ] && echo 1 || echo 0)"

if [ "$FAIL" = 0 ]; then note "ALL GATES PASSED"; else note "SOME GATES FAILED"; fi
for path in "$R1" "$R2" "$VERIFY"; do
  [ -f "$path" ] && sed -i 's/\r//g' "$path"
done
exit "$FAIL"
