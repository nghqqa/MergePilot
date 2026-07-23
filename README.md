# MergePilot · 多 Agent PR 审修闭环

> **不止提意见——闭环修复,高危人控。**
> 把每一个 Pull Request 当作一次"代码事故":多 Agent 自动完成 审查 → 修复 → 验证,高风险变更强制人工审批,失败自动回滚,全程可审计。
> 以 **[AgentTeams / HiClaw](https://hiclaw.io)**(多 Agent 协同框架)为基点构建。

---

## 这是什么

MergePilot 是一个面向研发团队的 **PR 自动审查修复自治系统**。它不是"只提意见的 PR Bot",而是一个多 Agent 团队,把 PR 从接入一直推进到**可审计的合并**:

- **6 类职能**(Coordinator / Triage / Reviewer / Fix Planner / Fixer / Verifier),当前 MVP 由 **4 个运行 Agent** 承载(Triage 合并至 Coordinator、Fix Planner 合并至 Fixer)。
- **L0/L1/L2 三级风险自治**:低危自动修复合并、中危人工复核、**高危仅出方案并强制人工审批**,验证失败自动回滚。
- **真实工具**:Reviewer 调用自研 [`sast-scan`](skills/sast-scan/)(正则密钥 + AST 注入 + 依赖漏洞)。
- **全程可审计**:执行 Trace、审查/修复/验证报告、补丁说明、可视化看板。

## 为什么

- PR review 排队是研发效率瓶颈;现有 AI 工具只提意见,不做闭环修复。
- 依赖升级、密钥、删除等高风险变更缺少审批、回滚与审计边界,难以满足合规。
- MergePilot 的差异:**把修复纳入可治理闭环**——做完,并管住高危。

## 架构(8 段闭环 + 2 条回路)

```
PR 接入 → 任务拆解 → 上下文共享 → Skill/MCP 工具调用 → 验证 → 证据沉淀 → 审批/回滚 → RAG 沉淀
                ↑ Review–Fix 协商回路 / Fix–Verify 回退回路 ↓
```

详细:6 Agent 职责见 [`docs/附录A-Agent-Identity清单.md`](docs/附录A-Agent-Identity清单.md);Skill 体系见 [`docs/附录B-Skill清单.md`](docs/附录B-Skill清单.md)。

## 🔥 架构修复(2026-07-23):Manager 必须是 OpenClaw

**根因**:AgentTeams v1.1.2 安装器默认 Manager = QwenPaw(copaw),其消费者/会话管理不可靠 → Manager 从不处理消息、@mention 打错、流程跑偏。

**修复**:安装时选 **Manager 运行时 = OpenClaw**(第 10 步,不要选默认的 QwenPaw)。一个设置,解决所有可靠性问题。

**验证**:全 OpenClaw 架构(Manager + 3 独立 Worker,零 copaw)首次可靠跑通完整闭环:review(6 findings, sast-scan 实测)→ fix(L2 挂人审、中低危自动修)→ verify(重扫全清洁)→ **MERGE ✅**

## 当前状态(已验证 vs 规划)

**已验证(MVP)**:
- 4 个运行 Agent 在 HiClaw + DeepSeek 上端到端跑通 review→fix→verify
- Reviewer 调真实 `sast-scan`,findings 标注工具来源
- 风险分级自治:L2 高危挂人审、批准后执行并合并;验证通过、未触发回滚(回滚作为失败分支设计能力保留)
- Verifier 完成 40 项样例检查;产出执行 Trace + 证据产物 + 可视化看板
- 可靠触发:经系统 Manager 路由(诊断并绕过直戳 leader 的不稳问题)

**规划中(复赛/生产)**:真实 GitHub/CI 写操作、Nacos、PolarDB-PG、RocketMQ、RAG、AgentLoop/SLS 实时可观测、阿里云官方 `alibabacloud-sls-query` Skill。

> 不得将规划能力表述为当前已运行。

## 快速开始

**前置**:WSL2 + Docker、HiClaw(AgentTeams)、DeepSeek API Key(OpenAI 兼容)。详见 [`docs/环境搭建-HiClaw-WSL.md`](docs/环境搭建-HiClaw-WSL.md)。

```bash
# 1) 按 runbook 在 HiClaw 建 Team mergepilot(coordinator + reviewer/fixer/verifier),注入 4 份 SOUL
#    见 docs/原型搭建-Team重建.md

# 2) 经 Manager 路由提交一条样例 PR(可靠触发)
MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp tools/submit_pr_manager.py hiclaw-manager:/tmp/ && \
  docker cp samples/input/pr-01.md hiclaw-manager:/tmp/pr.md && \
  docker exec hiclaw-manager python3 /tmp/submit_pr_manager.py /tmp/pr.md <admin_password>'

# 3) 汇总 Trace 与看板
python tools/trace_aggregator.py
python tools/make_dashboard.py
python tools/audit_trail.py
```

## 仓库结构

```
MergePilot/
├── README.md
├── LICENSE                      # Apache 2.0
├── config/                      # team.yaml + 4 个运行 Agent 的 SOUL.md
├── skills/sast-scan/            # 真实 SAST Skill(纯标准库)
├── tools/                       # 触发 / Trace / 看板 / 审计 / 房间运维
├── samples/                     # 样例 PR 输入 + 实跑证据输出
└── docs/                        # 环境搭建、Team 重建 runbook、Demo 剧本、设计附录 A/B
```

## 工具链

| 组件 | 角色 | 状态 |
|---|---|---|
| AgentTeams (HiClaw) | 多 Agent 协同基点(必选) | 已验证 |
| Higress | AI 网关,Worker 仅持 consumer-token | 随框架 |
| DeepSeek `deepseek-v4-flash` | LLM(OpenAI 兼容) | 已验证 |
| sast-scan | 自研真实 SAST Skill | 已验证 |
| PolarDB-PG / Nacos / RocketMQ / AgentLoop / SLS | 数据/治理/消息/可观测 | 规划接入 |

## License

[Apache License 2.0](LICENSE)。

## 团队

队伍「分子」· 邱全安(队长,架构/Agent 编排/风险门)· 彭明(Skill 与 MCP/Demo)· 何斌(基础设施/可观测/文档开源)

---

> 本项目为 [GOAI · Agent Infra 赛道](https://www.goaihz.com/tracks?track=infra)参赛作品。
