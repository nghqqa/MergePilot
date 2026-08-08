#!/usr/bin/env bash
# Git Bash entry: M5-0C C1 identity runner via MergePilot-Test (test daemon only).
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m5_0c/run_c1_identity.sh
