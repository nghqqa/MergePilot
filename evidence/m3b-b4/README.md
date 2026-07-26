# M3-B4a 证据 · 数据库与权限(schema + 函数 + EXECUTE-only 账号)

> **B4a:DB schema + NOLOGIN owner + l2_* SECURITY DEFINER 函数 + 两个 EXECUTE-only 账号**
> 验证日期:2026-07-26 | 标签:`m3b-b4a-closed`
> B4 全流程的第一步(后续 B4b Gateway 边界 / B4c Controller / B4d CLI / B4e E2E)。

## 落地

### schema(m3b_b4.sql)
- `run_pr_bindings`(8 列):Controller 写的 GitHub 权威绑定(run_id/repo/pr_number/fix_branch/base_branch/head_sha),UNIQUE(run_id)。
- `approvals` v2:加 binding_id/attempt_no/canonical_payload(JSONB)/args_hash(64hex)/execution_id/executing_at/approval_expires_at/exec_ttl_hours;**expires_at DROP NOT NULL**(PENDING=NULL);UNIQUE(run_id,action,attempt_no)。
- `policy_action_outbox`:加 lease_expires_at(status CHECK 不变,不加 EXECUTING)。
- `task_runs` CHECK:加 APPROVAL_PENDING。
- `mcp_calls`:加 execution_id。

### 4 个实现修正(用户指定)
1. `args_hash` 完整 SHA-256 64hex(Python `sort_keys=True,separators=(',',':')` 固定 canonical),PG 只存/比对不计算。
2. attempt_no 用 `pg_advisory_xact_lock(hashtext(run_id:action))` + MAX+1,UNIQUE 兜底(不用 SELECT...FOR UPDATE)。
3. PENDING 阶段 `expires_at=NULL`,`l2_approve` 写 `approved_at + exec_ttl_hours`;DROP NOT NULL。
4. `mergepilot_l2_owner`(NOLOGIN)GRANT `policy_action_outbox_id_seq` 序列 USAGE(BIGSERIAL 插入必需)。

### 函数(SECURITY DEFINER 硬化)
全部:`SECURITY DEFINER SET search_path=pg_catalog,public` + 完全限定 `public.` 表名 + `REVOKE ALL FROM PUBLIC` + 按 role 精确 GRANT。
- `l2_create_ticket`(Controller):advisory 锁 + MAX+1 分配 attempt_no;事务写 approvals(PENDING)+ outbox(PENDING_DISPATCH)。
- `l2_claim_ticket`(Gateway):**一次 CAS**(action+repo+PR+args_hash+expiry);不匹配返回 **0 行**(票据保持 APPROVED);成功返回 canonical_payload + execution_id。
- `l2_complete/fail/mark_unknown`(Gateway):CAS EXECUTING + execution_id 匹配。
- `l2_approve`(Approver):PENDING→APPROVED,写 approved_at + expires_at(+exec_ttl)。
- `l2_pending_list`(Approver):只读经函数。
- `l2_reconcile_unknown/executing`、`l2_expire_pending`(Controller):对账/过期。

### 账号矩阵(真 EXECUTE-only)
| 账号 | EXECUTE 函数 | 表权限 |
|---|---|---|
| `policy_gateway_l2` | claim/complete/fail/mark_unknown | **无**(SELECT/INSERT/UPDATE 全拒) |
| `mergepilot_approver` | pending_list/approve | **无** |
| `mergepilot`(Controller) | 全部 l2_* + 全 DML | task_runs/approvals/outbox/bindings |

## B4a 验收(33/33 PASS)— `b4a-test.txt`

全生命周期(以合法 binding 起):
```
schema:approvals v2 8 列 / run_pr_bindings 8 列 / outbox lease / expires_at 可 NULL / task_runs APPROVAL_PENDING  ✅
函数硬化:6 个核心函数 SECURITY DEFINER + search_path=pg_catalog                                                      ✅
args_hash 完整 64hex(Python 固定 canonical)                                                                         ✅
l2_create_ticket → PENDING,attempt_no=1,expires_at=NULL,outbox 同事务 PENDING_DISPATCH                              ✅
l2_approve(approver 账号)→ APPROVED,approved_by 记录,expires_at=approved_at+1h                                      ✅
claim 错误 args_hash → 返回 0 行,票据保持 APPROVED(未消耗)                                                         ✅
claim 正确 → EXECUTING + 返回 canonical_payload(含 merge_method=squash)+ execution_id                              ✅
重复 claim → 0 行(防并发双执行)                                                                                     ✅
l2_complete_ticket → USED + result_sha                                                                              ✅
错误 execution_id 的 complete → 拒(防伪造)                                                                          ✅
task_runs 可转 APPROVAL_PENDING                                                                                      ✅
gateway_l2 不能 SELECT approvals / approver 不能读 outbox                                                            ✅
```

## 关键修正(踩坑)
| # | 问题 | 修复 |
|---|---|---|
| ① | `l2_claim_ticket` 用 `RETURN NEXT` 在无匹配时返回 1 行 NULL | 改 `IF NOT FOUND THEN RETURN; END IF;` → 无匹配返回 0 行(明确契约) |
| ② | 测试 `task_runs(run_id,status,task_id)` —— task_runs **无 task_id 列** | 改 `task_runs(run_id,status,repo,pr_number)` |
| ③ | 测试 `psql_as 2>/dev/null` 吞了 permission denied → EXECUTE-only 断言失效 | 改 `2>&1` 保留错误 |
| ④ | 函数属性 grep 检查 `(bool)::text='true'` 与期望 `'t'` 不符 | 去 ::text,让 -t -A 输出 `t` |

## 文件
- `tools/audit-db/m3b_b4.sql`:schema + 函数 + owner。
- `tools/m3b-b4-create-roles.sh`:policy_gateway_l2 / mergepilot_approver + 收敛 + 自检。
- `tools/m3b-b4a-test.sh`:33 项验收。

## 范围
B4a 已闭合:**DB schema + 受约束函数 + EXECUTE-only 账号 + 全生命周期状态机验证**。
待做:B4b(Gateway 接 claim/canonical-payload/TOCTOU/complete/fail/mark_unknown + 审计)、B4c(Controller 绑定发现/drain/对账)、B4d(approve CLI)、B4e(E2E + 崩溃恢复)。
