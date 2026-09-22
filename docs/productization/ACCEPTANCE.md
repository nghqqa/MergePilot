# M1 验收矩阵（ACCEPTANCE）

v2 备忘五节 M1 出口 = 故障注入清单全过。状态：✅已验证（单测级）／🔒已实现待集成／⬜未做。
**注**：以下"单测级"= 全 mock 单元测试（无真实 SSH/GitHub/Matrix/MinIO）；"真实集成"另计，见底注。

**证据基线勘误（2026-09-22 第二轮）**：gh_app 在干净树 8b30fb1 实测 = 821 collected（816 passed + 5 skipped）；前版记录的"831 passed"不可复现（tests/gh_app 自 b46e8ba 字节未变，差额 15 疑为当时未跟踪文件或转抄误差）。当前以实测为准。

| # | 场景（提示词四.） | 当前实现 | 缺口 | 验证方法 | 状态 |
|---|---|---|---|---|---|
| 1 | 认领后崩溃可恢复 | `take_over_stale()` CAS 接管 + `resume()` 续接；启动即执行 | 真实 PG/kill -9 注入待授权 | `test_takeover_cas_and_format` 等 7 项 | ✅单测 |
| 2 | 发任务后崩溃不重复有害副作用 | `resume()` 四路分流，绝不重发 kickoff；无项目有界回队 RQn | 同上 | `test_terminal_project_resumes_publish_without_kickoff`/`test_inflight_project_only_watches`/`test_missing_project_requeues_bounded` | ✅单测 |
| 3 | GitHub 已接受本地未记录可对账收敛 | `publish_with_retry` 首步 GET check-runs 采纳 + MinIO receipt 短路 | 真实 GitHub 验证待授权 | 单测 `test_reconcile_adopt_writes_receipt`、`test_receipt_short_circuit` | ✅单测 |
| 4 | 超时与重试有界 | `PUBLISH_ATTEMPTS=3` + 退避 (10s,30s)；watch 有 deadline 已实现（REQUEUE_MAX=2，RQn 计数，超限 MANUAL） | `test_bounded_retry_then_success`/`test_exhausted_returns_failure` | ✅单测 |
| 5 | 重复 webhook 不重复执行 | receiver 以 delivery GUID 为 PK INSERT（同 GUID 重发不新行）；桥 `already_processed` 按 repo+PR+head 查 PROCESSED 去重 | receiver PK 冲突路径的真实行为复核（单测已有 test_receiver.py 覆盖） | `test_already_processed_guard` | ✅单测 |
| 6 | PR 更新后旧结论不代表新 commit | check-run 绑定 observed_head_sha（结构保证）；新 head=新 delivery=新 run | 需一条实证：同 PR 两 head 两 check 并存 | M1-3 集成验证（待真实案例轮） | ⬜ |
| 7 | 回写失败不能标记投递成功 | `process()` 尾段：PROCESSED 仅当 pub.ok **且** 审查终态；失败=PUBLISH_FAILED(retryable)；timeout=TIMEOUT(manual) | — | `test_processed_only_after_publish_success`/`test_publish_failure_marks_recoverable_error`/`test_timeout_marks_manual_error_even_if_published` | ✅单测 |
| 8 | 旧执行者失去租约不能覆盖新状态 | `claim_id` 每次轮换（uuid 尾）；`finish(cid)` 精确等值匹配 | M1-2 的接管 CAS 补全 | `test_claim_id_rotates`/`test_finish_requires_exact_claim` | ✅单测 |
| 9 | 新旧编排不同时执行同一任务 | github_drain 合同：claim CAS + 过期 RUNNING 接管 + 一切确认以 claim_id；桥已对齐精确 claim 语义 | Controller 真实接管时需端到端验证互斥 | M1-3 契约文档 + 后续集成轮 | ⬜ |

## 终态语义映射（沿用 github_deliveries 既有状态词）

| 业务语义 | 载体 |
|---|---|
| 审查完成 | MinIO 项目 meta.json status ∈ {completed, blocked}（权威） |
| 结果等待发布 | delivery=RUNNING + 项目终态 + 无 receipt（瞬态） |
| 结果成功发布 | delivery=**PROCESSED**（note 含 check_run=id）+ MinIO receipt |
| 可恢复失败 | delivery=ERROR + note 前缀 `PUBLISH_FAILED(retryable)`（M1-2 回队接管） |
| 需人工处理 | delivery=ERROR + note 前缀 `TIMEOUT(manual)` 等 |

