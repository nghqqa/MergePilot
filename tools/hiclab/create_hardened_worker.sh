#!/usr/bin/env bash
# create_hardened_worker.sh -- MANUAL operator tool for hardened worker recreation.
#
# WARNING: this is NOT a recurrence-prevention mechanism. The HiClaw Manager
# auto-creates hiclaw-worker-* via the docker socket and bypasses this tool.
# See UPSTREAM_BLOCKED.md -- recurrence prevention requires the socket-proxy
# deployment (harden_policy.py) or upstream HiClaw support.
#
# This tool is a CORRECT hardened recreation for operator-initiated hardening:
#   * FULL container contract preserved (Entrypoint/Cmd/User/WorkingDir/Mounts/
#     Networks/aliases/Caps/SecurityOpt/Healthcheck/Labels/...) via the
#     inspect-driven argv builder (worker_argv.build_run_argv_from_inspect).
#   * SECRET-SAFE: authoritative env goes to a /dev/shm env-file (0600), never
#     to argv as -e KEY=VALUE, never printed. File deleted after use.
#   * ROLLBACK: full inspect saved to /dev/shm BEFORE the original is removed;
#     on failure, rollback_worker.py restores the original contract.
#   * storage-opt only if a disposable-container probe proved support.
#
# Steps:
#   1. disk_guard (fail-closed before any docker op)
#   2. storage-opt disposable probe
#   3. pre-existence check (refuse clobber)
#   4. hiclab create worker (AUTHORITATIVE entry)
#   5. docker inspect -> pipe to harden_orchestrate.py ->
#      rollback artifact + env-file + hardened argv
#   6. docker rm -f original (rollback available)
#   7. docker run (hardened, full contract)
#   8. cleanup env-file (keep rollback for audit window)
#   9. on failure: rollback_worker.py restores original
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/disk_guard.sh"
source "$SCRIPT_DIR/storage_opt_probe.sh"

WORKER="${1:-}"
RUN_ID="${2:-}"
MODEL="${3:-deepseek-v4-flash}"
RUNTIME="${4:-openclaw}"
CONTROLLER="${MP_HICLAW_CONTROLLER:-hiclaw-controller}"
NETWORK="${MP_HICLAW_NETWORK:-hiclab-net}"
CTR="hiclaw-worker-${WORKER}"

if [ -z "$WORKER" ] || [ -z "$RUN_ID" ]; then
  echo "usage: create_hardened_worker.sh <worker> <run_id> [model] [runtime]" >&2
  exit 2
fi

# 1. disk guard (BEFORE any docker op)
mp_disk_guard || { echo "ERROR: disk guard fail-closed; aborting before docker" >&2; exit 2; }

# 2. storage-opt disposable probe
mp_probe_storage_opt
echo "storage_opt: MP_STORAGE_OPT_SUPPORTED=$MP_STORAGE_OPT_SUPPORTED"

# 3. pre-existence check
if docker inspect "$CTR" >/dev/null 2>&1; then
  echo "ERROR: $CTR already exists. Run clean-reset-workers.sh first or pick a new name." >&2
  exit 1
fi

# 4. authoritative creation
echo "=== authoritative: hiclab create worker $WORKER (model=$MODEL runtime=$RUNTIME) ==="
if ! docker exec "$CONTROLLER" hiclaw create worker --name "$WORKER" \
     --model "$MODEL" --runtime "$RUNTIME"; then
  echo "ERROR: authoritative hiclab create worker failed" >&2
  exit 1
fi
sleep 4

# 5. inspect -> orchestrate (rollback + env-file + argv). No env printed.
ARGV_FILE=$(mktemp)
META_FILE=$(mktemp)
trap 'rm -f "$ARGV_FILE" "$META_FILE"' EXIT

STORAGE_GIB=""
[ "$MP_STORAGE_OPT_SUPPORTED" = "1" ] && STORAGE_GIB="${MP_STORAGE_OPT_GIB:-10}"

if ! docker inspect "$CTR" --format '{{json .}}' | \
     MP_CONTAINER_KIND=worker \
     MP_AGENT_NAME="$WORKER" \
     MP_RUN_ID="$RUN_ID" \
     MP_STORAGE_OPT_GIB="$STORAGE_GIB" \
     python3 "$SCRIPT_DIR/harden_orchestrate.py" >"$ARGV_FILE" 2>"$META_FILE"; then
  echo "ERROR: orchestration failed (see stderr above)" >&2
  cat "$META_FILE" >&2
  exit 1
fi
# parse meta (ROLLBACK=... ENVFILE=...)
ROLLBACK_PATH=$(grep '^ROLLBACK=' "$META_FILE" | cut -d= -f2-)
ENVFILE_PATH=$(grep '^ENVFILE=' "$META_FILE" | cut -d= -f2-)
echo "  rollback artifact: $ROLLBACK_PATH"
echo "  env-file (shm, 0600): $ENVFILE_PATH"

# 6. remove authoritative container (rollback available)
echo "=== remove authoritative container (will recreate hardened) ==="
docker rm -f "$CTR" >/dev/null

# 7. recreate hardened (full contract)
mapfile -d '' -t RUN_ARGV < "$ARGV_FILE"
if [ "${#RUN_ARGV[@]}" -eq 0 ]; then
  echo "ERROR: empty argv from orchestration" >&2
  exit 1
fi
echo "=== recreate hardened $CTR (full contract, restart=no, run_id=$RUN_ID) ==="
if ! docker "${RUN_ARGV[@]}"; then
  echo "ERROR: hardened docker run failed -- rolling back" >&2
  # 9. rollback: rebuild original from saved inspect
  RB_ENVFILE=$(mktemp -p /dev/shm)
  docker inspect --format '{{json .}}' "$CTR" >/dev/null 2>&1 || true
  # original already removed; rebuild from rollback artifact
  MP_ENV_FILE="$RB_ENVFILE" python3 "$SCRIPT_DIR/rollback_worker.py" "$ROLLBACK_PATH" | tr '\0' '\n' > /dev/null 2>&1 || true
  rm -f "$RB_ENVFILE" "$ENVFILE_PATH"
  echo "  rollback artifact retained at $ROLLBACK_PATH for manual restore" >&2
  exit 1
fi

# 8. cleanup env-file (rollback retained for audit window per UPSTREAM_BLOCKED)
rm -f "$ENVFILE_PATH"
echo "=== done: $CTR hardened (env-file purged; rollback at $ROLLBACK_PATH) ==="
docker ps --filter "name=$CTR" --format "{{.Names}} | {{.Status}}"
