# M3-B4 设计 v2 · 审批票据与 Action Outbox

> 状态:**设计稿 v2(待复审,未实现)**。**取代 v1**(v1 保留在 git 历史)。
> 目标:L2 动作(merge/close)的人工审批闭环:绑定来源不可信任 LLM、状态机原子、Gateway EXECUTE-only、崩溃可对账、TOCTOU 防护。
> 前置:B1/B2/B2.1/B2.2/B3/B3.1/B3.2 已 closed。

---

## 0. v2 Changelog(相对 v1 的修订)

| # | v1 缺陷 | v2 修订 |
|---|---|---|
| 1 | `list_pull_requests(head=fix/<run_id>-*)` 假设支持通配 | **不支持通配**。Controller 分页 `list_pull_requests(state=open)` 后本地按 `head.ref` 前缀过滤,要求恰好 1 条;单 PR 用 `pull_request_read(method=get)` |
| 2 | 文档称"coordinator token 只在 Gateway env" | Controller drain outbox → 必须持 coordinator Bearer token(非 PAT,放 `controller.env`);token 在 Gateway env + Controller env,**不进任何 worker** |
| 3 | 票据只绑 repo/PR/SHA | 票据绑**完整 canonical payload**(含 mergeMethod/commit_title 等);Gateway 从 claim 返回的 payload 构造上游调用,不信任 call 传入的散参 |
| 4 | `l2_claim_ticket` 只收 ticket_id,先消耗再比对 | **一次 CAS 同时校验** action+repo+PR+args_hash+expiry;不匹配则票据保持 APPROVED 不消耗;成功返回 canonical payload + execution_id |
| 5 | 无 execution_id;只对账 UNKNOWN | 加 `execution_id`/`executing_at`;Controller 同时对账 **UNKNOWN + 超时 EXECUTING** |
| 6 | outbox DISPATCHED 无恢复规则 | DISPATCHED 带 lease;Controller 恢复时按 approval 实际态决定重派/对账/完成;DISPATCHED 非终态 |
| 7 | 给 Gateway `SELECT approvals`/`INSERT outbox`/reconcile | **真 EXECUTE-only**:Gateway 只 EXECUTE claim/complete/fail/mark_unknown;approver 只 EXECUTE pending_list/approve;outbox 只 Controller 写;reconcile 只 Controller |
| 8 | SECURITY DEFINER 未硬化 | NOLOGIN owner + 固定 `search_path` + 完全限定表名 + REVOKE PUBLIC EXECUTE + 按 role 精确 GRANT |
| 9 | task_runs CHECK 无 APPROVAL_PENDING | B4 migration 显式加 `APPROVAL_PENDING` + 状态映射 |
| 10 | 列了 revert/delete_file | **revert 不直接实现**(bridge 无工具)→ 走"建 revert PR → 正常审批 merge";`delete_file` 保持 disabled |

---

## 1. 六个锁定的决策

1. **Controller drain outbox,Gateway 请求驱动**(不加后台 drainer)。Controller 持 coordinator token(非 PAT)。
2. **Controller 对账 UNKNOWN + 超时 EXECUTING + 滞留 DISPATCHED**;Gateway 只 claim/execute/complete/fail/mark_unknown。
3. **独立 `mergepilot_approver` 账号**,仅 EXECUTE `l2_pending_list`/`l2_approve`;CLI 接收**精确 ticket_id**。**B4d.1 hardening**:`l2_approve` 改用 `session_user`(认证 DB 登录角色)写 `approved_by`,**忽略** `p_approved_by`(签名不变,故 B4a frozen allowlist 仍有效);逐人身份 = 逐人 LOGIN 角色授予 EXECUTE。原 B4d 的 `id -un@hostname` 派生降级为"主机审计标签",不作强身份(持 approver 密码者无法再借参数冒名)。
4. **close** 走 `update_pull_request(state=closed)` 并绑 PR/head SHA;**revert** 暂不实现 → 走"建 revert PR → 正常审批 merge";`delete_file` 保持 disabled。
5. **TTL**:PENDING 审批期 24h;APPROVED 起执行期默认 1h(可配,上限 24h);EXECUTING 到期**对账不直接 EXPIRED**。
6. **ticket_id**:
   ```
   ticket_id       = tkt-<UUIDv4>
   attempt_no      = DB 原子分配(per run_id+action)
   UNIQUE(run_id, action, attempt_no)
   idempotency_key = sha256(run_id + action + binding_id + attempt_no)
   ```
   新 attempt 仅由 Controller 显式动作创建(前次 FAILED 后重试),**不因网络重试自动生成**。

