# M3-B4b 证据 · Gateway L2 Claim 流 + 故障路径覆盖

> **L2 claim→TOCTOU→上游→complete/fail/mark_unknown + FAULT_INJECT test_mode 门 + 生产断言**
> 验证日期:2026-07-27 | 标签:`m3b-b4b-closed`(commit `1d30874`)
> 注:tag 注释误写 "M4b",标签名 `m3b-b4b-closed` 正确,不移动。

## 落地

### Gateway L2 流(gateway.py)
- **claim**:`l2_claim_ticket` 一次 CAS(action+repo+PR+args_hash+expiry);0 行→CLAIM_MISMATCH;DB 故障→L2_DB_UNAVAILABLE(不混为 mismatch)
- **TOCTOU**:`asyncio.wait_for(timeout=L2_TIMEOUT_SECONDS)`;head.sha + state=open + base==target_branch 全校验;**读超时→FAILED**(写未发,安全);不匹配→FAILED
- **上游调用**:从 canonical_payload 构造 args(不转发散参/approval_ticket);`asyncio.wait_for` 超时;**写超时→UNKNOWN**(请求已进入,结果未知,绝不重试)
- **结果分类**:明确成功→complete;GitHub 明确拒绝→fail;网络超时/中断→mark_unknown
- **三态 l2_exec**:APPLIED/CAS_MISMATCH/DB_ERROR;complete 返回非 APPLIED→STATE_COMMIT_PENDING(B4c 对账)

### FAULT_INJECT 安全门
- 仅 `/tmp/.test_mode` 存在时激活;生产无此文件→设了 FAULT_INJECT 也 SystemExit 拒绝启动
- 测试容器 bind-mount `/tmp/.test-mode:/tmp/.test_mode`
- 生产断言:gateway 无 FAULT_INJECT env + 无 /tmp/.test_mode

## 验证(15+13=28 PASS)

### 主测试(main-test.txt)15/15
缺票/伪造票/TOCTOU 不匹配/hash 不匹配/过期/审计(ticket_id+execution_id)/成功 merge→USED+RESULT/真并发互斥(同 PR+同票+2 并行→1 USED)/bad-L2-DSN→L2_DB_UNAVAILABLE。

### 故障测试(fault-test.txt)13/13
| # | 场景 | 结果 |
|---|---|---|
| 1 | bad audit DSN → AUDIT_UNAVAILABLE | ✅ |
| 2 | audit fail 后 ticket=FAILED | ✅ fail_ticket 三态 |
| 3 | PR still open after audit fail | ✅ GitHub 未调 |
| 4 | PR head SHA unchanged | ✅ |
| 5 | close → USED(独立 PR2) | ✅ |
| 6 | TOCTOU 读超时 → FAILED(写未发) | ✅ |
| 7 | 上游 is_error → FAILED | ✅ |
| 8 | complete DB_ERROR → EXECUTING | ✅ STATE_COMMIT_PENDING |
| 9 | 写超时 → UNKNOWN(不重试) | ✅ |
| 10 | upstream.call_tool entered before timeout | ✅ 证明请求已进入 |
| 11 | FAULT_INJECT 无 .test_mode → 拒绝启动 | ✅ |
| 12 | 生产:无 FAULT_INJECT | ✅ |
| 13 | 生产:无 .test_mode | ✅ |

### 审计证据
- `audit-summary.txt`:10 行(ticket_id+phase+decision+reason_code+action+status)
- `fault-audit.txt`:10 行(同结构)
- `approvals-snapshot.txt`:票据状态快照
- `fault-approvals.txt`:故障测试票据快照

## 关键修正
| # | 问题 | 修复 |
|---|---|---|
| ① | FAULT_INJECT 是生产后门 | /tmp/.test_mode 门;无文件→SystemExit |
| ② | write_timeout 在调上游前 raise | 0.001s 超时让 upstream.call_tool 被进入 |
| ③ | audit-summary 空文件算 PASS | JOIN 查询 + 非空断言 + 不计 PASS |
| ④ | close 改 PR 状态影响后续 fault | 独立 PR2(run_id=b4bf-close-run) |
| ⑤ | write_timeout 合并 PR 影响后续 | 放循环最后 |
| ⑥ | fail_ticket 三态被忽略 | INTENT-audit-fail + upstream-reject 都检查 APPLIED/CAS_MISMATCH/DB_ERROR |
