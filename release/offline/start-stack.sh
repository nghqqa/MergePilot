#!/usr/bin/env bash
# MergePilot offline stack launcher (two-phase: measure postgres bridge IP,
# then start the full stack). WSL2 / Linux.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  printf 'MERGEPILOT_RUN_ID=offline-demo\nMERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=0.0.0.0\n' > .env
fi

echo "[1/3] starting postgres to measure the bridge IP ..."
docker compose up -d --no-deps postgres

echo -n "[2/3] waiting for postgres healthy "
for i in $(seq 1 60); do
  h="$(docker inspect mergepilot-isolated-postgres-1 --format '{{.State.Health.Status}}' 2>/dev/null || true)"
  [ "$h" = "healthy" ] && break
  echo -n "."; sleep 2
done
echo
PGIP="$(docker inspect mergepilot-isolated-postgres-1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' | head -1)"
echo "measured IP: $PGIP"
printf 'MERGEPILOT_RUN_ID=offline-demo\nMERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=%s\n' "$PGIP" > .env

echo "[3/3] starting full stack ..."
docker compose up -d --no-build

echo
echo "Done. preflight runs once; check:  docker compose ps -a"
echo "console:  http://127.0.0.1:8600     webhook health: http://127.0.0.1:8090/healthz"