---

## 2. 架构总览

```
[Controller] (持 coordinator Bearer token, 不持 PAT)
   查 GitHub 读权威绑定 → 建 ticket(PENDING)+ outbox(PENDING_DISPATCH)同事务
   drain outbox: DISPATCHED + lease → 经 Gateway /coordinator/sse 调 L2 工具(带 ticket_id)
   对账:扫描 UNKNOWN / 超时 EXECUTING / 滞留 DISPATCHED → 查 GitHub → 迁移
        │
        ▼ (coordinator Bearer token)
[Policy Gateway] /coordinator/sse  ── 验 ticket_id → claim(CAS 全载荷)→ 调 bridge → complete/fail/mark_unknown
        │ (mcp-backend-net)
        ▼
[github-mcp bridge] (持 PAT) → GitHub

[approve CLI] (host-only, mergepilot_approver 账号) → l2_approve(ticket_id)
```

**coordinator token 分布**:Gateway env + Controller `controller.env`(chmod 600)。**绝不进 worker**。Controller 用它认证到 Gateway,不用 PAT。

---

## 3. 数据模型 v2

### 3.1 新表 `run_pr_bindings`(Controller 写,GitHub 权威绑定)

```sql
CREATE TABLE run_pr_bindings (
    binding_id   TEXT PRIMARY KEY,          -- bnd-<UUIDv4>
    run_id       TEXT NOT NULL REFERENCES task_runs(run_id),
    repo         TEXT NOT NULL,             -- owner/repo
    pr_number    INTEGER NOT NULL,
    fix_branch   TEXT NOT NULL,             -- head.ref,如 fix/<run_id>-xxx
    head_sha     TEXT NOT NULL,             -- FIX 完成时 GitHub 实际 head
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
UNIQUE(run_id)                              -- 一个 run 一个 fix PR 绑定
```

### 3.2 `approvals` v2(在已有表上 ALTER 加列)

已有列保留(ticket_id, run_id, action, repo, pr_number, target_branch, expected_head_sha, revert_commit_sha, status, approved_by, approved_at, expires_at, used_at, result_sha, error, created_at)。新增:

```sql
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS binding_id     TEXT REFERENCES run_pr_bindings(binding_id);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS attempt_no     INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS canonical_payload JSONB NOT NULL;  -- 完整上游调用参数
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS args_hash     TEXT NOT NULL;        -- sha256(canonical_payload) 前 16
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS execution_id  UUID;                 -- claim 时分配
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS executing_at  TIMESTAMPTZ;          -- claim 时间(对账用)
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approval_expires_at TIMESTAMPTZ;    -- PENDING 审批期(24h)
-- 已有 expires_at 复用为"执行期"(APPROVED 起 1h)
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_run_action_attempt_key;
ALTER TABLE approvals ADD CONSTRAINT approvals_run_action_attempt_key UNIQUE (run_id, action, attempt_no);
```

`canonical_payload` 示例(merge):
```json
{"owner":"nghqqa","repo":"MergePilot","pullNumber":7,"commit_title":"Merge fix","merge_method":"squash"}
```

### 3.3 `policy_action_outbox` v2(加 lease)

已有列保留。新增:
```sql
ALTER TABLE policy_action_outbox ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
-- status CHECK 不变(PENDING_DISPATCH/DISPATCHED/SUCCEEDED/FAILED/UNKNOWN)—— 不加 EXECUTING
```
**outbox 不加 EXECUTING**(执行态归 approvals)。DISPATCHED 带 `lease_expires_at`;滞留时按 approval 态恢复(§7)。

### 3.4 `task_runs` v2(加 APPROVAL_PENDING 状态)