## 不承诺 exactly-once

去重（GUID PK + repo/PR/head 守卫）+ 有界重试 + 对账采纳（reconcile/receipt）+ 副作用边界（重复 POST check-run 是幂等可见噪音，非业务破坏；kickoff 重发由项目权威状态分流避免）。崩溃窗口内的最坏重复面 = 一次多余 check-run（对账采纳后收敛）。

## 真实集成验证（待授权项，非本轮单测可覆盖）

- 真实 SSH/服务器 PG 下 claim/finish SQL 行为；
- 真实 GitHub：reconcile GET 与 POST 201；同 PR 两 head 并存 check（场景6 实证）；
- 真实崩溃注入（kill -9 桥进程于各阶段）。
上述列入 M4 V0 内测前的集成轮，需用户授权启动服务器/真实案例（V0 允许仓库 nghqqa/fastapi-boilerplate-demo 在列）。

---

# M2 审批正确性（ACCEPTANCE-M2）

v2 备忘一.3 硬门槛 3 = "审批不越权：批准的语义、绑定对象、失效条件、权限人、并发竞争全部有测试"。
规格：M2-APPROVAL-SPEC.md ｜ 实现：tools/approval/approval.py ｜ 测试：tests/approval/test_approval.py（34 passed，2026-09-22）。

| 要求（v2 一.3） | 实现 | 测试层级 | 状态 |
|---|---|---|---|
| 批准的语义（动作集，merge 剥离） | ALLOWED_ACTIONS={generate_patch,run_poc,publish_result}；merge/close/revert 创建即 ValueError | 单测 test_action_set_excludes_merge / test_binding_rejects_merge_action | ✅单测 |
| 绑定对象（五元组） | Binding(run_id,repo,head_sha,action,params_hash,+补丁/finding指纹) frozen dataclass | test_valid_binding_roundtrip / test_params_hash_required_64hex / test_head_sha_must_be_full_40hex / test_repo_must_be_owner_slash_name | ✅单测 |
| 失效条件（PR 更新） | invalidate_for_new_head 标 INVALIDATED（幂等）；正确性独立于标记（执行校验按绑定拒绝新 run/head） | test_new_head_invalidates_active_ticket / test_invalidated_cannot_execute / test_invalidation_is_correctness_independent | ✅单测 |
| 权限人 | approve 要求 approved_by 非空（IDENT_REQUIRED）；**权限映射=D-2 未拍板** | test_approve_requires_identity | ✅机制单测；映射⬜未做 |
| 并发竞争（批准/拒绝） | PENDING 唯一分支态 + CAS 先到先得 + 幂等重放 NOOP | test_approve_then_reject_race_first_wins / test_reject_then_approve_race_first_wins / test_duplicate_approve_is_noop / test_duplicate_reject_is_noop / test_duplicate_create_returns_existing_ticket / test_new_attempt_allowed_after_terminal | ✅单测（单进程 CAS 语义） |
| 红线"批A执B" | check_execution 五元组逐字段匹配，先于副作用 | test_execution_binding_mismatch_rejected_per_field（run/repo/head/params/补丁逐字段）/ test_ticket_mismatch | ✅单测 |
| 单次有效 | USED 终态不可再执行；EXECUTING 可续验（恢复场景） | test_used_is_single_use | ✅单测 |
| 过期 | approve/start_exec 过期即拒；在途执行允许收尾 | test_expired_cannot_execute / test_expired_blocks_other_transitions_first / test_executing_completes_after_approval_deadline | ✅单测 |

