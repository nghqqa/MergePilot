#!/usr/bin/env bash
# M5-0 Candidate Controller startup script (independent entry).
#
# Starts a SEPARATE Docker container for the M5-0 Candidate Controller.
# Does NOT touch, restart, rename, or delete the production mergepilot-controller.
#
# Container name: mergepilot-m5-0-candidate (fixed, independent)
# Requires: production Controller must have RESERVED_RUN_PREFIXES set to match
#           Candidate's M4F_RUN_PREFIX (verified via preflight below).
# Fix 1: builds a dedicated image from the current working tree to guarantee
#        the running code matches M5-0A source. Does NOT trust :latest.
# v2.4: Candidate uses a SEPARATE minimal-privilege Gateway identity
#        (GATEWAY_ROLE=m5coordinator, GATEWAY_TOKEN). It does NOT receive the
#        production COORDINATOR_TOKEN. Gateway policy grants m5coordinator only
#        read-class tools (pull_request_read / get_pr_diff / get_pr_files).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER_NAME="mergepilot-m5-0-candidate"
IMAGE_TAG="mergepilot-m5-0-candidate:current"
PROD_CONTAINER="mergepilot-controller"
BUILD_CTX="${ROOT}/tools/workflow-controller"

# Required env (must be set externally)
: "${ADMIN_PW:?ADMIN_PW required}"
: "${PG_PASS:?PG_PASS required}"
: "${M4F_RUN_PREFIX:?M4F_RUN_PREFIX required (e.g. m5live-)}"
: "${GATEWAY_TOKEN:?GATEWAY_TOKEN required (Candidate minimal-privilege Gateway token; NOT the production coordinator token)}"

# Defaults
MATRIX_USER="${MATRIX_USER:-m5-0-ctrl}"
CONTROLLER_CONSUMER_NAME="${CONTROLLER_CONSUMER_NAME:-m5-0-candidate}"
M4F_ENABLED="${M4F_ENABLED:-1}"
M4F_LIVE_MODE="${M4F_LIVE_MODE:-1}"
M4F_ONLY_MODE="${M4F_ONLY_MODE:-1}"
M4F_ALLOWED_ROOMS="${M4F_ALLOWED_ROOMS:-}"
M4F_ALLOWED_SENDERS="${M4F_ALLOWED_SENDERS:-manager,reviewer,fixer,verifier}"
M4F_SNAPSHOT_DSN="${M4F_SNAPSHOT_DSN:-}"
RESERVED_RUN_PREFIXES="${RESERVED_RUN_PREFIXES:-}"
MATRIX_HS="${MATRIX_HS:-http://hiclaw-controller:6167}"
GATEWAY_URL="${GATEWAY_URL:-http://policy-gw:8083}"
GATEWAY_ROLE="${GATEWAY_ROLE:-m5coordinator}"
L2_MERGE_ENABLED="${L2_MERGE_ENABLED:-0}"
POLL_INTERVAL="${POLL_INTERVAL:-8}"

echo "=== M5-0 Candidate Controller startup ==="
echo "container=$CONTAINER_NAME user=$MATRIX_USER consumer=$CONTROLLER_CONSUMER_NAME prefix=$M4F_RUN_PREFIX"

# Step 0: build dedicated image from current working tree
echo "=== build dedicated image $IMAGE_TAG (ctx=$BUILD_CTX) ==="
docker build -q -t "$IMAGE_TAG" "$BUILD_CTX" 2>&1 | tail -3
# Verify the built image contains controller.py with M5-0 code
if ! docker run --rm --entrypoint python "$IMAGE_TAG" -c \
    "import sys; sys.path.insert(0,'.'); import controller; assert hasattr(controller,'verify_m5_sender'); assert hasattr(controller,'m5_parse_m4f_run'); print('image-verify: M5-0A code present')" 2>/dev/null; then
  echo "ERROR: built image does not contain M5-0A code; aborting"
  exit 1
fi

