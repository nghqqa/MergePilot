#!/bin/bash
# 构建 + 启 mergepilot-controller 独立容器。
# 密码从 /home/ngh/.config/mergepilot/controller.env 读取(chmod 600)。
set -uo pipefail
ENV_FILE=/home/ngh/.config/mergepilot/controller.env

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE 不存在。请先创建(chmod 600):"
  echo "  mkdir -p /home/ngh/.config/mergepilot"
  echo "  cat > $ENV_FILE <<EOF"
  echo "  ADMIN_PW=<admin密码>"
  echo "  PG_PASS=<pg密码>"
  echo "  EOF"
  echo "  chmod 600 $ENV_FILE"
  exit 1
fi

echo "=== build controller image ==="
docker build -t mergepilot-controller:latest /mnt/d/goai/tools/workflow-controller/ 2>&1 | tail -3

echo "=== run controller container ==="
docker rm -f mergepilot-controller 2>/dev/null || true
docker run -d --name mergepilot-controller --network hiclab-net --restart unless-stopped \
  --env-file "$ENV_FILE" \
  -e PG_HOST=audit-pg -e PG_PORT=5432 \
  -e PG_DATABASE=mergepilot_audit -e PG_USER=mergepilot \
  -e MATRIX_HS=http://hiclaw-controller:6167 \
  mergepilot-controller:latest

sleep 8
echo "=== 状态 ==="
docker ps --filter name=mergepilot-controller --format "{{.Names}} | {{.Status}}"
echo "=== 日志 ==="
docker logs mergepilot-controller 2>&1 | head -6