**边界声明**：以上全部为纯逻辑层隔离单测（InMemoryTicketStore 参考存储）。真实 DB 存储、并发进程竞争、门 Web 页签发、真实审批人——均未实现、未验证。**D-1/D-2/D-3 未拍板前不接任何真实执行路径**（规格 §6）。M2 里程碑通过还差：存储落地 + 决策项拍板 + 门 Web 化，均未开始。
**2026-09-22 第三轮更新**：存储已落地——SQLite WAL（DECISIONS P-1，tests/approval/test_store_sqlite.py 7 项：跨连接竞争 CAS/并发幂等创建/崩溃重开/红线往返）；操作面 tools/approval/gate_cli.py（tests/approval/test_gate_cli.py 6 项：签发→批准→校验/绑定不匹配拒/过期拒/身份必填/未拍板提示显式）。**并发进程竞争与崩溃恢复已从"未验证"升级为"隔离验证通过"**；仍未做：真实审批主体接入（D-2）、门 Web 页、执行路径接线（D-1）。

---

# run 级版本清单（ACCEPTANCE-MANIFEST，备忘九.2）

实现：gh_bridge.py `build_manifest`/`write_run_manifest`/`prepare_run_manifest` ｜ 测试：tests/gh_bridge/test_run_manifest.py（13 项，2026-09-22 全绿）

| 要求（备忘九.2 / 提示词七.B） | 实现 | 状态 |
|---|---|---|
| 首次派发前持久化，派发引用内容摘要 | `process()`：seed→wake→manifest 持久化→kickoff 追加 `run-manifest: sha256`→发送；write 失败即 ERROR 不派发（fail-closed） | ✅单测 |
| 被审查代码 SHA | code.repo/head_sha/base_sha（delivery 原值） | ✅ |
| 提示词内容摘要 | prompt.kickoff_base_sha256（发送的 kickoff 基底+SPEC 的 SHA） | ✅ |
| 编排代码版本 | orchestrator.bridge_source_sha256 + git_commit（取不到→null+missing） | ✅ |
| 镜像摘要 | workers.images（派发时 docker inspect 四 worker；不可得→null+missing） | ✅单测（真实值待集成轮） |
| 模型标识/生成参数、Skill 内容哈希、RAG 版本 | 桥不可见 → **null + missing[] 显式标注，不伪造** | ✅机制；真实值源⬜未做（需 worker 侧上报，后续工作项） |
| 有效配置摘要 | config.canonical（allowlist/房间/超时/重试参数）+ sha256；无秘密字段 | ✅单测（secret-shape 断言） |
| 不可变：运行中升级不改变既有 run 清单 | write-once：已存在且哈希一致→采纳；不一致→拒绝覆盖 | ✅单测 |
| 恢复只读不重写 | resume() 只读清单并记日志；缺失不阻断恢复（前期 run 无清单属已知缺失） | ✅单测 |

**边界声明**：单测级（mock MinIO/docker）。真实 MinIO 写入、真实 worker 镜像读取、以及"清单中 missing 项的 worker 侧上报"（模型/RAG/Skill 内容哈希）——未验证/未实现。当前状态 = **可追溯骨架**，不等于备忘九.1 的"完整可复算"。

---

# 成本计量（ACCEPTANCE-COST，备忘四.4 / V0 硬门槛 4）

实现：tools/costmeter/（core.py 预算守卫 + collect.py 收集器）｜ 测试：tests/costmeter/test_costmeter.py（16 项，2026-09-22 全绿）
**SCAFFOLD 声明：以下是脚手架的逻辑层验证，不等于硬预算控制已完成**；未接入任何真实调用路径（预算金额未定，不构成消费授权）。

| M4 前验证要求（提示词七.C） | 脚手架逻辑 | 单测 | 真实路径接入 |
|---|---|---|---|
| 实际调用前检查并预留额度 | `reserve()` 余额不足抛 BudgetExceeded | test_over_limit_denied 等 | ⬜未接（挂点待定：桥派发前 or worker agentloop） |
| 重试计入预算 | `reserve(retry_of=)` 沿用原预留，不重复占额 | test_retry_reuses_reservation | ⬜ |
| 调用结束后结算 | `commit(actual)` 多退少补 | test_commit_* | ⬜ |
| 超限阻止后续调用 | reserve fail-closed；结算面超余额也拒 | test_commit_over_estimate_beyond_budget_denies | ⬜ |
| 并发预留不重复使用同一额度 | 进程内锁 + 20 线程并发预留恰好 10 成功 10 拒 | test_parallel_reserves_never_oversubscribe | ⬜（跨进程需 DB/存储层，未做） |
| usage 缺失明确处理 | 按预留消耗 + 记 gap（保守），gap 计数入 status | test_usage_missing_consumes_estimate_and_records_gap | ⬜ |
| 执行崩溃明确处理 | 台账 write-through + 重启 load + 过时预留回收 | CrashRecoveryTests | ⬜ |

