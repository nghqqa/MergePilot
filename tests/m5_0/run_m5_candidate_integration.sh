#!/usr/bin/env bash
# Wrapper: M5-0A candidate integration (official Git Bash entry via wsl_test.sh).
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m5_0/_candidate_integration_inner.sh
