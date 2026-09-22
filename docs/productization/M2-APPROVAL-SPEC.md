# M2 审批票据规格（APPROVAL SPEC）

**日期**：2026-09-22 ｜ **依据**：产品化备忘 v2 二.2a（四问）+ V0 范围一节 ｜ **状态**：规格 v1（绑定校验器已实现，见 tools/approval/）

## 0. 目的与红线

审批回答一个问题：**人是否允许系统执行某个具体动作**。红线（v2 二.2a）：

> "用户批准了 A、系统执行了 B" 是设计红线。

实现手段 = 票据只绑定一个不可变的执行绑定（§2），执行方在执行前必须出示与绑定逐项一致的请求，校验器任一项不一致即拒绝（fail-closed）。

## 1. 四问回答（已定机制 vs 待定政策）

### 四问 1：批准的具体动作是什么？

**V0 动作集（候选，机制已定，启用集合是决策项 D-1）**：

| 动作 | 含义 | V0 状态 |
|---|---|---|
| `generate_patch` | 允许对某 finding 生成修复补丁（结果=补丁文件+指纹，不触仓库） | 候选 |
| `run_poc` | 允许在隔离沙箱对某 finding 执行 PoC 验证 | 候选 |
| `publish_result` | 允许把 blocked 结论推进为含修复建议的最终结果并投递 check-run | 候选 |
| ~~push_branch~~ | 推送修复分支 | **V0 明确排除**（v2 二.2b：不推代码，维持低权限卖点） |
| ~~merge / close / revert~~ | 旧 L2 合并审批动作 | **剥离，不迁移**（见 §5） |

无需票据的动作：只读操作（clone、只读 API、审查本身）。审查与投递 check-run 是产品默认闭环，**不**设门（否则每个 run 都要人点一次，违背"人工确认"而非"人工放行"的定位）。

### 四问 2：票据绑定什么？

五元组 + 环境指纹，缺一不可（校验器强制）：

1. `run_id` — 一次审查运行的唯一标识；
2. `repo` — owner/name 全名；
3. `head_sha` — 被审查 commit 的完整 SHA（40 hex）；
4. `patch_fingerprint` — 该 finding 对应补丁内容的 sha256（64 hex）；无补丁动作（如 run_poc）绑 finding 指纹；
5. `params_hash` — 执行参数的规范序列化 sha256（防"批准了温和参数、执行了危险参数"）。

附加油 irrespective 上下文（记录但不参与匹配）：`finding_id`、`ticket_id`、`created_at`、`created_by_run`、`expires_at`。**绑定内不得出现明文秘密**（params_hash 只存哈希）。

### 四问 3：PR 更新后旧批准是否立即失效？

**是（已定，对应 V0 硬门槛 1）**。规则：

- 票据绑 `head_sha`；同一 PR 出现新 head ⇒ 新 delivery ⇒ 新 run。
- 旧票据不自动删除，但**永久不可执行**：执行校验要求 run 与 head 同时匹配，新 run 必然 mismatch。
- 编排侧收到新 head 的 delivery 时可主动调 `invalidate_for_new_head()` 将旧活动票标 `INVALIDATED`（幂等、仅未终结票可标）——用于门页面即时显示"已失效"，非正确性依赖（正确性由执行校验独立保证）。

### 四问 4：谁有权批准？重复点击与批准/拒绝竞争如何仲裁？

- **身份**：票据记录 `approved_by`（非空才能转 APPROVED）。**权限映射（哪个用户可批哪个仓库/动作）待 Web 控制台登录设计（决策项 D-2）**；V0 内测前必须有至少一个具名审批人机制，不得匿名放行。
- **重复创建**：同一 `(run_id, action, finding_id)` 已有活动票（PENDING/APPROVED/EXECUTING）时，重复请求**返回既有票**（幂等），不新建；终态票（REJECTED/EXPIRED/INVALIDATED/FAILED）之后允许新 attempt（attempt_no+1），与 l2 的 attempt 惯例一致。
- **批准/拒绝竞争**：PENDING 是唯一可分支状态。`approve` 与 `reject` 都是 CAS 转移（要求前置态 PENDING），**先到先得**；后到者收到 `INVALID_TRANSITION`，不生效、不覆盖。APPROVED 之后不可 reject；退出途径只有 EXECUTING→USED/FAILED 或 EXPIRED。
- **重复批准/重复拒绝**（幂等重放）：对已处目标态的票重复同一动作返回 `NOOP`，不算错误也不改变状态。

