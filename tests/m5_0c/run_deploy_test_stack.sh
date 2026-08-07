#!/usr/bin/env bash
# Git Bash entry: M5-0C C0 deploy_test_stack via MergePilot-Test.
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ACTION="${1:-status}"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m5_0c/deploy_test_stack.sh "$ACTION"
