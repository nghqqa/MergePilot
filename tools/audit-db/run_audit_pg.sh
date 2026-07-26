#!/bin/bash
# 起 PolarDB-PG 兼容审计库,使用命名卷(数据持久化,容器重建不丢)。
set -uo pipefail
PG_USER="${PG_USER:-mergepilot}"
PG_PASS="${PG_PASS:?需 PG_PASS 环境变量}"
PG_DB="${PG_DB:-mergepilot_audit}"
CTR=audit-pg
VOL=mergepilot-pgdata

# 创建命名卷(幂等)
docker volume create "$VOL" >/dev/null 2>&1 || true

# 如果容器已存在但停止,直接 start(不重建,保留数据)
if docker ps -a --format '{{.Names}}' | grep -q "^${CTR}$"; then
  if docker inspect "$CTR" --format '{{.State.Running}}' 2>/dev/null | grep -q false; then
    echo "$CTR 已存在但停止 → docker start"
    docker start "$CTR"
    sleep 5
  else
    echo "$CCR 已在运行"
  fi
else
  echo "创建新容器(命名卷 $VOL)..."
  docker run -d --name "$CTR" --network hiclaw-net -p 5432:5432 --restart unless-stopped \
    -v "${VOL}:/var/lib/postgresql/data" \
    -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB="$PG_DB" \
    pgvector/pgvector:pg16
fi

echo "等 PG 就绪..."
for i in $(seq 1 30); do
  if docker exec "$CTR" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    echo "PG ready"; break
  fi
  sleep 1
done

# 应用 schema(幂等)
docker cp /mnt/d/goai/tools/audit-db/init.sql "$CTR":/tmp/init.sql 2>/dev/null
docker exec "$CTR" psql -U "$PG_USER" -d "$PG_DB" -f /tmp/init.sql 2>&1 | grep -iE "CREATE|ERROR" | head -10
docker cp /mnt/d/goai/tools/audit-db/m3_state.sql "$CTR":/tmp/m3_state.sql 2>/dev/null
docker exec "$CTR" psql -U "$PG_USER" -d "$PG_DB" -f /tmp/m3_state.sql 2>&1 | grep -iE "CREATE|ALTER|ERROR" | head -15

echo ""
echo "=== 表清单 ==="
docker exec "$CTR" psql -U "$PG_USER" -d "$PG_DB" -c "\dt" 2>&1
echo ""
echo "audit-pg 已就绪(hiclaw-net,命名卷 $VOL)。"
