# MergePilot 产品化推进状态（STATUS）

**更新**：2026-09-22（第九轮：首个真实案例运行前准备）｜ **分支**：`chore/backfill-r3-ops` ｜ 授权范围：M1→M4 至 V0 技术就绪

## 本轮新增：真实案例运行前核对 + 运行前门禁 + 一次确认

| 工作项 | 状态 | 说明 |
|---|---|---|
| 触发路径核对（只读） | ✅ | 台账 0 PENDING；全部开放 PR 当前 head 均已 PROCESSED（already_processed 会跳过重放）⇒ 新真实案例的自然触发 = 向获批测试分支推**一个空提交**（synchronize → 新 head → 天然全链） |
| 候选目标 | ✅ 已定 | **PR #9**（feat/skill-exercise，head `f0dc76fbba030779fa28f647e8eb6b40b6dd1002`，2 文件 +29 行）⇒ 分级 TRIVIAL（单审查器）＝最小真实用例规模 |
| 运行前门禁 | ✅ 实现+8 测试 | tools/integration_prep/prerun_gate.py：git 固定/副本摘要/语料快照/模式/唯一执行者/容器/RAG/**预算 MISSING_ACK 即 FAIL**/投递前置 九项检查，全绿才允许起桥 |
| 口径勘误（五项） | ✅ 完成 | off 冒烟≠端到端回归；status 只读；run_id=服务端参数级（worker MCP 未透传，R5）；217/225=分套口径非全仓库；回退已备未演练（见第八轮节与 EXECUTION-RECORD） |
| 预算现实核对 | ✅ 记录（DECISIONS #16） | worker 进程内硬预算不存在（R5 缺口）——首次案例有界方案=单投递+20min 硬观察截止+3 次发布重试+provider 侧 gateway key 消费硬上限+运行后 OTel/审计事后计量；**不为开跑把脚手架标记为已接入** |
| v3 真实 on 缺口 | ✅ 明确记录 | adapter 无真实 Agent runner——本轮真实运行=**加固旧链路**，v3 仅以 shadow 同轮对照；不冒充 v3 验证 |

## 前轮：R4+R7 执行（第八轮）

| 工作项 | 状态 | 结果 |
|---|---|---|
| R4 运行副本同步 | ✅ 完成 | 备份 3 份(.bak-20260922-204703)；同步 12 文件（bridge+orchestrator+corpus_tool）全部 sha256 双侧一致；off 冒烟 PASS（status 只读 exit 0、adapter 从 r3work 加载、off 零副作用、shadow 诚实降级）；**MERGEPILOT_REVIEW_V3 保持 off** |
| R7 语料同步+rag-live | ✅ 完成 | 幂等导入 changed=false（语料本就同字节 fd34c304…）；/health chunks=12+corpus_file_sha256 部署一致；检索验证命中 source_refs 正确；run_id 透传入审计；**服务按条件已停止** |
| 回退 | 未需要 | 回退命令已记录并存档（EXECUTION-RECORD §回退） |
| 边界遵守 | ✅ | 零 R1/R2/R3 操作、零 GitHub 写入、零共享环境故障注入、零模型调用；现网桥未运行，下次启动=加固版默认 off |

## 前轮：授权前本地准备（第七轮）

| 工作项 | 状态 | 说明 |
|---|---|---|
| 最终决策/授权表 | ✅ 产出 | AUTH-DECISION-PACKAGE.md 重构：A 决定类（D-1..D-6 含预算/R6/密钥轮换）+ B 授权类（R4/R7/R1/R2/R3，各 6 要素）+ 三阶段执行顺序 + 启用铁律；**未代批** |
| integration_prep 包 | ✅ 实现+测试 | R1/R2/R3 声明式执行计划（步骤+证据点）、R4/R7 同步计划（备份/校验/同步/off 冒烟/回退）、证据采集器（集中脱敏：gh-token/DSN/API key/OTel key）、授权闸门（默认 dry-run，执行需批复+MERGEPILOT_IT_AUTH=1） |
| rag-live 事实源增强 | ✅ 实现+测试 | /health 暴露 corpus_file_sha256（部署核对）；search 可选 run_id 透传落审计（R5 接通即闭合 run 关联；向后兼容） |
| 预算接入点工厂 | ✅ 实现+测试 | costmeter/hooks.py：MERGEPILOT_RUN_BUDGET_TOKENS 设置即 fail-closed（超限拒+台账恢复），未设置=None（现状语义不变）；接入点盘点：桥派发/调度执行器（已可接）/worker 面（R5） |
| 既有问题登记 | ✅ 记录 | tests/ 全目录跑时 m4c/m4e/skills 模块重名收集冲突为陈年问题（无 __init__.py），不在本轮范围 |

