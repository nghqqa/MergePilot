#!/usr/bin/env bash
# MergePilot-Test entry: C0 negative + idempotency tests.
# Sources mp_guard, then runs the Python test runner.
set -uo pipefail
ROOT_WSL="/mnt/d/goai/mergepilot-os"
source "$ROOT_WSL/tools/test-env/mp_guard.sh"
python3 "$ROOT_WSL/tests/m5_0c/c0_negative_tests.py"
