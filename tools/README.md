# tools/ — 核心组件与开发工具

## 核心组件(生产链路)

| 目录 | 说明 | 状态 |
|---|---|---|
| [gh-bridge/](gh-bridge/) | webhook 交付台账→AgentTeams 播种/唤醒/kickoff→终态判读→check run 回写 | ✅ 生产验证 |
| [gh-app/](gh-app/) | GitHub webhook 接收端(HMAC 验签/防重放台账) + check run 发布器 + App 令牌管理 | ✅ 生产验证 |
| [workflow-controller/](workflow-controller/) | 完整 PR 生命周期控制器(PG 权威状态机/Transactional Outbox/审批票据) | 🔒 赛后集成 |
| [agentteams/](agentteams/) | 角色契约(CASE-MANIFEST 模板) + 团队管理 | ✅ |
| [cli/](cli/) | mergepilot CLI(安装/启动/状态) | ✅ |
| [m4f-runtime/](m4f-runtime/) | M4-F 确定性 Skill 运行时 | ✅ |

## Skill MCP
- 根目录 `skill-mcp-server.mjs` — Skill MCP 服务器(Agent 通过 MCP 按需调用确定性 Skill)

## 开发/验证/调试工具
| 类别 | 文件 | 说明 |
|---|---|---|
| 验证脚本 | `m3*-*.sh` `m4*-*.sh` | 各里程碑验证脚本 |
| 调试工具 | `room-recent.py` `list_rooms.sh` `delete_rooms.sh` | Matrix 房间操作 |
| 部署脚本 | `start-*.sh` `deploy-*.sh` | 各组件部署 |
| 观测工具 | `observe-demo*.py` `trace_aggregator.py` | 运行观测与 Trace 聚合 |
| E2E 工具 | `e2e-lib.sh` `collect_e2e_evidence.py` | 端到端测试与证据收集 |

> 决赛冲刺期间产生的大量编号脚本(m3b-b4c1_6-hardening.sh 等)为各阶段验证过程的保留,
> 核心链路代码在上方"核心组件"表中。
