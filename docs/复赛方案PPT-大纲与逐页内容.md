# MergePilot · 复赛方案 PPT — 大纲与逐页内容（V1 草案）

> 用途：复赛提交物之一「更新版项目方案」的逐页底稿。每页给出**版面文字**（可直接抄进 PPT）、**口播要点**（约 30 秒/页）和**证据指针**（仓库内文件路径，评委追问时可现场打开）。
> 赛程锚点：复赛 8.25–9.3 提交截止；评审 9.4–9.10；9.10 公布决赛 Top 15。
> 叙事主线：**问题（AI 生成代码的可信交付缺口）→ 架构（确定性控制面 + Agent 决策面）→ 真实结果（run35 + hiclaw_live + 冻结 Benchmark）→ 诚实边界与路线图**。
> 初赛方案讲的是设想；复赛方案的核心动作是**逐条兑现初赛承诺**——P2 的兑现表是全篇的钩子。

## 评分维度映射（做页时的心智地图）

| 评分维度 | 权重 | 主要承载页 |
|---|---:|---|
| 场景价值与行业可复制性 | 25% | P3、P11、P14 |
| 多 Agent 协同与自主闭环 | 25% | P5–P8、P10 |
| Skill 工程体系与生态复用 | 25% | P9 |
| 工程落地、运行验证与安全审计 | 20% | P7、P10、P12、P13 |
| 开放/开源贡献 | 5% | P13、P15 |

## 提交物映射（赛事要求的三项）

| 赛事提交物 | 本方案承担 | 其他承担物 |
|---|---|---|
| 更新版项目方案 | 本 PPT | — |
| 可执行 AgentTeams 代码包 | P13/P15 给出入口 | v0.1.0-preview.1 安装包（待按《复赛提交材料说明》对齐形态） |
| 可运行 Demo / Demo 视频 | P12 展示控制台 | `docs/preview/DEMO-SCRIPT.md` 5–8 分钟脚本 + 录屏（待录） |

---

## P1 封面

**版面文字**

- 标题：MergePilot —— 多 Agent 受治理的 PR 审修闭环
- 副标题：不止提建议，而是完成可审计、可回滚的修复交付
- 赛道一「新智基座 Agent Infra」· 团队：分子 · 复赛更新版方案
- 页脚：Apache-2.0 开源 · v0.1.0-preview.1 可安装

**口播**：一句话定位——我们在 AgentTeams 之上做的是"可信交付层"：AI 能修代码，但修什么、怎么修、何时合并、失败怎么退，全部受治理、可审计。

**证据指针**：`README.md`、`PRODUCT.md`

---

## P2 初赛承诺 → 复赛兑现（全篇钩子页）

**版面文字**（左列 = 初赛方案写的，右列 = 复赛现状，全部可验证）

| 初赛承诺 | 复赛兑现 | 证据 |
|---|---|---|
| 4 Agent 协同但"偶需人工 nudge" | 零 nudge 确定性交接 + Controller 状态机 | M3-A E2E PASS |
| HiClaw live 未接入（hiclaw_live=false） | 真实 AgentTeams v1.2.2 生产 live 64/64 PASS | `hiclaw_live=true` |
| 11 个 Skill 设计、少量实测 | 5 个核心 Skill 发布，481 项契约/行为测试 | M4-A..E 标签 |
| 回滚靠脚本手动触发 | verify-fail 自动进入 revert child-run，33/33 | `m3c-closed` |
| 无量化数据 | Benchmark N=10×2 冻结 + RAG 确认性 16/16 门控 | `benchmark/formal-summary.json` |
| Demo 需手工拼脚本 | 一键 bootstrapper + 只读控制台 + run35 真实投影 | v0.1.0-preview.1 |

**口播**：初赛我们讲了设想，这一页是逐条对账——左列每一条当初的"规划"，右列都有标签、证据目录或发布物可查。本次汇报不引入任何没有证据的新主张。

**证据指针**：`docs/项目状态.md`、`docs/标签SHA映射.md`、`docs/复赛路线图.md`

---

## P3 问题：AI 生成代码的可信交付缺口

**版面文字**

- 现状：AI Code Review 工具止步于"提建议"，修复仍由人完成
- 三个不敢：
  - 低风险不敢自动推进（人力瓶颈还在）
  - 高风险不敢交给 Agent（越权写仓库）
  - 失败不敢自动处理（改坏了无法可信回退）
