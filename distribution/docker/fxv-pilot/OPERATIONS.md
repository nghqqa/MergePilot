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
