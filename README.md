# MergePilot v0.1.0 — 安全审查工作台

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

自托管的 PR 安全审查、发现管理和修复验证平台，带完整审计追踪。

## 当前能力

| 能力 | 状态 |
|---|---|
| Review Agent（只读 PR 审查 + 阶段推导） | ✅ |
| Console（实时控制台 + 服务端会话 + 仓库 allowlist） | ✅ |
| A 链组织知识检索（词法，reference-only） | ✅ |
| Fixer / Verifier（隔离环境中的修复与独立验证） | ⚙️ 受控 |
| Docker 一键部署 | ✅ |

## 能力边界（诚实声明）

- ❌ **不自动 merge** — 所有合并决策由人工执行
- ❌ **不自动 approve** — 审批由人工操作
- ❌ **完整 RAG 未完成** — C 链历史案例检索处于 BLOCKED
- ❌ **生产自动修复未全量启用** — Fixer/Verifier 仅在隔离环境运行
- ❌ **embedding 未启用** — 不下载、不生成向量
- ❌ **GitHub 写入默认关闭** — 只读审查

## Quickstart

```bash
cd distribution/docker
cp .env.example .env
# 编辑 .env 填入生成的密钥
docker compose up -d
# 访问 http://127.0.0.1:4730
```

详细指南见 [distribution/docs/QUICKSTART.md](distribution/docs/QUICKSTART.md)

## Docker 部署

镜像：`ghcr.io/<your-org>/mergepilot-console:v0.1.0`

详见 [distribution/docs/DOCKER-DEPLOY.md](distribution/docs/DOCKER-DEPLOY.md)

## 架构

详见 [distribution/docs/ARCHITECTURE.md](distribution/docs/ARCHITECTURE.md)

## API

详见 [distribution/docs/API-CONTRACTS.md](distribution/docs/API-CONTRACTS.md)

## 安全

详见 [distribution/docs/SECURITY.md](distribution/docs/SECURITY.md)

## 已知限制

详见 [distribution/docs/LIMITATIONS.md](distribution/docs/LIMITATIONS.md)

## RAG 状态

详见 [distribution/docs/RAG-STATUS.md](distribution/docs/RAG-STATUS.md)

## Fixer/Verifier 边界

详见 [distribution/docs/FIXER-VERIFIER.md](distribution/docs/FIXER-VERIFIER.md)

## 旧版本说明

> 此仓库此前包含比赛/演示版本的代码（tag: `legacy/pre-v0.1.0`）。
> v0.1.0 是第一个产品化版本，架构和能力边界与旧版有显著差异。
> 旧版仅用于历史追溯，不代表当前生产架构。

## License

Apache 2.0 — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)
