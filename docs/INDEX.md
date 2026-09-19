# 文档导航

## 快速了解
| 文档 | 内容 |
|---|---|
| [README.md](../README.md) | 项目主页（快速开始 / 三条安全路径 / 十二轮运行 / 架构图） |
| [docs/architecture/](architecture/) | 架构图（SVG 源文件） |
| [PRODUCT.md](../PRODUCT.md) | 产品定位 |
| [DESIGN.md](../DESIGN.md) | 系统设计概要 |

## 部署与运行
| 文档 | 内容 |
|---|---|
| [DEPLOY.md](../DEPLOY.md) | 隔离栈三种部署方式（离线镜像 / compose / GHCR） |
| [docs/deploy/](deploy/) | 部署手册（webhook 入口 / 全链恢复脚本） |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | 源码开发与本地测试 |
| [docker/](../docker/) | 全部镜像配方 + 运行时 hooks |

## 核心组件
| 目录 | 说明 |
|---|---|
| `tools/gh-bridge/` | webhook 台账→AgentTeams→check run 回写桥 |
| `tools/gh-app/` | webhook 接收端(HMAC 验签) + check run 发布器 + GitHub App 令牌 |
| `tools/workflow-controller/` | 完整 PR 生命周期控制器(赛后集成) |
| `skills/` | 6 个确定性 Skill(纯计算/schema 校验/fail-closed) |
| `evidence/` | 36 个 SHA256SUMS 锁定证据包(十二轮真实运行) |

## 历史设计文档(M1–M7 开发阶段)
以下为开发过程中各里程碑的设计冻结与验收文档,按阶段归档保留:
- `M3-*` / `M4-*` / `M5-*` / `M6-*` / `M7-*` — 各阶段设计冻结与实现
- `ISOLATED-LIVE-*` — 隔离栈 PG/验证相关设计
- `D2B-3-*` — Docker Socket Proxy 设计

## 比赛材料
- `docs/决赛优化/` — 决赛阶段优化文档
- `docs/复赛材料/` — 复赛阶段材料
- `docs/showcase/` · `docs/preview/` — 展示与预览资产

## 其他
- `docs/archive/` — 早期归档
- `docs/复赛路线图.md` · `docs/项目状态.md` — 阶段性状态文档
