#!/usr/bin/env bash
# MergePilot Docker test-environment ISOLATION proof (v2 — no production access).
#
# HARD CONSTRAINT: this script NEVER invokes wsl.exe -d Ubuntu-22.04 and never
# reads the production Docker daemon. Production-untouched is proven by the fact
# that Ubuntu-22.04 is never started/accessed (verified Stopped before AND after).
#
# Proves:
#   - mp_guard.sh fail-closes (rc=2) when WSL_DISTRO_NAME is NOT MergePilot-Test,
#     and does so BEFORE any docker command (a fake-docker sentinel proves zero
#     docker invocations reached the fail-closed exit).
#   - mp_guard.sh passes (rc=0) inside MergePilot-Test.
#   - a canary container in the TEST daemon (unique name + label) is visible
#     only there; the test daemon exposes no production container and no
#     hiclaw-worker/manager/controller.
#   - precise canary cleanup via EXIT trap (residue=0).
#
# Runs from Windows/Git Bash; all docker operations target MergePilot-Test only.
set -uo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT_WSL="/mnt/d/goai/mergepilot-os"
GUARD="$ROOT_WSL/tools/test-env/mp_guard.sh"
TEST_DISTRO="MergePilot-Test"
PROD_DISTRO="Ubuntu-22.04"   # name only — NEVER passed to wsl.exe -d here
RUN_ID="mp-iso-$$-$(date +%s)"
CANARY="mp-canary-${RUN_ID}"
CANARY_LABEL="com.mergepilot.test_run=${RUN_ID}"

PASS=0; FAIL=0
gate() { if [ "$2" = "0" ]; then echo "GATE PASS: $1"; PASS=$((PASS+1)); else echo "GATE FAIL: $1 (rc=$2)"; FAIL=$((FAIL+1)); fi; }

echo "=== MergePilot test-env isolation proof (run_id=$RUN_ID) ==="

# ── 0. Ubuntu-22.04 must be Stopped BEFORE (we never start/access it) ──
STATE_BEFORE=$(wsl.exe -l -v 2>/dev/null | tr -d '\0' | grep "$PROD_DISTRO" | grep -oE 'Stopped|Running|Starting' | head -1)
echo "$PROD_DISTRO state BEFORE: '$STATE_BEFORE' (expect Stopped)"
gate "0. Ubuntu-22.04 Stopped BEFORE (no production access)" "$([ "$STATE_BEFORE" = "Stopped" ] && echo 0 || echo 1)"

# ── 1. fake-docker NEGATIVE: guard fail-closes before any docker call ──
# Inside MergePilot-Test: run the dedicated negative helper which installs a
# fake `docker` sentinel, sources the guard with WSL_DISTRO_NAME=Ubuntu-22.04,
# and reports GUARD_RC + SENTINEL + TCP_HOST_RC + EVIL_SOCK_RC. The guard MUST
# exit 2 on the WSL_DISTRO_NAME check before any docker command (sentinel NO).
# It must also reject tcp:// and arbitrary unix:// DOCKER_HOST values (rc=2).
NEG_OUT=$(wsl.exe -d "$TEST_DISTRO" -u root -- bash "$ROOT_WSL/tests/m5_0/fixtures/run_neg_guard.sh" "$GUARD" 2>/dev/null | tr -d '\0')
NEG_RC=$(echo "$NEG_OUT" | grep -oE 'GUARD_RC=[0-9]+' | head -1 | cut -d= -f2)
NEG_SENTINEL=$(echo "$NEG_OUT" | grep -oE 'SENTINEL=[A-Z]+' | head -1 | cut -d= -f2)
NEG_TCP=$(echo "$NEG_OUT" | grep -oE 'TCP_HOST_RC=[0-9]+' | head -1 | cut -d= -f2)
NEG_SOCK=$(echo "$NEG_OUT" | grep -oE 'EVIL_SOCK_RC=[0-9]+' | head -1 | cut -d= -f2)
echo "fake-docker negative: rc=$NEG_RC sentinel=$NEG_SENTINEL tcp_rc=$NEG_TCP sock_rc=$NEG_SOCK"
gate "1a. guard fail-closed rc=2 on WSL_DISTRO_NAME=Ubuntu-22.04" "$([ "$NEG_RC" = "2" ] && echo 0 || echo 1)"
gate "1b. zero docker calls before fail-closed (sentinel=NO)" "$([ "$NEG_SENTINEL" = "NO" ] && echo 0 || echo 1)"
gate "1c. tcp:// DOCKER_HOST rejected (rc=2)" "$([ "$NEG_TCP" = "2" ] && echo 0 || echo 1)"
gate "1d. arbitrary unix socket rejected (rc=2)" "$([ "$NEG_SOCK" = "2" ] && echo 0 || echo 1)"

