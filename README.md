# MergePilot · 多 Agent PR 审修与风险治理闭环

> **不止提意见——把 PR 从审查推进到受治理的修复、验证、审批与回滚。**
> 高危变更强制人工审批，失败可回滚，全程可审计。以 [AgentTeams / HiClaw](https://hiclaw.io) 为可适配的 Agent runtime。

**当前状态**：复赛阶段（2026-08-11）。确定性控制面、6 Skill DAG、回滚链、最小权限 Gateway 与 **D2B-3 fail-closed Docker socket proxy** 均已验证。**D2B-3 真实 AgentTeams v1.2.2 生产 live 已通过（`hiclaw_live=true`，64/64 PASS）**。权威 evidence：[`evidence/m5/0d/hiclaw-v122-true-live-pass.json`](evidence/m5/0d/hiclaw-v122-true-live-pass.json)。所有声明均可在 [声明—证据矩阵](docs/初赛声明-证据矩阵.md) 中逐项核对。

---

## 为什么需要 MergePilot

- **单 Agent 审查只提意见，不做闭环**：误报堆积，修复仍靠人，验证与合并无人兜底。
- **高风险变更缺边界**：密钥、依赖升级、危险删除缺少审批、回滚与审计，难以满足合规。
- **LLM 编排本质不可靠**：阶段交接、幂等、崩溃恢复、并发隔离不能交给 Prompt 自觉。
- **凭证必须收敛**：让 Worker 零凭证是真实安全要求，不是可选项。
- **MergePilot 的方式**：确定性控制面 + Agent 语义决策 + 工具层权限门 + 失败可回滚 + 全程结构化审计。

## 系统架构

![MergePilot 系统架构：GitHub PR 经确定性控制面、多 Agent、Skill DAG、Policy Gateway 与 GitHub MCP 完成受治理闭环](docs/assets/mergepilot-architecture.svg)

- **唯一事实来源**是 PostgreSQL 状态机 + Outbox（Controller 持有任务状态、阶段转换、事件去重、超时与恢复）。
- **AgentTeams / HiClaw** 是当前适配的 Agent runtime 之一，负责语义决策与协作，**不是状态权威**。
- Agent 职责见 [`docs/附录A-Agent-Identity清单.md`](docs/附录A-Agent-Identity清单.md)，Skill 体系见 [`docs/附录B-Skill清单.md`](docs/附录B-Skill清单.md)。

## 已验证能力

| 能力 | 结果 | 证据 |
|---|---|---|
| 状态机 + Outbox + L2 审批 + 故障恢复 / 回滚 | B4e **43/43**、B5 **50/50**、M3-C **33/33** | [evidence/m3b-b4e/](evidence/m3b-b4e/README.md) · [evidence/m3b-b5/](evidence/m3b-b5/README.md) · [evidence/m3c/](evidence/m3c/README.md) |
| 最小权限 Policy Gateway | 8 类负向全 fail-closed（50/50） | [evidence/m3b-b5/](evidence/m3b-b5/README.md) |
| 6 Skill DAG（确定性子进程，非 LLM 自主调用） | diff-parse/risk-classify/sast-scan/test-runner/pr-lifecycle/case-retrieval，共 **481** 项测试 | [evidence/m4/](evidence/m4/) |
| AgentTeams 全链协议 E2E | **16/16** 门禁 + **6/6** 回归；6 Skill 全 SUCCEEDED | [evidence/m4/m4f/verification.txt](evidence/m4/m4f/verification.txt) |
| 真实 GitHub MCP（审查→修复→验证→合并） | PR #1 审查 → 修复 PR #3，5/5 resolved，squash 合并 | [docs/项目状态.md](docs/项目状态.md) |
| PostgreSQL 16 + pgvector（审计 / RAG） | 5 tasks / 6 findings / 3 decisions / 9 audit events；Docker E2E `all_passed=true` | [evidence/m4/m4e/](evidence/m4/m4e/README.md) |
| HiClaw 隔离 C3 十轮稳定性 | **10/10 PASS**（MergePilot-Test 隔离栈） | [evidence/m5/0c/c3-10x.json](evidence/m5/0c/c3-10x.json) |
| D2B-1 离线回归 | **17/17 + 6/6** | [evidence/m5/0d/offline-regression.json](evidence/m5/0d/offline-regression.json) |

## Benchmark（N=10×2，已冻结 · post-run 机器复算）

单 Agent（A）vs MergePilot 多角色（B），同模型 `deepseek-v4-flash`、synthetic fixtures、每对单次运行。

| 指标 | A 单 Agent | B MergePilot | Δ |
|---|---:|---:|---:|
| precision | 36.36% | 57.14% | +20.78 pp |
| **recall** | **70.59%** | **70.59%** | **0** |
| F1 | 48.00% | 63.16% | +15.16 pp |
| decision accuracy | 50.00% | 40.00% | **−10.00 pp** |
| false positives | 21 | 9 | −12 |
| tokens | 12062 | 16037 | +32.95% |
| API requests | 10 | 18 | +80% |
| infrastructure completion | 10/10 | 10/10 | 20/20 |
| semantic case pass | 2/10 | 3/10 | 5/20 |

> 这是 **controlled local orchestration（受控本地编排），不是真实 Gateway/controller/GitHub/HiClaw E2E**。
> 改善主要来自 FP 从 21 降至 9；**recall 两组相同，不证明多角色提高 recall**；B 的 decision accuracy 低于 A，风险处置校准仍需改进。
> C3 10/10 是独立的真实隔离栈证据，不与本 Benchmark 混为一项。详见 [`benchmark/formal-summary.md`](benchmark/formal-summary.md)。

## 安全与可靠性

- **Worker 不持有 GitHub PAT**：PAT 仅存在于 `github-mcp` 隔离 sidecar（私有 `mcp-backend-net`），Worker 经 `mcporter` 零凭证访问。
- **所有写操作经 Policy Gateway**：角色 token 认证 + 写路径约束 + INSERT-only 审计，跨角色 / 旁路 / 票据伪造均 fail-closed。
- **L2 高危强制人工审批**：审批票据（room/run/repo/pr/result_sha）+ CAS + 单次执行，未批准不合并。
- **fail-closed**：8 类负向场景 50/50 全部拒绝；Skill 对 raw secret / 超时 / 依赖故障显式失败，不静默降级。
- **状态持久化、幂等与恢复**：`idempotency_key` 去重、Controller 崩溃后从 PG 恢复、lease 异常对账、Gateway 降级→恢复（熔断）。
- **失败可回滚**：合并后验证失败 → `POST_MERGE_VERIFY_FAILED` → child-run revert → 复验（M3-C 33/33）。
- **证据可溯源**：evidence 绑定 `source_commit`；secret pattern 扫描 `real_credential_hits=0`。

## 快速开始

当前**可复现、不依赖生产 HiClaw** 的最短路径：

以下 Bash 命令需在 Linux、WSL2 或 Git Bash 环境执行；Python 命令使用项目支持的 Python 环境。

```bash
# 1) 6 Skill 确定性测试（宿主 Python，需 jsonschema）
bash tests/skills/run_all.sh

# 2) AgentTeams 协议级全链 E2E（fixture，16 门禁）
bash tests/m4f1/run_all.sh

# 3) Benchmark 离线校验（复算冻结产物，不发外部请求）
python benchmark/test_offline.py

# 4) 存储防膨胀 / guarded startup 单测（240 项）
python tests/hiclab/run_tests.py
```

> 早期 HiClaw v1.1.2 手动 Demo 路径（`tools/demo.sh` 等）依赖一个已存在的本地 HiClaw + DeepSeek 环境，已归档至 [`docs/README-历史运行记录.md`](docs/README-历史运行记录.md)，环境搭建见 [`docs/环境搭建-HiClaw-WSL.md`](docs/环境搭建-HiClaw-WSL.md)。

## 当前边界

- **`hiclaw_live=true`**（D2B-3 PASSED）：MergePilot Docker socket proxy 已在真实 AgentTeams v1.2.2 生产环境验证通过（64/64 PASS）。
- **OTel / SLS 未实现**（D2B-2 缺口）；**Nacos / RocketMQ 未接入**。
- **Benchmark 为受控本地评测**：N=10 小样本、单模型、synthetic fixtures、每对单次运行；不等于 E2E 完成率，不证明 recall 提升。
- **Manager 阶段交接偶需人工 nudge**；M5-0B 候选工作流已确定性闭环（14/14+13/13），但仅限候选 / 隔离栈，不可外推为生产零人工。
- **SAST 新旧两路径并存**：新版 `skills/sast_scan/`（87 测试、schema、fail-closed）与旧版 `skills/sast-scan/scan.py` 并存，新版属性不外推到旧版。
- **MIG-B4-001**：B4 链迁移非幂等，当前支持路径为 forward-only。

## 证据与文档

- [`docs/初赛证据索引.md`](docs/初赛证据索引.md) — 评审 3 分钟定位任意声明的证据
- [`docs/初赛声明-证据矩阵.md`](docs/初赛声明-证据矩阵.md) — 逐项声明 vs 限制（唯一措辞权威）
- [`docs/项目状态.md`](docs/项目状态.md) — 已验证 / 框架能力 / 规划的区分
- [`docs/复赛路线图.md`](docs/复赛路线图.md) — 后续里程碑与验收标准
- [`benchmark/formal-summary.md`](benchmark/formal-summary.md) — 正式 Benchmark 冻结结论（机器生成）
- [`docs/README-历史运行记录.md`](docs/README-历史运行记录.md) — 开发期排障与旧 Demo 路径（归档）

## 仓库结构

```
MergePilot/
├── README.md                         # 本文件
├── LICENSE                            # Apache 2.0
├── THIRD_PARTY.md                     # 依赖、费用、数据与替代方案
├── config/                            # team.yaml + Agent SOUL.md
├── skills/                            # 6 Skill DAG + common runtime + 旧版 sast-scan
├── tools/                             # 触发 / Trace / 看板 / 审计 / Gateway / 运维
├── tests/                             # skills / m4f1 / m5_0 / hiclab 等
├── benchmark/                         # N=10×2 数据集、raw-runs、冻结产物
├── evidence/                          # 机器可验的运行证据（按里程碑）
├── samples/                           # 样例 PR / fixture 输入与首轮输出
└── docs/                              # 设计、状态、路线图、附录、归档
```

## Roadmap

下一主线（详见 [`docs/复赛路线图.md`](docs/复赛路线图.md)）：OTel/SLS 可观测 → Benchmark N≥20 → 多仓库稳定性。D2B-3 socket proxy 已完成（`hiclaw_live=true`）。规划能力不写成已运行。

## 团队

队伍「分子」· 邱全安（队长，架构 / Agent 编排 / 风险门）· 彭明（Skill 与 MCP / Demo）· 何斌（基础设施 / 可观测 / 文档开源）。

## License

[Apache License 2.0](LICENSE)。本作品为 [GOAI Agent Infra 赛道](https://www.goaihz.com/tracks?track=infra)参赛项目。
