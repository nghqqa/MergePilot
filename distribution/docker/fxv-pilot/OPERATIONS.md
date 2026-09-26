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
