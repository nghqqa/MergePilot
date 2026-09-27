# 术语表与语言契约（GLOSSARY）

> 单一权威入口：项目自造术语的中文定义 + 机器值原文。UI/文案/报告与代码不一致时以本表为准。
> 语言策略（成文契约，2026-09-27 语言一致性 P1 修复波确立）：
>
> - **用户可见层用中文**：界面文案、告警、空态/错误态、操作说明、报告摘要与标题。
> - **机器可识别层保持英文原文**：API 路径、配置变量（env）、JSON 字段名、错误码（`error.reason`）、
>   状态枚举、命令、文件路径、commit SHA。**翻译不得进入机器可判断字段**（后端 `reason` 一律
>   稳定英文机器码，中文解释放 `detail` / `note` / `message`）。
> - **原始证据保留原文**：原始日志、第三方工具输出（git/GitHub API/pg）、模型与 provider 返回、
>   RAG 引用的原文片段/路径/行号——包装时可截断附注，不得翻译替代来源。
> - **状态语义不可翻译漂移**：READY / VERIFIED / BLOCKED / TRIAL_READY 等判定词的技术语义在任何
>   语言层都不得被改变或拔高；中文标签只做解释，判定以英文机器值为准。
> - 不引入 i18n 框架：当前无英文用户群（产品红线"页面一律中文"），字符串量级 ~1000 条，
>   维护成本不匹配收益；机器值/人读文案已结构性分离（status-map.js）并由契约测试锁定。

## 状态词表（成熟度与判定口径）

| 机器值（唯一拼写） | 中文显示 | 含义与红线 |
|---|---|---|
| `TRIAL_READY` | 试验就绪 | **唯一机器拼写**。隔离栈内全绿、可复跑试验；≠生产可用，不得对外宣称生产级。历史冻结报告（`deploy/local-rag-trial/LOCAL_RAG_TRIAL_REPORT.md`）中的 `LOCAL_RAG_TRIAL_READY` 为遗留拼写，仅作历史记录，新代码/新报告一律用 `TRIAL_READY`。 |
| `BLOCKED` | 已阻断 | 缺真实依赖（密钥/模型/网络）时的如实状态；恒带 `blocked_condition(s)` 具体原因，**不伪造 READY**。 |
| `READY` | 就绪 | 组件全部真实探测通过（C 链=三组件 READY/ATTESTED）。 |
| `VERIFIED` | 已验证 | 仅由独立测试证据（harness/test）支撑的成功终态；RAG 证据**不得**作为 VERIFIED 输入（见排除清单）。 |
| `PENDING` | 待审批（票据）/ 待处理（投递） | 票据语境=等待人工审批；投递语境=等待认领。同一英文值按上下文区分，见各页面映射。 |
| `UNKNOWN`（阶段） | 未知（决策缺失） | gate 记录存在但 decision 缺失/无法识别——fail-closed 数据异常态，**不冒充 PASSED 也不武断 BLOCKED**。 |
| `degraded` | 已降级 | 检索服务显式降级（503），带 `degraded_reason`；≠空成功。 |
| `hit` | 命中 | RAG 检索成功态。**A 链端点与 ragtrial 内部统一发射 `hit`**（上游 org-rag 的 `ok` 在边界归一为 `hit`；前端单值消费）。 |

RAG 六状态全集（`lib/ragtrial/store.mjs` `QUERY_STATES`）：
`hit`（命中）/ `empty`（无结果）/ `model_missing`（模型未注册）/ `index_stale`（索引过期）/
`provider_unavailable`（嵌入服务不可用）/ `error`（检索错误），另有 API 层 `backend_not_wired`、
`ready`、`degraded`。中文映射见 `console/frontend/src/status-map.js`（RAG 键空间）。

## 状态映射单源契约

- **单一权威源**：`console/frontend/src/status-map.js`。各状态族使用**独立键空间**，避免同名枚举
  语义/颜色冲突（如 `APPROVED` 在票据=已批准、FXV=审批通过、门禁=已批准修复，三键空间互不共用）。
