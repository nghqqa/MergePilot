# MergePilot Console

只读审查工作台（Review Workbench）：面向**个人开发者/小团队自托管**的 PR 安全
审查实时控制台，展示 PR 阶段、待处理事项、运行趋势和系统健康状态；源码自建
路径可开启**本地 RAG 增强**（本地语料库检索，reference-only）。不依赖云端
SaaS，数据不出本机。

## 当前能力

| 能力 | 状态 | 说明 |
|---|---|---|
| Console 只读查询 | ✅ 运行中 | 五面 LIVE（overview/pending/repos/PR-detail/audit） |
| PR 阶段推导 | ✅ 运行中 | 后端权威枚举（REVIEWING/ACTION_REQUIRED/PASSED/BLOCKED/STALE） |
| Session/CSRF/TTL | ✅ 运行中 | 服务端会话 + 仓库 allowlist |
| 本地 RAG 语料检索 | ⚙️ 试用 | 源码自建栈（`deploy/local-rag-trial/`）；`local-hash-v1` 默认嵌入，零模型下载；检索结果 reference-only |
| bge-m3 语义嵌入 | ⚙️ 试用（可选） | 需自带模型工件 + manifest 校验；8C16G 档 |
| A 链（组织知识词法检索） | 已关闭 | `a_chain_disabled`（feature flag 控制） |
| Fixer（隔离 fixture） | 就绪 | 未启动生产容器 |
| Verifier（隔离 fixture） | 就绪 | 独立验证，不接受 Fixer reasoning |
| C 链（历史案例检索） | ⛔ Roadmap | Enterprise Roadmap，见 PUBLIC-CAPABILITY-MATRIX.md |
| GitHub 写入 | ❌ 关闭 | 设计上只读 |

## 快速开始

路径 A（预构建发行包，2C2G）：

```bash
cd distribution/docker
docker load -i mp-console-image.tar
cp .env.example .env
# 编辑 .env 填入所有密钥（参见 .env.example 中的生成命令）
docker compose up -d
# 访问 http://127.0.0.1:4730
```

路径 B（源码自建 + 本地 RAG 试用，4C8G）见 [QUICKSTART](QUICKSTART.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [Quickstart](QUICKSTART.md) | 两条路径启动指南 + 资源档位 |
| [Local-RAG-Guide](LOCAL-RAG-GUIDE.md) | 本地语料库/案例库：导入·索引·删除·备份恢复；嵌入策略 |
| [Capability-Matrix](PUBLIC-CAPABILITY-MATRIX.md) | Developer / Self-hosted Pilot / Enterprise Roadmap 能力矩阵 |
| [Architecture](ARCHITECTURE.md) | 系统架构与组件关系 |
| [API-Contracts](API-CONTRACTS.md) | 五 Core API 合同 |
| [Authentication](AUTHENTICATION.md) | 会话/CSRF/TTL/allowlist |
| [Review-Agent](REVIEW-AGENT.md) | 审查 Agent 使用说明 |
| [Fixer-Verifier](FIXER-VERIFIER.md) | Fixer/Verifier 使用边界 |
| [Docker-Deploy](DOCKER-DEPLOY.md) | Docker 部署详解 + 资源档位 |
| [Configuration](CONFIGURATION.md) | 配置与 secrets 管理 |
| [Monitoring](MONITORING.md) | 运维监控 |
| [Audit](AUDIT.md) | 审计与证据 |
| [Rollback](ROLLBACK.md) | 回滚手册 |
| [Security](SECURITY.md) | 安全模型 |
| [CONTRIBUTING](CONTRIBUTING.md) | 贡献指南 |
| [LIMITATIONS](LIMITATIONS.md) | 已知限制 |
| [RAG-Status](RAG-STATUS.md) | RAG 链路状态与红线 |

## 安全红线

- BLOCKED/STALE/ACTION_REQUIRED/DEGRADED **永不**显示为 PASSED
- 无 receipt **永不**产生 success
- stale head **永不**放行
- RAG 检索结果**仅为带行号引用的参考**（reference-only），不自动生成
  finding/ticket/gate/VERIFIED，不参与风险决策
- Fixer/Verifier **不能**自动处理真实 PR

## 定位边界

本项目面向个人开发者/小团队自托管。多租户隔离、Secret Manager 集成、
external attestation、C 链历史案例检索属于 Enterprise Roadmap（未实现），
本项目当前**没有**企业生产客户或企业生产接入。
