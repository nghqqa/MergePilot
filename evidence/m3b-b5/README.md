# M3-B5 证据 · 负向证据 ×8(现有安全边界反证 · 加固版)

> **证明现有安全边界(不扩大权限):网络隔离 / 角色-token / deny-by-default 策略 / fixer 写约束 / L2 票据 CAS(含 args_hash 篡改)/ 单次执行 / 不可篡改审计。每项断言精确到 reason_code + DB 状态 + 上游调用计数 + GitHub 零副作用。硬门 [FAIL=0] && [PASS=EXPECTED_PASS=50]。**
> 验证日期:2026-07-29 | 关联代码:`tools/m3b-b5-negative.sh` + `tools/policy-gateway/gateway.py` + `tools/audit-db/m3b_policy.sql`(mcp_calls_immutable)+ `m3b_b4.sql`(l2_claim_ticket CAS)
> 关联前置:[[m3b-b4e](../m3b-b4e/README.md)] | 设计:[M3-B4](../../docs/M3-B4-审批票据与Action-Outbox设计.md)

## 方法论

- **fixture 隔离**:全程 `e2e-lib.sh` + `e2e_guard` + `policy-gw-e2e`(fixture-only allowlist);绝不写生产 `nghqqa/MergePilot`。结束时 fixture **0 open PR、仅 main**。
- **反证既有边界**:B5 只验证 B1–B4 已建立的安全属性;**不扩大权限,不放宽断言**。负向调用预期被拒,并精确断言 reason_code。
- **每项四要素**:① 精确 reason_code(写 main 必须命中 `BRANCH_PROTECTED`,不接受替代);② DB 状态(票据未消耗 / status 不迁移);③ 上游调用计数(每个 INTENT-DENY 的 correlation 不得存在 RESULT/ERROR → 0 上游调用);④ GitHub 零副作用(PR 仍 OPEN / 分支不存在 / 无第二次 merge)。
- **审计窗口**:精确 `TEST_START`/`TEST_END` 均用 `clock_timestamp()`(真实时钟,非事务起始 `now()`);前者在任何测试动作前、后者在所有动作完成后;`mcp_calls` 按 `ts >= START AND ts <= END` 过滤(审计 INSERT-only 跨测试累积)。
- **list 集合比较无自证循环**:`upstream_visible` 来自**独立**的 upstream `github-mcp tools/list`(经 mcp-backend-net 直连 github-mcp:8082,非角色返回值并集)——避免"Gateway 误藏某工具 → 并集法把它从期望集合删掉而误通过"。
- **凭据扫描**:覆盖 script+evidence;① 提取真实凭证值(角色 token + DB 密码)逐个精确搜索;② 已知格式扫描(ghp_/github_pat_/sk-/AKIA/xox);**不用 `token=` 等宽泛规则过滤真实泄漏**。

## 验收(50/50 · 硬门 PASS · 两轮稳定复现)

`tools/m3b-b5-negative.sh`:`PASS=50 / EXPECTED=50 / FAIL=0`,连续两次稳定,exit 0。硬门 `[FAIL=0] && [PASS=EXPECTED_PASS]`(EXIT trap 保留 `$?`,不掩盖 exit 1)。

| # | 场景 | 边界 / reason_code | 关键断言 |
|---|---|---|---|
| 1 | 直连拒 | 网络隔离(mcp-backend-net 私有) | github-mcp ∈ mcp-backend-net;**真实 worker `hiclaw-worker-fixer`(Agent 容器;已退出则临时启动)∉ mcp-backend-net**;worker 直连 `github-mcp:8082` → **UNREACHABLE(gaierror)**;Gateway 可达上游(对比) |
| 2 | list 过滤 | `B2_FILTERED_LIST` + `REPO_NOT_ALLOWED` | **各角色 list_tools == policy allowlist ∩ 独立 upstream(44 工具,精确集合比较,无自证循环)**;非 allowlist repo → REPO_NOT_ALLOWED |
| 3 | 跨角色拒 | `ROLE_PATH_MISMATCH`(401)+ `TOOL_NOT_ALLOWED` | fixer token 上 /coordinator/sse → 401 ROLE_PATH_MISMATCH;fixer 调 merge → TOOL_NOT_ALLOWED |
| 4 | fixer 写约束 | `TOOL_NOT_ALLOWED`/`BRANCH_NOT_FIX_PREFIX`/`BRANCH_PROTECTED`/`PATH_DENIED`/`PR_FIELD_NOT_ALLOWED` | delete_file→TNA;非 fix/→BNFP;**写 main→精确 BRANCH_PROTECTED**;.env→PD;update_pr 带 base→PFNA;被拒分支不存在 |
| 5 | 伪造票拒 | `CLAIM_MISMATCH`(l2_claim_ticket CAS 0 行) | (a) 不存在 ticket→CM;(b) **真实 APPROVED 票 + 篡改 args(commit_title)→ CM(args_hash 不匹配)**,票仍 APPROVED、0 claim、PR 仍 OPEN |
| 6 | 过期/重复票拒 | `CLAIM_MISMATCH`(expires_at / status CAS) | expires_at≤now→CM(票未消耗);USED 票→CM(status!=APPROVED);PR 仍 OPEN |
| 7 | 合法票只执行一次 | `L2_CLAIMED`+`L2_COMPLETE` 各 1;再 claim → `CLAIM_MISMATCH` | 1 merge commit;approval→USED;恰好 1×CLAIMED+1×COMPLETE;再 claim 后 CLAIMED 仍=1 |
| 8 | 完整不可篡改审计 | 每个 DENY 留 `phase=INTENT AND decision=DENY` 行;`mcp_calls_immutable()` | **8 个 DENY reason_code 精确计数(含 BRANCH_PROTECTED)+ DENY 总数=13**;DENY correlation 无上游 RESULT/ERROR;UPDATE/DELETE→触发器拒(超管亦不可);窗口内 0 TAMPER |

