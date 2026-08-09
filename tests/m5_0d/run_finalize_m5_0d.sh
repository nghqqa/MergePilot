#!/usr/bin/env bash
# M5-0D finalizer entry — validates 3 evidence + runs evaluator + emits attestation.
# Runs on the host (pure Python, reads evidence JSON + git HEAD). No WSL needed.
# Evidence must NOT be committed; attestation goes to repo-external path.
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
PY="python3"; command -v "$PY" >/dev/null 2>&1 || PY="python"
exec "$PY" "$(dirname "$0")/finalize_m5_0d.py"
