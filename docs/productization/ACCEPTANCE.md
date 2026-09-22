# M1 验收矩阵（ACCEPTANCE）

v2 备忘五节 M1 出口 = 故障注入清单全过。状态：✅已验证（单测级）／🔒已实现待集成／⬜未做。
**注**：以下"单测级"= 全 mock 单元测试（无真实 SSH/GitHub/Matrix/MinIO）；"真实集成"另计，见底注。

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