- 覆盖键空间：`EXECUTION`（投递/执行 + console-pg·contract 执行族 SUCCEEDED/FAILED/SUPERSEDED）、
  `GATE`（人工门）、`SEVERITY`（严重度，含 `CRITICAL`）、`STAGE`（控制面阶段 8 值，含 UNKNOWN）、
  `OUTCOME`（运行结果摘要）、`FXV`（修复编排 23 态）、`FXV_ARTIFACT`（工件归档）、
  `CCHAIN` / `CCHAIN_OVERALL`（C 链）、`RAG`（检索服务）、`TICKET` / `TICKET_ACTION`（审批票据）。
- **未知枚举兜底**：label 保留原始机器值、note 标注"未知状态（原始枚举：X）"——永不吞掉机器值。
- 契约测试：`console/backend/test/status-map.test.mjs`（node --test，锁语义红线与覆盖度）。

## RAG 证据排除清单（单一权威常量）

`lib/ragtrial/review.mjs` `RAG_EXCLUDED_FROM = ['finding', 'ticket', 'gate', 'VERIFIED', 'fixer_patch_input', 'verifier_evidence']`
——RAG 检索结果仅作 reference-only 辅助引用，不得自动成为以上任何目标；所有提及处（policy.note、
rag_policy_note、usage_note、页面文案）与本清单同步更新。

## 产品术语

| 术语 | 定义 |
|---|---|
| **FXV** | Finding→Ticket→Fi**x**er→Test→**V**erifier 的修复编排子系统（`console/backend/lib/fxv/`）。状态机 23 态，权威定义 `orchestrator.mjs`；默认 dry-run，真实 GitHub 写入需显式授权。 |
| **A 链** | 组织知识检索链路（`/api/rag/org-search`）：org-rag 词法检索（或 ragtrial 内部接线），供 Review 辅助参考。 |
| **C 链** | 模型供应链信任链（cchain）：模型缓存内容寻址（manifest 校验）+ provider 在线 attestation + RUN_BINDING_AUTH 密钥分发，三组件真实探测，`/api/cchain/status`。 |
| **RAG** | 检索增强生成（Retrieval-Augmented Generation）。本项目 RAG 试验栈=独立 pgvector 索引 + MinIO 原文归档；结果一律带引用、reference-only。 |
| **reference-only** | 仅作人工参考引用的证据用法标记：不构成 finding/ticket/gate/VERIFIED/fixer 输入，Verifier 不接受。 |
| **staging** | 受控隔离联调环境（非生产）。console 语境下指隔离 PG 实时库（会话 allowlist 内）。 |
| **隔离（isolated）** | 与生产资源物理/逻辑分离的验证形态：隔离 PG 库、隔离工作区 dry-run、隔离 fixture。隔离验证通过≠生产验证。 |
| **审批** | 人工放行动作（/approvals 页；票据 APPROVE/REJECT 决策）。 |
| **审计** | 不可篡改的记录留痕（skill_gate_audit、fxv.audit_events、ticket_audit）。与"审批"是两件事：审批=做决定，审计=留证据。 |
| **凭据（credentials）** | 用户登录时输入的身份信息（用户名/密码）。登录失败提示"凭据无效"。 |
| **凭证（token/secret 类）** | 服务下发的会话凭证与令牌（mp_session/mp_csrf Cookie 等）。产品红线"不下发任何凭证"指此类。 |

## 拼写与惯例

- `TRIAL_READY` 是唯一机器状态拼写（勿用 `LOCAL_RAG_TRIAL_READY` 等变体；历史文档例外见上表）。
- commit message 惯例：`type(scope): 中文描述——补充细节`（type 前缀英文 + 正文中文）。
- API 契约文档（`distribution/docs/API-CONTRACTS.md`）保持英文（契约文档惯例）。
