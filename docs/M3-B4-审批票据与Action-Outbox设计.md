# M3-B4 设计 · 审批票据与 Action Outbox

> 状态:**设计稿(待复审,未实现)**
> 目标:M3-B 最后一块 —— 给 L2 动作(merge/revert/close)装上"人工审批票据 + 确定性派发 + 原子执行 + TOCTOU 防护 + 可对账"的闭环。
> 前置:B1/B2/B2.1/B2.2/B3/B3.1/B3.2 已 closed。

---

## 0. 设计原则(贯穿全文)

1. **PG 是唯一权威状态** —— 票据状态机在 PG,不在内存、不在 LLM、不在 Matrix。
2. **不信任 LLM 自报的任何东西** —— run_id、PR 号、head SHA 一律以 GitHub 实际状态为准,由确定性 Controller 读回。
3. **Gateway 是 LLM 边界** —— 它是唯一既接 LLM 又接 GitHub 的组件,所以它的 DB 权限必须最小化(只 EXECUTE 受约束函数,无表级 UPDATE)。
4. **Outbox 继承 M3-A 幂等** —— 派发意图可重放,执行侧原子去重。
5. **fail-closed** —— 审计/票据/对账任何环节不可信时,拒绝执行(B3 已为审计做了 fail-closed,B4 把它延伸到 L2 执行)。

---

## 1. 四个必须锁定的设计决策

### 1.1 可信绑定来源:`run_id → branch → PR number → head SHA`

**问题**:fixer(LLM)创建修复分支和 PR,然后"声称"PR 号和 head SHA。但 LLM 可被 prompt injection 误导或幻觉,**不能作为绑定来源**。

**决策:绑定来自 GitHub,由确定性 Controller 读回,不是 fixer 自报。**

绑定链(全是 Controller 主动查 GitHub,被动的是 GitHub 实际状态):

```
① Controller 为 task 分配 run_id(确定性,task_runs.run_id)
② Controller 派发 FIX 阶段时,告诉 fixer 分支前缀 fix/<run_id>-<short>(前缀由 Controller 决定,不是 fixer 起名)
③ fixer 经 gateway 建 fix/<run_id>-* 分支 + PR(gateway 审计 INTENT+RESULT,但不解析 LLM 文本)
④ Controller 在 FIX 完成后,主动查 GitHub:list_pull_requests(head=fix/<run_id>-*, state=open)
   → 拿到真实 pr_number + head_sha(权威,非 LLM)
⑤ Controller 写 run_pr_bindings(run_id, repo, pr_number, fix_branch, head_sha, recorded_at)
⑥ VERIFY 用此绑定;L2 票据创建时从此表读绑定,写进 approvals.expected_head_sha / pr_number
⑦ Gateway 执行 merge 前,再查一次 GitHub get_pull_request(pr_number).head.sha
   与 approvals.expected_head_sha 比对 → 不一致(被 force-push)→ DENY(TOCTOU 防护)
```

**新增表 `run_pr_bindings`**:
```sql
CREATE TABLE run_pr_bindings (
    run_id        TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    pr_number     INTEGER NOT NULL,
    fix_branch    TEXT NOT NULL,
    head_sha      TEXT NOT NULL,        -- FIX 完成时的 GitHub 实际 head
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at  TIMESTAMPTZ           -- 执行时再确认的 head_sha(若有变化)
);
```
- 由 Controller 写(FIX 完成后查 GitHub)。
- 票据创建时读;执行时 Gateway 再查 GitHub 比对(防 TOCTOU)。

### 1.2 `policy_gateway_l2` 账号:只 EXECUTE 受约束函数,不给表级 UPDATE

**问题**:Gateway 若有 `approvals` / `policy_action_outbox` 的表级 UPDATE,被 compromise 的 Gateway 可绕状态机(如把 PENDING 直接标 USED,或改 binding)。

**决策:状态机封装在 PL/pgSQL `SECURITY DEFINER` 函数里;`policy_gateway_l2` 只有 `EXECUTE` 这些函数 + `SELECT approvals`(校验用),无任何表级写。**

函数即唯一的状态转移入口,在 SQL 层强制迁移合法性(CAS + CHECK)。即使 Gateway 的 L2 凭证泄露,也无法越过函数直接改表。

