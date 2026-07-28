# M3-B4e 证据 · 总 E2E(review→fix→verify→discover→ticket→approve→drain→MERGED)+ 韧性

> **完整运行中 E2E:真 Controller(双 loop)+ 真 Gateway(policy-gw-e2e)+ 真 GitHub 写(fixture)+ 真 DB;Agent 决策由 `process_event`(Controller 自身 Matrix 事件处理器)确定性注入。**
> 验证日期:2026-07-28 | 关联代码:`tools/m3b-b4e-e2e.sh` + `tools/workflow-controller/controller.py` + `tools/approve.sh`
> 关联设计:[M3-B4 §B4e](../../docs/M3-B4-审批票据与Action-Outbox设计.md) | 关联前置:[[m3b-b4c](../m3b-b4c/README.md)] [[m3b-b4c-hardening](../m3b-b4c-hardening/README.md)] [[m3b-b4d](../m3b-b4d/README.md)]

## 方法论(与 B4 系列约定一致)

- **确定性 Agent 注入,非 LLM Agent**:review→fix→verify 经 `controller.process_event(event_id, room, sender, body, ts)` 注入(Controller 自身的 Matrix 事件处理函数),`sender` 严格匹配 Controller 校验(submit=admin / review=reviewer / fix=fixer / verify=verifier),`VERDICT=PASS` 独立行触发 L2 链。fix 阶段产生**真 fix PR**(经 e2e_GW fixer 在 fixture 仓 create_branch+file+PR),使绑定发现能权威读回。
- **为何不用 LLM Agent**:B4e 的核心是韧性(崩溃恢复 / UNKNOWN-EXECUTING 对账 / Gateway 降级恢复 / Matrix 循环存活),这些必须是**可复现**的;LLM Agent 会令其不可重复。live-Agent Matrix 链已由 [m3a-final-04](../m3a-final-04/README.md) 验证(12/12)。
- **fixture 隔离**:全程 `e2e-lib.sh` + `e2e_guard` + `policy-gw-e2e`(fixture-only policy);绝不写生产 `nghqqa/MergePilot`。结束时 fixture **0 open PR、仅 main**。

## 关键不变量(评审主线,本测试再次确认)

1. **PG 唯一权威**:状态转换 + outbox 同事务;Gateway 调用不持 DB 事务。
2. **绝不重 merge**:UNKNOWN/EXECUTING/USED/DISPATCHED 绝不自动重新 merge;每 ticket 恰好 1 次 L2_CLAIMED。
3. **故障域分离**:Loop A(PG-驱动 L2)与 Loop B(Matrix /sync+dispatch)独立 try/except —— Gateway 降级时 Loop A 进 breaker,Loop B 继续运行。
4. **TOCTOU + canonical payload**:merge/close 经票据 claim + canonical_payload + args_hash CAS。
5. **approved_by 不可伪造**:由 DB `session_user` 写(B4d.1),approve CLI 无 `--by` 入口。

## 验收(43/43 PASS · 两轮稳定复现)

`tools/m3b-b4e-e2e.sh`:`source e2e-lib.sh` + `e2e_guard`,经 `policy-gw-e2e` 在 fixture 仓建真 PR/票/合并。

| # | 阶段 | 场景 | 关键断言 |
|---|---|---|---|
| 1 | 全链 E2E | review→fix→verify(VERDICT=PASS)→discover→ticket→approve.sh→drain→MERGED | stage_runs review/fix/verify COMPLETED;task MERGED;approval USED;outbox SUCCEEDED;result_sha;approved_by=mergepilot_approver;**恰好 1 次 L2_CLAIMED**;fixture PR=MERGED |
| 2 | lease 崩溃恢复 | drain 对不可达 Gateway → DISPATCHED+lease+attempts=1(TRANSIENT)→**真容器 restart run_forever 恢复** | attempts 1→2;approval USED;恰好 1 次 L2_CLAIMED(恢复未重 merge) |
| 3a | 对账·EXECUTING 未合并 | reconcile_l2 | approval FAILED;outbox FAILED;task HOLD |
| 3b | 对账·UNKNOWN 已合并 + 绝不重 merge | reconcile→USED;再 drain+reconcile | task MERGED;**L2_CLAIMED 计数收敛前后不变=1** |
| 4 | Gateway 降级→恢复 | 真容器 run_forever;关 GW → breaker 开 → 恢复 GW → 自动恢复 | 降级期 approval APPROVED / 0 L2_CLAIMED / 进程存活 / 日志见 DEGRADED;恢复后 MERGED,1 次 L2_CLAIMED |
| 5 | Matrix 非 L2 循环存活 | 降级期注入 TASK_SUBMITTED → Loop B 派发 @reviewer 到真 Matrix 房间 | **matrix_event_id 派发成功**(Loop B 独立于 Gateway) |
| 6 | 证据固化 | db-snapshot / mcp-calls / controller+gateway logs / dispatch-outbox / github-residue / transcript | 无凭证泄漏 |
| 7 | 收尾 gate | fixture 0 open PR / 仅 main;`[PASS-eq 43] && [FAIL-eq 0]` | fixture 干净 |