## 前轮：M3.5 复核+冒烟+决策包（第六轮）

| 工作项 | 状态 | 说明 |
|---|---|---|
| b00a095 提交复核（九项清单） | ✅ 完成 | 197 项声称与当前代码对应当✓；清单第 9 项发现缺口→已修（见下） |
| fail-soft 可观测性修复 | ✅ 修复+测试 | hook 异常原只写 stdout→新增 v3_hook_errors 表持久痕迹 + GET /api/hook-errors；测试 3 项 |
| 取代保真修复 | ✅ 修复+测试 | _supersede_old_runs 重建 StageRecord 丢失 error/时间戳→保留字段；取代不再抹掉既有降级原因 |
| 本地冒烟（真实服务+浏览器） | ✅ 完成 | 端口 4191 真实进程；shadow+fixture 两条 run；浏览器截图核验页面/API/详情；POST/PUT/DELETE=405；MANUAL_ATTENTION 红色/部分完成未压缩为成功 |
| 授权决策包 | ✅ 产出 | AUTH-DECISION-PACKAGE.md：R1-R7 合并表（推荐/目标/操作/影响/证据/回退/权限/外部写费用）+ D-1/2/3、预算、R6、密钥轮换独立决策；**未代用户批准** |

## 前轮：M3.5 v3 本地接线（验收见 ACCEPTANCE-M35）

| 工作项 | 状态 | 说明 |
|---|---|---|
| v3 adapter 三态接线 | ✅ 实现+测试 | off（默认零开销）/ shadow（真实 PR 只读→分级+计划+状态持久化，零 Agent 零外部写）/ on（授权前按 shadow 并显式记录） |
| 桥派发边界 hook | ✅ 实现+测试 | v3_shadow_hook fail-soft：shadow 炸掉旧链路照常 PROCESSED；桥内不重写任何 v3 逻辑 |
| RunStore（v3_runs 表） | ✅ 实现+测试 | 幂等 UPSERT/重启恢复/取代标记；与票据库分库，状态机唯一（stages.RunStages） |
| 纵向链路（fixture） | ✅ 13 测试 | risk→plan→并行 reviewers→聚合去重保源→finding validation→outcome→store→read model |
| shadow 诚实性 | ✅ 5 测试 | 审查器 SKIPPED 不伪装；diff 不可得档位 FAILED；关键跳过⇒MANUAL_ATTENTION |
| 只读控制台 | ✅ 实现+6 测试 | GET-only（写方法 405）；shadow/fixture 强制标签；部分完成不压缩为成功；交接见 docs/productization/console/STATUS.md |

## 前轮：架构 v3 骨架（设计见 ARCHITECTURE-V3.md，验收见 ACCEPTANCE-ARCHV3）

| 工作项 | 状态 | 说明 |
|---|---|---|
| 架构 v3 文档与状态机 | ✅ 完成 | 11 步流水线；finding validation 与 patch validation 为两个独立阶段；维度状态×run outcome 正交 |
| 风险分级纯逻辑 | ✅ 实现+测试 | Trivial/Lite/Full 可配置规则；敏感路径命中无条件 FULL+人工强制复核；无 LLM 调度 |
| 调度器接口 | ✅ 实现+测试 | DispatchPlanner 唯一调度权；多 PR 上限 1 固化；审查器并发 2（barrier 并行证明）；串行模式 |
| 聚合器+verifier 接口 | ✅ 实现+测试 | 去重保源（精确+Jaccard 近似）；verifier 输入类型构造性隔离推理字段 |
| 降级与预算 | ✅ 实现+测试 | 关键审查器超时→MANUAL 不通过；非关键超时→PARTIAL 可发布；重试预算/全局预算钩子 |
| 控制台字段契约 | ✅ 实现+测试 | 部分完成/降级/超时原因逐字段可见，禁止单一绿色 |
| TicketStore 抽象 | ✅ 实现+测试 | Protocol 固化五操作；SQLite 单实例边界；PG 迁移占位（DECISIONS P-1/#12） |
| feature flag | ✅ 默认关 | MERGEPILOT_REVIEW_V3；v3 未接线 bridge，旧串行链未删 |
| 真实 Agent 接入（v3 第 11 步） | ⬜ 待授权 | R1/R2 批复后最后执行 |

## 历史工作项（第二/三轮）