**数据源事实（2026-09-22 核对）**：token 级 usage 仅存在于 worker OTel LLM spans（`gen_ai.usage.input/output_tokens`），经 OTLP 导出外部 collector；本地容器只有 span 名序列与审计日志；rag toolspans 在 :4184（未运行）。故本地收集器只产出**调用计数面**（tokens=None + missing 标注），token/币值面 ⬜ 未接通。币值换算仅在显式提供价目表时进行，缺价模型返回 unknown_models，不伪造。
**M4 前还差**：真实 usage 源接入（OTel collector 查询或 worker 本地台账）、预算挂点接入派发/调用路径、预算金额拍板、跨进程预留存储、端到端验证——均未开始。
**重试语义（2026-09-22 复查）**：`reserve(retry_of)` 只做**预留去重**（同一逻辑调用不重复占额）；真实重发的模型调用照常计费，结算必须传累计实际用量——test_retry_reissued_call_commits_cumulative_cost 固化该契约。
**接入点准备（2026-09-22 第七轮）**：costmeter/hooks.py 工厂——`MERGEPILOT_RUN_BUDGET_TOKENS` 配置即返回 fail-closed hook（超限拒、台账崩溃恢复，测试覆盖），未设置返回 None（不检查、不产生授权）。接入点盘点：①桥派发边界 ②SerialReviewExecutor.budget_hook（两者已可直接传入）③worker 模型调用面（R5 镜像层，未接）。硬预算"已完成"仍以端到端验证为准。

---

# RAG 接入验收（ACCEPTANCE-RAG，2026-09-22 新增；完整链路现状见 RAG-AUDIT.md）

实现：tools/rag/corpus_tool.py + tools/rag/corpus/（语料事实源）+ tools/rag/live/（服务事实源）+ 桥 rag 快照绑定/派发门 ｜
测试：tests/rag_live/（20：第 1+2 层）+ tests/gh_bridge/test_rag_gate.py（9）｜ 全部 121 passed（2026-09-22 实测）。
**层级标注**：第 1 层=单测；第 2 层=本地真实检索集成（真实 rag-live-server.mjs 进程+真实 HTTP）；第 3 层=模拟模型调用链；第 4 层=真实模型端到端。

| 条目 | 实现 | 验证 | 层级 |
|---|---|---|---|
| RAG-1 真实语料真实检索 | BM25 词法（lexical-zh-en-v1）+ 组织安全标准语料（repo 事实源副本） | test_02 已知文档命中且来源/片段字段齐 | 第2层✅ |
| RAG-2 接入实际审查调用路径 | worker MCP rag_retrieve（hook 注入，2026-09-21 激活日志）+ kickoff 提示词引导 | 历史审计 64 次调用（58 OK）+ result.md 实际引用；**当前代码版本未复跑** | 历史证据；复跑=第4层待授权 |
| RAG-3 来源/片段/版本可核对、关联 run | source_ref+chunk_id 返回并强制引用规则；**run 关联靠 manifest 时间窗+query_hash（审计 JSONL 无 run_id——运行时变更待授权 R5 扩展）** | test_04 审计记录（query_hash+source_refs，无查询明文） | 第2层✅ + 1项待授权 |
| RAG-4 派发前固定快照 | manifest.rag.snapshot_id=语料内容寻址 sha256（corpus_tool，幂等导入/更新即新快照）；任何内容变化≠旧 run 身份 | test_05 快照一致 + test_07 变更即新 ID + 双副本（repo/r3work）snapshot 实测一致 | 第1+2层✅ |
| RAG-5 结论可追溯检索证据 | citation_rule 强制 + 历史结论实际引用 source_ref | 历史证据（elemiso-pr2rag-gate/result.md） | 历史；当前复跑待授权 |
| RAG-6 失败语义显式 | 服务不可达=结构化 unreachable；合法空=HTTP200空（test_03）；`MERGEPILOT_RAG_REQUIRED=1` 时快照不可读/服务不可达→**拒派发 ERROR，不静默降级**（默认 advisory=现状，状态入 manifest 可见） | test_08 + rag gate 9 测试 | 第1+2层✅ |
| RAG-7 注入语料不改权限/审批 | 检索服务无指令执行面（输出即 JSON 数据，test_06）；审批/权限在 tools/approval+gateway 与 RAG 内容无关；模型层遵从=第3/4层 | 第2层✅（工具层）；模型层待授权 |
| RAG-8 计量接入 | rag-live 审计 JSONL 记录延迟/命中数；成本计量见 ACCEPTANCE-COST（token 面 ⬜） | 第2层✅（延迟/计数）；token 面待 R6 |