```sql
ALTER TABLE task_runs DROP CONSTRAINT IF EXISTS chk_task_status;
ALTER TABLE task_runs ADD CONSTRAINT chk_task_status CHECK (
    status IN ('SUBMITTED','RUNNING','PASS','FAIL','HOLD','MERGED','ROLLED_BACK',
               'APPROVAL_PENDING')   -- B4 新增
);
```
状态映射:
```
VERIFY 完成 + 需 L2      → APPROVAL_PENDING
approval USED (merge 成功) → MERGED
approval FAILED / EXPIRED  → HOLD(人工)
approval UNKNOWN 对账后失败 → HOLD
close USED                → HOLD/CLOSED(复用 HOLD,current_stage=verified-closed)
```

### 3.5 mcp_calls(已有)—— B4 让 L2 审计行带 `ticket_id` + `execution_id`(已存在 ticket_id 列,加 execution_id)

```sql
ALTER TABLE mcp_calls ADD COLUMN IF NOT EXISTS execution_id UUID;
```

---

## 4. 受约束的 DB 函数(SECURITY DEFINER 硬化)

### 4.1 硬化模板(所有 l2_* 函数遵循)

```sql
CREATE ROLE mergepilot_l2_owner NOLOGIN;
GRANT SELECT, INSERT, UPDATE ON run_pr_bindings, approvals, policy_action_outbox TO mergepilot_l2_owner;

CREATE OR REPLACE FUNCTION l2_claim_ticket(...)
RETURNS TABLE(...) LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public        -- 固定 search_path
AS $$
BEGIN
  -- 完全限定表名:public.approvals
  UPDATE public.approvals SET ...
  WHERE ... AND status='APPROVED' AND ... ;
END $$;
REVOKE ALL ON FUNCTION l2_claim_ticket(...) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION l2_claim_ticket(...) TO policy_gateway_l2;
```

### 4.2 函数 → 执行角色(精确授权)

| 函数 | 执行角色 | 作用 |
|---|---|---|
| `l2_create_ticket(p_binding_id, p_action, p_canonical_payload, p_ttl_hours)` | `mergepilot`(Controller) | 原子分配 attempt_no;写 approvals(PENDING)+ policy_action_outbox(PENDING_DISPATCH)同事务;返回 ticket_id |
| `l2_pending_list()` | `mergepilot_approver` | 返回 PENDING 票据(只读,经函数不暴露表 SELECT) |
| `l2_approve(p_ticket_id, p_approved_by)` | `mergepilot_approver` | CAS PENDING→APPROVED;校验未过审批期;**B4d.1:`approved_by = session_user`(忽略 `p_approved_by`,签名不变)**;逐人身份 = 逐人 LOGIN 角色 |
| `l2_claim_ticket(p_ticket_id, p_action, p_repo, p_pr_number, p_args_hash)` | `policy_gateway_l2` | **一次 CAS**:APPROVED→EXECUTING,WHERE 同时校验 action/repo/pr_number/args_hash/expires_at>now();分配 execution_id+executing_at;0 行返回 NULL(票据不消耗);成功 RETURN canonical_payload+execution_id |
| `l2_complete_ticket(p_ticket_id, p_execution_id, p_result_sha)` | `policy_gateway_l2` | CAS EXECUTING→USED;校验 execution_id 匹配 |
| `l2_fail_ticket(p_ticket_id, p_execution_id, p_reason)` | `policy_gateway_l2` | CAS EXECUTING→FAILED |
| `l2_mark_unknown(p_ticket_id, p_execution_id, p_reason)` | `policy_gateway_l2` | CAS EXECUTING→UNKNOWN(网络超时) |
| `l2_reconcile_unknown(p_ticket_id, p_merged bool, p_actual_sha)` | `mergepilot`(Controller) | UNKNOWN→USED/FAILED(GitHub 实际态) |
| `l2_reconcile_executing(p_ticket_id, p_merged bool, p_actual_sha)` | `mergepilot`(Controller) | 超时 EXECUTING→USED/FAILED |
| `l2_expire_pending(p_ticket_id)` | `mergepilot`(Controller) | PENDING 超审批期→EXPIRED |

