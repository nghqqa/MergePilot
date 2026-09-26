# PRODUCTIONIZATION_GAP_CLOSURE_GATE — 缺口分析与生产授权包

基线：9feb669 · v0.1.0 · 2026-09-26

## 一 · 生产运行环境清单

### ✅ 已就绪

| 项 | 状态 | 说明 |
|---|---|---|
| 镜像 | ✓ | GHCR v0.1.0（digest 1056df76，Trivy 0） |
| Compose 模板 | ✓ | docker-compose.yml + .env.example |
| 健康检查 | ✓ | /api/health + Docker healthcheck |
| 备份 | ✓ | pg_dump 可执行（419 行） |
| 回滚镜像 | ✓ | v0.1.0-rc1 / rc-20260926 / sha-157a107 / 41bd3029 |
| 升级路径 | ✓ | docker pull + compose up --force-recreate |
| 监控 | ✓ | ops-monitor 19 项 + Docker healthcheck |
| 日志脱敏 | ✓ | redact() 全响应 + 日志零敏感 |
| 审计保留 | ✓ | PG skill_receipt_outbox + skill_gate_audit |

### ⛔ 缺口（需 Owner 提供）

| # | 项 | 需要 | 影响 |
|---|---|---|---|
| 1 | 生产主机 | 指定服务器/云平台 | 无法部署生产 |
| 2 | 域名 | 如 mergepilot.dev | 官网无法发布 |
| 3 | HTTPS 证书 | TLS 证书（Let's Encrypt 或商业） | 无 HTTPS |
| 4 | 反向代理 | Nginx/Caddy/Traefik 配置 | 无 TLS 终端 |
| 5 | PG 生产实例 | 独立或托管 PG（RDS/CloudSQL） | 当前仅 Docker |
| 6 | MinIO 生产实例 | 独立或 S3 兼容存储 | 当前仅 Docker |
| 7 | Secrets 管理 | Vault/AWS SM/1Password | 当前 .env 文件 |
| 8 | 告警通知 | Slack/Email/PagerDuty webhook | 当前无通知 |
| 9 | 日志收集 | ELK/Loki/CloudWatch | 当前 docker logs |
| 10 | RPO/RTO | Owner 决定 | 未定义 |

## 二 · 生产能力边界

### Review Agent：✅ 可生产启用
- 只读 PR 审查 + 阶段推导
- 五面 LIVE 已验证
- Allowlist 服务器端过滤

### Fixer/Verifier：三档策略

| 档 | 用户 | 仓库 | 审批 | Rollback |
|---|---|---|---|---|
| **关闭** | 无 | 无 | N/A | N/A |
| **单仓库受控**（当前） | pilot | fastapi-boilerplate-demo | 人工 approve（第二维护者） | git revert + 分支删除 |
| **多仓库生产**（未来） | pilot + 新增 | Owner 授权列表 | 人工 approve | 同上 + PR 关闭 |

当前档位：**单仓库受控**（仅 fastapi-boilerplate-demo/test 分支/PR #16）

### A 链：reference-only ✅
- 不改变 gate/stage/ticket/success
- 词法检索（lexical-zh-en-v1）
- 审计五字段完整

### C 链：⛔ BLOCKED
```
RAG_CASE_RETRIEVAL = BLOCKED
embedding = DISABLED
pgvector = DISABLED
RUN_BINDING_AUTH = NOT_WIRED
```
三前置缺口：model cache（D7 需授权）→ metadata attest → key distribution

## 三 · 真实 GitHub 治理

| 项 | 状态 |
|---|---|
| PR #16 = 测试 PR | ✓ 已标记 [CANARY-TEST] |
| 不自动 approve | ✓ 设计禁止 |
| 不自动 merge | ✓ 设计禁止 |
| 第二维护者 | ⛳ 等待（需 Owner 指定） |
| #426/#2 只读 | ✓ 4\|0\|2 零变化 |
| 不扩大仓库/用户 | ✓ |

第二维护者操作步骤：
1. 登录 GitHub（有 write 权限的账号）
2. 打开 https://github.com/nghqqa/fastapi-boilerplate-demo/pull/16/files
3. 审查 diff（realpath containment 修复）
4. 点击 "Review changes" → "Approve" → "Submit review"
5. 通知 Owner 已 approve，由 Owner 决定是否 merge

## 四 · 生产安全门禁（全过）

| 门禁 | 结果 |
|---|---|
| allowlist 403 | ✓ |
| 401/404 | ✓ |
| 五面 LIVE | ✓ |
| FXV 状态机 | ✓ 9/9 |
| FXV 生产 Canary | ✓ 20/20 |
| stale/drift/no-receipt 拒绝 | ✓ 负向 10/10 |
| Verifier 独立 | ✓ 无 fixer_reasoning |
| PG/MinIO 读回 | ✓ |
| GitHub 写入 | ✓ 0 |
| 镜像 digest | ✓ 1056df76 |
| Trivy | ✓ 0 漏洞 |
| Rollback | ✓ 多轮演练 |
| 日志敏感 | ✓ 0 |

## 五 · 官网发布准备

| 项 | 状态 |
|---|---|
| CSP | ✓ 静态零外部依赖 |
| 敏感信息 | ✓ 0 命中 |
| 能力边界文案 | ✓ 含 ✗auto-merge ✗full RAG ✗auto-fix ✗embedding |
| Quickstart | ✓ |
| Docker 指南 | ✓ |
| API 合同 | ✓ |
| 安全边界 | ✓ |
| 限制说明 | ✓ |
| 域名 | ⛳ 待 Owner |
| HTTPS | ⛳ 待域名 |
| 托管 | ⛳ 待决定 |
| CSP header | ⛳ 部署时配置 |

## 六 · 判定

**PRODUCTIONIZATION_WAITING_FOR_INPUT**

缺少：生产主机、域名、HTTPS 证书、反向代理配置、PG/MinIO 生产实例、secrets 管理方案、告警通知渠道、日志收集系统、RPO/RTO 定义、第二名 GitHub 维护者、C 链 D7 授权。

以上全部或部分到位后，可进入 PRODUCTIONIZATION_AUTHORIZATION_READY。