### 1.3 原子状态转换、唯一权威状态、UNKNOWN 对账

**唯一权威状态 = `approvals.status`**:
```
PENDING ──approve──→ APPROVED ──claim──→ EXECUTING ──complete──→ USED
                                  │
                                  ├── fail ──→ FAILED
                                  └── timeout ──→ UNKNOWN ──reconcile──→ USED / FAILED
APPROVED/EXECUTING 超时 expires_at → EXPIRED(终态,不可执行)
```

- **原子 claim**:`UPDATE approvals SET status='EXECUTING' WHERE ticket_id=? AND status='APPROVED' RETURNING *`。0 行 → 已被领/已过期/状态不符 → 拒(防并发双执行)。
- **UNKNOWN 是非终态中间态**:网络超时(merge 调用了但不知结果)→ 标 UNKNOWN,绝**不**自动重试 merge(L2 动作不可重试,可能已经成功)。必须由对账流程查 GitHub 实际状态后,合法迁移到 USED(已合并)/ FAILED(未合并)。
- **EXPIRED**:APPROVED/EXECUTING 超 `expires_at` → 对账后标 EXPIRED,终态。

**`policy_action_outbox` 的定位 = 派发意图(非执行权威)**,见 1.4。

### 1.4 outbox 的 EXECUTING 统一(你点出的 gap)

**现状**:`approvals.status` CHECK 含 `EXECUTING`;`policy_action_outbox.status` CHECK **不含** EXECUTING(`PENDING_DISPATCH/DISPATCHED/SUCCEEDED/FAILED/UNKNOWN`)。

**统一决策:执行状态只活在 `approvals`;outbox 只表达派发意图,不需要 EXECUTING。**

| 表 | 角色 | 状态机 | 谁写 |
|---|---|---|---|
| `approvals` | **执行权威**(被批准了吗?在执行吗?完成了吗?) | PENDING→APPROVED→EXECUTING→USED/FAILED/UNKNOWN/EXPIRED | Controller 建、CLI approve、Gateway claim/complete/fail |
| `policy_action_outbox` | **派发意图**(Controller 要 Gateway 执行某 L2 动作,幂等可重放) | PENDING_DISPATCH→DISPATCHED→SUCCEEDED/FAILED/UNKNOWN | Controller 建并 drain,Gateway 回写结果 |

- "原子领取到 EXECUTING" = Gateway 领取 **outbox 行(PENDING_DISPATCH→DISPATCHED)** 后,对 **approvals** 做 APPROVED→EXECUTING 的原子 claim。
- outbox 的 DISPATCHED 表示"Controller 已派发该调用给 Gateway";SUCCEEDED/FAILED 镜像 approvals 的 USED/FAILED(供 Controller 更新 task_runs)。
- **outbox 不加 EXECUTING**(执行态归 approvals)。本设计明确两者职责,消除歧义。

---

## 2. 数据模型

### 2.1 新增/调整表

- `run_pr_bindings`(新,见 1.1):Controller 写的 GitHub 权威绑定。
- `approvals`(已存在,B4 启用):票据状态机。无 schema 改动(CHECK 已含 EXECUTING)。
- `policy_action_outbox`(已存在,B4 启用):派发意图。无 schema 改动(不加 EXECUTING)。
- `mcp_calls.ticket_id`(已存在):L2 调用的审计行带上 ticket_id,串起审计与票据。

### 2.2 受约束的 DB 函数(`SECURITY DEFINER`,状态机唯一入口)

