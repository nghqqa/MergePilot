#!/usr/bin/env bash
# D2B-2 host entry. OTel/SLS is collected only after the production live run
# has stopped; the isolated MergePilot-Test distro queries the deploy-owned
# sink using tmpfs files. No credential is accepted in argv or environment.
set -euo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

TEST_DISTRO="MergePilot-Test"
PROD_DISTRO="Ubuntu-22.04"
ROOT_WSL="/mnt/d/goai/mergepilot-os"

if [[ $# -ne 3 ]]; then
  echo "usage: $0 m5live-run-id UTC-window-start UTC-window-end" >&2
  exit 2
fi
RUN_ID="$1"
WINDOW_START="$2"
WINDOW_END="$3"
[[ "$RUN_ID" =~ ^m5live-[A-Za-z0-9.-]+$ ]] || {
  echo "D2B-2 fail-closed: invalid run_id" >&2
  exit 2
}
[[ "$WINDOW_START" =~ ^[0-9TZ:+.-]+$ && "$WINDOW_END" =~ ^[0-9TZ:+.-]+$ ]] || {
  echo "D2B-2 fail-closed: invalid UTC window" >&2
  exit 2
}

wsl_state() {
  wsl.exe -l -v 2>/dev/null | tr -d '\0' |
    awk -v distro="$1" '$0 ~ distro {for(i=1;i<=NF;i++) if($i ~ /^(Stopped|Running|Starting)$/){print $i; exit}}'
}

[[ "$(wsl_state "$PROD_DISTRO")" == "Stopped" ]] || {
  echo "D2B-2 fail-closed: $PROD_DISTRO must be Stopped" >&2
  exit 2
}
[[ "$(wsl_state "$TEST_DISTRO")" == "Running" ]] || {
  echo "D2B-2 fail-closed: $TEST_DISTRO must already be Running" >&2
  exit 2
}
wsl.exe -d "$TEST_DISTRO" -u root -- bash -lc \
  'test "${WSL_DISTRO_NAME:-}" = "MergePilot-Test"' || {
  echo "D2B-2 fail-closed: distro identity mismatch" >&2
  exit 2
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  wsl.exe --terminate "$TEST_DISTRO" >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

wsl.exe -d "$TEST_DISTRO" -u root -- bash -lc \
  "cd '$ROOT_WSL' && python3 tests/m5_0d/capture_otel_sls.py --run-id '$RUN_ID' --window-start '$WINDOW_START' --window-end '$WINDOW_END'"
