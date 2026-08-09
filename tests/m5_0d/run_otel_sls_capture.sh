#!/usr/bin/env bash
# M5-0D D2B-2 OTel/SLS capture entry. The raw capture is deploy-owned and
# must be supplied as a repo-external file; this script never receives creds.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/repo-external-otel-sls-raw.json" >&2
  exit 2
fi
INPUT="$1"
if [[ ! -f "$INPUT" ]]; then
  echo "raw capture not found: $INPUT" >&2
  exit 2
fi

exec "$PY" "$ROOT/tests/m5_0d/capture_otel_sls.py" --input "$INPUT"
