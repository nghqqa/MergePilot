#!/usr/bin/env bash
# M5-0D D2B-3 production tier-C host entry.
#
# Requirements:
#   - Ubuntu-22.04 must be Running (production HiClaw stack)
#   - MergePilot-Test must be Stopped (isolation)
#   - WSL_DISTRO_NAME inside Ubuntu must be Ubuntu-22.04
#   - Production authorization marker at /dev/shm/m5d/production-authz
#     (regular, non-symlink, mode 0600, content == "operator-authorized-tier-c")
#   - Matrix admin password at /dev/shm/m5d/matrix-admin-password (same file checks)
#   - Args: m5live-run-id room-id UTC-window-start UTC-window-end
#
# The collector runs INSIDE Ubuntu-22.04 (production environment).
# probe-tools.py is copied to policy-gw:/tmp/m5d-probe-tools.py and removed by trap.
# The collector does NOT need a GitHub PAT secret-file: it queries Gateway/github-mcp
# via docker exec policy-gw (the Gateway container already has its own deploy-owned
# credentials internally; the collector never reads, passes, or mounts the PAT).
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

PROD_DISTRO="Ubuntu-22.04"
TEST_DISTRO="MergePilot-Test"
ROOT_WSL="/mnt/d/goai/mergepilot-os"
PROBE_SRC="tools/policy-gateway/probe-tools.py"
PROBE_DST_CONT="policy-gw"
PROBE_DST_PATH="/tmp/m5d-probe-tools.py"
AUTHZ_FILE="/dev/shm/m5d/production-authz"
MATRIX_PW_FILE="/dev/shm/m5d/matrix-admin-password"
AUTHZ_EXPECTED="operator-authorized-tier-c"

if [[ $# -ne 4 ]]; then
  echo "usage: $0 m5live-run-id room-id UTC-window-start UTC-window-end" >&2
  exit 2
fi
RUN_ID="$1"; ROOM_ID="$2"; WINDOW_START="$3"; WINDOW_END="$4"
[[ "$RUN_ID" =~ ^m5live-[A-Za-z0-9.-]+$ ]] || { echo "D2B-3 fail-closed: invalid run_id" >&2; exit 2; }
[[ "$ROOM_ID" =~ ^![A-Za-z0-9._=-]+:[A-Za-z0-9.:-]+$ ]] || { echo "D2B-3 fail-closed: invalid room_id" >&2; exit 2; }
[[ "$WINDOW_START" =~ ^[0-9TZ:+.-]+$ && "$WINDOW_END" =~ ^[0-9TZ:+.-]+$ ]] || { echo "D2B-3 fail-closed: invalid UTC window" >&2; exit 2; }

wsl_state() {
  wsl.exe -l -v 2>/dev/null | tr -d '\0' |
    awk -v d="$1" '$0 ~ d {for(i=1;i<=NF;i++) if($i ~ /^(Stopped|Running|Starting)$/){print $i; exit}}'
}

[[ "$(wsl_state "$PROD_DISTRO")" == "Running" ]] || { echo "D2B-3 fail-closed: $PROD_DISTRO must be Running" >&2; exit 2; }
[[ "$(wsl_state "$TEST_DISTRO")" == "Stopped" ]] || { echo "D2B-3 fail-closed: $TEST_DISTRO must be Stopped" >&2; exit 2; }

# Verify distro identity + secret-file contract + authorization marker content
wsl.exe -d "$PROD_DISTRO" -u root -- bash -lc '
  test "${WSL_DISTRO_NAME:-}" = "Ubuntu-22.04" || exit 1
  for f in '"$AUTHZ_FILE"' '"$MATRIX_PW_FILE"'; do
    test -f "$f" || exit 2
    test ! -L "$f" || exit 3
    test "$(stat -c %a "$f")" = "600" || exit 4
    test -s "$f" || exit 5
  done
  test "$(cat '"$AUTHZ_FILE"')" = "'"$AUTHZ_EXPECTED"'" || exit 6
' || { echo "D2B-3 fail-closed: distro identity, secret-file contract, or authz content failed" >&2; exit 2; }

# Install trap BEFORE any docker cp so cleanup always runs
cleanup() {
  local rc=$?
  local cleanup_failed=0
  trap - EXIT INT TERM
  # Precisely remove only the probe file we copied
  wsl.exe -d "$PROD_DISTRO" -u root -- bash -lc \
    "docker exec '"$PROBE_DST_CONT"' rm -f '"$PROBE_DST_PATH"' 2>/dev/null && \
     ! docker exec '"$PROBE_DST_CONT"' test -f '"$PROBE_DST_PATH"' 2>/dev/null" >/dev/null 2>&1 || cleanup_failed=1
  wsl.exe --terminate "$PROD_DISTRO" >/dev/null 2>&1
  # Poll Ubuntu-22.04 until Stopped (max 5s)
  local i prod_after
  for i in 1 2 3 4 5; do
    prod_after="$(wsl_state "$PROD_DISTRO")"
    [ "$prod_after" = "Stopped" ] && break
    sleep 1
  done
  [ "$prod_after" = "Stopped" ] || cleanup_failed=1
  if [ "$cleanup_failed" -ne 0 ]; then
    echo "D2B-3 cleanup failed: probe_del=$([ "$cleanup_failed" = 1 ] && echo unknown) prod=$prod_after" >&2
    [ "$rc" -ne 0 ] || rc=3
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# Copy probe-tools.py to policy-gw (after trap installed)
wsl.exe -d "$PROD_DISTRO" -u root -- bash -lc \
  "docker cp '$ROOT_WSL/$PROBE_SRC' '$PROBE_DST_CONT:$PROBE_DST_PATH' && docker exec $PROBE_DST_CONT test -f $PROBE_DST_PATH" \
  || { echo "D2B-3 fail-closed: probe-tools.py copy failed" >&2; exit 2; }

# Run collector inside Ubuntu-22.04 (production environment)
wsl.exe -d "$PROD_DISTRO" -u root -- bash -lc \
  "cd '$ROOT_WSL' && python3 tests/m5_0d/capture_production_live.py \
    --run-id '$RUN_ID' --room-id '$ROOM_ID' \
    --window-start '$WINDOW_START' --window-end '$WINDOW_END'"
capture_rc=$?
exit "$capture_rc"
