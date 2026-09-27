# MergePilot v0.1.0 — 个人开发者/小团队的自托管 PR 审查系统

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

MergePilot 是一台跑在你自己机器上的 PR 安全审查工作台：只读审查、发现管理、
修复验证，带完整审计追踪；可选开启**本地 RAG 增强**（本地语料库检索，为审查
提供可引用的参考证据）。面向个人开发者和小团队自托管使用，不依赖任何云端
SaaS，数据不出本机。

## 适合谁

- **个人开发者**：想在自己的仓库上跑一套带审计的只读 PR 审查台，2C2G 小机器即可。
- **小团队**：想在内部机器上自托管，把团队安全规范做成可检索的本地语料库，
  审查时自动带出规范原文引用（4C8G 起）。
- **不适合**：需要多租户隔离、企业密钥管理或外部合规背书的企业生产环境——
  这些在 Enterprise Roadmap 里（见 [能力矩阵](distribution/docs/PUBLIC-CAPABILITY-MATRIX.md)），当前版本未实现。

## 两条启动路径

| 路径 | 得到什么 | 资源档位 |
|---|---|---|
| **A. 预构建发行包**（`distribution/docker/`，导入离线镜像） | PR 审查工作台：Console 五面 + 只读审查 + 阶段推导 + 审计 | 2C2G |
| **B. 源码自建 + 本地 RAG 试用**（`deploy/local-rag-trial/`，本地构建镜像） | 路径 A 全部 + 本地 RAG：语料导入/索引/检索/删除/回滚（默认 `local-hash-v1` 哈希嵌入，零模型下载） | 4C8G |

路径 B 追加可选语义嵌入 `bge-m3`（需自带模型工件 + manifest 校验，8C16G 档），
详见 [本地 RAG 指南](distribution/docs/LOCAL-RAG-GUIDE.md)。

## 当前能力

| 能力 | 状态 |
|---|---|
| Review Agent（只读 PR 审查 + 阶段推导） | ✅ |
| Console（实时控制台 + 服务端会话 + 仓库 allowlist） | ✅ |
| 本地 RAG 语料检索（reference-only，`local-hash-v1` 默认） | ⚙️ 试用（源码自建路径） |
| bge-m3 语义嵌入（可选，自带工件 + manifest） | ⚙️ 试用（8C16G 档） |
| A 链组织知识检索（词法，默认关，需显式开启） | ⚙️ 受控 |
| Fixer / Verifier（隔离环境中的修复与独立验证） | ⚙️ 受控 |
| Docker 部署（离线镜像包 / 源码自建） | ✅ |

## 能力边界（诚实声明）

- ❌ **不自动 merge** — 所有合并决策由人工执行
- ❌ **不自动 approve** — 审批由人工操作
- ❌ **RAG 只返回 reference** — 检索结果不自动生成 finding/ticket/gate/VERIFIED，
  不参与风险决策（详见 [RAG-STATUS](distribution/docs/RAG-STATUS.md)）
- ❌ **C 链历史案例检索未启用** — 处于 Enterprise Roadmap（见能力矩阵）
- ❌ **生产自动修复未全量启用** — Fixer/Verifier 仅在隔离环境运行
- ❌ **默认不做任何模型下载** — `local-hash-v1` 零下载；语义模型须由你自带并过 manifest 校验
- ❌ **GitHub 写入默认关闭** — 只读审查
- ❌ **不是多租户系统** — 单工作台设计，无租户隔离

## Quickstart（路径 A：预构建包）

```bash
cd distribution/docker
docker load -i mp-console-image.tar   # 导入离线镜像（约 60MB）
cp .env.example .env
# 编辑 .env 填入生成的密钥
docker compose up -d
# 访问 http://127.0.0.1:4730
```

路径 B（本地 RAG 试用）与资源档位说明见
[Quickstart](distribution/docs/QUICKSTART.md)。

## Docker 部署与资源档位

镜像：`mp-canonical-console:candidate`（digest `sha256:1056df76…`，随包
`image-digest.txt`；本地 RAG 栈镜像为源码本地构建，不入发行物）。

2C2G / 4C8G / 8C16G 档位说明详见
[DOCKER-DEPLOY](distribution/docs/DOCKER-DEPLOY.md)。

## 本地 RAG 增强

- 语料库导入、索引、删除、备份恢复：[LOCAL-RAG-GUIDE](distribution/docs/LOCAL-RAG-GUIDE.md)
- 嵌入策略（`local-hash-v1` 默认 / `bge-m3` 可选）：[LOCAL-RAG-GUIDE](distribution/docs/LOCAL-RAG-GUIDE.md#嵌入策略)
- 状态与红线：[RAG-STATUS](distribution/docs/RAG-STATUS.md)

## 架构

详见 [distribution/docs/ARCHITECTURE.md](distribution/docs/ARCHITECTURE.md)

## API

详见 [distribution/docs/API-CONTRACTS.md](distribution/docs/API-CONTRACTS.md)

## 安全

详见 [distribution/docs/SECURITY.md](distribution/docs/SECURITY.md)

## 已知限制

详见 [distribution/docs/LIMITATIONS.md](distribution/docs/LIMITATIONS.md)

## Fixer/Verifier 边界

详见 [distribution/docs/FIXER-VERIFIER.md](distribution/docs/FIXER-VERIFIER.md)

## Enterprise Roadmap（未实现）

以下能力**当前版本不存在**，列为路线图，面向未来有相应组织投入的团队：
Secret Manager 集成、external attestation 服务端点、多租户隔离、C 链历史案例
检索、RUN_BINDING_AUTH 密钥分发。详见
[能力矩阵](distribution/docs/PUBLIC-CAPABILITY-MATRIX.md)。本项目当前没有
任何企业生产客户或企业生产接入。

## 旧版本说明

> 此仓库此前包含比赛/演示版本的代码（tag: `legacy/pre-v0.1.0`）。
> v0.1.0 是第一个产品化版本，架构和能力边界与旧版有显著差异。
> 旧版仅用于历史追溯，不代表当前生产架构。

## License

Apache 2.0 — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)