- 一个没有：全程不可审计——出问题无法回答"哪个 Agent、哪个决定、哪次写操作"
- 我们的实测数据支撑问题真实存在：单 Agent 方案误报 21 项，多角色治理后降至 9 项（同召回 70.59%）

**口播**：价值主张不是"再做一个 Review 工具"，而是把建议变成**受治理的修复闭环**：低风险自动推进、高风险人控审批、失败可回滚、全程可审计。右下角这组数字来自我们冻结的 Benchmark，问题不是想象的，是测出来的。

**证据指针**：`benchmark/formal-summary.md`、`docs/复赛路线图.md` §1

---

## P4 架构总览：确定性控制面 + Agent 语义决策面

**版面文字**（配架构图，可复用 `docs/showcase/architecture.svg` 重绘）

- 设计原则：LLM Agent 干语义的事（审查、修复、验证、裁定）；确定性 Controller 干可靠的事（状态、幂等、重试、超时、恢复）
- Workflow Controller：唯一任务状态，PG 权威五表（task_runs / stage_runs / stage_events / dispatch_outbox / controller_offsets）
- PR 专属 Matrix 任务房间：会话隔离，跨 PR 零上下文污染
- Policy / Approval Gateway：权限与审批在**工具层**强制执行，不依赖 Prompt 自觉
- 每次阶段执行携带 `run_id + task_id + stage + attempt + idempotency_key`

**口播**：核心架构判断一句话——不给 LLM 提权。凡是必须严格可靠的（状态推进、幂等、审批、回滚），全部放在确定性组件里；Agent 只做语义决策。这是后面所有安全性证据的结构性来源。

**证据指针**：`DESIGN.md`、`docs/复赛路线图.md` §3、`docs/M4-D-PRLifecycle设计冻结.md`

---

## P5 多 Agent 协同：4 Agent、六职能、零 nudge

**版面文字**

- 4 个运行 Agent 承载 6 类职能：Manager（编排/裁定）+ Reviewer / Fixer / Verifier
- 状态机是唯一事实来源：RECEIVED → REVIEWING → FIXING → VERIFYING →（L2 审批）→ MERGED / HOLD / ROLLED_BACK
- 零人工 nudge：handoff_watcher 检测 TASK_COMPLETED 自动推进（M1 验证），后升级为 PG 权威 Controller（M3-A）
- 多 PR 并行互不污染：每 PR 独立房间 + 独立 session + run 前缀隔离

**口播**：初赛最大的工程疑问是"Manager 会不会卡住不派活"。我们从 SOUL 约束（不可靠）走到 watcher（可用）再走到 Controller 状态机（可靠），这条演进线本身就是多 Agent 协同工程化的样本。

**证据指针**：`docs/复赛路线图.md` M1/M3-A、`evidence/m3a-final-04/`

---

## P6 可靠性内核：幂等、崩溃恢复、事件去重（M3-A）

**版面文字**

- 状态转换 + Outbox 写入同一 PG 事务（不丢不重）
- `idempotency_key` UNIQUE；重复 TASK_COMPLETED → DUPLICATE；重试退避 5s→60s
- 崩溃恢复实测：docker restart Controller → task/stage/outbox/events 计数零变化
- 流式快照误消费防护：无 `VERDICT=` 的中间消息 → PARTIAL，不终结阶段
- 验收脚本 `m3a-verify.sh` 12/12 PASS；修复过程 6 个阻断问题全部留档

**口播**：评委问"Agent 系统怎么保证不重复执行、不丢事件"，答案在这一页：所有可靠性都是数据库约束和事务，不是 Prompt。6 个阻断问题的修复表我们也保留在文档里——工程成熟度看修 bug 的方式。

**证据指针**：`evidence/m3a-final-04/`、`docs/复赛路线图.md` M3-A（含 6 问题修复表）

---

## P7 安全与最小权限：deny-by-default + L2 审批（M3-B）

**版面文字**

