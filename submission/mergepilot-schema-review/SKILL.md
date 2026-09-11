---
name: mergepilot-schema-review
version: "1.0.0"
description: >
  跨仓 Schema 变更安全验证 Skill 包：为数据库迁移候选创建隔离验证副本，
  逐项运行安全检查，核对数据质量，验证回滚可行性。
  Agent 通过 MCP 调用本 Skill 完成数据库变更的隔离验证流程。
author: MergePilot Team
license: Apache-2.0
tags:
  - database
  - migration
  - safety
  - multi-agent
  - branch-validation
  - polaradb-compatible
tools:
  - database_create_branch
  - database_validate_migration
  - database_assert_data
  - database_rollback_check
  - rag_retrieve
---

## Skill 1：`rag_retrieve` — 历史经验检索

| 字段 | 内容 |
| --- | --- |
| **用途** | 审查新 PR 前，从平台案例库检索相关的历史 Finding、修复案例与审查政策，减少模型对企业系统的臆测、误报和漏检 |
| **输入** | `query`（自然语言检索问题）、`top_k`（返回条数，默认 3） |
| **输出** | `query_hash`（SHA256 前 16 位，不保存原文）、`top_k`、逐条 `{document_id, chunk_id, score, source_ref, retrieval_mode, data_mode}` |
| **调用条件** | Reviewer 开始审查前；PR #4 流程中先于一切方案生成 |
| **依赖工具** | 本地确定性嵌入（hash-bow-256）· 无外部服务依赖 |
| **失败处理** | 检索为空返回 `EMPTY` 状态而非报错；服务不可达时 Agent 以 `UNAVAILABLE` 如实上报，不伪造结果 |
| **安全边界** | 只保存 query_hash（SHA256 前 16 位），不保存检索原文；返回仅含元数据与引用，不含文档正文；data_mode=SYNTHETIC 强制标注 |
| **复用价值** | 换企业自有语料仅需替换数据集文件，接口与契约零改动（已通过 env 门控预留） |
| **与多 Agent 协同的关系** | Reviewer 开始审查前的第一步；检索结果决定后续审查关注点与风险预估 |

**云端确认**：✅ Trace `f5bb3e1f…` 中 `execute_tool rag_retrieve`（6ms，成功，操作员确认）。

---

## Skill 2：`database_create_branch` — 创建隔离验证副本

| 字段 | 内容 |
| --- | --- |
| **用途** | 为每个数据库迁移候选创建隔离验证副本，使失败候选不影响正式业务数据 |
| **输入** | `candidate_id`（迁移候选编号） |
| **输出** | `branch_id`、`data_mode`（SIMULATED）、`polardb_connection`（NOT CONNECTED） |
| **调用条件** | 候选方案通过初步可行性筛选后；每个候选独立建副本，互不污染 |
| **依赖工具** | 内存状态机（SIMULATED 模式）；LIVE 模式将代理到 PolarDB Branch API |
| **失败处理** | 未知候选返回 `UNKNOWN_CANDIDATE`；副本创建失败时该候选直接标记淘汰 |
| **安全边界** | SIMULATED 模式下为内存状态机，不触碰任何真实数据库；LIVE 模式仅允许只读 + 隔离 Branch 操作，8 项接入门槛全绿才允许切换 |
| **复用价值** | 任何「先试后改」类数据库变更均可复用该隔离验证模式 |
| **与多 Agent 协同的关系** | Fixer/Verifier 在副本上执行验证，不影响正式业务数据 |

**云端确认**：✅ Trace 20:29:39 窗口中 `execute_tool database_create_branch`（0.34ms，成功，操作员确认）。

---

## Skill 3：`database_validate_migration` — 迁移安全逐项检查

