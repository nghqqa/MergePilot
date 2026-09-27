# Docker 部署

## 前置条件
- Docker Engine 24+
- Docker Compose v2

## 两条部署路径

| 路径 | 目录 | 镜像 | 包含本地 RAG |
|---|---|---|---|
| A. 预构建发行包 | `distribution/docker/` | `mp-console-image.tar` 导入（digest 见 `image-digest.txt`） | 否（v0.1.0 发行物不含 `/api/rag-trial/*`） |
| B. 源码自建 | `deploy/local-rag-trial/` | `local-rag-trial-console:local`（本地构建） | 是（`local-hash-v1` 默认；`bge-m3` 可选） |

## 路径 A：发行包目录结构
```
distribution/docker/
├── docker-compose.yml    # 编排模板
├── .env.example          # 环境变量模板（不含秘密）
├── mp-console-image.tar  # 镜像导出包（~60MB）
├── image-digest.txt      # sha256:1056df76...
├── image-sbom.cdx.json   # CycloneDX SBOM
└── image-trivy.txt       # Trivy 漏洞报告（0 漏洞）
```

## 部署步骤（路径 A）
1. 复制 `.env.example` → `.env`
2. 生成所有密钥（参见 .env.example 注释）
3. `docker load -i mp-console-image.tar`（导入镜像）
4. `docker compose up -d`
5. 验证：`curl http://127.0.0.1:4730/api/health`

## 部署步骤（路径 B）
见 [QUICKSTART · 路径 B](QUICKSTART.md#path-b)
与 [LOCAL-RAG-GUIDE](LOCAL-RAG-GUIDE.md)。

## 资源档位

按目标机器规格选择路径与可选组件。数字依据：发行包 compose 的容器内存限额
（PG 512MB / MinIO 512MB / Console 256MB）、`bge-m3` 公开规格（权重约 2.3GB、
冷启约 10 秒级、CPU 前向延迟秒级）。

| 档位 | 建议配置 | 可跑能力 | 说明 |
|---|---|---|---|
| **2C2G** | 2 vCPU / 2GB RAM / ~5GB 磁盘 | 路径 A：PR 审查工作台 | 容器限额合计约 1.3GB，剩余留给宿主；A 链词法检索可开（默认关） |
| **4C8G** | 4 vCPU / 8GB RAM / ~15GB 磁盘 | 路径 A + 路径 B：本地 RAG（`local-hash-v1`） | pgvector + MinIO + 自建 console 三服务；哈希嵌入为 CPU 轻量计算（sha256 投影）；构建期还需 Node.js 前端构建的临时开销 |
| **8C16G** | 8 vCPU / 16GB RAM / ~25GB 磁盘 | 路径 B + `bge-m3` 语义嵌入（可选） | bge-m3 权重约 2.3GB 磁盘、常驻内存约 0.3–1GB 预算、CPU 前向延迟秒级（查询延迟明显高于哈希嵌入，可接受再启用） |

注：
- 资源档位是**建议值**，不是硬限制；发行包 compose 未设 CPU 限额。
- `bge-m3` 需自带模型工件并配置 manifest 校验（fail-closed），见
  [LOCAL-RAG-GUIDE · 嵌入策略](LOCAL-RAG-GUIDE.md#嵌入策略)。
- 更高配置不会解锁新能力——Secret Manager、多租户、C 链等属
  Enterprise Roadmap（未实现），与机器规格无关。

## 网络安全
- PG 和 MinIO 在 `internal: true` 网络中（不可外部访问）
- Console 绑定回环地址（默认 127.0.0.1:4730）
- 如需外部访问，修改 ports 为 `0.0.0.0:4730`（**须配置防火墙和 TLS**）
- 路径 B 同样仅发布 127.0.0.1 端口（console 48440 / pg 15436 / minio 9103）

## 数据持久化
- PG 数据：命名卷 `pgdata`（路径 B：`localragtrial-pgdata`）
- MinIO 数据：命名卷 `miniodata`（路径 B：`localragtrial-miniodata`）
- `docker compose down` 保留数据
- `docker compose down -v` 删除所有数据
- 备份恢复见 [LOCAL-RAG-GUIDE · 备份与恢复](LOCAL-RAG-GUIDE.md#备份与恢复)

## 资源限制（路径 A 发行包 compose）
- PG：512MB 内存限制
- MinIO：512MB
- Console：256MB

## 回滚
```bash
docker compose down
docker tag mp-canonical-console:rollback-prev mp-canonical-console:candidate
docker compose up -d
```
路径 B 的回滚（镜像为源码本地构建，随基线代码重建）见
[LOCAL-RAG-GUIDE · 备份与恢复](LOCAL-RAG-GUIDE.md#回滚)。
