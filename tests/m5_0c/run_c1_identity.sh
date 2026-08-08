#!/usr/bin/env bash
# MergePilot-Test entry: C1 identity + Matrix login closure.
# Sources mp_guard (fail-closed test-daemon gate), then runs the C1 runner.
# Running THIS script via wsl_test.sh is the operator authorization for C1
# Matrix writes; the runner re-checks M5C_C1_ALLOW_MATRIX_WRITES=1 before any
# register/room/event write.
set -uo pipefail
ROOT_WSL="/mnt/d/goai/mergepilot-os"
source "$ROOT_WSL/tools/test-env/mp_guard.sh"
export M5C_C1_ALLOW_MATRIX_WRITES=1
python3 "$ROOT_WSL/tests/m5_0c/c1_identity_runner.py"