```sql
-- Controller 账号调用(建票 + 派发)
CREATE FUNCTION l2_create_ticket(p_run_id, p_action, p_repo, p_pr_number, p_target_branch,
                                 p_expected_head_sha, p_revert_commit_sha, p_ttl_seconds)
RETURNS TEXT  -- ticket_id
-- 从 run_pr_bindings 读绑定(拒绝 LLM 自报);写 approvals(PENDING)+ 可选写 outbox;同一事务
-- 生成 ticket_id = "tkt-" + run_id + "-" + action(确定性,幂等)

CREATE FUNCTION l2_pending_list() RETURNS TABLE(...)   -- CLI 列待审批票

-- approve CLI 账号调用
CREATE FUNCTION l2_approve(p_ticket_id, p_approved_by)
-- CAS: UPDATE ... SET status='APPROVED', approved_by, approved_at WHERE status='PENDING' RETURNING

-- Gateway 账号(policy_gateway_l2)调用 —— 全部 SECURITY DEFINER,这是它唯一能改票据的途径
CREATE FUNCTION l2_claim_ticket(p_ticket_id)
RETURNS TABLE(...)  -- 原子 APPROVED→EXECUTING,返回 binding 供 Gateway 校验 call args;0 行返回 NULL
CREATE FUNCTION l2_complete_ticket(p_ticket_id, p_result_sha)   -- EXECUTING→USED
CREATE FUNCTION l2_fail_ticket(p_ticket_id, p_reason)           -- EXECUTING→FAILED
CREATE FUNCTION l2_mark_unknown(p_ticket_id)                    -- EXECUTING→UNKNOWN(网络超时)
CREATE FUNCTION l2_reconcile_unknown(p_ticket_id, p_merged bool, p_actual_sha)  -- UNKNOWN→USED/FAILED
```

每个函数内部用 `UPDATE ... WHERE status=<expected> RETURNING`(CAS),迁移非法时 RAISE EXCEPTION(函数失败 → Gateway 拒绝执行)。

### 2.3 账号矩阵

| 账号 | 组件 | 权限 | 写票据途径 |
|---|---|---|---|
| `mergepilot`(超管,Controller 容器内) | Controller | task_runs/approvals/outbox/bindings 全 DDL/DML | 直接 INSERT + 调 l2_create_ticket |
| `policy_gateway_audit`(已存在) | Gateway(审计) | INSERT mcp_calls | — |
| `policy_gateway_l2`(**新**) | Gateway(L2 执行) | **EXECUTE l2_claim/complete/fail/mark_unknown/reconcile** + SELECT approvals + INSERT policy_action_outbox(回写结果) | **只能调函数,无表级 UPDATE** |
| `mergepilot_approver`(**新**,host-only CLI) | approve CLI | EXECUTE l2_approve + l2_pending_list + SELECT approvals | 只调 l2_approve |

Gateway 拿两张 PG 连接(审计账号 + L2 账号),职责隔离。Controller 与 CLI 在可信环境(容器/host),可用更强账号。

---

## 3. 三方职责:谁创建/审批/领取/完成

| 操作 | Controller | approve CLI | Gateway | fixer/LLM |
|---|---|---|---|---|
| 建 PENDING 票据 | ✅(查 GitHub 读绑定,调 `l2_create_ticket`) | ❌ | ❌ | ❌ |
| approve(PENDING→APPROVED) | ❌ | ✅(`l2_approve`,approved_by 取执行环境) | ❌ | ❌ |
| 派发(outbox PENDING_DISPATCH) | ✅(与建票同事务) | ❌ | ❌ | ❌ |
| 领取(APPROVED→EXECUTING) | ❌ | ❌ | ✅(`l2_claim_ticket` 原子 CAS) | ❌ |
| 调 GitHub merge/revert | ❌ | ❌ | ✅(持 coordinator token,经 bridge) | ❌ |
| 完成(EXECUTING→USED + result_sha) | ❌ | ❌ | ✅(`l2_complete_ticket`) | ❌ |
| 失败(EXECUTING→FAILED) | ❌ | ❌ | ✅(`l2_fail_ticket`) | ❌ |
| 超时(EXECUTING→UNKNOWN) | ❌ | ❌ | ✅(`l2_mark_unknown`) | ❌ |
| 对账(UNKNOWN→USED/FAILED) | ✅(查 GitHub 实际态,调 `l2_reconcile_unknown`) | ❌ | (备选:也可由 Gateway 对账) | ❌ |
| 回写 outbox(SUCCEEDED/FAILED) | ✅(据 Gateway 结果) | ❌ | (备选) | ❌ |
| 读 task 状态推进 | ✅ | ❌ | ❌ | ❌ |

