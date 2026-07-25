# MergePilot · 多 Agent PR 审修闭环

> **不止提意见——闭环修复,高危人控。**
> 把每一个 Pull Request 当作一次“代码事故”：Manager 编排多 Agent 推进审查 → 修复 → 验证，高风险变更强制人工审批，失败回滚执行链已实测，全程可审计。
> 以 **[AgentTeams / HiClaw](https://hiclaw.io)**(多 Agent 协同框架)为基点构建。

---

## 这是什么

MergePilot 是一个面向研发团队的 **多 Agent PR 协同审修系统**。它不是"只提意见的 PR Bot",而是一个多 Agent 团队,把 PR 从接入一直推进到**可审计的合并**:

- **6 类职能**(Coordinator / Triage / Reviewer / Fix Planner / Fixer / Verifier),当前 MVP 由 **4 个运行 Agent** 承载(Triage 合并至 Coordinator、Fix Planner 合并至 Fixer)。
- **L0/L1/L2 三级风险治理**：低危可自动推进、中危人工复核、**高危仅出方案并强制人工审批**；验证失败进入回滚分支，当前由脚本显式触发。
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

## 当前环境的运行时经验(2026-07-23)

**现象**:在当前本地 AgentTeams v1.1.2 环境中，默认 QwenPaw Manager 出现消费者/会话处理不可靠。

**处理**:安装时将 Manager 运行时切换为 OpenClaw。该配置在本环境中让 review→fix→verify 处理稳定；这是环境范围内的验证结论，不作为所有版本和部署的普遍要求。

**验证**:全 OpenClaw 架构(Manager + 3 独立 Worker)完成双场景：PR #42 发现 6 项问题并最终 MERGE(人审后)；PR #43 发现 4 项问题、确认 3+ 已知 CVE，裁定 HOLD 并明确 Do not merge。

### GitHub MCP 接入经验(2026-07-24)

**目标**:让 Reviewer/Fixer 经 `mcporter` 读取真实 GitHub PR 与源码,且 Worker 不持有 GitHub 凭证(与 quickstart Step 7「Higress 托管 MCP、PAT 集中保管、Worker 用 mcporter」的架构目标一致)。

**遇到的问题**:官方 `setup-mcp-server.sh` / 安装器 `setup-higress.sh` 通过 `PUT /v1/mcpServer` 把工具定义(`rawConfigurations` + `accessToken`)写入 Higress。在本机 v1.1.2 环境中,该 PUT 返回 200 并回显配置,但 `rawConfigurations` 不持久化(GET 回空),mcp-server 插件拿不到工具/凭证,请求被透传到 `api.github.com` 返回 400。安装器与 setup 脚本使用同一段写入代码,因此重装未必能解决。

**采用的方案(凭证隔离桥)**:自建 `github-mcp-bridge` 镜像,以 `mcp-proxy` 把 GitHub 官方 MCP server(stdio)桥接为网络 SSE 服务,**GitHub PAT 仅存在于桥容器 env**(经 `--pass-environment` 转发给 stdio 子进程)。Worker 经 `mcporter` 连 `http://github-mcp:8082/sse`,**不持有任何 GitHub 凭证**——保住了「Worker 零凭证」的安全属性。该结论限定于当前本地环境与版本。

**验证**:Worker 经 MCP 读到 `nghqqa/mergepilot-test` 仓库 `feature/vulnerable-pr` 分支的真实代码(SQLi + 硬编码密钥),与 `sast-scan` 检测点对齐;并已实测完整写链路 —— 建修复分支 `fix/security-hardening`、写入修复版 `user_service.py`、提修复 [PR #2](https://github.com/nghqqa/mergepilot-test/pull/2) 并回读校验修复内容。44 个 GitHub 工具(读 PR、建分支、提 PR、合并等)可用。

### 自主编排与任务隔离(2026-07-25,已验证)

**① handoff 零-nudge(确定性 watcher)**:Manager 的 LLM 编排不可靠(即便 SOUL 有显式状态机,review 后仍停)。改用**确定性 handoff watcher**(`tools/handoff_watcher_v2.py`)——常驻 manager 容器,动态发现 Matrix 房间,检测 `TASK_COMPLETED` 后向下一阶段 worker 发**真 @mention** 驱动(经实测:worker 只认真 mention 胶囊,不认纯文本 @)。已在干净环境端到端验证:一次提交 → review→fix→verify→裁定,全程零人工 nudge。

**② per-task room 任务隔离**:每个 PR 建专属 Matrix 任务房间(`tools/submit_pr_taskroom.py`)→ OpenClaw 按 Matrix 房间隔离 session(session key = `agent:main:matrix:channel:<room_id>`,实测)→ 零跨-PR 上下文污染。已验证全链路在单个隔离任务房间内跑通,不再需要重创 worker。

**关键工程结论**:① LLM 编排本质不可靠,确定性 watcher 是正解(不是 SOUL 状态机);② OpenClaw session 按房间隔离是框架白拿的能力,per-task room 设计天然解决上下文串味;③ worker 只响应真 @mention 胶囊(`formatted_body` + `m.mentions`)。

## 当前状态(已验证 vs 规划)

**已验证(MVP)**:
- 4 个运行 Agent 在 HiClaw + DeepSeek 上跑通 review→fix→verify；Manager 负责编排，阶段交接偶需人工 `nudge`
- Reviewer 调真实 `sast-scan`,findings 标注工具来源
- 风险分级治理：L2 高危挂人审、批准后执行并合并；**回滚执行链已实测**（坏修复 → sast 判 FAIL → revert commit → 复验 PASS），当前由脚本触发，见 [`evidence/rollback-demo/`](../evidence/rollback-demo/)
- Verifier 完成 40 项样例检查;产出执行 Trace + 证据产物 + 可视化看板
- 可靠触发:经系统 Manager 路由(诊断并绕过直戳 leader 的不稳问题)
- 双场景:PR #42 MERGE(人审后)；PR #43 HOLD / Do not merge，未记录最终 REJECT 动作
- **真实 GitHub MCP 接入(凭证隔离)**:reviewer/fixer/verifier 经 `mcporter` 调用 GitHub 官方 MCP server,**GitHub PAT 仅存于隔离 sidecar,Worker 零 GitHub 凭证**。已验证读 + 写闭环:读到 `mergepilot-test` 的 SQLi + 硬编码密钥代码(命中 sast-scan 检测点)→ 经 MCP 建修复分支、写修复代码、提修复 PR → 经 `merge_pull_request` **合并**(PR #3 已 squash 合并);4 个写操作(create_branch / create_or_update_file / create_pull_request / merge_pull_request)全部实测通过。**并由 Manager 编排，reviewer→fixer→verifier 经 MCP 完成真实 PR #1 端到端审修；阶段交接偶需人工 `nudge`**(reviewer 6 findings、fixer 真实 [PR #3](https://github.com/nghqqa/mergepilot-test/pull/3)、verifier ✅ PASS),证据见 [`docs/项目状态.md`](docs/项目状态.md)

**已验证的生产化底座**：本地 PostgreSQL 16 + pgvector 已完成结构化审计与 RAG 小样本召回，兼容迁移至 PolarDB-PG。**规划中（复赛/生产）**：Nacos、RocketMQ、AgentLoop/SLS 实时可观测、阿里云官方 `alibabacloud-sls-query` Skill。

> 不得将规划能力表述为当前已运行。
>
> **安全说明**:包内出现的 `sk-live-1234567890abcdef`、`sk-test-*` 等字符串均为确定性 fixture / 测试数据，用于验证密钥检测规则，不是真实凭证。

## 提交包证据索引

| 核查项 | 位置 |
|---|---|
| 运行入口 | `tools/submit_pr_manager.py`、`tools/submit_manager_orchestrate.py` |
| Demo 环境准备 | `tools/demo_prepare.sh` |
| 依赖与数据边界 | 本 README、`THIRD_PARTY.md` |
| Agent 配置 | `config/team.yaml`、`config/souls/*/SOUL.md` |
| 样例输入 | `samples/input/`；提交包另含 `samples/input/fixtures/` |
| 首轮基准证据 | 提交包 `evidence/code-audit-20260722-*`、`samples/output/dashboard.html` |
| 双场景证据 | 提交包 `evidence/mergepilot-openclaw-run/`、`docs/双场景验证摘要.md` |

## 快速开始

**前置**:WSL2 + Docker、HiClaw(AgentTeams)、DeepSeek API Key(OpenAI 兼容)。详见 [`docs/环境搭建-HiClaw-WSL.md`](docs/环境搭建-HiClaw-WSL.md)。

```bash
# 1) 按 runbook 在 HiClaw 建 Team mergepilot(coordinator + reviewer/fixer/verifier),注入 4 份 SOUL
#    见 docs/原型搭建-Team重建.md

# 2a) 原 Team 路径:经 Manager 路由提交一条样例 PR
MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp tools/submit_pr_manager.py hiclaw-manager:/tmp/ && \
  docker cp samples/input/pr-01.md hiclaw-manager:/tmp/pr.md && \
  docker exec hiclaw-manager python3 /tmp/submit_pr_manager.py /tmp/pr.md <admin_password>'

# 2b) 全 OpenClaw 双场景路径:由系统 Manager 直接编排 reviewer/fixer/verifier
MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp tools/submit_manager_orchestrate.py hiclaw-manager:/tmp/ && \
  docker exec hiclaw-manager python3 /tmp/submit_manager_orchestrate.py <admin_password>'

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
├── THIRD_PARTY.md               # 依赖、费用、数据与替代方案
├── config/                      # team.yaml + 4 个运行 Agent 的 SOUL.md
├── skills/sast-scan/            # 真实 SAST Skill(纯标准库)
├── tools/                       # 触发 / Trace / 看板 / 审计 / 房间运维
├── samples/                     # 样例 PR / fixture 输入 + 首轮实跑证据输出
├── evidence/                    # 提交包内附双场景 findings/fix/verify/trace
└── docs/                        # 环境搭建、Team 重建 runbook、Demo 剧本、设计附录 A/B
```

## 工具链

| 组件 | 角色 | 状态 |
|---|---|---|
| AgentTeams (HiClaw) | 多 Agent 协同基点(必选) | 已验证 |
| Higress | AI 网关(LLM/路由),Worker 仅持 consumer-token | 随框架 |
| DeepSeek `deepseek-v4-flash` | LLM(OpenAI 兼容) | 已验证 |
| sast-scan | 自研真实 SAST Skill | 已验证 |
| GitHub MCP(凭证隔离桥) | 真实 GitHub 读/写/合并;GitHub PAT 仅存 sidecar,Worker 经 mcporter 零凭证访问 | 已验证(读+写+合并) |
| PostgreSQL 16 + pgvector（兼容 PolarDB-PG） | 审计事件/finding/decision 结构化沉淀 + 5 条知识项的小样本经验召回 | 本地已验证；未连接 PolarDB 云实例 |
| Nacos / RocketMQ / AgentLoop / SLS | 配置治理/消息/实时可观测 | 规划接入 |

## License

[Apache License 2.0](LICENSE)。

## 团队

队伍「分子」· 邱全安(队长,架构/Agent 编排/风险门)· 彭明(Skill 与 MCP/Demo)· 何斌(基础设施/可观测/文档开源)

---

> 本项目为 [GOAI · Agent Infra 赛道](https://www.goaihz.com/tracks?track=infra)参赛作品。
