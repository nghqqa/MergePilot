#!/usr/bin/env bash
# hiclab_supervisor.sh -- guarded startup entry (thin wrapper).
#
# Runs AFTER docker.service (via hiclab-guarded-start.service). Because all
# managed containers are restart=no, Docker boot does NOT auto-start them;
# this supervisor is the ONLY path that starts them, and only after the
# host+guest disk guard passes AND the phased health gate succeeds.
#
# Boot flow: docker.service starts (no HiClaw auto-start) -> this supervisor
# -> disk_guard -> phased health-gated startup. Boot CANNOT bypass the
# host-disk gate.
#
# All startup/health/rollback logic lives in guarded_start.py (testable).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. disk guard FIRST (fail-closed)
source "$SCRIPT_DIR/disk_guard.sh"
if ! mp_disk_guard; then
  echo "hiclab_supervisor: disk guard FAIL-CLOSED; refusing to start containers" >&2
  exit 2
fi

# 2. phased health-gated startup (Python core)
exec python3 "$SCRIPT_DIR/guarded_start.py"