结尾门:`[ "$FAIL" -eq 0 ]`(全过才退 0;少跑/失败退 1)。

## 终态快照(`db-snapshot.txt`,权威)

| run_id | task | stage | approval | outbox | approved_by | result_sha |
|---|---|---|---|---|---|---|
| b4e-e2e | MERGED | l2_done | USED | SUCCEEDED/- | mergepilot_approver | (merge commit) |
| b4e-crash | MERGED | l2_done | USED | SUCCEEDED/TRANSIENT | mergepilot_approver | (merge commit) |
| b4e-deg | MERGED | l2_done | USED | SUCCEEDED/- | mergepilot_approver | (merge commit) |
| b4e-execfail | HOLD | l2_drain_failed | FAILED | FAILED/CLAIM_FAILED | — | — |
| b4e-execused | MERGED | l2_done | USED | SUCCEEDED/- | — | (merge commit) |
| b4e-matrix | RUNNING | review | — | — | — | —(Loop B 派发探针,不经 L2) |

- MCP 审计(`mcp-calls.txt`):**4 × L2_CLAIMED + 4 × L2_COMPLETE**(happy/crash/deg/execused 各 1 次真 merge;execfail FAILED 无 claim;execused 的 claim 来自对账前的构造合并)。无双 claim。
- Matrix 派发证据(`controller-b4e-logs.txt`):`outbox #NNN → reviewer @ !<room> (eid=$...)` —— 降级期 Loop B 真 Matrix 派发成功。

## 证据文件

| 文件 | 内容 |
|---|---|
| `e2e-test.out` | 43/43 原始输出 + 各场景 DB 断言 |
| `e2e-transcript.txt` | 全量录像 transcript(tee 全输出) |
| `run-raw.log` | 一次性 Controller 调用原始输出(含 `[ctrl]` 日志,排障用) |
| `db-snapshot.txt` | b4e-* 全 run 终态(task/stage/approval/outbox/approved_by/result_sha) |
| `stage-runs.txt` | review/fix/verify 阶段状态(全 Agent 链) |
| `mcp-calls.txt` | L2 审计行(L2_CLAIMED/L2_COMPLETE per ticket) |
| `dispatch-outbox.txt` | Matrix 派发队列(含 matrix_event_id 派发证据 + 测试中和行) |
| `controller-b4e-logs.txt` | 韧性容器完整日志(breaker 开/恢复 + Matrix Loop B 派发 + L2 drain) |
| `gateway-logs.txt` | policy-gw-e2e 日志尾段 |
| `github-residue.txt` | 本测试在 fixture 创建/合并的真实 PR 清单 |
| `credential-scan.txt` | 无凭证泄漏(0 字节) |

## 工程要点(实现中解决)

- `inject_complete` 的 verify body 用 `$'\n'`(真换行)而非 `$(printf '\n')`(命令替换吞尾随换行),否则 `VERDICT=PASS` 不在独立行 → 误入 PARTIAL 分支。
- 迁移只重跑幂等的 `m3b_b4c1.sql`/`m3b_b4c1_1.sql`;基线 `m3b_b4.sql` 的 `l2_approve` 被 B4d.1 改过默认参数,重跑非幂等(持久库已在 B4a–B4c 闭合时应用)。
- Phase 5 Matrix 存活:先中和(neutralize)Phases 1-3 残留的 `dispatch_outbox`(PENDING+RETRY,确定性注入不经 Matrix),否则会以低 id 堵塞 `dispatch_pending`(遇首个失败行即抛出)饿死 Phase 5 的高 id 行。
- Matrix 房间用 `trusted_private_chat` + invite(create_task_room.py 同款);`private_chat` 会被 hiclaw-controller 标为 "unknown version"。
- `create_fix_pr` 带 3 次重试(抗 GitHub/Gateway 瞬时抖动)。

## 后续

- **B5**:负向证据 8 项(直连拒 / list 过滤 / 跨角色拒 / fixer 写约束 / 票据伪造-过期-重复拒 / 合法票据只成功一次 / 全审计)。
- **M5**:Benchmark(N≥10/20)+ 基线对比(单 Agent vs 多 Agent)。
- 现场录像(M7)可在本脚本 asciinema 包裹下录制(本证据的 `e2e-transcript.txt` 即完整 transcript)。