- 网络：github-mcp 在私有 `mcp-backend-net`，Worker 无法解析、无法直连（B1 旁路封闭实测）
- 认证：角色 opaque Bearer token（非 URL 路径）；跨角色 → ROLE_PATH_MISMATCH
- 授权：`policy.yaml` deny-by-default 工具矩阵；12 个过权工具全禁；搜索 boolean 逃逸闭合（B2.2）
- 审批：L2 高风险动作必须审批票据（B4 全链：claim → TOCTOU 防护 → 三态执行 → 对账；UNKNOWN/EXECUTING 绝不自动重 merge）
- 审计：INSERT-only 账号 + fail-closed；`approved_by` 由 session_user 硬派生，**不可参数伪造**
- 负向证据 B5：直连拒 / 跨角色拒 / 票据伪造-过期-重复拒 / 合法票仅一次 / 全审计 —— **50/50 PASS**

**口播**：这页回答"Worker 没有 PAT 是否仍可能越权"。权限不在 Prompt 里，在 Gateway 的工具层；连审批人身份都是数据库会话派生的，连参数都伪造不了。B4e 总 E2E 43/43 包含真 squash merge、崩溃恢复和降级恢复。

**证据指针**：`evidence/m3b-b4e/`、`evidence/m3b-b5/`、`docs/M3-B4-审批票据与Action-Outbox设计.md`

---

## P8 失败恢复：状态感知的自动回滚（M3-C）

**版面文字**

- FAIL 分支由状态决定，不由猜测决定：
  - 未达重试上限 → 回 Fixer（MAX_VERIFY_ATTEMPTS=3）
  - 未合并就失败 → HOLD，**零误 revert**
  - 已合并后失败 → revert child-run（`parent_run_id` + `revert_run_id`，复用审批链）
- revert 后强制复验：PASS → RECOVERED；仍 FAIL → HOLD 升级人工（绝不二次回滚）
- changed-files / merge-parent 全部由 GitHub 权威 API 派生，不信本地推算
- M3-C E2E **33/33 连续两次**；全链 fresh-DB 铺设 9/9 + Schema 断言 19/19

**口播**：误回滚比不回滚更危险。我们的答案是"先问状态再动手"：只有确认 main 真的等于那次坏合并、revert 分支真的基于父提交，L2 门才会放行；任何读失败或冲突都 fail-closed 进 HOLD。

**证据指针**：`evidence/m3c/`、`docs/复赛路线图.md` M3 任务 C

---

## P9 Skill 工程体系：5 个核心 Skill、契约化、可复用（M4）

**版面文字**

| Skill | 职能 | 测试 |
|---|---|---:|
| diff-parse + risk-classify | 结构化变更 + L0/L1/L2 分级 | 96 |
| sast-scan + test-runner | 扫描与隔离执行（8 MiB tmpfs、网络隔离） | 87 |
| pr-lifecycle | branch/write/PR/merge/revert 受控写能力 | 54 |
| case-retrieval | 只读 RAG 检索（pgvector，untrusted 恒真） | 169 |
| common contract/runtime | 统一 envelope/脱敏/错误码 | 75 |

- 每个 Skill：JSON Schema（Draft 2020-12）契约 + happy/bad/timeout 测试 + 跨 Agent 复用
- 安全内建：raw-secret fail-closed、引擎不可降级、deadline、路径安全
- 多 Agent 复用实例：sast-scan 同时服务 Reviewer 与 Verifier；diff-parse 服务三方

**口播**：Skill 体系不追求数量，11 个设计收敛成 5 个真正达到 DoD 的。每个都有 schema 契约和确定性测试，任何 Agent 调用同一个 Skill 拿到同一份合同——这是"生态复用"的工程含义。

**证据指针**：`skills/`、`evidence/m4/`、`docs/复赛路线图.md` M4

---

## P10 真实运行验证：从 fixture 到生产 live（M5 / D2B-3 / run35）

**版面文字**

- 真实 AgentTeams v1.2.2 **生产 live** 64/64 PASS，`hiclaw_live=true`（初赛时为 false）
- fail-closed Docker socket proxy：权威资源绑定、label 伪造 8 类绕过全拒
- **run35 端到端（2026-08-24）**：`b8-e2e-run35`，GITHUB_E2E_COMPLETE——
  - 17 个阶段全绿（init → complete）
  - 16 项前置检查 verified；receipt/matrix verified
  - 6 条服务间路由逐边探测 VERIFIED（bridge/proxy/controller/gateway/reporter 全链）
  - 11 个服务真实启动；HiClaw 五容器（manager/fixer/reviewer/verifier + Tuwunel Matrix）联动
- 稳定性：C3 十轮连跑 10/10；离线回归 17/17 + 6/6

