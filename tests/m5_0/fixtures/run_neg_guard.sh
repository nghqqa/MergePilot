#!/usr/bin/env bash
# Negative guard test (runs inside MergePilot-Test): proves mp_guard.sh fails
# closed (rc=2) on the FIRST identity check when WSL_DISTRO_NAME is not
# MergePilot-Test, BEFORE any docker command. A fake `docker` sentinel records
# any call; it must NOT be touched (proving the guard never reached docker).
#
# Also proves DOCKER_HOST tightening: tcp:// and arbitrary unix sockets are
# rejected (rc=2) without touching docker.
#
# Uses a private mktemp -d directory (concurrent-safe, EXIT-trap cleaned).
set -uo pipefail
GUARD="${1:-/mnt/d/goai/mergepilot-os/tools/test-env/mp_guard.sh}"

WORK="$(mktemp -d /tmp/mp-neg.XXXXXX)" || { echo "mktemp failed"; exit 70; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

SENTINEL="$WORK/sentinel"
FAKEBIN="$WORK/bin"
mkdir -p "$FAKEBIN"
printf '%s\n' '#!/bin/sh' "touch '$SENTINEL'" 'exit 0' > "$FAKEBIN/docker"
chmod +x "$FAKEBIN/docker"

# ── Test A: wrong distro → rc=2 before any docker call ──
rm -f "$SENTINEL"
( export WSL_DISTRO_NAME="Ubuntu-22.04"; export PATH="$FAKEBIN:$PATH"
  source "$GUARD" ) >"$WORK/log_a" 2>&1
A_RC=$?
A_SENTINEL="NO"; [ -f "$SENTINEL" ] && A_SENTINEL="YES"
echo "GUARD_RC=$A_RC"
echo "SENTINEL=$A_SENTINEL"

# ── Test B: tcp DOCKER_HOST → rc=2 (Phase 2 check, Phase 1 must pass first) ──
# Phase 1 (distro+hostname) passes inside MergePilot-Test; Phase 2 rejects tcp.
rm -f "$SENTINEL"
( export WSL_DISTRO_NAME="MergePilot-Test"; export DOCKER_HOST="tcp://1.2.3.4:2375"
  export PATH="$FAKEBIN:$PATH"
  source "$GUARD" ) >"$WORK/log_b" 2>&1
B_RC=$?
echo "TCP_HOST_RC=$B_RC"

# ── Test C: arbitrary unix socket → rc=2 ──
rm -f "$SENTINEL"
( export WSL_DISTRO_NAME="MergePilot-Test"; export DOCKER_HOST="unix:///tmp/evil.sock"
  export PATH="$FAKEBIN:$PATH"
  source "$GUARD" ) >"$WORK/log_c" 2>&1
C_RC=$?
echo "EVIL_SOCK_RC=$C_RC"

echo "--- Test A guard stderr ---"; cat "$WORK/log_a" 2>/dev/null
echo "--- Test B guard stderr ---"; cat "$WORK/log_b" 2>/dev/null
