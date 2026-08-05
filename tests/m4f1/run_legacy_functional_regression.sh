#!/usr/bin/env bash
# M4-A~E legacy functional regression on authoritative platforms.
#
# Runs the PURE functional pytest suites (never the release-scope gates that
# would rewrite the released M4-A~E evidence) and asserts the exact
# passed/skipped counts per platform:
#
#   M4-A      tests/skills   75 passed                       (win32, m4a-venv)
#   M4-B      tests/m4b      96 passed                       (win32, m4a-venv)
#   M4-C      tests/m4c      87 passed                       (win32, m4a-venv)
#   M4-D      tests/m4d      54 passed                       (win32, m4a-venv)
#   M4-E win  tests/m4e     166 passed / 3 skipped           (win32, m4a-venv)
#   M4-E posix tests/m4e   158 passed / 11 skipped           (Linux container --init)
#
# Windows-only suites use the same m4a-venv (jsonschema 4.25.1, pytest 8.4.2)
# that validated the M4-A~E releases. The POSIX suite runs in the project
# runtime image with `--init` (tini) so POSIX real process-group / process-tree
# reaping behaves correctly -- a bare slim container is NOT an authoritative
# environment for those tests.
#
# Writes evidence/m4/m4f/legacy-functional-regression.txt and exits non-zero if
# any suite's counts mismatch or any test fails.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/tools/test-env/mp_guard.sh"  # fail-closed: MergePilot-Test daemon only
VENV_PY="${M4A_VENV_PY:-/mnt/d/goai/m4a-venv/Scripts/python.exe}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-mergepilot-m4f-runtime:demo}"
OUT="$ROOT/evidence/m4/m4f/legacy-functional-regression.txt"
TMP="$(mktemp -d /tmp/m4f-leg.XXXXXX)"
trap 'rc=$?; rm -rf -- "$TMP"; exit "$rc"' EXIT

[ -x "$VENV_PY" ] || { echo "legacy: m4a-venv python not executable: $VENV_PY" >&2; exit 1; }

HEAD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
# Windows python emits CRLF; strip CR so the report stays LF-only for hygiene.
VENV_VER="$("$VENV_PY" --version 2>&1 | tr -d '\r')"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

parse_counts() { # log path -> "passed\tskipped\tfailed\terrors"
  python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
def grab(word):
    m = re.search(r"(\d+)\s+" + word, text)
    return int(m.group(1)) if m else 0
print("%d\t%d\t%d\t%d" % (grab("passed"), grab("skipped"), grab("failed"), grab("error")))
PY
}

declare -a ROWS=()
MISMATCH=0

record() { # label dir platform cmd exp_pass exp_skip log rc
  local label="$1" dir="$2" platform="$3" cmd="$4" exp_p="$5" exp_s="$6" log="$7" rc="$8"
  local p s f e
  IFS=$'\t' read -r p s f e <<<"$(parse_counts "$log")"
  local status="MATCH"
  if [ "$rc" -ne 0 ] || [ "$f" -ne 0 ] || [ "$e" -ne 0 ] \
     || [ "$p" != "$exp_p" ] || [ "$s" != "$exp_s" ]; then
    status="MISMATCH"
    MISMATCH=1
  fi
  ROWS+=("$label|$dir|$platform|$cmd|$p|$s|$f|$e|$rc|$exp_p|$exp_s|$status")
}

# ── Windows host suites via m4a-venv (WSL interop runs it as win32 python) ──
for spec in "M4-A|skills|75|0" "M4-B|m4b|96|0" "M4-C|m4c|87|0" "M4-D|m4d|54|0" "M4-E-win|m4e|166|3"; do
  IFS='|' read -r label dir exp_p exp_s <<<"$spec"
  log="$TMP/${label}.log"
  rc=0
  "$VENV_PY" -m pytest "tests/$dir" -p no:cacheprovider -q >"$log" 2>&1 || rc=$?
  record "$label" "tests/$dir" "win32 (m4a-venv)" \
    "$VENV_PY -m pytest tests/$dir" "$exp_p" "$exp_s" "$log" "$rc"
done

# ── POSIX suite: project runtime image with --init (real process-tree reaping) ──
log="$TMP/M4-E-posix.log"
rc=0
docker run --rm --init -v "$ROOT:/workspace:ro" -w /workspace --entrypoint python \
  "$RUNTIME_IMAGE" -m pytest tests/m4e -p no:cacheprovider -q >"$log" 2>&1 || rc=$?
record "M4-E-posix" "tests/m4e" "posix (container --init)" \
  "docker run --init $RUNTIME_IMAGE -m pytest tests/m4e" 158 11 "$log" "$rc"

# ── write the report ──
mkdir -p "$(dirname "$OUT")"
matched=0
for row in "${ROWS[@]}"; do
  case "$row" in *\|MATCH) matched=$((matched + 1)) ;; esac
done
{
  echo "MergePilot M4-A~E legacy functional regression (authoritative platforms)"
  echo "generated_at: $NOW"
  echo "head: $HEAD"
  echo "m4a_venv_python: $VENV_PY ($VENV_VER)"
  echo "runtime_image: $RUNTIME_IMAGE"
  echo "note: Windows suites via m4a-venv (jsonschema 4.25.1, pytest 8.4.2); POSIX via container --init (tini process-tree reaping). Pure functional pytest only; release-scope gates are NOT run."
  echo ""
  echo "[suites]"
  printf 'label\tdir\tplatform\tpassed\tskipped\tfailed\terrors\trc\texpected_pass\texpected_skip\tstatus\n'
  for row in "${ROWS[@]}"; do
    echo "$row" | tr '|' '\t'
  done
  echo ""
  echo "suites_total: ${#ROWS[@]}"
  echo "suites_matched: $matched"
  echo "suites_mismatched: $(( ${#ROWS[@]} - matched ))"
  echo ""
  if [ "$MISMATCH" -eq 0 ]; then
    echo "legacy_regression_rc: 0"
    echo "LEGACY FUNCTIONAL REGRESSION ALL MATCHED"
  else
    echo "legacy_regression_rc: 1"
    echo "LEGACY FUNCTIONAL REGRESSION MISMATCH (see suites above)"
  fi
} > "$OUT"

cat "$OUT"
[ "$MISMATCH" -eq 0 ] || exit 1
exit 0
