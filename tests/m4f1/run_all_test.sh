#!/usr/bin/env bash
# MergePilot-isolated wrapper for tests/m4f1/run_all.sh (official Git Bash entry).
#
# run_all.sh sources tools/test-env/mp_guard.sh at its top (added in the
# isolation round), so execing it via the unified wsl_test.sh entry fail-closes
# (rc=2) before any docker build/run/rm if not on the MergePilot-Test daemon.
#
# Usage (from Windows/Git Bash): bash tests/m4f1/run_all_test.sh
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m4f1/run_all.sh
