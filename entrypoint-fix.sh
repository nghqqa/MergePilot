#!/bin/sh
set -e
F=/opt/agentteams/scripts/copaw-worker-entrypoint.sh
if ! grep -q 'COPAW_WORKING_DIR_FIX' "$F"; then
  sed -i '/^INSTALL_DIR=/i export COPAW_WORKING_DIR="/root/.copaw-worker/${AGENTTEAMS_WORKER_NAME}/.copaw" # COPAW_WORKING_DIR_FIX' "$F"
  echo "PATCHED $F"
else
  echo "ALREADY PATCHED"
fi