| 工作项 | 状态 | 说明 |
|---|---|---|
| RAG 全链路审计 + V0 最小闭环 | ✅ 第1/2层 | RAG-AUDIT.md；语料快照绑定（fd34c304…）+ RAG_REQUIRED fail-closed 门；第3/4层待授权 |
| M2 审批：规格+纯逻辑+SQLite 存储+CLI | ✅ 实现层 | 47 测试；真实审批主体/动作启用待 D-1/D-2/D-3 |
| 成本计量脚手架 | ✅ 逻辑层 | 预留/重试累计/结算/并发/崩溃 17 测试；未接真实路径 |
| M1 桥加固（发布语义/恢复/互斥契约） | ✅ 单测级 | 46 测试；场景 3/6/7/9 真实实证待授权 |

## 阻塞重新分类（第三轮定稿）

| 项 | 分类 | 处置 |
|---|---|---|
| P-1 票据存储 | B 已自主决策 | SQLite WAL + TicketStore 接口（PG 迁移路径固化） |
| D-1/D-2/D-3 审批政策 | D 真业务决策 | 机制全实现且拒绝未配置状态；启用待用户 |
| 预算金额 | D 真业务决策 | 接口全实现；未配置不产生消费授权 |
| R1-R4/R7 真实环境 | C/D 外部授权 | 待批；每场景独立判据 |
| R5 镜像层 / R6 usage 源 | C 需授权/选路 | 待批 |
| 密钥轮换 | D 用户挂起中 | M4 出口条件 |

## 里程碑真实状态

- **M1**：单测级收口；真实实证未做——未通过。
- **M2**：规格+存储+CLI 完成；真实审批未启用——未通过。
- **M3**：未开始（待真实 run）。
- **M4**：RAG 第1/2层、成本接口层、v3 本地骨架达成；**V0 技术就绪未达成**，真实内测未开始。

## 下一条可执行动作（按序）

1. **（可选）shadow 证据同步到 MinIO 项目目录**：当前 run 记录在本地 RunStore（~/.mergepilot/v3-runs.db）；如需随证据包走，加导出钩子（本地操作，可自主）；
2. **门 Web 只读页对接 RunStore**：复用 console_v3 读模型 + gate_cli 票据数据（UI 依赖 D-1/D-2 的部分仍关闭）；
3. **待授权批复后**：R7 语料同步 → R1+R2+R4 真实案例轮（v3 shadow 模式将随第一轮真实案例产出对照证据；on 模式真实 Agent 验证在 R1/R2 之内）→ R3 双 head。

## 已验证结果（证据，2026-09-22 实测 @ 工作树）

- `python -X utf8 -m pytest tests/gh_bridge/ tests/console_v3/ tests/orchestrator/ tests/approval/ tests/rag_live/ tests/costmeter/ tests/integration_prep/ -q` → **217 passed**（分套口径=上述 7 个目录；**不等于仓库全部测试通过**：m4c/m4e/skills 存在陈年收集冲突未纳入，另有 1 项顺序敏感偶发（重跑通过，保留观察））
- **口径勘误（第八轮）**：off 冒烟≠加固桥端到端回归（含发布/恢复/manifest/RAG 门改动待真实案例验证）；RAG run_id 透传=服务端参数级验证，worker MCP 未透传；回退命令已备未演练。
- 本地冒烟（.smoke 临时目录，已清理）：console 真实进程 :4191；shadow run=MANUAL_ATTENTION（coverage 三缺失+degradations 带原因）；fixture run=REVIEW_COMPLETED；写方法 405；浏览器截图与 DOM 双重核验
- `python -X utf8 -m pytest tests/gh_app/ -q` → 816 passed, 5 skipped（上轮实测；本轮不触其引用面）
- RAG 双副本快照一致（fd34c304…）；M1 九场景 1/2/3/4/5/7/8 ✅单测、9 ✅契约、6 🔒结构保证

## 当前阻塞（外部条件）

- **首个真实案例的一次性确认待用户回复**（运行前门禁已就绪；确认单见第九轮报告：目标 PR #9 + 空提交触发 + rag-live 案例期间运行 + v3=shadow + provider 侧预算硬上限）；
- D-1/D-2/D-3 不阻塞仅审查案例（真实审批保持关闭）；D-6 密钥轮换不阻塞（现网凭证自洽，未验收不声明）。

## 提交记录（第四/五轮新增，均未 push）

- TicketStore 接口抽象（SQLite 边界+PG 占位）
- 架构 v3 骨架（风险/状态/调度/聚合/verifier/控制台契约/flag + 41 测试）
- M3.5：adapter 三态接线 + RunStore + 桥 hook + 只读控制台（65 新测试）

## 环境事实（2026-09-22 实测）

- 本地栈 8 容器运行中；rag-live 未运行；worker rag_mcp hook 最近激活 2026-09-21。
- 桥运行副本 **已于 R4 同步**（12 文件 sha256 一致，含 v3 hook，默认 off）；rag-live 已停止，启动命令见 EXECUTION-RECORD。

