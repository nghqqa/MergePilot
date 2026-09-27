# FXV 受控试点运维手册
- 部署：cp .env.example .env → 填入生成的密钥（openssl rand）→ docker compose up -d → 健康检查 /api/health 200。
- 备份：docker run --rm -v fxv-pilot_fxv-pgdata:/data -v $PWD:/bk alpine tar czf /bk/pg-$(date +%F).tgz /data（MinIO 同法 fxv-miniodata）。
- 恢复：停栈 → 解包覆盖卷 → up -d → 验证 /api/fxv/attempts 条数与 artifact COMPLETE 数一致。
- 回滚：console 镜像回退上一 digest（ compose image 行改回旧 digest）→ up -d；数据卷向后兼容（fxv schema 幂等）。
- 升级：新 digest 替换 image 行 → docker compose up -d（滚动 console）→ 跑 fxv-e2e-archive.integration 验证归档链。
- MinIO 镜像部署前按官方 digest 固定并替换本文件 NO-pin-placeholder。
- 安全红线：dry-run 默认开（FXV_DRY_RUN=0 才可能真实写且需 FXV_GITHUB_WRITE=authorized+具名 grant）；禁公网暴露（127.0.0.1 绑定）；密钥只入 .env（gitignore）。

## B 轨加固说明（feat/core-b-parallel，2026-09-26）
- minio digest 已真实 pin（elestio/minio@sha256:25348a25…，与受控 staging 验证同源；容器内 curl 健康检查实测 200）。原 `sha256:_NO-pin-placeholder` 占位缺陷已消除。
- console 增加 CONSOLE_HOST=0.0.0.0（容器内默认 127.0.0.1 会导致宿主端口映射不可达——staging 实测坑）与 wget /api/health 健康检查，并 depends_on minio healthy。
- 新增 fxv-modelcache / fxv-keystore 卷与 C 链 env 透传。诚实边界：当前 pinned 镜像（a0bfa15 构建）尚无 /api/cchain/* 端点——这些 env 在含 cchain 接线的新镜像发布前为前向兼容 no-op；届时无需改 compose 即生效。
- G-07 多凭证：FXV_ACCESS_MODEL_JSON / FXV_USER_CREDENTIALS_JSON 配置后启用每用户独立口令（默认拒绝；不配置回落 legacy 单用户）。口令只入 .env/secrets，绝不入库入仓。

## 生产候选更新（2026-09-27，main=c07a4d89）
- console 镜像固定到 **sha256:8a6427b0dd2c…747d17**（`fxv-staging-20260927-hardening`，Trivy HIGH/CRITICAL=0，SBOM 49；含 C 链接线与安全加固波）。compose 内保留旧 digest 为**回滚锚点**（947ab1b6…，6h 稳定性验证基线）。
- 回滚：将 image 行换回锚点 digest → `docker compose up -d` → 健康检查 /api/health。旧镜像无 /api/cchain/* 端点——回滚期间 C 链面板/端点 404 属预期，不影响 FXV 主链。
- 后端 CI：新增 `.github/workflows/console-backend.yml`（单测/合同全量 + FXV 18 + E2E 15 + C 链合同/PG 审计 + 前端构建，PG/MinIO 服务容器化）；live-v3 集成保持 env-skip（需 legacy 4191 fixture harness，历史项，非本仓库可在 CI 起的服务——skip 原因登记于 workflow 注释与本文件）。

## mainline-20260927 溯源镜像（2026-09-27）
- console 固定 `sha256:f7ffce6d…fea6`（mainline-20260927：.dockerignore 重建，镜像不含 test/internal；远端 digest 与本地逐字节核对一致）。
- 回滚锚点双保留：8a6427b0（上一候选 hardening）/ 947ab1b6（6h 基线）。promote3 栈已实测 f7ffce6d↔8a6427b0 双向 6s 切换恢复。

## RAG Trial 发行（2026-09-27，rag-trial-20260927）
- **状态声明**：RAG = **TRIAL_READY 非生产级**——使用 **local-hash-v1 词法级试验模型（非生产 embedding）**；**不自动生成 finding/ticket/gate/VERIFIED**；Fixer 不接受 RAG-only 输入；Verifier 只接受独立 harness/test 证据。真实 embedding、worker 接线、机器间验签、C 链三输入仍未完成。
- **console digest**：`sha256:dec398d0…bb6b`（三锚点回滚见 compose 注释；**旧镜像无 RAG 端点时 404 属预期**）。promote3 实测双向切换 5s/6s。
- **PG = pgvector/pg16（digest pin）**：RAG vector 扩展必需。**从 postgres:16-alpine 升级必须重建 fxv-pgdata 卷**（musl↔glibc 跨 libc collation 不兼容；FXV schema 幂等重建；卷内验证数据可重置）。备份/恢复照旧（pg_dump/tar 均适用）。
- **语料**：部署后 `docker compose cp console ./corpus/. /app/rag-corpus/` 灌入（或 bind 挂载）；须带逐文件 SHA256 manifest；容器内只读。
- **回滚**：仅改 console image 行 → `up -d`；pg 卷无需随 console 回滚重建（pgvector 向后兼容纯 SQL 数据）。
