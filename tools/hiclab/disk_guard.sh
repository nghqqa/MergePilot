#!/usr/bin/env bash
# disk_guard.sh -- sourceable host+guest disk threshold guard.
#
# Sourced BEFORE any docker build/run/rm/restart in startup scripts.
# Core logic lives in disk_guard.py (injectable probes, unit-tested on host).
#
# Exposes: mp_disk_guard  (returns 0 = OK, 2 = fail-closed)
#
# Configuration (env, all optional with fail-closed defaults):
#   MP_WSL_VHDX_PATH        Windows path to the WSL ext4.vhdx (REQUIRED for
#                           host check; unset/empty -> fail-closed)
#   MP_DISK_MIN_GUEST_GIB   guest free threshold GiB (default 100)
#   MP_DISK_MIN_HOST_GIB    host free threshold GiB (default 150)
#   MP_DOCKER_ROOT          guest docker root (default /var/lib/docker)
#
# Usage in a startup script:
#   source "$(dirname "$0")/hiclab/disk_guard.sh"
#   mp_disk_guard || { echo "disk guard fail-closed" >&2; exit 2; }
set -uo pipefail

mp_disk_guard() {
  local script_dir py
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  py="$script_dir/disk_guard.py"
  if [ ! -f "$py" ]; then
    echo "disk_guard: missing core module $py" >&2
    return 2
  fi
  python3 "$py"
}