**口播**：这页是"工程落地、运行验证"维度的实锤。注意时间戳——run35 是 8 月 24 日凌晨完成的最新一轮真实 E2E，评委可以现场用控制台回放这份投影。

**证据指针**：`evidence/m5/0d/hiclaw-v122-true-live-pass.json`、`docs/preview/projections/complete.run35.json`、`evidence/m5/0c/c3-10x.json`

---

## P11 量化价值：冻结的 Benchmark 与基线对比（M5 / M7-P2）

**版面文字**

- **单 Agent vs MergePilot 多角色（N=10×2，同模型，冻结于 2026-08-11）**
  - precision：36.36% → **57.14%**
  - F1：48.00% → **63.16%**
  - recall：持平 70.59%；误报 FP：21 → **9**
  - 代价：token +32.95%、API 请求 +80%（如实呈现）
  - decision accuracy：B=40% 低于 A=50% —— 风险处置校准列为已知短板
- **RAG CaseRetrieval 确认性 held-out（N=25，预注册 16 项门控）**：hit@1=1.0、MRR=1.0、abstention=0.60、scope leak=0，**16/16 PASS**
- 方法论：post-run 机器复算、原子写、SHA256 冻结、fail-closed
- **明确限制（原文照录）**：受控本地编排，Group B 非真实 E2E；N=10 小样本单模型；不证明多角色提高 recall

**口播**：我们只报告可复算的数字，并且把不利数字和限制一起放在同一页——decision accuracy 更低、成本更高都写着。诚实的量化比漂亮的量化更可信。

**证据指针**：`benchmark/formal-summary.json`、`benchmark/formal-run-manifest.json`、`evidence/m7/benchmark/rag-n20-confirmatory.json`

---

## P12 差异化亮点：只读运维控制台——诚实的可观测性

**版面文字**

- 四状态诚实渲染：live（未知态）/ complete（run35 全绿）/ failed（第 10 阶段红 + 稳定错误码）/ stale（数据变旧如实变旧）
- **没有任何写操作**：没有 apply、没有 delete，只读管道
- 五项真实性边界全部 NOT_VERIFIED 如实展示——"一次成功的 E2E 不翻转任何真实性边界"
- 直连路由声明为 false（走 wsl-user-relay 中继），逐边探测才标 VERIFIED
- failed 态 30 秒定位：失败阶段 + 错误码 + 哪条边挂了 + 中继资源归属
- 独立评审结论：APPROVED；5 张带出处的截图

**口播**：大多数 Demo 控制台把系统画成绿的，我们的控制台把"没有验证过的东西"画成本来的样子。这套四状态渲染本身就是产品能力——运维要的不是仪表盘，是可信的坏消息。

**证据指针**：`docs/preview/DEMO-SCRIPT.md`、`docs/preview/SCREENSHOTS.md`、`docs/preview/PROJECTIONS.md`

---

## P13 交付与可复现：可安装的代码包 + 发布工程（M7）

**版面文字**

- **v0.1.0-preview.1 已发布（GitHub Pre-release）**：
  - Windows + WSL2 bootstrapper：五项环境检查 / install / status / 回退
  - 镜像 tar 401MB + SHA256 checksum 校验；install.current/previous 清单
  - 回退合同：`docs/preview/ROLLBACK.md`
- 发布质量：发布审查链 PR #206 / #207 双轮独立审查后合并；14 项发布契约测试
- 测试总量：全树 3700+ 通过（提交前以统计脚本复算为准）；核心套件两轮稳定
- 开源：Apache-2.0 + THIRD_PARTY 清单 + QUICKSTART 十分钟上手
- 已知技术债如实披露：MIG-B4-001（migration forward-only，决赛前修）

**口播**：评委拿到的不是"仓库链接"，是能装、能跑、能回退的安装包。发布本身走了 PR 审查链——我们用自己的 PR 审修流程发布自己的产品。

**证据指针**：`release/preview/`（bootstrapper.ps1、make_package.ps1、manifests）、`docs/preview/QUICKSTART.md`、`docs/preview/ROLLBACK.md`

---

## P14 诚实边界与路线图：NOT_VERIFIED 是设计，不是遮掩

**版面文字**

- 当前五项真实性边界（控制台原文）：全部 NOT_VERIFIED
  - 生产环境部署 / 直连路由（现为 wsl-user-relay 中继）/ 多仓库规模 / 长时稳定性 / 生产凭据链路
