# MergePilot 核心 Skill 清单

> 按复赛指南要求提供：每个 Skill 说明用途、输入输出、调用条件、失败处理、安全边界及复用价值。
> 全部 Skill 均在真实 AgentTeams/CoPaw 环境中注册并由 Agent 调用。云端 Trace 关联边界如实说明：`rag_retrieve` 已在一次真实 Agent run 中执行并与阿里云 AgentLoop 云端 Trace 关联（已确认样本，trace `f5bb3e1f…`）；平台级 Agent+LLM+Tool 合并 Trace 为历史权威 Trace（已确认样本 n=1，`fbf4a3ce…`）；数据库类 Skill（2–5）当前为 SIMULATED 契约服务，span 仅留存本地契约审计，未做云端确认。详见文末「可观测性 Skill」。

## 总览

| # | Skill 名称 | 职能域 | 一句话用途 | 数据模式 |
| --- | --- | --- | --- | --- |
| 1 | `rag_retrieve` | 检索 | 从历史案例库检索相关经验与风险 | SYNTHETIC/REDACTED |
| 2 | `database_create_branch` | 数据库 | 为迁移候选创建隔离验证副本 | SIMULATED |
| 3 | `database_validate_migration` | 数据库 | 在副本上逐项运行迁移安全检查 | SIMULATED |
| 4 | `database_assert_data` | 数据库 | 核对副本数据质量（脏数据清零） | SIMULATED |
| 5 | `database_rollback_check` | 数据库 | 验证副本可弃、主库未动 | SIMULATED |
| 6 | `probe_comparison` | 验证 | 独立探针比对修复前后行为（HTTP 200→404） | REPLAY |

> 运行环境说明：1 号 Skill 通过 MCP stdio 服务由 Agent 进程拉起（`rag-mcp-server.mjs`）；2–5 号当前以平台内契约服务形式提供（PolarDB LIVE 门槛满足后切换真实实例，接口不变）。6 号为 Verifier 的独立验证 Skill，已随三案例回放验证。

---

## 1. `rag_retrieve` — 历史经验检索

- **用途**：审查新 PR 前，从平台案例库中检索相关的历史 Finding、修复案例与审查政策，减少模型对企业系统的臆测、误报和漏检。
- **输入**：`query`（自然语言检索问题）、`top_k`（返回条数，默认 3）。
- **输出**：`query_hash`（检索编号，不保存原文）、`top_k`、逐条 `{document_id, chunk_id, score, source_ref, retrieval_mode, data_mode}`。
- **调用条件**：Reviewer 开始审查前；PR #4 流程中先于一切方案生成。
- **失败处理**：检索为空返回 `EMPTY` 状态而非报错；无引用来源的答案系统层面禁止标记为「已验证」；服务不可达时 Agent 以 `UNAVAILABLE` 如实上报，不伪造结果。
- **安全边界**：只保存 query_hash（SHA256 前 16 位），不保存检索原文；返回仅含元数据与引用，不含文档正文；data_mode=SYNTHETIC 强制标注。
- **复用价值**：换企业自有语料仅需替换数据集文件，接口与契约零改动（已通过 env 门控预留）。
- **真实执行证据**：云端 Trace `f5bb3e1f…` 中 `execute_tool rag_retrieve`（6ms，成功）；本地契约审计 tool-spans.jsonl 同步留痕。

## 2. `database_create_branch` — 创建隔离验证副本

- **用途**：为每个数据库迁移候选创建隔离验证副本，使失败候选不影响正式业务数据。
- **输入**：`candidate_id`（迁移候选编号）。
- **输出**：`branch_id`、`data_mode`、`polardb_connection`。
- **调用条件**：候选方案通过初步可行性筛选后；每个候选独立建副本，互不污染。
- **失败处理**：未知候选返回 `UNKNOWN_CANDIDATE`；副本创建失败时该候选直接标记淘汰。
- **安全边界**：SIMULATED 模式下为内存状态机，不触碰任何真实数据库；LIVE 模式仅允许只读 + 隔离 Branch 操作，8 项接入门槛全绿才允许切换。
- **复用价值**：任何「先试后改」类数据库变更均可复用该隔离验证模式。