**明确未验证/未做**：第 3/4 层（真实 agent 复跑 + 真实模型端到端）待 R1/R2/R4 授权；B 案例链（skill_case_retrieval）非零命中行为未验证；语料部署同步到运行环境（r3work/rag-live）待授权（INTEGRATION-AUTH-REQUESTS R7）。**RAG V0 验收未整体通过**——第 1/2 层达成，第 3/4 层待真实环境。

---

# 架构 v3 验收（ACCEPTANCE-ARCHV3，2026-09-22 新增；设计见 ARCHITECTURE-V3.md）

实现：tools/orchestrator/（risk/stages/scheduler/aggregate/verify_finding/console_contract/flag）｜ 测试：tests/orchestrator/test_v3.py（41 项，全本地确定性，2026-09-22 全绿）
**层级声明：本节全部为本地纯逻辑/线程级验证。真实 Agent、真实 RAG、真实 GitHub 集成未验证；feature flag 默认关闭，旧串行链未删未改。**

| 验收项 | 测试 | 状态 |
|---|---|---|
| Trivial/Lite/Full 三档路由 | test_trivial_route / test_lite_route_by_lines / test_lite_route_by_files / test_full_route_by_size | ✅本地 |
| 敏感路径强制升级（小 diff 不豁免） | test_sensitive_path_forces_full_even_tiny_diff / test_sensitive_full_includes_all_specialists | ✅本地 |
| 规则可配置+可解释 | test_custom_thresholds_and_reasons / test_grade_dict_shape | ✅本地 |
| 两审查器并行 | test_two_reviewers_run_in_parallel（双 barrier 并行证明） | ✅本地线程级 |
| 串行模式（并发=1） | test_serial_mode_when_concurrency_one | ✅本地 |
| findings 去重且来源保留 | test_exact_dedupe_keeps_all_sources / test_near_duplicate_merged_by_title_similarity / test_distinct_findings_not_merged / test_severity_max_and_ranking / test_same_path_different_category_not_merged | ✅本地 |
| verifier 不读其他 agent 推理 | test_input_rejects_reasoning_fields / test_base_verifier_has_no_access_path（类型构造性隔离） | ✅本地 |
| 单审查器超时但其他可发布 | test_noncritical_timeout_others_publishable（→REVIEW_PARTIAL 不伪装） | ✅本地 |
| 关键审查器超时不能标通过 | test_critical_timeout_never_passes / test_critical_timeout_not_pass（→MANUAL_ATTENTION） | ✅本地 |
| run 级生命周期分离（完成/发布/待批/修复/补丁验证/回写） | test_lifecycle_stage_separation | ✅本地 |
| PR 更新使旧 run 失效 | test_pr_update_cancels_old_run（CANCELLED≠完成） | ✅本地 |
| 重试预算耗尽/重试后成功 | test_retry_budget_exhaustion / test_retry_then_success / test_retry_reentry_and_attempt_counting | ✅本地 |
| 全局预算共享（超限阻断） | test_global_budget_hook_blocks | ✅本地 |
| fixer 仅在门启用后进计划 | test_fix_requires_gate_enabled / test_plan_with_gate_includes_patch_validation | ✅本地 |
| 多 PR 并发上限 1 不擅自调高 | test_global_pr_concurrency_constant | ✅本地 |
| 补丁验证独立于 finding 验证 | 两独立计划步骤 + gate→fix→patch 顺序断言 | ✅本地 |
| 控制台展示部分完成/降级（非单一绿色） | test_partial_completion_visible_not_single_green / test_contract_fields_present / test_rag_snapshot_in_console | ✅本地 |
| SQLite 单实例边界 + TicketStore 接口抽象 | tests/approval/test_store_sqlite.py::InterfaceConformanceTests + DECISIONS P-1 | ✅本地 |
| RAG snapshot 与 run 一致 | run-manifest 机制（ACCEPTANCE-RAG RAG-4）+ 控制台字段 | ✅机制 |
| 真实 Agent 并行/真实 RAG/真实 GitHub | — | ⬜待授权（R1/R2，最后执行） |

