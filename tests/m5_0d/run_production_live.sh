#!/usr/bin/env bash
# M5-0D D2B-3 production capture boundary.
# A deploy-owned operator collector must first write raw records outside the
# repository. This wrapper only imports that file; it never accepts tokens.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/repo-external-production-live-raw.json" >&2
  exit 2
fi
INPUT="$1"
if [[ ! -f "$INPUT" ]]; then
  echo "raw production capture not found: $INPUT" >&2
  exit 2
fi

# Explicit production authorization is required even for importing a raw
# capture. The value is an authorization marker, never a credential.
if [[ "${M5_0D_PRODUCTION_AUTHZ:-}" != "operator-authorized-tier-c" ]]; then
  echo "production tier-C authorization marker missing" >&2
  exit 2
fi

exec "$PY" "$ROOT/tests/m5_0d/capture_production_live.py" --input "$INPUT"
