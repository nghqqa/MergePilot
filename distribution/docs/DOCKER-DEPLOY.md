# Docker 部署

## 前置条件
- Docker Engine 24+
- Docker Compose v2
- 2GB 可用内存

## 目录结构
```
distribution/docker/
├── docker-compose.yml    # 编排模板
├── .env.example          # 环境变量模板（不含秘密）
├── mp-console-image.tar  # 镜像导出包（~60MB）
├── image-digest.txt      # sha256:1056df76...
├── image-sbom.cdx.json   # CycloneDX SBOM
└── image-trivy.txt       # Trivy 漏洞报告（0 漏洞）
```

## 部署步骤
1. 复制 `.env.example` → `.env`
2. 生成所有密钥（参见 .env.example 注释）
3. `docker load -i mp-console-image.tar`（导入镜像）
4. `docker compose up -d`
5. 验证：`curl http://127.0.0.1:4730/api/health`

## 网络安全
- PG 和 MinIO 在 `internal: true` 网络中（不可外部访问）
- Console 绑定回环地址（默认 127.0.0.1:4730）
- 如需外部访问，修改 ports 为 `0.0.0.0:4730`（**须配置防火墙和 TLS**）

## 数据持久化
- PG 数据：命名卷 `pgdata`
- MinIO 数据：命名卷 `miniodata`
- `docker compose down` 保留数据
- `docker compose down -v` 删除所有数据

## 资源限制
- PG：512MB 内存限制
- MinIO：512MB
- Console：256MB

## 回滚
```bash
docker compose down
docker tag mp-canonical-console:rollback-prev mp-canonical-console:candidate
docker compose up -d
```