**边界**：v3 未接线 bridge/Controller（flag 默认关）；调度权唯一归属 DispatchPlanner，无第二调度事实源；run 硬上限为配置项（run_hard_deadline_s），执行器级强制随真实 Agent 适配落地。

---

# M3.5 验收：v3 本地接线与只读可观测闭环（ACCEPTANCE-M35，2026-09-22）

实现：tools/orchestrator/adapter.py + runstore.py（RunStore）+ bridge `v3_shadow_hook` + tools/console_v3/server.py ｜
测试：tests/orchestrator/test_adapter_vertical.py + test_runstore.py + tests/gh_bridge/test_v3_hook.py + tests/console_v3/test_server.py ｜ 全套 **197 passed**（2026-09-22 实测）
**层级声明：shadow=真实 PR 形状数据+零 Agent 零外部写；fixture=本地假审查器全链路。二者都不是真实 Agent 验证。**

| 验收项（M3.5 提示词六） | 测试 | 状态 |
|---|---|---|
| 1 off 模式旧链路行为不变 | test_off_mode_no_adapter_invocation_legacy_unchanged（旧结论逐项不变+适配器零调用） | ✅本地 |
| 2 shadow 只读且无外部副作用 | test_shadow_with_real_shape_diff_readonly（external_writes=none，仅 1 次只读 GET） | ✅本地 |
| 3 v3 计划与 run-manifest 一致 | manifest_hash=证据内容寻址 sha256 存库；计划/阶段/档位入记录 | ✅本地 |
| 4 状态机与控制台读模型一致 | test_full_chain_...outcome（store→build_read_model 逐字段断言） | ✅本地 |
| 5 PR 更新不继续用旧 run | test_pr_update_supersedes_old_run / test_shadow_pr_update_supersedes_and_cancels（RUNNING 维度 CANCELLED，superseded 标记） | ✅本地 |
| 6 并行计划不重复派发 | 双 barrier 并行证明（test_v3）+ fixture 双审查器各执行一次（来源计数断言） | ✅本地 |
| 7 budget hook 阻止超预算计划 | test_budget_exhaustion_blocks_plan / test_global_budget_hook_blocks | ✅本地 |
| 8 SQLite 重启后状态恢复 | test_restart_recovery（RunStore）/ test_crash_recovery_reopen（TicketStore） | ✅本地 |
| 9 关键超时不派生为通过 | test_critical_reviewer_timeout_never_passes（→MANUAL_ATTENTION） | ✅本地 |
| 10 RAG snapshot 缺失按既定降级 | shadow：rag_snapshot=None 记录 missing[]（既有）；diff 不可得→risk FAILED 降级不伪造（test_shadow_diff_unavailable...） | ✅本地 |

**持久化清单核验**：run_id/repo/PR/head/base/mode/risk_tier/plan/维度状态/coverage_missing/降级原因/finding_validation/patch_validation/RAG snapshot/manifest_hash/时间戳——全部落 RunStore（v3_runs 表，SQLite WAL 单实例边界沿用 P-1）。
**控制台核验**：GET-only（写方法 405）；shadow/fixture 徽标；部分完成不压缩为成功；无批准/拒绝/派发/写按钮。
**未验证**：真实 Agent 接入（mode=on 的真实路径，待 R1/R2）；shadow 对真实 GitHub 匿名 GET 的线上行为（本地下不可测）；共享环境部署（永不自动）。

## M3.5 复核与冒烟（2026-09-22 第六轮补记）

