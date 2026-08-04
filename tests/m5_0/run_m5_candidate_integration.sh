#!/usr/bin/env bash
# Wrapper: delegates to the WSL-side inner script.
set -euo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'
exec wsl.exe -e bash /mnt/d/goai/mergepilot-os/tests/m5_0/_candidate_integration_inner.sh