## 3. `database_validate_migration` — 迁移安全逐项检查

- **用途**：在隔离副本上逐项运行迁移安全检查（历史脏数据回填、唯一性冲突、新旧程序兼容、枚举扩展等）。
- **输入**：`branch_id`、`candidate_id`。
- **输出**：`assertion_count`、`failed_assertions[]`、逐项 PASS/FAIL、裁决（VERIFIED / DEGRADED / REJECTED）。
- **调用条件**：副本创建成功后立即执行；每个候选必须全量跑完。
- **失败处理**：任一检查未通过即给出失败清单；失败候选自动进入淘汰名单，禁止晋级。
- **安全边界**：只读检查，不执行真实迁移；裁决由检查结果推导，不允许人工改写。
- **复用价值**：检查项清单可按企业 Schema 规范扩展。

## 4. `database_assert_data` — 副本数据质量核对

- **用途**：核对迁移后副本的数据质量（脏数据清零、唯一约束生效、新枚举行数）。
- **输入**：`branch_id`、`candidate_id`。
- **输出**：`row_counts`（总行数/缺客户号/重复订单/新状态行数）、`passed`。
- **调用条件**：迁移检查通过后执行，作为「数据层证据」。
- **失败处理**：数据核对未通过时候选回退到淘汰名单。
- **安全边界**：只读聚合，不修改数据；结果随契约审计留痕。
- **复用价值**：核对维度可按企业数据质量规范扩展。

## 5. `database_rollback_check` — 回滚安全检查

- **用途**：验证副本可安全丢弃、正式主库未受任何影响——保证「失败候选零成本退出」。
- **输入**：`branch_id`、`candidate_id`。
- **输出**：`main_untouched`、`branch_droppable`、`passed`。
- **调用条件**：每个候选验证的最后一步，通过后才允许进入人工门。
- **失败处理**：主库受影响属致命错误，候选直接淘汰并告警。
- **安全边界**：仅验证可回滚性，不执行真实回滚动作；LIVE 模式下同样只读。
- **复用价值**：「失败候选留在 Branch 中不影响正式数据」承诺的技术保证。

## 6. `probe_comparison` — 独立探针行为比对（PR #2 验证 Skill）

- **用途**：Verifier 用独立探针比对修复前后的外部行为（修复前路径穿越返回 200，修复后返回 404），不信任 Fixer 的自述。
- **输入**：目标接口与测试载荷。
- **输出**：`before`/`after` 状态码与响应摘要。
- **调用条件**：Fixer 完成修复后、任务关闭前。
- **失败处理**：探针结果与预期不符时验证失败，任务退回。
- **安全边界**：探针为只读请求；结果写入 MinIO 任务证据，随 SHA256 清单锁定。
- **复用价值**：探针模式可推广到任意「修复前后行为比对」类验证。

---

## 可观测性 Skill（横向能力）

所有 Skill 的每次调用均在平台侧产生契约审计记录（本地 `tool-spans.jsonl`，逻辑名 `rag.retrieve` / `database.*`，运行时工具名 `execute_tool <name>`），包含：工具名、arguments_hash、result_status、行数/文档数、时延、data_mode、source_refs——字段白名单强制，不记录正文与凭据。

云端关联的真实边界（如实说明，不拔高）：

- **已云端确认（已确认样本）**：`rag_retrieve` 在一次真实 Agent run 中执行，`execute_tool rag_retrieve` 与 `agent_step / invoke_agent / genai.llm.call / chat deepseek-chat` 同属云端 Trace `f5bb3e1f…`；平台级 Agent → LLM → Tool 合并 Trace 以历史权威 Trace `fbf4a3cec0493990d76e10a102418be1`（2026-08-30，Agent 1 / LLM 24 / Tool 8）为已确认样本 n=1。
- **仅本地契约审计、未做云端确认**：数据库类 Skill（2–5）当前以平台内契约服务形式运行在 SIMULATED fixture 上，其 span 只留存于本地 `tool-spans.jsonl`，不声称已合并进云端 Trace。
- 不声称多轮稳定覆盖，不声称全部 Skill 调用均已云端确认；云端可见性为操作员控制台确认，非 API 级可复现查询记录。