# ── 2. guard PASSES inside MergePilot-Test ──
wsl.exe -d "$TEST_DISTRO" -u root -- bash -lc "source '${GUARD}'" >/tmp/mp_pos_guard.log 2>&1
POS_RC=$?
echo "guard on MergePilot-Test rc=$POS_RC (expect 0)"
gate "2. guard passes inside MergePilot-Test (rc=0)" "$([ "$POS_RC" = "0" ] && echo 0 || echo 1)"

# ── 3. canary in TEST daemon (unique name+label) + EXIT-trap cleanup ──
# Install the cleanup trap BEFORE creating the canary so it is always removed.
cleanup() {
  set +e
  wsl.exe -d "$TEST_DISTRO" -u root -- docker rm -f "$CANARY" >/dev/null 2>&1 || true
  wsl.exe -d "$TEST_DISTRO" -u root -- docker ps -aq --filter "label=$CANARY_LABEL" \
    | xargs -r wsl.exe -d "$TEST_DISTRO" -u root -- docker rm -f >/dev/null 2>&1 || true
}
trap cleanup EXIT
wsl.exe -d "$TEST_DISTRO" -u root -- docker run -d --name "$CANARY" --label "$CANARY_LABEL" \
  busybox sleep 120 >/dev/null 2>&1 || true
sleep 1
TEST_SEES_CANARY=$(wsl.exe -d "$TEST_DISTRO" -u root -- docker ps -a --filter "name=$CANARY" --format '{{.Names}}' 2>/dev/null | tr -d '\0' | head -1)
echo "test sees canary: '$TEST_SEES_CANARY'"
gate "3. canary visible in TEST daemon (unique name)" "$([ "$TEST_SEES_CANARY" = "$CANARY" ] && echo 0 || echo 1)"

# ── 4. test daemon has NO production container + no hiclaw-worker ──
PROD_VISIBLE_FROM_TEST=""
for _c in mergepilot-controller policy-gw audit-pg github-mcp hiclaw-manager hiclaw-controller; do
  if wsl.exe -d "$TEST_DISTRO" -u root -- docker inspect "$_c" >/dev/null 2>&1; then
    PROD_VISIBLE_FROM_TEST="$PROD_VISIBLE_FROM_TEST $_c"
  fi
done
FORBIDDEN=$(wsl.exe -d "$TEST_DISTRO" -u root -- docker ps -a --format '{{.Names}}' 2>/dev/null | tr -d '\0' | grep -cE 'hiclaw-worker|hiclaw-manager|hiclaw-controller' || true)
echo "prod visible from test: '${PROD_VISIBLE_FROM_TEST:-none}'; forbidden hiclaw: $FORBIDDEN"
gate "4a. no production container visible from test daemon" "$([ -z "$PROD_VISIBLE_FROM_TEST" ] && echo 0 || echo 1)"
gate "4b. no hiclaw-worker/manager/controller in test daemon" "$([ "$FORBIDDEN" = "0" ] && echo 0 || echo 1)"

# ── 5. precise canary cleanup (EXIT trap fires) + residue ──
cleanup
TEST_RESIDUE=$(wsl.exe -d "$TEST_DISTRO" -u root -- docker ps -a --filter "label=$CANARY_LABEL" --format '{{.Names}}' 2>/dev/null | tr -d '\0' | grep -c . || true)
echo "test residue (labeled canary): $TEST_RESIDUE (expect 0)"
gate "5. precise canary cleanup (residue=0)" "$([ "$TEST_RESIDUE" = "0" ] && echo 0 || echo 1)"
trap - EXIT

# ── 6. Ubuntu-22.04 must STILL be Stopped (never started/accessed) ──
STATE_AFTER=$(wsl.exe -l -v 2>/dev/null | tr -d '\0' | grep "$PROD_DISTRO" | grep -oE 'Stopped|Running|Starting' | head -1)
echo "$PROD_DISTRO state AFTER: '$STATE_AFTER' (expect Stopped)"
gate "6. Ubuntu-22.04 still Stopped AFTER (production never touched)" "$([ "$STATE_AFTER" = "Stopped" ] && echo 0 || echo 1)"

echo "=== SUMMARY: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" = "0" ] && exit 0 || exit 1
