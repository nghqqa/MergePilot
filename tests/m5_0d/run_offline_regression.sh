#!/usr/bin/env bash
# M5-0D D2B-1 entry — run offline regression (17/17 + 6/6) + capture evidence.
# Runs in MergePilot-Test via wsl_test.sh. No PAT/Matrix/MinIO/OTel needed.
# Ubuntu-22.04 must remain Stopped. Source commit from git HEAD (not caller).
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
TEST_DISTRO="MergePilot-Test"
PROD_DISTRO="Ubuntu-22.04"

wsl_state() {
  wsl.exe -l -v 2>/dev/null | tr -d '\0' |
    awk -v distro="$1" '$0 ~ distro { for (i=1;i<=NF;i++) if ($i ~ /^(Stopped|Running|Starting)$/) {print $i; exit} }'
}

prod_before="$(wsl_state "$PROD_DISTRO")"
if [ "$prod_before" != "Stopped" ]; then
  echo "D2B-1 fail-closed: $PROD_DISTRO must be Stopped (got '${prod_before:-missing}')" >&2
  exit 2
fi
test_before="$(wsl_state "$TEST_DISTRO")"
if [ "$test_before" != "Running" ]; then
  echo "D2B-1 fail-closed: $TEST_DISTRO must already be Running (got '${test_before:-missing}')" >&2
  exit 2
fi

wsl.exe -d "$TEST_DISTRO" -u root -- bash -lc \
  'test "${WSL_DISTRO_NAME:-}" = "MergePilot-Test"' || {
  echo "D2B-1 fail-closed: distro identity mismatch" >&2; exit 2; }

cleanup() {
  capture_rc=$?
  trap - EXIT INT TERM
  wsl.exe --terminate "$TEST_DISTRO" >/dev/null 2>&1
  exit "$capture_rc"
}
trap cleanup EXIT INT TERM

wsl.exe -d "$TEST_DISTRO" -u root -- bash -lc \
  'cd /mnt/d/goai/mergepilot-os && python3 tests/m5_0d/capture_offline_evidence.py'
capture_rc=$?
exit "$capture_rc"
