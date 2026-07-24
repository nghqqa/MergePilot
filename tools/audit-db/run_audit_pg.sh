#!/bin/bash
# 起 PolarDB-PG 兼容审计库(PostgreSQL 容器,接 hiclaw-net)+ 建 schema。
# 用法: MSYS_NO_PATHCONV=1 wsl -- bash /mnt/d/goai/tools/audit-db/run_audit_pg.sh
set -euo pipefail
PG_USER=mergepilot
PG_PASS=mp_audit_2026
PG_DB=mergepilot_audit
CTR=audit-pg

docker rm -f "$CTR" 2>/dev/null || true
# pgvector/pgvector:pg16 自带 vector 扩展;发布 5432 到宿主让 host python 能连(做 embedding)
docker run -d --name "$CTR" --network hiclaw-net -p 5432:5432 --restart unless-stopped \
  -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB="$PG_DB" \
  pgvector/pgvector:pg16

echo "等 PG 就绪..."
for i in $(seq 1 30); do
  if docker exec "$CTR" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    echo "PG ready"; break
  fi
  sleep 1
done

docker cp /mnt/d/goai/tools/audit-db/init.sql "$CTR":/tmp/init.sql
docker exec "$CTR" psql -U "$PG_USER" -d "$PG_DB" -f /tmp/init.sql 2>&1 | grep -iE "CREATE|ERROR" | head -20
echo ""
echo "=== 表清单 ==="
docker exec "$CTR" psql -U "$PG_USER" -d "$PG_DB" -c "\dt" 2>&1
echo ""
echo "audit-pg 已起(hiclaw-net,容器名 audit-pg,库 $PG_DB)。worker 可经 postgres://$PG_USER:$PG_PASS@audit-pg:5432/$PG_DB 访问。"
