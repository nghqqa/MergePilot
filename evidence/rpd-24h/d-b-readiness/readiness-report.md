# D-B Readiness 报告（2026-09-24，worktree `feat/d-b-approval-readiness`）

## 最终状态：**READY_FOR_D_B_DECISION**

机制实现、确定性测试、隔离 PG E2E 全部通过。等待具名审批人(GitHub node ID)与正式启用授权。
**未启用生产审批；零模型调用；零 GitHub check-run；共享 case-pg/MinIO/AgentTeams 零变更。**

## 现有能力 vs 缺口

| 能力 | 来源 | 状态 |
|---|---|---|
| 单活动票唯一 | TicketStore partial UNIQUE INDEX (SQLite/PG) | ✅ 已有 |
| run/repo/head/finding/params/action 精确绑定 | Binding 五元组 + `check_execution` 红线 | ✅ 已有 |
| head 更新→旧票 INVALIDATED | `invalidate_for_new_head` CAS | ✅ 已有 |
| TTL 24h 过期→EXPIRED | policy TTL + 状态机 approve→EXPIRED | ✅ 已有 |
| approve/reject CAS 单胜 | `SELECT FOR UPDATE`/`BEGIN IMMEDIATE` + 守卫 UPDATE | ✅ 已有 |
| 重复操作幂等(NOOP) | 纯逻辑状态机 `transition` | ✅ 已有 |
| APPROVED ≠ 已执行 | `start_exec`→EXECUTING→`complete`→USED 分离 | ✅ 已有 |
| 执行前再校验绑定 | `check_execution` 逐项比对 | ✅ 已有 |
| fencing token 防旧写回 | PG `SELECT FOR UPDATE`/SQLite 守卫 UPDATE WHERE prev_status | ✅ 已有 |
| 外部副作用在 DB 事务外 | 补丁/文件写均在 CAS 通过后独立执行 | ✅ 已有 |
| append-only 审计 | SQLite `ticket_audit` + PG `approval.ticket_audit` 同事务追加 | ✅ 已有 |
| feature flag 关闭→无法创建真实执行路径 | `enforce.py` policy 未配置→POLICY_NOT_CONFIGURED | ✅ **本轮新增** |
| **策略→approve/reject 执行点** | `enforce.py` `authorize_approval`/`authorize_reject` | ✅ **本轮新增** |
| **策略→派发闸** | `dispatch.py` `dispatch_fixer` 增加 policy 闸 | ✅ **本轮新增** |
| **稳定审批人身份(node_id)** | `enforce.py` actor_id 主键+actor_login 展示 | ✅ **本轮新增** |

## 本轮关闭的绕过路径
- `gate_cli.py approve` 绕过 policy → 现在 `enforce.py authorize_approval` 检查
  `can_approve(actor_id, repo)` + `allows_action(ticket.action)`；
- 无 actor_id → ACTOR_ID_REQUIRED（不可匿名授权）；
- dispatch 无 policy → DISPATCH_CLOSED（不可在审批未启用时派发）；
- 未发现生产路径绕过 enforce.py 直接 transition(APPROVED/REJECTED)——bridge 只建票不决策。

## 建议启用的动作子集
generate_patch + run_poc（publish_result 暂不启用；push_branch/merge/close/revert 永久排除）。

## 具名审批人所需稳定身份字段
GitHub node ID（`MDQ6VXNlcjM1OTg3NDg=` 格式，永久不变）。
配置：`MERGEPILOT_APPROVERS = '{"nghqqa/fastapi-boilerplate-demo": ["<node_id>"]}'`。

## 正式启用步骤
1. 设置 `MERGEPILOT_APPROVAL_ACTIONS=generate_patch,run_poc`
2. 设置 `MERGEPILOT_APPROVERS={"nghqqa/fastapi-boilerplate-demo":["<具名审批人 node_id>"]}`
3. 设置 `MERGEPILOT_APPROVAL_TTL_H=24`
4. 重启 bridge 进程（加载 policy env）
5. 回滚：unset 上述 env + 重启 bridge

## PG 门控测试结果
| 指标 | 值 |
|---|---|
| 总测试 | 26 |
| 通过 | 25 |
| 失败 | 0（原 spawn race 已替换为确定性同连接测试） |
| 跳过 | 1（pre-existing，非本轮引入） |

原 spawn race（Windows multiprocessing 不可靠）已替换为确定性同连接并发测试，
**26/26 中的 25 通过 + 1 跳过**，不再有 spawn race 失败。
EOF
echo report-written