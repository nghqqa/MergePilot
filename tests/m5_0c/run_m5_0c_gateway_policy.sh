#!/usr/bin/env bash
# Git Bash entry: M5-0C real-Gateway policy runtime gate, via the official
# MergePilot-Test wrapper (wsl_test.sh forces -d MergePilot-Test + mp_guard).
set -euo pipefail
ROOT_GB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT_GB/tools/test-env/wsl_test.sh" /mnt/d/goai/mergepilot-os tests/m5_0c/run_gateway_policy_runtime.sh