**关键**:`policy_gateway_l2` 只能调 4 个函数(claim/complete/fail/mark_unknown),**无任何表级权限**(无 SELECT approvals、无 INSERT outbox)。canonical_payload 由 claim 函数 RETURN,不需 SELECT。

### 4.3 账号矩阵 v2

| 账号 | 组件 | 权限 |
|---|---|---|
| `mergepilot` | Controller | 全 DML + EXECUTE 全部 l2_* 函数 |
| `policy_gateway_audit` | Gateway 审计 | INSERT mcp_calls(不变) |
| `policy_gateway_l2`(**新**) | Gateway L2 | **仅 EXECUTE** claim/complete/fail/mark_unknown |
| `mergepilot_approver`(**新**) | approve CLI | **仅 EXECUTE** pending_list/approve |

---

## 5. 绑定发现(修 #1:无通配)

Controller 在 FIX 完成后:
```
1. list_pull_requests(owner, repo, state=open, perPage=100, page=1..)  # 不带 head 通配
2. 本地过滤:head.ref STARTS WITH 'fix/<run_id>-'
3. 恰好 1 条 → 继续;0 条 → task HOLD("无 fix PR");>1 条 → task HOLD("fix PR 不唯一")
4. pull_request_read(method=get, owner, repo, pullNumber=<那条>) → 拿 head.sha + mergeable_state
5. 写 run_pr_bindings(binding_id, run_id, repo, pr_number, fix_branch, head_sha)
```
**head_sha 来自 GitHub 权威**,不信任 fixer 自报。`pull_request_read(method=get)` 是单 PR 读的真实工具(不是不存在的 get_pull_request)。

---

## 6. 原子 claim(修 #4:一次 CAS 全校验)

Gateway 收到 `merge_pull_request(..., approval_ticket=<ticket_id>)`:
```
1. 从 call args 计算 args_hash(sha256(canonical args))
2. l2_claim_ticket(ticket_id, action='merge', repo, pr_number, args_hash)
   内部:UPDATE public.approvals SET status='EXECUTING', execution_id=gen_random_uuid(),
                                executing_at=now()
         WHERE ticket_id=? AND status='APPROVED' AND action=? AND repo=? AND pr_number=?
           AND args_hash=? AND expires_at > now()
         RETURNING canonical_payload, execution_id, expected_head_sha;
   → 0 行:返回 NULL(票据保持 APPROVED,未消耗)→ Gateway 返回 POLICY_DENIED reason=CLAIM_MISMATCH
3. 拿 RETURN 的 canonical_payload → 用它(不是 call 传入的散参)构造上游 merge_pull_request 调用
4. TOCTOU:再查 GitHub get pull_request head.sha == expected_head_sha;不一致 → l2_fail_ticket + DENY
5. 调上游 merge
   ├─ 成功 → l2_complete_ticket(ticket, execution_id, merge_commit_sha) → USED
   ├─ 明确失败 → l2_fail_ticket → FAILED
   └─ 网络超时(不知结果)→ l2_mark_unknown → UNKNOWN(不重试!)
```
**call 传入的 args 只用于 args_hash 比对,实际执行用票据绑定的 canonical_payload**(防 Controller 侧或传输中被篡改)。

---

## 7. 状态机 + 崩溃恢复(修 #5/#6)

### 7.1 approvals 状态机

```
PENDING ──approve──→ APPROVED ──claim──→ EXECUTING ──complete──→ USED
   │                     │                    ├─ fail ──→ FAILED
   超 approval_expires_at                    └─ timeout ──→ UNKNOWN
   → EXPIRED                                      │
                                          Controller 对账(查 GitHub):
                                          UNKNOWN/超时EXECUTING → USED/FAILED
```

### 7.2 policy_action_outbox DISPATCHED 恢复(修 #6)

DISPATCHED **非终态**。Controller 周期扫描 DISPATCHED 行,按其 approval 实际态决定:

