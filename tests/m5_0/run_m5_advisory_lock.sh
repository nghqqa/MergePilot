#!/usr/bin/env bash
# Wrapper: delegates to the WSL-side inner script where Docker is available.
# MSYS_NO_PATHCONV prevents Git Bash from rewriting the /mnt/d/ WSL path.
set -euo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'
exec wsl.exe -e bash /mnt/d/goai/mergepilot-os/tests/m5_0/_advisory_lock_inner.sh
