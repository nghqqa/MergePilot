# FXV 受控试点运维手册
- 部署：cp .env.example .env → 填入生成的密钥（openssl rand）→ docker compose up -d → 健康检查 /api/health 200。
- 备份：docker run --rm -v fxv-pilot_fxv-pgdata:/data -v $PWD:/bk alpine tar czf /bk/pg-$(date +%F).tgz /data（MinIO 同法 fxv-miniodata）。
- 恢复：停栈 → 解包覆盖卷 → up -d → 验证 /api/fxv/attempts 条数与 artifact COMPLETE 数一致。
- 回滚：console 镜像回退上一 digest（ compose image 行改回旧 digest）→ up -d；数据卷向后兼容（fxv schema 幂等）。
- 升级：新 digest 替换 image 行 → docker compose up -d（滚动 console）→ 跑 fxv-e2e-archive.integration 验证归档链。
- MinIO 镜像部署前按官方 digest 固定并替换本文件 NO-pin-placeholder。
- 安全红线：dry-run 默认开（FXV_DRY_RUN=0 才可能真实写且需 FXV_GITHUB_WRITE=authorized+具名 grant）；禁公网暴露（127.0.0.1 绑定）；密钥只入 .env（gitignore）。