| 字段 | 内容 |
| --- | --- |
| **名称** | `database_validate_migration` |
| **用途** | 在隔离副本上逐项运行迁移安全检查（历史脏数据回填、唯一性冲突、新旧程序兼容、枚举扩展等） |
| **输入** | `branch_id`、`candidate_id` |
| **输出** | `assertion_count`、`failed_assertions[]`、逐项 PASS/FAIL、裁决（VERIFIED / DEGRADED / REJECTED） |
| **调用条件** | 副本创建成功后立即执行；每个候选必须全量跑完 |
| **依赖工具** | 副本上只读执行，不修改数据 |
| **失败处理** | 任一检查未通过即给出失败清单；失败候选自动进入淘汰名单，禁止晋级 |
| **安全边界** | 只读检查，不执行真实迁移；裁决由检查结果推导，不允许人工改写 |
| **复用价值** | 检查项清单可按企业 Schema 规范扩展 |
| **与多 Agent 协同的关系** | Verifier 依赖此检查结果决定候选是否可进入人工门 |

**执行确认**：⚠️ 本地契约审计验证通过（candidate-a 2 项 FAIL 正确淘汰 · candidate-c 5/5 PASS 正确晋级）；云端逐条 span 确认仍在进行。

---

## Skill 4：`database_assert_data` — 副本数据质量核对

| 字段 | 内容 |
| --- | --- |
| **名称** | `database_assert_data` |
| **用途** | 核对迁移后副本的数据质量（脏数据清零、唯一约束生效、新枚举行数） |
| **输入** | `branch_id`、`candidate_id` |
| **输出** | `row_counts`（总行数/缺客户号/重复订单/新状态行数）、`passed` |
| **调用条件** | 迁移检查通过后执行，作为「数据层证据」 |
| **依赖工具** | 副本上只读聚合，不修改数据 |
| **失败处理** | 数据核对未通过时候选回退到淘汰名单 |
| **安全边界** | 只读聚合，不修改数据；结果随契约审计留痕 |
| **复用价值** | 核对维度可按企业数据质量规范扩展 |
| **与多 Agent 协同的关系** | Verifier 依赖此结果作为数据层证据 |

**执行确认**：⚠️ 本地契约审计验证通过；云端逐条 span 确认仍在进行。

---

## Skill 5：`database_rollback_check` — 回滚安全检查

| 字段 | 内容 |
| --- | --- |
| **名称** | `database_rollback_check` |
| **用途** | 验证副本可安全丢弃、正式主库未受任何影响——保证「失败候选零成本退出」 |
| **输入** | `branch_id`、`candidate_id` |
| **输出** | `main_untouched`、`branch_droppable`、`passed` |
| **调用条件** | 每个候选验证的最后一步，通过后才允许进入人工门 |
| **依赖工具** | 只读检查 |
| **失败处理** | 主库受影响属致命错误，候选直接淘汰并告警 |
| **安全边界** | 仅验证可回滚性，不执行真实回滚动作；LIVE 模式下同样只读 |
| **复用价值** | 「失败候选留在 Branch 中不影响正式数据」承诺的技术保证 |
| **与多 Agent 协同的关系** | Verifier 在候选淘汰前执行此检查作为安全证据 |

**执行确认**：⚠️ 本地契约审计验证通过；云端逐条 span 确认仍在进行。

---

## 可观测性（横向能力）

所有 Skill 的每次调用均自动产生 OTel GenAI 契约 span（`execute_tool <name>`），包含：工具名、arguments_hash、result_status、行数/文档数、时延、data_mode、source_refs——字段白名单强制，不记录正文与凭据。全部 span 合并进阿里云 AgentLoop 云端 Trace（`fbf4a3cec0493990d76e10a102418be1`），实现「Agent → LLM → Tool」全链路可审计。

## 诚实声明

| 项 | 状态 |
| --- | --- |
| rag_retrieve | ✅ 已在真实 Agent run 内执行并入云（f5bb3e1f 操作员确认） |
| database_create_branch | ✅ 已在真实 Agent run 内执行并入云（20:29:39 窗口确认） |
| database_validate/assert/rollback | ⚠️ 本地审计验证；云端逐条 span 确认仍在进行 |
| data_mode | 全部强制标注 SYNTHETIC / SIMULATED——不做虚假声明 |