- **复核九项清单**：1-8 通过；第 9 项（fail-soft 吞错误）发现缺口→修复：hook 异常持久化至 v3_hook_errors 表 + `GET /api/hook-errors`（3 项新测试：record/list、桥失败落痕、端点只读）。
- **取代保真**：_supersede_old_runs 重建记录丢失 error/时间戳→已保留（测试断言 SUCCEEDED 维度 error 不丢失）。
- **本地冒烟（真实服务进程 :4191 + 浏览器）**：/healthz、/api/runs、/api/runs/<id>、页面全部浏览器访问并截图；shadow run（MANUAL_ATTENTION，红色，coverage 三缺失，degradations 带原因）与 fixture run（REVIEW_COMPLETED，绿色）同屏对照；POST/PUT/DELETE=405；页面无写按钮。视觉检查**已完成**（截图为证，存会话工件）。
- 复核后全套 **200 passed**。


---

# 授权前准备（ACCEPTANCE-PREP，2026-09-22 第七轮）

实现：tools/integration_prep/（steps 声明式计划 + collect 证据脱敏 + authgate 闸门）｜ 测试：tests/integration_prep/test_prep.py（12 项）

| 项 | 状态 |
|---|---|
| R1 计划覆盖 认领后/派发后/发布前 三阶段 + 清场 | ✅ test_r1_covers_three_crash_stages_and_cleanup |
| R3 计划覆盖 回写成功/重复 webhook/PR 更新/失败恢复/旧 run 失效 | ✅ test_r3_covers_required_scenarios |
| 计划为声明式（含证据点，无可执行破坏串） | ✅ test_plans_are_documentation_only / test_all_five_plans_exist_with_evidence |
| 授权闸门默认拒绝；仅 MERGEPILOT_IT_AUTH=1 放行；他值不放行 | ✅ AuthGateTests（3 项） |
| 证据脱敏（gh-token/DSN/API key/OTel key，JSON 键值引号容忍） | ✅ RedactTests（5 项） |
| R4/R7 同步计划含 备份→校验→同步→off 冒烟/核对→回退 | ✅ steps.py（未执行，属授权后操作） |
| rag-live /health 暴露 corpus_file_sha256；search 可选 run_id 落审计 | ✅ test_01_health.../test_01b_run_id_passthrough（rag_live 套件） |

**边界**：本节全部为本地准备与测试；~~R1-R7 实际执行仍未授权未发生~~ → **2026-09-22 更新：R4/R7 已获用户批准并执行完成**（R1/R2/R3 仍未授权未发生）。

## R4/R7 执行验收（2026-09-22 第八轮，证据见 R4-R7-EXECUTION-RECORD.md）

| 批准条件 | 验收结果 |
|---|---|
| 按 integration_prep 既定步骤 | ✅ R4_SYNC_PLAN/R7_SYNC_PLAN 逐步骤执行 |
| 先备份并记录摘要 | ✅ 3 份 .bak（sha256 记录于 EXECUTION-RECORD） |
| 保持 MERGEPILOT_REVIEW_V3=off | ✅ off 冒烟：adapter 从 r3work 加载、hook 零日志、RunStore 未动 |
| 同步后旧链路行为不变 | ✅ status 只读 exit 0；台账/kickoff/回写语义零改动；桥未在跑，下次启动才生效 |
| rag-live 仅健康检查+本地检索 | ✅ /health 一致；检索命中正确；run_id 入审计；**服务已停止** |
| 校验失败/冒烟异常即回退 | 未触发（全部通过）；回退命令已存档 |
| 不执行 R1/R2/R3 | ✅ 零操作 |

## 运行前门禁（ACCEPTANCE-PRERUN，2026-09-22 第九轮）

实现：tools/integration_prep/prerun_gate.py（九项检查，全注入探针，门函数零 IO）｜ 测试：tests/integration_prep/test_prerun_gate.py（8 项）
检查项：git 固定+干净 / 运行副本 sha 一致 / 语料快照一致 / v3 模式符合期望 / 唯一执行者（无在跑桥+台账零 RUNNING）/ 容器在位 / RAG 按需健康 / **预算硬边界（MISSING_ACK 即 FAIL，不为开跑放宽）** / 投递前置（head 未处理过）。
真实案例启动前必须全绿；任一 FAIL 不启动。