| approval.status | 处置 |
|---|---|
| USED | outbox → SUCCEEDED |
| FAILED | outbox → FAILED |
| UNKNOWN | outbox → UNKNOWN(先对账 approval) |
| EXECUTING 且 executing_at + 执行超时 < now | 触发 reconcile_executing(GitHub 实际态) |
| EXECUTING 且未超时 | 继续等 |
| APPROVED 且 lease_expires_at > now | 继续等(Gateway 还没 claim) |
| APPROVED 且 lease_expires_at < now | **Gateway 未按时 claim,安全重派**:outbox → PENDING_DISPATCH,attempts++ |

### 7.3 Controller 对账扫描项(修 #5)

- `approvals.status='UNKNOWN'` → `l2_reconcile_unknown`(查 GitHub merged?)。
- `approvals.status='EXECUTING' AND executing_at < now() - 执行超时` → `l2_reconcile_executing`(查 GitHub merged?)。
- `approvals.status='PENDING' AND approval_expires_at < now()` → `l2_expire_pending` → EXPIRED → task HOLD。
- `outbox.status='DISPATCHED'` → 按 §7.2 处置。

**绝不对 L2 动作自动重试**(可能已成功 → 二次 merge)。对账只读 GitHub 实际态,迁移状态。

---

## 8. revert / close / delete_file(决策 #4/#10)

- **close**:`update_pull_request(state=closed)` 进 L2 票据流,绑 PR + head SHA(canonical_payload 含 state=closed)。
- **revert**:bridge **无 revert 工具**,B4 不直接实现。路径:Controller 检测到需 revert → 派 fixer 用 git revert 建新 `fix/<run_id>-revert-*` 分支 + PR → 走正常 review→verify→审批 merge 路径(复用 merge 票据流,merge_method=merge)。
- **delete_file**:保持 disabled(policy.yaml 已在 disabled 类)。不借 B4 票据意外开放。

---

## 9. TTL(决策 #5)

- `approval_expires_at` = created_at + 24h(PENDING 审批期,可配)。
- `expires_at`(执行期)= approved_at + 1h(默认,可配,上限 24h)。
- EXECUTING 执行超时 = 60s(GitHub merge 调用应有回;超时即对账)。
- EXECUTING 到期**不直接 EXPIRED**,走 `l2_reconcile_executing`。

---

## 10. ticket_id / attempt(决策 #6)

- `l2_create_ticket` 内:`SELECT max(attempt_no)+1 ... FOR UPDATE`(原子分配)。
- 新 ticket:`ticket_id=tkt-<UUIDv4>`,`attempt_no` 递增。
- 仅 Controller 显式调用(前次 FAILED 后的人工/策略重试)。**网络重试不新建 attempt**(由 outbox idempotency_key 去重 + claim CAS 防重复执行)。
- `idempotency_key = sha256(run_id + action + binding_id + attempt_no)`(outbox UNIQUE)。

---

## 11. 端到端流程(正向 close/merge,含 execution_id)

```
[Controller] VERIFY 完成 + 需 merge
  → 绑定发现(§5):list_pull_requests 分页 + 本地过滤 + pull_request_read → run_pr_bindings
  → l2_create_ticket(binding_id, action=merge, canonical_payload={owner,repo,pullNumber,commit_title,merge_method}, ttl=1h)
    事务:approvals(PENDING, attempt_no=N, approval_expires_at)+ outbox(PENDING_DISPATCH, idempotency_key)
  → task_runs = APPROVAL_PENDING

[approve CLI] host: approve.sh <ticket_id>   (严格 1 参数;多余参数 → exit 2,票保持 PENDING)
  → B4d.1: approved_by 由 l2_approve 写 session_user(DB 登录角色);CLI 不传、不可伪造
  → l2_approve(ticket_id) → APPROVED + expires_at(执行期);approved_by = session_user

[Controller] drain outbox
  → PENDING_DISPATCH 且 ticket=APPROVED → 标 DISPATCHED + lease_expires_at=now()+60s
  → 带 coordinator token 调 Gateway /coordinator/sse: merge_pull_request(approval_ticket=ticket_id, owner, repo, pullNumber, ...)
    (Controller 传的 args 仅用于 args_hash 比对)

[Gateway] claim(CAS 全载荷)→ canonical_payload + execution_id
  → TOCTOU 查 GitHub head.sha == expected_head_sha
  → 用 canonical_payload 调上游 merge
  → complete/fail/mark_unknown(带 execution_id)
  → 审计 mcp_calls(ticket_id + execution_id + correlation_id)

[Controller] 据 Gateway 返回 + 对账扫描 → outbox SUCCEEDED/FAILED;task_runs → MERGED/HOLD
```

