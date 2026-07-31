#!/usr/bin/env bash
# Fixture-only Policy Gateway control for the M4-D production adapter E2E.
# The container binds only 127.0.0.1:18083 and uses the existing fixture-only
# policy. Secrets are read from deploy-owned files and are never printed.
set -euo pipefail

ROOT=/mnt/d/goai/mergepilot-os
TOOLS="$ROOT/tools"
source "$TOOLS/e2e-lib.sh"
e2e_guard

NAME=policy-gw-m4d
DIR=/home/ngh/.config/mergepilot
FIX_POLICY="$TOOLS/policy-gateway/policy-e2e-fixture.yaml"
ACTION="${1:-}"

stop_gateway() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}

case "$ACTION" in
  setup-python)
    VENV=/home/ngh/.cache/mergepilot-m4d-venv
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install \
      -r "$ROOT/skills/pr_lifecycle/requirements.txt"
    "$VENV/bin/python" - <<'PY'
import importlib.metadata as metadata
import sys

print(
    sys.version.split()[0],
    metadata.version("mcp"),
    metadata.version("httpx"),
    metadata.version("anyio"),
)
PY
    ;;
  start)
    ROLE_TOKENS=$(python3 -c \
      'import json;print(json.dumps(json.load(open("/home/ngh/.config/mergepilot/role-tokens-e2e.json"))))')
    PGW_AUDIT_USER=$(grep '^PGW_AUDIT_USER=' "$DIR/audit-db.env" | cut -d= -f2-)
    PGW_AUDIT_PASS=$(grep '^PGW_AUDIT_PASS=' "$DIR/audit-db.env" | head -1 | cut -d= -f2-)
    PGW_AUDIT_DB=$(grep '^PGW_AUDIT_DB=' "$DIR/audit-db.env" | cut -d= -f2-)
    L2_USER=$(grep '^POLICY_GATEWAY_L2_USER=' "$DIR/b4-roles.env" | cut -d= -f2-)
    L2_PASS=$(grep '^POLICY_GATEWAY_L2_PASS=' "$DIR/b4-roles.env" | head -1 | cut -d= -f2-)
    AUDIT_DSN="postgresql://${PGW_AUDIT_USER}:${PGW_AUDIT_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
    L2_DSN="postgresql://${L2_USER}:${L2_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"

    stop_gateway
    docker run -d --name "$NAME" --network hiclab-net --restart no \
      -p 127.0.0.1:18083:8083 \
      -v "$FIX_POLICY":/app/policy-e2e-fixture.yaml:ro \
      -e POLICY_FILE=/app/policy-e2e-fixture.yaml \
      -e ROLE_TOKENS="$ROLE_TOKENS" \
      -e UPSTREAM_URL=http://github-mcp:8082/sse \
      -e AUDIT_DSN="$AUDIT_DSN" \
      -e L2_DSN="$L2_DSN" \
      policy-gateway:latest >/dev/null
    docker network connect mcp-backend-net "$NAME" >/dev/null 2>&1 || true

    READY=0
    for _ in $(seq 1 30); do
      if docker logs "$NAME" 2>&1 | grep -qa 'upstream ready'; then
        READY=1
        break
      fi
      sleep 1
    done
    [ "$READY" = "1" ]
    docker ps --filter "name=^/${NAME}$" --format '{{.Names}} {{.Status}} {{.Ports}}'
    ;;
  stop)
    stop_gateway
    ;;
  token)
    ROLE="${2:-}"
    case "$ROLE" in fixer|coordinator) ;; *) exit 2 ;; esac
    python3 - "$ROLE" <<'PY'
import json
import sys

with open("/home/ngh/.config/mergepilot/role-tokens-e2e.json") as fh:
    print(json.load(fh)[sys.argv[1]])
PY
    ;;
  *)
    echo "usage: gateway-control.sh {setup-python|start|stop|token <fixer|coordinator>}" >&2
    exit 2
    ;;
esac
