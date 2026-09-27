# Quickstart

两条启动路径，按你需要的能力和机器资源选择。

| 路径 | 得到什么 | 资源档位 | 前置 |
|---|---|---|---|
| A. 预构建发行包 | PR 审查工作台（Console 五面 + 只读审查 + 审计） | 2C2G | Docker + Compose |
| B. 源码自建 + 本地 RAG | 路径 A 全部 + 本地 RAG 语料检索（`local-hash-v1`） | 4C8G | Docker + Compose + Node.js 20+（构建前端） |

---

## 路径 A：预构建发行包（约 5 分钟）

环境前提：Docker Engine 24+、Docker Compose v2、本机 2GB 可用内存。

```bash
# 1. 进入部署目录
cd distribution/docker

# 2. 导入离线镜像（约 60MB；跳过此步 up 会因缺镜像失败）
docker load -i mp-console-image.tar

# 3. 复制环境模板
cp .env.example .env

# 4. 生成密钥并填入 .env
openssl rand -hex 16  # PG_PASSWORD
openssl rand -hex 8   # MINIO_USER
openssl rand -hex 16  # MINIO_PASSWORD
openssl rand -hex 12  # CONSOLE_PASSWORD
openssl rand -hex 24  # SESSION_SECRET

# 5. 设置 allowlist（你授权的仓库）
# REPO_ALLOWLIST=your-org/your-repo

# 6. 启动
docker compose up -d

# 7. 验证
curl http://127.0.0.1:4730/api/health  # → {"ok":true,...}

# 8. 访问
open http://127.0.0.1:4730
```

登录：使用 `.env` 中的 `CONSOLE_USER` 和 `CONSOLE_PASSWORD`。

注意：预构建镜像基于 v0.1.0 发行（digest `sha256:1056df76…`），**不含**本地
RAG 端点（`/api/rag-trial/*`）。需要本地 RAG 请走路径 B。

---

<a id="path-b"></a>
## 路径 B：源码自建 + 本地 RAG 试用（约 15 分钟）

环境前提：Docker Engine 24+、Compose v2、Node.js 20+ 与 npm（构建 console
前端 dist）、本机 8GB 内存（4C8G 档）、约 10GB 磁盘。全程离线可跑（镜像
pull_policy: never，需本机已有 `pgvector/pgvector:pg16` 与 digest-pinned
`elestio/minio`；首次可用 `docker pull pgvector/pgvector:pg16` 预取）。

```bash
# 1. 构建 console 前端（dist 不入库，须本地构建）
cd console/frontend && npm ci && npm run build && cd ../..

# 2. 进入本地 RAG 栈目录
cd deploy/local-rag-trial

# 3. 复制环境模板（全部为本地一次性占位值）
cp .env.example .env

# 4. 构建并起栈（console 镜像由本分支源码本地构建）
docker compose up -d --build
docker compose ps   # 三服务 healthy

# 5. 访问 http://127.0.0.1:48440 → 登录 → 「RAG 试验」页
```

RAG scope 门已**默认配置**（compose 内置默认值仅放行试验 scope
`nghqqa/mergepilot@feat/local-rag-trial`）。要检索自己的仓库，在 `.env` 中覆盖：

```bash
RAGTRIAL_ALLOWED_SCOPES=your-repo@main   # 逗号分隔多 scope
```

改后 `docker compose up -d` 重建生效。显式置空（`RAGTRIAL_ALLOWED_SCOPES=`）=
全拒绝（fail-closed，查询 403）。语料导入/检索/删除/备份恢复的完整操作见
[LOCAL-RAG-GUIDE](LOCAL-RAG-GUIDE.md)。

可选：`bge-m3` 语义嵌入（8C16G 档，需自带模型工件）见
[LOCAL-RAG-GUIDE · 嵌入策略](LOCAL-RAG-GUIDE.md#嵌入策略)。

---

## 停止

```bash
docker compose down          # 任一路径；卷保留，数据不丢
docker compose down -v       # 销毁（连卷删除，慎用）
```

## 数据保留

- 路径 A：PG 和 MinIO 使用命名卷（`pgdata`/`miniodata`）。
- 路径 B：命名卷前缀 `localragtrial-*`（pg/minio/evidence）。
- `docker compose down` 不会删除数据；如需完全清除：`docker compose down -v`。
- 备份与恢复（pg_dump + 卷）见 [LOCAL-RAG-GUIDE · 备份恢复](LOCAL-RAG-GUIDE.md#备份与恢复)。

## 资源档位速查

| 档位 | 跑什么 | 依据 |
|---|---|---|
| 2C2G | 路径 A 发行包 | 容器限额合计约 1.3GB（PG 512M + MinIO 512M + Console 256M） |
| 4C8G | 路径 B 本地 RAG（`local-hash-v1`） | pgvector + MinIO + 自建 console + 构建期 Node 开销 |
| 8C16G | 路径 B + `bge-m3` 语义 sidecar | 权重约 2.3GB 磁盘、冷启约 10 秒级、CPU 前向延迟秒级 |

详见 [DOCKER-DEPLOY · 资源档位](DOCKER-DEPLOY.md#资源档位)。
