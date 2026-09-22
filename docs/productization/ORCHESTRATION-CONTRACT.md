# 编排互斥契约（ORCHESTRATION-CONTRACT）

**适用**：gh-bridge（现役）与 Workflow Controller（接管期）对 `github_deliveries` 的并发操作。
**目的**：验收场景 9——新旧编排路径不会同时执行同一任务；场景 8——旧执行者失去租约后不能覆盖新执行者状态。

## 1. 状态所有权

| 状态列 | 所有者 | 说明 |
|---|---|---|
| `github_deliveries.status/claim_id/claimed_at/error` | **当前持有该行有效 claim 的编排器** | 一切转移必须带精确 claim_id CAS |
| MinIO 项目 `meta.json` 状态/`result.md`/gate 记录 | **AgentTeams 控制面（Leader/Agent）** | 单次审查运行的权威进度与终态；编排器只读 |
| MinIO `check-run-receipt.json` | **发布成功的编排器** | 发布凭据；恢复路径据此免重发 |
| GitHub check-run | GitHub | 对账事实源（GET check-runs by head_sha） |

## 2. claim_id 命名空间（互斥的物理边界）

- 桥（新格式）：`<delivery_id[:14]>-bridge-<8hex>`；特征段 `-bridge-`。
- Controller（github_drain 合同）：claim_id 每次轮换，格式不含 `-bridge-` 特征段。
- **接管边界**：桥的 `take_over_stale()` 只接管 `claim_id LIKE '%-bridge-%'` 的过期 RUNNING——不会碰 Controller 的认领；反之 Controller 只接管其合同认定的过期租约。**任一方不得用 LIKE 覆盖对方命名空间的行。**

## 3. 转移规则（双方必须遵守）

1. **认领**：`UPDATE ... SET status='RUNNING', claim_id=<new>, claimed_at=now() WHERE delivery_id=? AND status='PENDING'`（或 Controller 的"过期 RUNNING 可接管"语义）——rowcount=1 才算持有。
2. **终结/中间态写入**：一律 `WHERE ... AND claim_id='<本次精确值>'`——被接管后 rowcount=0，写入自然失效（场景 8）。
3. **禁用 LIKE 终结**：LIKE 仅允许出现在"接管查询"里，不允许出现在状态写入里。
4. **接管必须换新 claim_id**：不允许沿用旧值写状态。

## 4. 恢复语义（桥侧已实现，Controller 接管时对齐）

崩溃后由接管方按**项目权威状态**续接（不重发 kickoff）：receipt→直接终结；项目终态→续发布；非终态→续观察；无项目→有界回队（`RQn` 计数，超限 MANUAL）。Controller 的等价物 = github_drain 的 stage_events 幂等 + run_id 派生复检。

## 5. 已验证 / 待验证

- ✅ 单测：精确 claim 终结（`test_finish_requires_exact_claim`）、接管 CAS 与格式（`test_takeover_*`）、恢复不重发 kickoff（`test_terminal_project_resumes_publish_without_kickoff` / `test_inflight_project_only_watches`）。
- ⬜ 待授权集成：真实服务器 PG 下双方并发认领互斥（kill -9 注入 + 双编排器同跑一轮）——列入 M4 前集成轮（见 ACCEPTANCE.md 底注）。

## 6. lease_expires_at 的客观边界（2026-09-22 代码核实）

桥的 `claim()` **不写** `lease_expires_at`（保持 NULL）。github_drain 的认领 CTE 对过期 RUNNING 的接管谓词是
`status='RUNNING' AND lease_expires_at < now()`——NULL 比较不成立，因此：

1. **Controller 不会抢走桥的在途行**（比 §2 的 LIKE 命名空间边界更强的客观保证，非约定依赖）；
2. 桥的接管谓词含 `%-bridge-%`，也不会碰 Controller 的行（UUID 字符集不含 `bridge`，`gen_random_uuid()` 不会撞命名空间）；
3. PENDING 行是中性命名空间：双方都会以 CAS 认领，每行单胜者——但两编排器**同时推进**仍违反场景 9 的运维约束（各自的项目派发/恢复语义不同），"同时只跑一个 drain"仍然是硬规则，不是可选项。

**接管切换（cutover）程序含义**：桥的在途 RUNNING 行（lease=NULL）对 github_drain **不可见**。从桥切到 Controller 时必须：
(a) 桥停止认领新行 → (b) 等桥把在途行全部终结或由桥自身 `take_over_stale()` 收敛完 → (c) 再启动 github_drain。
跳过 (b) 会让这些行成为"只有桥能回收"的孤儿（只能手工 SQL 修复）。此程序列入 Controller 接管工作项的验收前置。