**关键不变量**:
- fixer/LLM **完全无权** 触碰票据(outbox/approvals)—— 它只能建分支/PR,绑定由 Controller 从 GitHub 读回。
- Gateway **只能** 通过 `l2_*` 函数改票据,不能直接 UPDATE。
- approve CLI **只能** approve,不能领取/执行(职责分离:审批者 ≠ 执行者)。

---

## 4. 端到端流程(正向 merge 场景)

```
[Controller] VERIFY 完成,verifier 裁定需 merge
    ↓ 查 GitHub:list_pull_requests(head=fix/<run_id>-*) → 真实 pr_number + head_sha
    ↓ 写 run_pr_bindings(若 FIX 阶段未写)
    ↓ 事务:l2_create_ticket(run_id, action=merge, repo, pr_number, expected_head_sha, ttl) 
      → approvals(PENDING) + policy_action_outbox(PENDING_DISPATCH)
    ↓ task_runs 进入 APPROVAL_PENDING

[approve CLI] host: approve.sh <run_id>
    ↓ l2_pending_list → 看到 PENDING 票
    ↓ l2_approve(ticket_id, approved_by=$USER)  → APPROVED
    ↓ (不执行 GitHub!只改状态)

[Controller] drain policy_action_outbox
    ↓ 看到 PENDING_DISPATCH 且 ticket=APPROVED
    → 标 DISPATCHED,带 coordinator token 调 Gateway SSE: merge_pull_request(owner,repo,pullNumber,commit_title, approval_ticket=<ticket_id>)

[Gateway] 收到 merge 调用 + approval_ticket
    ↓ 1. l2_claim_ticket(ticket_id) 原子 APPROVED→EXECUTING → 拿 binding
    ↓ 2. 校验 call args 与 binding 一致(repo/pr_number/action 匹配);否则 l2_fail + DENY
    ↓ 3. TOCTOU:查 GitHub get_pull_request(pr_number).head.sha vs expected_head_sha;不一致 → l2_fail + DENY
    ↓ 4. 查 expires_at 未过;否则 l2_fail + DENY
    ↓ 5. 调 GitHub merge(经 bridge)
       ├─ 成功 → l2_complete_ticket(ticket_id, merge_commit_sha) → USED
       ├─ 明确失败 → l2_fail_ticket → FAILED
       └─ 网络超时(不知结果)→ l2_mark_unknown → UNKNOWN(不重试!)
    ↓ 6. 审计:写 mcp_calls(含 ticket_id,INTENT+RESULT,correlation_id)

[Controller] 据 Gateway 返回,标 outbox SUCCEEDED/FAILED;task_runs → MERGED / HOLD
[Controller] 定期对账:UNKNOWN 票据 → 查 GitHub 是否真合并 → l2_reconcile_unknown → USED/FAILED
```

---

## 5. UNKNOWN 对账(关键,防误重试)

L2 动作**绝不在 UNKNOWN 后自动重试**(可能已成功 → 二次 merge 报错或副作用)。对账流程:

```
Controller 后台扫描 approvals WHERE status='UNKNOWN':
    查 GitHub:get_pull_request(pr_number).merged === true?
        ├─ merged=true → l2_reconcile_unknown(ticket, merged=true, actual_sha) → USED
        └─ merged=false → l2_reconcile_unknown(ticket, merged=false) → FAILED
            → 决定是否进入人工处理或新票据(不自动重试同票)
```

EXPIRED 对账类似:超 `expires_at` 的 APPROVED/EXECUTING → 查 GitHub 实际态 → USED/FAILED/EXPIRED。

---

## 6. TOCTOU 防护

- 票据创建时锁 `expected_head_sha`(merge)/ `revert_commit_sha`(revert)。
- 执行前 Gateway 再查 GitHub 实际 head,比对。不一致 = 票据批准后分支被改(force-push / 新提交)→ **DENY**,票据 FAILED。
- 防的是"批准 A 状态、执行 B 状态"。

---

## 7. 与现有 gateway.py(B2/B3)的衔接