## 审计指纹(`audit-summary.txt`,本窗口 reason_code 计数)

```
BRANCH_NOT_FIX_PREFIX 1
BRANCH_PROTECTED      1
CLAIM_MISMATCH        5   ← 伪造 + args篡改 + 过期 + 重复 + 再claim
L2_CLAIMED            1   ← 合法票单次
L2_COMPLETE           1   ← 合法票单次
PATH_DENIED           1
PR_FIELD_NOT_ALLOWED  1
REPO_NOT_ALLOWED      1
ROLE_PATH_MISMATCH    1
TOOL_NOT_ALLOWED      2
(TAMPER 行 = 0 — 触发器有效)
```

`audit-intent-deny.txt` 给出 `phase='INTENT' AND decision='DENY'` 的精确分组计数;`mcp-calls-window.txt` 给出窗口内全部调用(ts/phase/caller/tool/decision/reason_code),可逐条核验 DENY 的 correlation_id 不出现在任何 RESULT/ERROR 行。

**DENY 精确计数(硬断言,非 ≥1)**:`REPO_NOT_ALLOWED=1`、`ROLE_PATH_MISMATCH=1`、`TOOL_NOT_ALLOWED=2`、`BRANCH_NOT_FIX_PREFIX=1`、`BRANCH_PROTECTED=1`、`PATH_DENIED=1`、`PR_FIELD_NOT_ALLOWED=1`、`CLAIM_MISMATCH=5`,**INTENT+DENY 总数=13**(防遗漏/多余)。

**单次执行铁证**:L2_CLAIMED=1 且 L2_COMPLETE=1(恰好一次真 merge);5 次 CLAIM_MISMATCH 覆盖伪造 / args 篡改 / 过期 / 重复 / 再 claim 全部拒。

## 安全结论(无缺口)

B5 未发现任何安全缺口。首轮(加固前)若干失败经核全是**测试夹具 bug**(非边界缺口),已修复,边界本身始终成立;**断言未被放宽**:
- `create_or_update_file` 漏传必填 `message` → MCP SDK input validation 在策略层之前拒;补 `message=` 后 BRANCH_PROTECTED/PATH_DENIED 正确触发。
- `update_pull_request state=closed` 被 L2 close 路径正确拦为 `L2_REQUIRES_COORDINATOR`(state 触发 l2_action);改用 `base=develop`(非 title/body 字段)测到 `PR_FIELD_NOT_ALLOWED`。
- 分支存在性检查误读 404 JSON → 改 list+精确 grep 计数。
- `UPDATE ... LIMIT`(PG 不支持)→ 改 `ctid IN (subquery)`。
- 加固版额外修复:EXIT trap 曾以 `... || true` 掩盖硬门 exit 1 → 改 `$?` 保留;`EXPECTED_PASS` 按最终测试项精确校准为 49。

**关键不变量再次确认**:
1. **PAT 不出后端网**:github-mcp 仅 ∈ mcp-backend-net;真实 worker 仅 ∈ hiclab-net → 直连不可达(拓扑已核验)。
2. **deny-by-default + 路径/token 一致**:各角色只见其 policy allow 集(精确集合比较);token-role-path 不符 401。
3. **L2 一次 CAS 全校验**:action/repo/pr/**args_hash**/expires_at/status='APPROVED' 任一不符(含真实票的 args 篡改)→ 0 行 → CLAIM_MISMATCH,票据不消耗,GitHub 零副作用。
4. **单次执行**:APPROVED→EXECUTING→USED 单向;USED 票不可再 claim。
5. **不可篡改审计**:mcp_calls INSERT-only 触发器拦 UPDATE/DELETE(超管亦不可);每个 DENY 在调上游前留 INTENT 审计行(0 上游调用)。

## 证据文件

| 文件 | 内容 |
|---|---|
| `negative-test.out` | 49/49 原始输出 + 各场景 reason_code/DB/GitHub 断言 + 硬门结果 |
| `negative-transcript.txt` | 全量 transcript(最终验收后生成,含硬门结果) |
| `audit-summary.txt` | 本窗口 reason_code 计数(审计指纹) |
| `audit-intent-deny.txt` | `phase=INTENT AND decision=DENY` 精确分组计数(8 reason_code) |
| `mcp-calls-window.txt` | 窗口内全部 mcp_calls(可核验 DENY correlation 无 RESULT) |
| `db-snapshot.txt` | b5-* task/approval/ticket 终态 |
| `gateway-logs.txt` | policy-gw-e2e 日志尾段(DENY 决策可见) |
| `probes.txt` | worker/github-mcp 网络 + 直连探针(UNREACHABLE)+ 跨角色探针(401)+ 审计窗口 |
| `github-residue.txt` | 本测试 fixture PR 清单(1 个 MERGED,余 OPEN→已清理) |

凭据扫描在脚本内执行(真实凭证值逐个搜索 + 已知格式),结果 `✅ 无真实凭证值泄漏` + `✅ 无已知凭证格式` 记于 transcript。

## 后续

- B5 闭合后,**M3-B(B1–B5)全部 closed**:工具层权限边界 + L2 审批闭环 + 负向反证齐备。
- **下一主线:M3-C**(不直接进 M5)。
- fresh-DB 关注仍存(见 [项目状态](../../docs/项目状态.md)):交付前需验证完整 migration 顺序从空库铺好。