## 2. 状态机

```
                 create
                   │
                   ▼
              ┌─PENDING──────── reject ──► REJECTED (终态)
              │      │    │
        approve│    expire（approval_expires_at 过）
              │      ▼    ▼
              │  APPROVED EXPIRED (终态)
              │      │
        start  │      ▼
              └─► EXECUTING ── complete ──► USED (终态：动作已完成且结果指纹已回填)
                     │  │
                 fail│  └─（执行中失去租约→接管方按项目状态续接，M1 语义）
                     ▼
                  FAILED (终态，允许新 attempt)
```

- 所有转移单方向、CAS（要求前置态精确匹配）；并发转移先到先得。
- `USED` 语义 = **批准的动作已完成且结果可追溯**（如补丁指纹已登记），不再表示"PR 已合并"。
- `INVALIDATED`（由新 head 触发）等价终态，允许新 attempt（属于新 run）。

## 3. 执行前校验（红线防线）

执行方（桥/Controller/门 Web 的执行入口）在执行任何被批动作前调用：

```
validate_execution(ticket, request) → (ok, reason)
```

request 必须出示：`ticket_id, run_id, repo, head_sha, patch_fingerprint/finding_fp, params_hash`。规则：

1. 票据状态必须 ∈ {APPROVED, EXECUTING}（PENDING=未批，USED=已执行过——单次有效）；
2. `approval_expires_at` 未过；
3. 五元组逐项相等，任一不等 → 拒绝，reason 指明哪一项（`BINDING_MISMATCH:<field>`）；
4. 拒绝必须发生在任何外部副作用之前（校验器是纯函数，无副作用）。

## 4. 与现状（demo 门）的衔接

当前桥的门 = MinIO `human-gate-{approval|rejection}.md` 文件存在性检查（`gate_record()`），无绑定/身份/仲裁。迁移路径：

1. M2（本规格）：纯逻辑层——票据模型 + 校验器 + 状态机（tools/approval/，已带单测）；
2. 门 Web 化（M4 前）：存储落 PG（复用 approvals 表结构改造）或 MinIO 票据对象 + Web 页签发/展示；
3. 桥切换：`gate_record()` 文件存在性 → 票据校验调用。切换属部署操作，另行按同步规程执行。

## 5. 剥离旧 merge 语义（v2 二.2a 明确要求）

- 旧 `l2_create_ticket` 的 action 集合 {merge, close} 与 `approvals.status=USED→(Controller)run MERGED/l2_done` 的合并审批链**不迁移**到本规格；
- 新动作集不含 merge/close/revert（§1）；
- 旧代码（tools/policy-gateway、workflow-controller 的 L2 路径）是决赛 demo 资产，**不改动**；本规格是独立新实现，未来门 Web 化时只复用其"存在性+状态流转"机制形状，不导入其合并语义。

## 6. 决策项（不得以默认值擅自生效）

| # | 决策 | 现状 | 阻塞什么 |
|---|---|---|---|
| D-1 | V0 启用哪几个动作（generate_patch / run_poc / publish_result 的子集） | 候选三动作机制已实现，启用集合=空 | 门 Web 化的页面动作清单；真实执行启用 |
| D-2 | 审批人身份与权限映射 | 校验器要求 approved_by 非空；无用户系统 | 门 Web 登录设计（M4 前） |
| D-3 | approval/exec TTL 默认值 | 校验器参数化（不写死），l2 惯例 1..24h 可沿用 | 产品默认策略 |

以上任何一项未拍板前：校验器只用于隔离测试与规格验证，**不接真实执行路径，不产生真实放行**。隔离测试使用明确标注的测试身份（`test-approver`）。

## 7. 验收映射（补 ACCEPTANCE.md M2 节）

| 要求（v2 一.3） | 实现 | 测试 |
|---|---|---|
| 批准的语义 | 本规格 §1/§5 | test_action_set_excludes_merge |
| 绑定对象 | §2 五元组 | test_binding_roundtrip / test_params_hash_required |
| 失效条件 | §1 四问3 | test_new_head_invalidates / test_invalidated_cannot_execute |
| 权限人 | §1 四问4（D-2 待定） | test_approve_requires_identity |
| 并发竞争 | §1 四问4 CAS | test_approve_reject_race / test_duplicate_create_idempotent / test_reapprove_noop |
| 红线"批A执B" | §3 执行校验 | test_execution_binding_mismatch_rejected（逐字段） |
