#!/usr/bin/env bash
# M5-0D entry — run the 22-formula hiclaw_live evaluator.
# Reads evidence JSON + committed code + git HEAD. No WSL/PAT needed for the
# evaluator itself (production live evidence is loaded from JSON if present).
# Outputs hiclaw_live = all(22) + auxiliary offline/OTel gates.
# Does NOT write final evidence this round.
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
PY="python3"; command -v "$PY" >/dev/null 2>&1 || PY="python"
exec "$PY" "$(dirname "$0")/hiclaw_live_runner.py"
