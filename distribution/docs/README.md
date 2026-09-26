# MergePilot Console

只读审查工作台（Review Workbench）：面向安全审查的实时控制台，展示 PR 阶段、待处理事项、运行趋势和系统健康状态。

## 当前能力

| 能力 | 状态 | 说明 |
|---|---|---|
| Console 只读查询 | ✅ 运行中 | 五面 LIVE（overview/pending/repos/PR-detail/audit） |
| PR 阶段推导 | ✅ 运行中 | 后端权威枚举（REVIEWING/ACTION_REQUIRED/PASSED/BLOCKED/STALE） |
| Session/CSRF/TTL | ✅ 运行中 | 服务端会话 + 仓库 allowlist |
| A 链（组织知识词法检索） | 已关闭 | `a_chain_disabled`（feature flag 控制） |
| Fixer（隔离 fixture） | 就绪 | 未启动生产容器 |
| Verifier（隔离 fixture） | 就绪 | 独立验证，不接受 Fixer reasoning |
| C 链（skill_case_retrieval） | ⛔ BLOCKED | 见 RAG-STATUS.md |
| GitHub 写入 | ❌ 关闭 | 设计上只读 |

## 快速开始

```bash
cd distribution/docker
cp .env.example .env
# 编辑 .env 填入所有密钥（参见 .env.example 中的生成命令）
docker compose up -d
# 访问 http://127.0.0.1:4730
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [Quickstart](QUICKSTART.md) | 5 分钟启动指南 |
| [Architecture](ARCHITECTURE.md) | 系统架构与组件关系 |
| [API-Contracts](API-CONTRACTS.md) | 五 Core API 合同 |
| [Authentication](AUTHENTICATION.md) | 会话/CSRF/TTL/allowlist |
| [Review-Agent](REVIEW-AGENT.md) | 审查 Agent 使用说明 |
| [Fixer-Verifier](FIXER-VERIFIER.md) | Fixer/Verifier 使用边界 |
| [Docker-Deploy](DOCKER-DEPLOY.md) | Docker 部署详解 |
| [Configuration](CONFIGURATION.md) | 配置与 secrets 管理 |
| [Monitoring](MONITORING.md) | 运维监控 |
| [Audit](AUDIT.md) | 审计与证据 |
| [Rollback](ROLLBACK.md) | 回滚手册 |
| [Security](SECURITY.md) | 安全模型 |
| [CONTRIBUTING](CONTRIBUTING.md) | 贡献指南 |
| [LIMITATIONS](LIMITATIONS.md) | 已知限制 |
| [RAG-Status](RAG-STATUS.md) | RAG 链路状态 |

## 安全红线

- BLOCKED/STALE/ACTION_REQUIRED/DEGRADED **永不**显示为 PASSED
- 无 receipt **永不**产生 success
- stale head **永不**放行
- A 链结果仅为组织规范参考，不参与风险决策
- Fixer/Verifier **不能**自动处理真实 PR
