# INTERNAL_AGENT_RELEASE_FINAL_CLOSEOUT — 最终收口报告

版本：v0.1.0-rc1 · digest：sha256:1056df76... · 基线：424ec2c · 2026-09-26

## 十四项确认

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| 1 | Review→Fixer→Verifier→PR 更新链路 | ✓ | PR #16 全链（finding→patch→verify→push→comment） |
| 2 | Fixer 事务状态机 | ✓ | fixer-txn 9/9 |
| 3 | Verifier 独立验证 | ✓ | 4/4（无 fixer_reasoning） |
| 4 | 负向场景 | ✓ | 10/10 |
| 5 | 审计/PG/MinIO/Console | ✓ | 可读 |
| 6 | GHCR digest/SBOM/Trivy/Compose/rollback | ✓ | 三 tag 一致、0 漏洞 |
| 7 | PR #426/#2 只读零变化 | ✓ | 4\|0\|2 |
| 8 | PR #16 OPEN/head=f950074/reviews=0 | ✓ | |
| 9 | 人工审批因同账号限制未验证 | ✓ | 如实记录 |
| 10 | 未创建临时 GitHub 账号 | ✓ | |
| 11 | 未 approve/merge/close/删分支 | ✓ | |
| 12 | C 链 BLOCKED | ✓ | 三缺口不变 |
| 13 | embedding/pgvector/RUN_BINDING_AUTH 关闭 | ✓ | |
| 14 | 生产 FXV 按受控策略运行 | ✓ | 仅隔离 |

## 内部稳定版能力矩阵

### ✅ 已验证就绪（内部可用）

| 能力 | 验证等级 | 证据链 |
|---|---|---|
| Console 只读查询 | E2E 8/8 + UA 14/14 + ops 3×19/19 | staging 运行中 |
| PR 阶段推导 | 合同测试 + LIVE | /api/overview |
| Session/CSRF/TTL/allowlist | 401/403/404 矩阵 | 全过 |
| A 链词法检索 | preflight 11 + CI 12 + pilot 10 + LUO 10 | reference-only |
| Fixer（隔离 fixture） | closure 27 + canary 20 | 事务状态机 9/9 |
| Verifier（独立判定） | preflight 7 + closure + canary | 无 fixer_reasoning |
| Fixer→Verifier 联调 | iso_chain 29 + closure 27 + canary 20 | 全负向 10/10 |
| 真实 PR 修复验证 | prod-fxv 20/20 | PR #16 f950074 |
| Docker 分发 | 跨机器导入 digest 一致 | 60MB |
| GHCR 镜像 | v0.1.0-rc1 三 tag 一致 | Trivy 0 |
| 回滚 | 多轮演练 | 41bd3029 可用 |
| 重启恢复 | 多轮验证 | 数据持久 |

### ⛔ 已知限制

| 限制 | 原因 | 影响 |
|---|---|---|
| C 链 BLOCKED | model cache + metadata + key distribution | 无历史案例检索 |
| 人工审批未验证 | GitHub 同账号限制 | 需第二名维护者 |
| PR #16 未 merge | 未授权 + 无第二维护者 | 测试 PR 保持 OPEN |
| 自动 merge 禁止 | 设计决定 | 所有合并须人工 |
| 单操作员 | 未扩大用户 | 仅 pilot |
| 内网部署 | 非公网 | 192.168.1.2:48400 |

## 运维和回滚手册

### 日常运维
```bash
# 健康检查
curl http://192.168.1.2:48400/api/health

# 监控（19 项检查）
node verification/gate/ops-monitor.mjs

# PG 备份
docker exec promote-pg pg_dump -U promote promote > backup.sql

# 查看日志
docker compose logs -f console
```

### 回滚
```bash
# 镜像回滚
docker tag mp-canonical-console:rollback-prev mp-canonical-console:candidate
docker compose up -d --force-recreate console

# A 链关闭（不回滚镜像）
# 从 .env 中删除 MERGEPILOT_ORG_RAG_A_CHAIN 后重启

# 完全清理
docker compose down -v
```

### 升级
```bash
docker pull ghcr.io/nghqqa/mergepilot-console:v0.1.0-rc1
docker compose up -d --force-recreate console
```

## 用户内测说明

### 允许
- 登录/退出 Console
- 浏览 overview/pending/repos/PR detail/audit（只读）
- A 链 reference-only 查询
- 隔离 clone/fixture 中的 Fixer/Verifier 测试
- 观察 degraded/恢复/回滚行为

### 禁止
- 修改真实 PR
- GitHub 写入
- approve/reject/push/merge
- 启用 C 链/embedding
- 启动生产 Fixer/Verifier
- 扩大用户/仓库/PR

## 后续授权清单

| # | 项 | 当前状态 | 需要什么 |
|---|---|---|---|
| 1 | 第二名维护者 approve PR #16 | 等待 | 真实 GitHub 账号 |
| 2 | PR #16 merge | 未授权 | approve 后单独授权 |
| 3 | C 链 D7 下载 | BLOCKED | 操作员提供 model cache |
| 4 | C 链 provider metadata attest | BLOCKED | live 合同测试 |
| 5 | C 链 RUN_BINDING_AUTH 密钥分发 | NOT_WIRED | 密钥管理机制 |
| 6 | 生产 Fixer/Verifier | 仅隔离 | 生产部署授权 |
| 7 | 增加用户 | 仅 pilot | Owner 批准 |
| 8 | 增加仓库/PR | 仅两 PR | Owner 批准 |
| 9 | 官网正式发布 | 仅预览 | 域名+HTTPS+Owner |
| 10 | Registry latest tag | 仅 v0.1.0-rc1 | 版本策略 |

## 判定

**INTERNAL_AGENT_RELEASE_FINALIZED**