- B2 的 L2 占位(`name in _L2_SET → L2_TICKET_REQUIRED`)改为:若 call 带 `approval_ticket` 参数 → 进入 B4 票据校验流;否则仍 `L2_TICKET_REQUIRED`。
- 票据校验流复用 B3 的 INTENT-first fail-closed:claim 前先写 mcp_calls INTENT(含 ticket_id),claim 失败/超时也审计。
- coordinator token 仍只在 Gateway env(B1),Controller 经 Gateway 调 merge(Controller 不持 GitHub PAT,不持 coordinator token)。

---

## 8. 验收标准(B4 实现后)

```
绑定来源:Controller 从 GitHub 读 pr_number/head_sha(fixer 自报被忽略)        ✅
建票:Controller 事务写 approvals(PENDING)+ outbox(PENDING_DISPATCH)            ✅
approve CLI:host-only,approved_by 取自 $USER,不接参数;只 PENDING→APPROVED     ✅
账号收敛:policy_gateway_l2 表级 UPDATE/DELETE 被拒(只能 EXECUTE 函数)          ✅
原子 claim:并发两个 merge 调用同一票,只有一个 APPROVED→EXECUTING              ✅
TOCTOU:批准后 force-push 改 head_sha → Gateway 执行前 DENY(查 GitHub 比对)    ✅
过期:超 expires_at 的票 → 拒;对账后 EXPIRED                                    ✅
UNKNOWN:merge 网络超时 → UNKNOWN,不自动重试;对账查 GitHub → USED/FAILED        ✅
幂等:重复派发(outbox idempotency_key)不重复执行                                ✅
失败链:无票/伪造票/过期/目标不符/重复领取 → 全 DENY + 审计                        ✅
单次性:合法票只成功一次(USED),result_sha 记 merge_commit_sha                   ✅
审计:每步 INTENT+RESULT 带 ticket_id + correlation_id                            ✅
退出码:测试 FAIL>0 非零退出                                                       ✅
```

证据目录:`evidence/m3b-b4/`。

---

## 9. 待你拍板/澄清的开放问题

1. **L2 动作执行触发方**:本设计是 **Controller drain outbox → 调 Gateway**(Gateway 保持纯 proxy,复用 M3-A drain 模式)。备选是给 **Gateway 加后台 drainer**。我倾向前者(Gateway 无状态更易测,Controller 已有 drain 经验)。你认可吗?

2. **对账主体**:Controller 还是 Gateway 跑 UNKNOWN 对账?我倾向 **Controller**(它已持 GitHub 读能力 via Gateway,且负责 task 状态)。Gateway 只做 claim/complete/fail/mark_unknown。

3. **`mergepilot_approver` 账号**:为彻底贯彻"EXECUTE-only",approve CLI 用独立账号(只 `l2_approve`)。还是 host-only CLI 直接用 mergepilot 超管可接受?(CLI 在 host,不暴露给 LLM。)

4. **revert 的 revert_commit_sha 来源**:同 merge 的 head_sha 逻辑 —— Controller 查 GitHub 锁定要 revert 的 commit SHA,不信任 LLM。close 走 update_pull_request(state=closed) 的 L2 路径。确认?

5. **TTL**:`expires_at` 默认值?我建议 APPROVED 后 24h(比赛演示可短到 1h)。EXPIRED 后若仍需执行,必须重新建票(不复活)。

6. **ticket_id 格式**:建议 `tkt-<run_id>-<action>`(确定性,便于审计追溯 + 幂等)。若同 run_id 同 action 需多次尝试(前次 FAILED),后缀 `-<attempt>`。确认?

---

## 10. 实现顺序(B4 复审通过后)

```
B4a:数据模型 + 函数 + 账号(run_pr_bindings、l2_* 函数、policy_gateway_l2/mergepilot_approver 账号 + 自检)
B4b:Controller 端(查 GitHub 读绑定、l2_create_ticket、drain outbox 调 Gateway、对账 UNKNOWN)
B4c:Gateway 端(L2 调用接票据流:claim/TOCTOU/complete/fail/mark_unknown + 审计)
B4d:approve CLI(approve.sh + l2_approve + pending list)
B4e:验收(正向 merge / TOCTOU 拒 / 并发领取 / UNKNOWN 对账 / 幂等 / 全 DENY 场景)
```

每步独立验证 + 证据落盘,沿用 B 系列的"不移动旧标签、新增 closed 标签"惯例。
