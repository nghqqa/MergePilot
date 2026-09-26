# Quickstart（5 分钟）

## 前置条件
- Docker + Docker Compose
- 本机 2GB 可用内存

## 步骤

```bash
# 1. 进入部署目录
cd distribution/docker

# 2. 复制环境模板
cp .env.example .env

# 3. 生成密钥并填入 .env
openssl rand -hex 16  # PG_PASSWORD
openssl rand -hex 8   # MINIO_USER
openssl rand -hex 16  # MINIO_PASSWORD
openssl rand -hex 12  # CONSOLE_PASSWORD
openssl rand -hex 24  # SESSION_SECRET

# 4. 设置 allowlist（你授权的仓库）
# REPO_ALLOWLIST=your-org/your-repo

# 5. 启动
docker compose up -d

# 6. 验证
curl http://127.0.0.1:4730/api/health  # → {"ok":true,...}

# 7. 访问
open http://127.0.0.1:4730
```

## 登录
使用 `.env` 中设置的 `CONSOLE_USER` 和 `CONSOLE_PASSWORD`。

## 停止
```bash
docker compose down
```

## 数据保留
PG 和 MinIO 使用命名卷，`docker compose down` 不会删除数据。
如需完全清除：`docker compose down -v`。