- 已知短板：B 组 decision accuracy 40%；RAG workflow utility 不可测（runtime 不消费检索结果）；worker 派单重投递依赖 A2-c HOLD 兜底
- 决赛路线图（每项对应已开工的基础设施）：
  1. decision 校准：风险处置策略迭代 + N≥20 扩测
  2. RAG 在环：core 消费 advisory evidence（架构已留接口）
  3. 多仓库 + 生产路由：直连验证与多 repo allowlist
  4. migration runner 正式化（消除 MIG-B4-001）

**口播**：把边界放在 PPT 里而不是藏起来，是因为每条边界都对应一条已排期的路线图。我们相信评委为"知道自己没做什么"的团队打更高的分。

**证据指针**：`docs/preview/PROJECTIONS.md`（五项边界定义）、`docs/项目状态.md`（诚实边界节）、`docs/复赛路线图.md` §7

---

## P15 复赛提交物对照：去哪里验证每一项

**版面文字**

| 提交物 | 入口 | 一句话验证方式 |
|---|---|---|
| 更新版项目方案 | 本 PPT | P2 兑现表逐条有证据指针 |
| 可执行 AgentTeams 代码包 | GitHub Pre-release v0.1.0-preview.1 + 仓库 | bootstrapper Check→Install→Status 三命令 |
| Demo / Demo 视频 | 控制台 + `DEMO-SCRIPT.md` 脚本 + 录屏 | 四状态切换 + run35 投影回放 |

- 证据总索引：`docs/初赛证据索引.md` + `evidence/` 目录（按里程碑分区）
- 复现入口：QUICKSTART 十分钟 / `m3a-verify.sh`、`run_all.sh` 等门禁脚本

**口播**：三项提交物共享同一套证据地基——PPT 里的每个数字都能在仓库里找到文件和 SHA。

**证据指针**：`docs/初赛证据索引.md`、`README.md`

---

## P16 结尾页

**版面文字**

- MergePilot：让 AI 修复代码这件事，第一次**可治理、可审计、可回滚**
- 团队：分子 · 赛道一 Agent Infra
- 仓库与发布：Apache-2.0 · v0.1.0-preview.1 Pre-release
- 谢谢 —— 欢迎用 run_id 追问任何一页

**口播**：收尾回扣钩子页：初赛的每句承诺都有了下文；没做到的，我们也标清楚了。欢迎现场抽查任何证据。

---

## 附：全篇数字出处速查（做 PPT 时防翻车）

| 数字 | 出处文件 |
|---|---|
| run35 = `b8-e2e-run35`，17 阶段 / 16 前置 / 6 边 / 11 服务，github_e2e=true | `docs/preview/projections/complete.run35.json` |
| hiclaw_live=true，64/64，v1.2.2，commit `849182a` | `evidence/m5/0d/hiclaw-v122-true-live-pass.json` |
| M3-C 33/33；B4e 43/43；B5 50/50；C3 10/10 | `evidence/m3c/`、`evidence/m3b-b4e/`、`evidence/m3b-b5/`、`evidence/m5/0c/c3-10x.json` |
| precision 36.36→57.14 / F1 48.00→63.16 / FP 21→9 / token +32.95% | `benchmark/formal-summary.json` |
| RAG 16/16 门控、hit@1=1.0、MRR=1.0 | `evidence/m7/benchmark/rag-n20-confirmatory.json` |
| Skill 测试 96+87+54+169+75=481 | `evidence/m4/` 各 README |
| 镜像 tar 401MB / checksum / manifests | `release/preview/`（**提交前以 make_package 产物实际大小为准**） |
| 全树测试 3700+ | **提交前跑统计脚本填准确值，勿凭记忆** |
| 发布链 PR #206/#207 | GitHub 仓库 PR（**成稿时贴链接**） |

## 制作与节奏备注

- 16 页为完整版（评委阅读型）；若要求现场讲 8 分钟：P6/P8 可合并口头带过，P15 可跳，核心路径 P1→P2→P3→P4→P5→P7→P10→P11→P12→P14→P16。
- 视觉基调建议延续初赛美化版 v4 的模板，仅换内容，降低制作成本。
- 每页"证据指针"建议做成 PPT 备注页，答辩时直接跳文件。