# Preflight 1: verify production Controller exists and is running
PROD_STATUS=$(docker inspect "$PROD_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
if [ "$PROD_STATUS" != "running" ]; then
  echo "ERROR: production controller '$PROD_CONTAINER' is not running (status=$PROD_STATUS); aborting"
  exit 1
fi
echo "preflight: production controller is running"

# Preflight 2: verify production has RESERVED_RUN_PREFIXES matching M4F_RUN_PREFIX
# (read-only env name check; does NOT output the value; fixed string comparison)
PROD_RESERVED=$(docker inspect "$PROD_CONTAINER" \
  --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
  | while IFS='=' read -r key val; do
    if [ "$key" = "RESERVED_RUN_PREFIXES" ]; then echo "$val"; fi
  done)
if [ -z "$PROD_RESERVED" ]; then
  echo "cutover: mismatch — production controller has empty RESERVED_RUN_PREFIXES"
  echo "         Set RESERVED_RUN_PREFIXES=$M4F_RUN_PREFIX on production controller and restart it first."
  exit 1
fi
# Fixed string comparison (not regex): split comma-separated, exact match
_CUTOVER_OK=""
IFS=',' read -ra _PREFIXES <<< "$PROD_RESERVED"
for _p in "${_PREFIXES[@]}"; do
  _p_trimmed="${_p## }"; _p_trimmed="${_p_trimmed%% }"
  if [ "$_p_trimmed" = "$M4F_RUN_PREFIX" ]; then
    _CUTOVER_OK=1
    break
  fi
done
if [ -z "$_CUTOVER_OK" ]; then
  echo "cutover: mismatch — production RESERVED_RUN_PREFIXES does not exactly contain '$M4F_RUN_PREFIX'"
  exit 1
fi
echo "cutover: match"

# Preflight 3: verify no existing candidate container
if docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null | grep -q "running"; then
  echo "ERROR: $CONTAINER_NAME already running; aborting"
  exit 1
fi
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# Preflight 4: validate prefix format (v2.4 Fix 3: charset + overlap)
case "$M4F_RUN_PREFIX" in
  *%*|*_* ) echo "ERROR: M4F_RUN_PREFIX must not contain SQL wildcards (% or _)"; exit 1 ;;
  "" ) echo "ERROR: M4F_RUN_PREFIX must not be empty"; exit 1 ;;
esac
# Charset: only [A-Za-z0-9.-]
if ! printf '%s' "$M4F_RUN_PREFIX" | grep -qE '^[A-Za-z0-9.-]+$'; then
  echo "ERROR: M4F_RUN_PREFIX must match [A-Za-z0-9.-]+ only (got: $M4F_RUN_PREFIX)"; exit 1
fi
# Parent-child overlap: M4F_RUN_PREFIX must not overlap any RESERVED prefix
if [ -n "$RESERVED_RUN_PREFIXES" ]; then
  IFS=',' read -ra _RP <<< "$RESERVED_RUN_PREFIXES"
  for _r in "${_RP[@]}"; do
    _r="${_r## }"; _r="${_r%% }"
    [ -z "$_r" ] && continue
    if [ "$_r" != "$M4F_RUN_PREFIX" ]; then
      case "$M4F_RUN_PREFIX" in "$_r"*) echo "ERROR: M4F_RUN_PREFIX '$M4F_RUN_PREFIX' is a child of RESERVED '$_r' (parent-child overlap)"; exit 1 ;; esac
      case "$_r" in "$M4F_RUN_PREFIX"*) echo "ERROR: RESERVED '$_r' is a child of M4F_RUN_PREFIX '$M4F_RUN_PREFIX' (parent-child overlap)"; exit 1 ;; esac
    fi
  done
fi

# Start candidate controller (v2.4: GATEWAY_TOKEN/GATEWAY_ROLE, NOT COORDINATOR_TOKEN)
docker run -d --name "$CONTAINER_NAME" \
  --network hiclab-net \
  -e PG_HOST=audit-pg -e PG_PORT=5432 \
  -e PG_DATABASE=mergepilot_audit -e PG_USER=mergepilot \
  -e PG_PASS="$PG_PASS" \
  -e ADMIN_PW="$ADMIN_PW" \
  -e MATRIX_HS="$MATRIX_HS" \
  -e MATRIX_USER="$MATRIX_USER" \
  -e CONTROLLER_CONSUMER_NAME="$CONTROLLER_CONSUMER_NAME" \
  -e GATEWAY_URL="$GATEWAY_URL" \
  -e GATEWAY_ROLE="$GATEWAY_ROLE" \
  -e GATEWAY_TOKEN="$GATEWAY_TOKEN" \
  -e M4F_ENABLED="$M4F_ENABLED" \
  -e M4F_LIVE_MODE="$M4F_LIVE_MODE" \
  -e M4F_ONLY_MODE="$M4F_ONLY_MODE" \
  -e M4F_ALLOWED_ROOMS="$M4F_ALLOWED_ROOMS" \
  -e M4F_ALLOWED_SENDERS="$M4F_ALLOWED_SENDERS" \
  -e M4F_RUN_PREFIX="$M4F_RUN_PREFIX" \
  -e M4F_SNAPSHOT_DSN="$M4F_SNAPSHOT_DSN" \
  -e RESERVED_RUN_PREFIXES="$RESERVED_RUN_PREFIXES" \
  -e L2_MERGE_ENABLED="$L2_MERGE_ENABLED" \
  -e POLL_INTERVAL="$POLL_INTERVAL" \
  "$IMAGE_TAG"

echo "=== Candidate started: $CONTAINER_NAME ==="
echo "Image: $IMAGE_TAG (built from current working tree)"
echo "Logs: docker logs -f $CONTAINER_NAME"
echo "Stop: docker rm -f $CONTAINER_NAME (production controller is NOT affected)"
