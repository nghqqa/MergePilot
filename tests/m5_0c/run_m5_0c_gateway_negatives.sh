#!/usr/bin/env bash
# Git Bash entry: M5-0C gateway gate negative + concurrency cases.
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m5_0c/run_gateway_policy_negatives.sh
