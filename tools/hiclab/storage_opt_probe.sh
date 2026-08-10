#!/usr/bin/env bash
# storage_opt_probe.sh -- sourceable --storage-opt capability probe.
#
# The probe runs a REAL disposable container with --storage-opt size=1g to
# prove support. Default is UNSUPPORTED (never statically assumes ext4).
# If Docker is unavailable (this round), the probe cannot run and the result
# is UNSUPPORTED (fail-safe) -- --storage-opt is skipped.
#
# Exposes: mp_probe_storage_opt  (sets MP_STORAGE_OPT_SUPPORTED=1|0)
set -uo pipefail

mp_probe_storage_opt() {
  local script_dir py rc
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  py="$script_dir/storage_opt_probe.py"
  if [ ! -f "$py" ]; then
    echo "storage_opt: missing core module $py -> assume unsupported" >&2
    MP_STORAGE_OPT_SUPPORTED=0
    export MP_STORAGE_OPT_SUPPORTED
    return 0
  fi
  set +e
  python3 "$py"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    MP_STORAGE_OPT_SUPPORTED=1
  else
    # rc=1 means probe ran and failed (unsupported) OR probe could not run
    # (daemon unreachable). Either way: unsupported, skip --storage-opt.
    MP_STORAGE_OPT_SUPPORTED=0
  fi
  export MP_STORAGE_OPT_SUPPORTED
}
