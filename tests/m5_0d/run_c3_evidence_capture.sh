#!/usr/bin/env bash
# M5-0D D1 entry — run capture_c3_evidence.py in MergePilot-Test (root) with the
# operator-injected C3 authorization. Operator must have placed the fixture PAT
# at /dev/shm/m5c-c3/fixture-pat (mode 600) in a RUNNING MergePilot-Test before
# invoking this. M5C_C3_ALLOW_GITHUB_WRITES=1 is the write gate (not a secret).
# Ubuntu-22.04 must remain Stopped. The capture runs committed c3_runner once,
# validates strictly, and atomically publishes evidence/m5/0c/c3-10x.json.
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

TEST_DISTRO="MergePilot-Test"
PROD_DISTRO="Ubuntu-22.04"
PAT_FILE="/dev/shm/m5c-c3/fixture-pat"

wsl_state() {
  wsl.exe -l -v 2>/dev/null | tr -d '\0' |
    awk -v distro="$1" '$0 ~ distro { for (i = 1; i <= NF; i++) if ($i ~ /^(Stopped|Running|Starting)$/) { print $i; exit } }'
}

cleanup() {
  capture_rc=$?
  cleanup_failed=0
  trap - EXIT INT TERM

  test_state="$(wsl_state "$TEST_DISTRO")"
  if [ "$test_state" = "Running" ] || [ "$test_state" = "Starting" ]; then
    wsl.exe -d "$TEST_DISTRO" -u root -- rm -f -- "$PAT_FILE" >/dev/null 2>&1 || cleanup_failed=1
  fi
  wsl.exe --terminate "$TEST_DISTRO" >/dev/null 2>&1 || cleanup_failed=1

  test_after="$(wsl_state "$TEST_DISTRO")"
  for _ in 1 2 3 4 5; do
    [ "$test_after" = "Stopped" ] && break
    sleep 1
    test_after="$(wsl_state "$TEST_DISTRO")"
  done
  [ "$test_after" = "Stopped" ] || cleanup_failed=1

  prod_after="$(wsl_state "$PROD_DISTRO")"
  [ "$prod_after" = "Stopped" ] || cleanup_failed=1

  if [ "$cleanup_failed" -ne 0 ]; then
    echo "D1 cleanup failed: test=$test_after production=$prod_after" >&2
    [ "$capture_rc" -ne 0 ] || capture_rc=3
  fi
  exit "$capture_rc"
}
trap cleanup EXIT INT TERM

prod_before="$(wsl_state "$PROD_DISTRO")"
if [ "$prod_before" != "Stopped" ]; then
  echo "D1 fail-closed: $PROD_DISTRO must be Stopped (got '${prod_before:-missing}')" >&2
  exit 2
fi

test_before="$(wsl_state "$TEST_DISTRO")"
if [ "$test_before" != "Running" ]; then
  echo "D1 fail-closed: $TEST_DISTRO must already be Running with operator PAT injected (got '${test_before:-missing}')" >&2
  exit 2
fi

# Verify the explicit distro identity and secret-file contract before capture.
wsl.exe -d "$TEST_DISTRO" -u root -- bash -lc \
  'test "${WSL_DISTRO_NAME:-}" = "MergePilot-Test" && test -s /dev/shm/m5c-c3/fixture-pat && test "$(stat -c %a /dev/shm/m5c-c3/fixture-pat)" = "600"'
preflight_rc=$?
if [ "$preflight_rc" -ne 0 ]; then
  echo "D1 fail-closed: distro identity or PAT secret-file preflight failed" >&2
  exit 2
fi

wsl.exe -d MergePilot-Test -u root -- env \
  M5C_C3_ALLOW_GITHUB_WRITES=1 \
  M5C_C3_FIXTURE_GITHUB_PAT_FILE=/dev/shm/m5c-c3/fixture-pat \
  bash -lc 'cd /mnt/d/goai/mergepilot-os && python3 tests/m5_0d/capture_c3_evidence.py'
capture_rc=$?
exit "$capture_rc"