---

## 12. 验收标准(B4e)

```
绑定:list_pull_requests 分页 + 本地 head.ref 过滤,恰好 1 条;0/>1 → HOLD          ✅
建票:canonical_payload + attempt_no + 同事务 outbox;ticket_id=tkt-UUID            ✅
approve CLI:独立账号,仅 EXECUTE pending_list/approve;approved_by=id@host         ✅
账号收敛:policy_gateway_l2 / mergepilot_approver 任何表级 SELECT/INSERT/UPDATE 被拒 ✅
SECURITY DEFINER:NOLOGIN owner + 固定 search_path + 完全限定表名 + 无 PUBLIC EXECUTE ✅
claim 一次 CAS:action/repo/PR/args_hash/expiry 全校验;不匹配票据保持 APPROVED     ✅
canonical_payload:Gateway 用票据载荷构造上游调用,忽略 call 散参                    ✅
TOCTOU:批准后 force-push 改 head_sha → 执行前 DENY(查 GitHub 比对)              ✅
并发:两个 merge 同票,只一个 EXECUTING                                             ✅
EXECUTING 崩溃:Gateway claim 后崩 → 超时 → Controller reconcile_executing → USED/FAILED ✅
UNKNOWN:网络超时 → UNKNOWN;对账查 GitHub → USED/FAILED;不自动重试               ✅
outbox DISPATCHED 滞留:lease 过期 + approval=APPROVED → 安全重派;其余按 approval 态 ✅
task_runs:APPROVAL_PENDING 转换合法(CHECK 已迁移)                                ✅
revert:不直接实现;走"建 revert PR → 正常 merge";delete_file 仍 disabled           ✅
幂等:重复派发(idempotency_key)不重复执行                                          ✅
单次性:合法票只 USED 一次,result_sha 记 merge_commit_sha                          ✅
失败链:无票/伪造/过期/载荷不符/重复领取 → 全 DENY + 审计(ticket_id+execution_id)  ✅
退出码:测试 FAIL>0 非零退出                                                        ✅
```
证据目录:`evidence/m3b-b4/`。

---

## 13. 实现顺序(复审通过后)

```
B4a:数据库与权限
    - run_pr_bindings / approvals v2 列 / outbox lease / task_runs APPROVAL_PENDING / mcp_calls.execution_id
    - mergepilot_l2_owner(NOLOGIN)+ l2_* 函数(SECURITY DEFINER 硬化)
    - policy_gateway_l2 / mergepilot_approver 账号(真 EXECUTE-only)+ 自检
B4b:Gateway 安全边界
    - L2 调用接票据流:claim(CAS 全载荷)+ canonical_payload 构造上游 + TOCTOU + complete/fail/mark_unknown + 审计
B4c:Controller 绑定 / drain / 对账
    - 绑定发现(分页+本地过滤)、l2_create_ticket、drain outbox(lease)、reconcile(UNKNOWN/超时EXECUTING/滞留DISPATCHED)、task_runs 状态推进
B4d:approve CLI
    - approve.sh <ticket_id>、l2_approve、pending list、approved_by=id@host
B4e:E2E + 崩溃恢复
    - 正向 merge / TOCTOU 拒 / 并发领取 / EXECUTING 崩溃对账 / UNKNOWN 对账 / DISPATCHED 滞留重派 / 全 DENY
```

每步独立验证 + 证据落盘,沿用"不移动旧标签、新增 closed 标签、commit 无任何 AI 标识"惯例。

---

## 14. 待确认(若你还有补充)

- 执行超时阈值:merge/close 默认 60s 合理?(GitHub merge 通常秒级,60s 留足余量)
- `merge_method` 默认值:squash / merge / rebase?我倾向 squash(单 commit,审计干净),但取决于仓库约定。
- outbox lease 时长:60s 够 Gateway claim?(claim 是本地 DB+上游首个包,秒级)
