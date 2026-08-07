#!/usr/bin/env bash
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m5_0c/run_c0_negative_tests.sh
