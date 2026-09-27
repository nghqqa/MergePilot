# Limitations

## Console
- Single-instance session store (no horizontal scaling)
- No TLS termination (requires reverse proxy)
- Stage derivation depends on PG data
- 单工作台设计：无多租户隔离（Enterprise Roadmap，未实现）

## RAG
- 预构建发行包（v0.1.0 镜像）不含本地 RAG 端点（`/api/rag-trial/*`）
- 本地 RAG 为试用（TRIAL）状态：默认 `local-hash-v1` 哈希嵌入，无语义泛化
  能力；`bge-m3` 语义嵌入需自带工件 + manifest（8C16G 档）
- scope 门默认拒绝（未配置/为空 → 查询 403）；栈 compose 已内置默认 scope
  （`nghqqa/mergepilot@feat/local-rag-trial`），`.env` 可覆盖、显式置空=全拒绝
  （见 LOCAL-RAG-GUIDE）
- A-chain: implemented but default OFF
- C-chain（案例库）: Enterprise Roadmap（3 gaps, see RAG-STATUS.md）
- 检索结果 reference-only：不自动生成 finding/ticket/gate/VERIFIED
- RUN_BINDING_AUTH: NOT_WIRED（Enterprise Roadmap）

## Fixer/Verifier
- Only verified in isolated fixture
- Cannot auto-process real PRs
- Production containers not started
- Requires model client (budget guard)

## GitHub
- Read-only (zero writes)
- No approve/reject/push/merge support

## Deployment
- Docker Compose only (no Kubernetes)
- 预构建镜像本地 tar 分发（`mp-console-image.tar`，digest
  `sha256:1056df76…`）；本地 RAG 栈镜像须源码自建
- No CI/CD pipeline
- 本地 RAG 栈离线运行前提：本机已有 `pgvector/pgvector:pg16` 与 digest-pinned
  MinIO 镜像（pull_policy: never）

## Known Issues
| ID | Description | Severity |
|---|---|---|
| FG-FB-01 | Staging password in test scripts | P3 |
