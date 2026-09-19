# MergePilot：多 Agent PR 审修闭环

**多 Agent 代码与数据库变更安全闭环系统**

PR 一提交，webhook 自动触发审查：AI 独立复现、确定性 Skill 算事实、RAG 查组织规范、
结论以 check run 回写 PR（与 CI 并排）。普通变更自动完成；高危变更停下等人工；
系统无 merge 权限——合并永远归维护者。

[![AgentLoop Cloud Trace](https://img.shields.io/badge/AgentLoop_Cloud_Trace-VERIFIED_LIVE_CLOUD-brightgreen)](#agentloop-云端-trace)
[![Three Safety Paths](https://img.shields.io/badge/Three_Safety_Paths-VERIFIED-brightgreen)](#三条安全决策路径)
[![RAG](https://img.shields.io/badge/RAG-SYNTHETIC_DEMO-blue)](#rag-检索能力)
[![Database Branch](https://img.shields.io/badge/Database_Branch-SIMULATED-yellow)](#polardb-与-branch-边界)
[![PolarDB](https://img.shields.io/badge/PolarDB-NOT_CONNECTED-red)](#polardb-与-branch-边界)
[![PR Auto Merge](https://img.shields.io/badge/PR_Auto_Merge-DISABLED-red)]
[![selftest](https://github.com/nghqqa/MergePilot/actions/workflows/selftest.yml/badge.svg)](https://github.com/nghqqa/MergePilot/actions/workflows/selftest.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

> 上排红黄徽章不是缺陷，是**如实声明的当前边界**——本项目把"还没做到的"钉在第一屏。

## 演示

**Demo 视频（75 秒 · 1080p · 中文旁白，早期版本界面）**：[Watch Demo](https://github.com/nghqqa/MergePilot/releases/download/v0.2.0/MergePilot-demo.mp4)

![MergePilot 决赛演示：三案例主线（PR #1 低风险自动 / PR #2 高危批准 VERIFIED / PR #3 高危拒绝 blocked），webhook 自动触发 + Skill/RAG/AgentLoop 全链，结论回写 PR check run；另附机制验证与数据库迁移附录，每张卡标注执行性质与证据等级](docs/assets/readme/portfolio-overview-v3.png)

**在线体验**：`git clone` 后执行 `cd demo-platform && node backend/server.mjs`，访问 `http://127.0.0.1:4173`（若该端口落在 Windows 保留段被拒，服务会自动顺延并在控制台打印实际地址）。零第三方依赖，无需 `npm install`。也可下载离线演示包（两案例最新版，含 25 个 SHA256SUMS 锁定证据目录与一键启动脚本，见 Releases）。

## 要解决的问题

AI 改代码很快，但企业不敢让它碰生产环境：出了高危漏洞谁负责？改坏了怎么追溯？MergePilot 解决的是"敢让 AI 改"这件事——每一步有审计，高危必须人工点头，拒绝就真的停下。

## 快速开始

**推荐顺序**：① 先跑演示平台（零依赖，无需 Docker）；② 需要完整隔离栈时，用离线镜像包 + 一键启动器（外部 Windows 机器已两轮实测 10/10）；③ 有网络时可选用 GHCR 镜像（预留，见 DEPLOY.md C）。

### 演示平台（推荐，零依赖）

```bash
git clone https://github.com/nghqqa/MergePilot.git
cd MergePilot/demo-platform
node backend/server.mjs        # 打开 http://127.0.0.1:4173
```

Node.js ≥ 18 即可，无需 `npm install`：前端已预构建（`frontend/dist/`），两个核心案例（确定性 Skill + RAG + AgentLoop 追踪）的回放证据随仓库分发（`evidence/FINALS-ELEM-*` 等 36 个 SHA256SUMS 锁定目录，含十二轮真实运行）。Windows 也可直接双击 `demo-platform/start-demo.bat`。

自测：`node backend/test/selftest.mjs`（57 项：脱敏、回放完整性、API 契约、无泄密扫描、证据等级与版本绑定闸门；[CI 在 Node 18/20/22 上自动运行](https://github.com/nghqqa/MergePilot/actions/workflows/selftest.yml)）。

不想 clone？下载离线演示包（含两案例证据与一键启动，见 [Releases](https://github.com/nghqqa/MergePilot/releases)），解压后双击 `start-demo.bat` 或执行 `node */backend/server.mjs`。

### 完整隔离栈（Agent 运行环境）

九轮真实运行使用的 Agent 运行环境（非演示平台，需要 Docker）：

| 组件 | 说明 | 状态 |
|---|---|---|
| AgentTeams 控制面 | Worker CR 调解 + Matrix 消息 + MinIO 存储 + Higress 网关 | 十二轮验证 |
| 4 个 CoPaw Worker | Leader / Reviewer / Fixer / Verifier（动态容器，自举激活） | 十二轮验证 |
| AgentLoop 埋点 | OTel span 直连 SLS + 跨 Agent traceparent 关联 | 十二轮验证 |
| RAG MCP | 组织规范知识库（citation-only） | SK2 验证 |
| Skills MCP | 5 个确定性 Skill（diff_parse / risk_classify / sast_scan / test_runner / case_retrieval） | SK3–SK5 验证 |
| 案例知识库 | PostgreSQL 16 + pgvector，7 条真实历史案例 | SK4 接通 |
| **Webhook 入口** | GitHub webhook → HMAC 验签 → 交付台账（公网 HTTPS，2C2G 自托管） | WH 轮验证 |
| **gh-bridge** | 台账消费 → 项目播种 → 唤醒 → kickoff → 终态判读（`tools/gh-bridge/`） | WH 轮验证 |
| **结论回写** | GitHub App（仅 Checks 读写）check run 回写 PR，与 CI 并排 | WH 轮验证 |

历史离线交付（v0.2.1 镜像包）保留在 [Releases](https://github.com/nghqqa/MergePilot/releases)——含 9 镜像 zstd 单包、配置包与一键启动器，外部 Windows 机器 10/10 验收通过。**当前 Agent 运行镜像 `copaw-worker:223ddc2-agentloop-v4boot` 为最新版**——OTel/Skill/RAG 激活自举（配置从 worker 自有 MinIO 前缀拉取，密钥不进镜像），冷容器出生即全链；构建配方见 `docker/Dockerfile.v4boot`（前一版保留于 `docker/Dockerfile.v3skills`，全部配方在 `docker/` 目录）。

仓库内的 `docker-compose.yml` 与 `docker/Dockerfile.*` 是隔离栈的构建配方。源码开发与本地测试命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 三条安全决策路径

| 路径 | 案例 | 行为 | 验证点 |
| --- | --- | --- | --- |
| **自主完成** | PR #1（普通变更） | AI 审查 → 修复 → 验证，全自动完成，无人工介入 | 项目 completed · PR 保持 OPEN |
| **批准后完成** | PR #2（高危 · CWE-22） | 人工安全门 → 批准 → 修复 → 探针验证（200→404） | 修复有效 · PR 保持 OPEN |
| **拒绝后停止** | PR #3（严重 · CWE-78） | 人工拒绝 → 永久 BLOCKED → 409 终态 | 拒绝不可翻转 · 审计归档 |

> 2026-09-19（收官轮）：三条路径全部由**真实 GitHub webhook 自动触发**复核——结论以 check run 回写 PR（PR #2 ✅ success / PR #3 ❌ failure / PR #1 ✅ success），证据见 [FINALS-ELEM-WH-MASTER-README](evidence/FINALS-ELEM-WH-MASTER-README.md)。
> 2026-09-16：三条路径已在隔离栈上以**真实 AgentTeams 多 Agent 运行**复核——批准路径干净复跑（R2，见下节）、拒绝路径真实执行（零派发，三层证据）、自主路径沿用既有证据。

## 决赛真实运行（2026-09-16 → 09-19 共十二轮）：独立审计 → 修正 → 多轮复现 → Skill 全链 → 三路径 → webhook 自动闭环

十二轮真实 AgentTeams 运行（非回放：真实派单、真实 deepseek-chat 调用、真实容器内执行、事件级留痕），并经**独立第三方审计**。演进主线（每轮证据锁定在 `evidence/`）：R1 审计修正 → R2 干净复跑 → R3t/V3 追踪接入 → RAG 接入 → Skill 集成 → 案例库接通 → 三路径覆盖 → **收官轮 webhook 全自动闭环**。

| 运行 | 结果 | 关键验收 | 证据 |
| --- | --- | --- | --- |
| **R2 主案例**（PR #2，CWE-22） | 非预设 SPEC 下 Reviewer 独立确认 HIGH → 现场人工门批准 → Leader 发出全新委派事件 → Verifier 独立验证 **VERIFIED PASS** | 门后**新委派事件** $uZ4Rj1SI…（≠任何被作废 ID），Fixer 零操作员介入 | [FINALS-ELEM-PR2-LIVE-20260916-R2](evidence/FINALS-ELEM-PR2-LIVE-20260916-R2) |
| **真实拒绝**（PR #3，CWE-78 RCE） | 操作员拒绝修复授权 → fix/verify **从未派发**，项目 blocked（消息/网关/状态存储三层印证） | 拒绝为终态，不可翻转 | [FINALS-ELEM-PR3-LIVE-20260916](evidence/FINALS-ELEM-PR3-LIVE-20260916) |
| **R1 首跑**（PR #2，同日早前） | 技术结论成立；门后派发链瑕疵被独立审计指出 → **如实修正**（AUDIT 修正版 v2），并以 R2 复跑闭环 | 审计发现→修正→复跑的完整记录 | [FINALS-ELEM-PR2-LIVE-20260916](evidence/FINALS-ELEM-PR2-LIVE-20260916) + AUDIT.md |

中期四轮（RAG/SK3/SK4/SK5，2026-09-17 → 09-18）完成能力接入，要点：RAG MCP 三角色真实调用（组织规范 citation-only）；确定性 Skill 经 MCP 真实调用（span 实测 ×10→15，`skill_risk_classify` 建议 L1 vs Agent 自主 HIGH 的分歧如实入档）；**skill_case_retrieval 返回真实历史案例**（pgvector 接通）；**skill_sast_scan 首次正式调用**；**PR #1 低风险自动路径首通**（62 秒）；**dual-reviewer 评审间信度**（两独立 Reviewer 同 PR 结论完全一致）。证据：[RAG](evidence/FINALS-ELEM-PR2-RAG-TRACED) · [SK3](evidence/FINALS-ELEM-PR2-SK3-TRACED) · [SK4](evidence/FINALS-ELEM-PR2-SK4-TRACED) · [SK5×3](evidence/FINALS-ELEM-PR2-SK5-TRACED) · [DUAL-REVIEWER](evidence/DUAL-REVIEWER-EXP-20260919)。

**收官轮（WH，2026-09-19）——webhook 自动闭环，四链齐备**：

| 案例 | 触发 | 结论（check run 回写 PR） | 关键验收 | 证据 |
| --- | --- | --- | --- | --- |
| PR #2 批准 | synchronize webhook | ✅ success：HIGH → 门批准 → 修复 → **VERIFIED** | 补丁 sha256 第 11 次一致（Verifier 独立复现）；窗口 skill ×8 | [PR2-WH](evidence/FINALS-ELEM-PR2-WH-20260919) |
| PR #3 拒绝 | synchronize webhook | ❌ failure：门拒绝 → blocked 零派发 | **rag ×2 命中 org-standards/cwe-78-command-injection.md**（确认发现后查规范，references only） | [PR3-WH](evidence/FINALS-ELEM-PR3-WH-20260919) |
| PR #1 自动 | synchronize webhook | ✅ success：NOT_CONFIRMED/LOW → 自动完成 | 63 秒；skill ×2 | [PR1-WH](evidence/FINALS-ELEM-PR1-WH-20260919) |

链路：`GitHub webhook → HMAC 验签(mergepilot.nghqqa.cn) → 交付台账 → gh-bridge(播种/唤醒/kickoff) → AgentTeams 真实执行(Skill+RAG+案例库) → result.md 权威判读 → GitHub App(仅 Checks 读写) check-run 回写 PR`。门决策为操作员投递（MinIO 记录+Matrix 指令），修复与验证全 Agent 执行；App 无 Contents Write / merge 权限。

补丁确定性：PR #2 的修复补丁在十一轮独立运行中 sha256 逐字节一致（`674356fc…16081`），收官轮 Verifier 全新 clone 独立复现同哈希——同一漏洞的确定性修复，多轮互证。

**三路径完整覆盖**（SK5 达成）：批准（PR #2 → VERIFIED completed）· 拒绝（PR #3 → blocked 零派发）· **自动**（PR #1 → NOT_CONFIRMED/LOW → 无人工门 → auto completed）。

**Dual-reviewer 评审间信度**（SK5）：两个独立 Reviewer（不同容器、不同账号、不同 session）对同一 PR #2 head SHA 独立审查，结论完全一致：FINDING_CONFIRMED / HIGH / CWE-22 / HVR:YES——多角色对抗结构不可替代性的统计级证据。

配套：**角色契约 v1.0**（Leader/Reviewer/Fixer/Verifier 跨案例冻结，新案例只填 [CASE-MANIFEST](tools/agentteams/roles/CASE-MANIFEST.template.md)）见 [tools/agentteams/roles/](tools/agentteams/roles/)；R2 用量 88 次调用 / 输入 4.65M token（98.9% 缓存命中）/ ≈¥1.1–2.5，全程网关日志逐条可查。

## Agent 协同设计

Reviewer、Fixer、Verifier 三个 Agent 职责分离、互相制衡：

- **Reviewer** 只做语义判断与风险分级（NORMAL / HIGH / CRITICAL 三级；`benchmark/` 数据集对同一维度标注为 L0 / L1 / L2），不修改代码
- **Fixer** 在被派发前保持 LOCKED，高危时必须等人工门放行
- **Verifier** 用独立探针复核修复效果（不信任 Fixer 的自述），结果写入 MinIO 证据
- **人工安全门**：高危时系统自动暂停（Fixer/Verifier LOCKED），批准或拒绝均为真实人工记录，拒绝后 409 终态不可翻转
- **DAG 依赖**：review-1 → fix-1 → verify-1，逐级锁定防止越权

Agent 只承担语义判断，六类 Skill 以 Schema、deadline、错误码和 fail-closed 合同执行。Workflow Controller 负责状态机、确定性交接、CAS、超时 HOLD 和回滚；Policy Gateway 负责 ALLOW/DENY/HOLD；GitHub MCP 是隔离服务，PAT 不进入 Worker。

角色行为契约 v1.0（Leader/Reviewer/Fixer/Verifier 跨案例冻结，新案例只填 [CASE-MANIFEST](tools/agentteams/roles/CASE-MANIFEST.template.md)）见 [tools/agentteams/roles/](tools/agentteams/roles/)。

想在自己的环境复用这套 Agent 设计与 Skill？按投入分四级（验证 / 单独跑 Skill / 完整闭环 / 指向你自己的仓库的真实闭环）：见 [docs/agent-runtime-adoption.md](docs/agent-runtime-adoption.md) 与 [docs/real-loop-your-repo.md](docs/real-loop-your-repo.md)。

![MergePilot 架构：webhook 自动触发，四 Agent 协作（Skill+RAG），人工卡点，结论以 check run 回写 PR，无 merge 权限](docs/assets/mergepilot-architecture.svg)

架构总览图（可编辑 SVG）：[`docs/assets/mergepilot-architecture.svg`](docs/assets/mergepilot-architecture.svg) —— 含两套控制面职责区分、四个运行时 Agent、六个确定性 Skill（接入状态分组）、AgentLoop 观测层与凭据边界

## AgentLoop 云端 Trace（九轮真实运行全程接入）

全部运行的标准 span 直连上报阿里云 AgentLoop / SLS：Agent 会话（AGENT_STEP）、LLM 调用（单层 genai.llm.call）、工具调用（genai 语义）三类齐全，**累计 450-530 span/轮 · 导出批次零失败**。

**收官轮（WH）实测**：会话累计 **1528 span**（四 worker），OTEL_EXPORT 全部 SUCCESS；上一轮（SK5）824 span / 197 个 LLM 调用 span / 15 次 skill 调用，两轮数据均锁定在包。跨 Agent 关联：委派通知携带 W3C traceparent，接手 Agent 上报同 trace 关联 span（delegation.link，属性级）——**一条追踪 ID 串联两个容器的证据**。

| 指标 | SK5 轮（PR #2） | SK5 轮（PR #3） | SK5 轮（PR #1 自动） | Dual-Reviewer |
| --- | --- | --- | --- | --- |
| 调用次数 | 86 | 33 | 45 | 31 |
| 输入 token | 15,643,868（93%） | 3,882,302（99%） | 5,084,301（99%） | 2,177,070（98%） |
| 输出 token | 36,522 | 9,022 | 14,661 | 4,827 |
| skill 调用 | diff_parse + risk_classify + case_retrieval | diff_parse + risk_classify | **sast_scan ×2（首次）** + risk_classify | — |

检索方式：AgentLoop 控制台按 service.name=`mergepilot-copaw` + 运行时间窗筛选，或按 run 对应的 trace id 直查（trace id 与 run 的映射见各证据包 README）。
| 会话 ID | N2KQqHVSBsSZc9utWsEeZ5f |

span 父子关系：`agent_step → invoke_agent → { chat deepseek-chat（原生）+ genai.llm.call（loongsuite 包装）+ tool.projectflow + tool.taskflow + matrix.send }`。原生 agentscope span 与 loongsuite 包装 span 共享全局 TracerProvider，天然合并。

## RAG 检索能力

内置 8 篇合成演示文档（9 分块），支持自然语言检索并返回引用来源。

- 每次检索返回 `query_hash`（不保存原文）、`top_k`、逐条 `{document_id, chunk_id, score, source_ref}`
- **无引用来源的答案不会被标记为已验证**
- 数据模式为 SYNTHETIC/REDACTED（合成演示集，非企业语料）
- 嵌入：本地确定性哈希词袋（无外部服务依赖）；检索策略 `v2-bigram-idf-hash4096`，由离线闭环从 `v1-unigram-hash256` 晋级（按语义族切分调优/held-out，held-out hit@1 75.0% → 91.7%，证据 `evidence/FINALS-RAG-LOOP-20260914`）

## PolarDB 与 Branch 边界

| 项 | 状态 |
| --- | --- |
| PolarDB | **NOT CONNECTED**（8 项接入门槛已在代码中预留） |
| Database Branch | **SIMULATED**（隔离模拟环境上的验证状态机） |
| PR Auto Merge | **DISABLED** |
| 候选 A 迁移验证（决赛新增） | **ISOLATED_POSTGRES** —— 在独立 PostgreSQL 克隆库上**真实执行** SQL 迁移与断言；**不是** Agentic Database 分支验证 |

支持 create_branch → validate_migration → assert_data → rollback_check 全链路（三候选对照仍在模拟 fixture 上运行）。真实 PolarDB 接入需满足 8 项门槛后由环境变量切换。

## 仓库结构

```
MergePilot/
├── demo-platform/       # 演示平台：前端 + 后端 + RAG + PolarDB 模拟适配器
│   ├── backend/         # 零依赖 Node 后端（server + API + RAG + PolarDB 边界）
│   ├── frontend/        # React 控制台（含预构建 dist）
│   ├── evidence-adapter/# 只读数据适配层 + 内置演示数据集
│   ├── rag-data/        # RAG 合成数据集
│   ├── SKILLS.md        # 核心 Skill 清单
│   └── test/            # 自测脚本
├── tools/gh-bridge/     # ★ webhook 台账→AgentTeams→check-run 回写的桥（Phase 3）
├── tools/gh-app/        # webhook 接收端（HMAC 验签/交付台账）+ check run 发布器 + App 令牌
├── evidence/            # 36 个锁定证据包（十二轮真实运行，SHA256SUMS）
├── skills/              # 确定性 Skill（纯计算、schema 校验、fail-closed）
├── skill-mcp-server.mjs # Skill MCP 服务器（Agent 按需调用）
├── docker/              # ★ 全部镜像配方 + 运行时 hooks(自举激活/OTel/Skill/RAG)
├── shared/ docs/ config/ LICENSE（Apache 2.0）
```

## 评估方法

结果评估与轨迹评估**分开执行、分别呈现**：

- **结果评估**（最终状态）：关联仓库识别、影响表识别、历史风险命中、候选结论、数据核对、回滚检查、人工门终态
- **轨迹评估**（过程顺序）：先检索 RAG、正确选择工具、创建隔离 Branch、拒绝后锁定、source_refs 保留、Agent→LLM→Tool Trace 形成

即使最终状态正确，若过程顺序违规（如未检索先动手、跳过人工门），轨迹评估仍判不通过。

## 决赛新增（2026-09-14）：三个可复现闭环，各带证据等级

| 闭环 | 做了什么 | 证据等级（如实） | 证据 / 复现 |
| --- | --- | --- | --- |
| 数据库迁移验证纳入 PR 验收 | `tools/audit-db/m9_migration_verification.sql` 在不可变 `revision_bindings` 与 `approvals` 之上挂 4 张不可变子表 + `db_release_gate()`；`tools/dbverify` 在克隆库上真实跑：代码测试 PASS → 迁移因历史数据 **FAIL(23502)** → 补取信息 → 修订同一候选 → **PASS 11/11** → 审批绑定版本 → 追加提交后旧批准 **STALE**；**授权执行点 `l2_claim_ticket` 内强制 `db_release_gate`**（stale/摘要变化/未批准/过期 → 不进 EXECUTING）；交付迁移方案包 `release/migration-plans/` | **真实 SQL 验证（ISOLATED_POSTGRES）**，非 Agentic Database 分支；PolarDB 仍 NOT CONNECTED | `evidence/FINALS-DB-MIGRATION-LOOP-20260914` · `python tools/dbverify/run_migration_loop.py` |
| 多 Agent 返工闭环 | 真实 `controller.py::process_event` + 真实 PostgreSQL + 真实验收测试驱动 VERDICT：review → fix#1 → **verify FAIL → 退回 Fixer** → fix#2 → verify PASS；重试上限 HOLD、BLOCKED 升级、非法输入 | **机制验证**：Agent 语义输出为受控输入；无 LLM、无 Matrix/Element 真实交接（另一等级，未执行） | `evidence/FINALS-REWORK-LOOP-20260914` · `python tools/agentteams/rework_loop_harness.py` · 演示页 `/rework` |
| RAG 观测→评估→数据集→优化→回测 | 观测（tool-span 审计：127 行 retrieve 仅 12 个不同查询）→ 18 语义族 54 条标注查询按族切分 → 调优集选策略 → held-out 单次回测 → 晋级 v2 | **真实离线实验**，语料 SYNTHETIC/REDACTED，小样本 | `evidence/FINALS-RAG-LOOP-20260914` · `node demo-platform/backend/experiments/rag-loop/rag_eval_loop.mjs` |
| 可靠性对照 | 长/跨文件 PR、小上下文、诱导性注释、干净对照、伪造批准 × 确定性层（diff_parse / risk_classify / sast_scan） | 确定性层**真实执行**（5/5 决策正确、0 误报 0 漏报）；模型轴 **NOT_EXECUTED**（付费调用需授权） | `evidence/FINALS-RELIABILITY-20260914` · `python benchmark/reliability/run_reliability.py` |

演示平台 PR#4 页现在回放上述真实 SQL 证据并提供**版本绑定的人工门**（REPLAY overlay，NO RUNTIME WRITE；旧版本批准返回 409）。

## 技术栈

- **主项目**：Python（pyproject.toml）· 1490 tests（可复现命令见 CONTRIBUTING）· 6 类 Skill · 4 Agent 承载 6 类职责
- **演示平台**：Node.js 零依赖后端 + React 前端（Vite 预构建）
- **可观测**：OpenTelemetry GenAI 语义约定 · loongsuite 探针 · 阿里云 AgentLoop

## 提交材料

- [`submission/`](submission/) — 决赛提交文档：SKILLS.md（分级如实口径）、跨仓 Schema 审查 Skill、演示平台 DEPLOY 说明与 `.env.example`、路演备用网页版（含同版 PDF）
- 离线交付方案与外部机器验证记录：见 [Release v0.2.1](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.1) 与 [DEPLOY.md](DEPLOY.md)

## 许可

[Apache License 2.0](LICENSE)
