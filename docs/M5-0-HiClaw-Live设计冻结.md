# M5-0 HiClaw Live 最小闭环设计冻结 v2.5

## 1. 状态声明

- **设计冻结 PASS**（v2.1 + v2.2 + v2.3 + v2.4 + v2.5 全部阻断项已精确冻结）
- **v2.5 极窄勘误**：Candidate provenance 角色校验冲突（详见 §23）
- **implementation NOT STARTED**
- **hiclaw_live = false**（M5-0 完成前不得变更为 true）
- **数据库契约不变**（无新表、新列、新函数、新索引、新 CHECK）
- 基线 R = `9a754e231c6d56077799cff8208e0ee6d529337c`
- 本文是设计规格，不是已实现事实。所有"将"描述的是计划行为

---

## 2. 目标

把 M4-F 六 Skill 全链从协议 fixture 切换到真实 HiClaw Manager/Worker/Matrix：

1. 真实 Matrix ingress（经 /sync 拾取，非直接调 process_event）
2. 真实 HiClaw Manager / Reviewer / Fixer / Verifier 因果参与
3. 六 Skill 保持现有冻结 envelope + SD API 契约
4. 真实 Policy Gateway / GitHub MCP 桥
5. 为 M5 Benchmark 提供真实运行基线（总耗时、各阶段、重试、Gateway 调用数原始数据）

## 3. 非目标

- 完整 OTel/SLS 平台建设（M6）
- Nacos / RocketMQ 治理（M6）
- 多租户（M6）
- 管理 UI（M6）
- 多仓库生产化（M6）
- 大规模 Benchmark（M5）
- 修改 M4-F 数据库契约（SQL / SD API / envelope）
- 自动 merge（PRLifecycle 仅 create_branch + create_pull_request）

## 4. 当前事实（v2.3 代码审计确认）

| 编号 | 事实 | 文件:行 |
|---|---|---|
| F-1 | consume_events body 过滤不含 M4F_RUN → /sync 丢弃 M4F_RUN 事件 | controller.py:852 |
| F-2 | process_event sender 校验硬编码 ADMIN | controller.py:322 |
| F-3 | controller_offsets.consumer_name 硬编码 'controller' | controller.py:832,858 |
| F-4 | Matrix login user 硬编码 'admin' | controller.py:20,200 |
| F-5 | consume_events 遍历所有加入房间（无 room filter） | controller.py:843 |
| F-6 | drain_outbox SELECT 无 run_id 前缀过滤 | controller.py:781-784 |
| F-7 | drain_m4f_events claim 无 run_id 前缀过滤 | controller.py:691-710 |
| F-8 | M4-F drain 后无六 Skill 完成检测 + review stage 创建 | controller.py:675-757 |
| F-9 | handoff_watcher_v2 不排除 m5live-* 前缀 | handoff_watcher_v2.py:54-60 |
| F-10 | sender 从 Matrix event 截断为 localpart（无 server_name 校验） | controller.py:850 |
| F-11 | send_mention txn 确定性派生 sha256(room:user:text) | controller.py:219 |
| F-12 | dispatch_outbox.run_id 列已存在，可用于参数化 LIKE 过滤 | m3_state.sql:81 |
| F-13 | stage_events.run_id 列已存在且有索引 | m3_state.sql:60 |
| F-14 | L2/rollback 查询不触及 M5-0 current_stage 值（自然隔离） | controller.py:1364-1890 |

**hiclaw_live=false 的准确原因**：M4F_RUN 不经 /sync；SkillWorker 直接驱动；上游为 fake_github_mcp；无 Manager/Worker 因果参与。

## 5. 冻结角色和因果链

| 角色 | Matrix user_id | 产生事件 | 因果（删除则失败） |
|---|---|---|---|
| Operator | @admin 或 @operator | NL/结构化 PR 审查请求 | 不请求则无任务 |
| Manager | @manager | `M4F_RUN: {json}` | 不转换则运行不启动 |
| Reviewer | @reviewer | `TASK_COMPLETED: m5live-<run>-review` | 无则停在 await_review |
| Fixer | @fixer | `TASK_COMPLETED: m5live-<run>-fix` | 无则停在 await_fix |
| Verifier | @verifier | `TASK_COMPLETED: m5live-<run>-verify` + `VERDICT=PASS` | 无则永不 COMPLETED |
| Controller | admin 或 m5-0-ctrl | send_mention 阶段推进 | 不消费 handoff 则链路停 |
| SkillWorker | 无 Matrix 身份 | 6 Skill 子进程 | 不执行则无 skill 结果 |

**因果证明**：删除任一 Manager/Reviewer/Fixer/Verifier 步骤 → 链路停在对应 await 状态 → 超时 HOLD → 运行失败。

## 6. 完整事件序列

```
STEP 1  Operator → Matrix 房间：发送 PR 审查请求
STEP 2  Manager(@manager) → 房间：发送 "M4F_RUN: {contract_version:1, run_id:m5live-*, ...}"
        产生：真实 Matrix event_id；Controller /sync 拾取
STEP 3  Candidate Controller /sync → consume_m5_live_events → process_event
        校验：room ∈ allowlist, sender=manager, run_id 前缀 m5live-
        → INSERT stage_events(M4F_PENDING)
STEP 4  drain_m4f_events_scoped(run_prefix='m5live-')
        → m4f_ingress.stage_agentteams_event
        → bind_revision + put_envelope × 6 + enqueue_skill_job × 6
STEP 5  SkillWorker → 6 Skills（确定性子进程）
        → skill_invocations 6/6 SUCCEEDED + output_schema_validated
STEP 6  Candidate reconcile_m5_skill_to_review（单事务）
        → INSERT stage_runs(review, PENDING_DISPATCH)
        → INSERT dispatch_outbox(m5-<run>-review-dispatch)
        → UPDATE task_runs current_stage = 'm4f_await_review'
STEP 7  Candidate drain_outbox_scoped → send_mention(@reviewer, skill summaries)
STEP 8  Reviewer(@reviewer) → "TASK_COMPLETED: m5live-<run>-review"
        → Candidate process → verify stage_runs review → COMPLETED
        → INSERT stage_runs(fix, PENDING_DISPATCH) + dispatch_outbox
        → current_stage = 'm4f_await_fix'
STEP 9  Fixer(@fixer) → "TASK_COMPLETED: m5live-<run>-fix"
        → fix → COMPLETED + INSERT verify stage + dispatch
        → current_stage = 'm4f_await_verify'
STEP 10 Verifier(@verifier) → "TASK_COMPLETED: m5live-<run>-verify" + "VERDICT=PASS"
        → verify → COMPLETED, verdict=PASS
        → task_runs status=HOLD, current_stage='m5_verify_passed'
STEP 11 Candidate send_mention(room, "RUN_COMPLETED: ...")
        → collect_e2e_evidence → live evidence
```

## 7. 事件契约（当前代码事实 vs M5-0 冻结契约）

### 7.1 当前代码事实（未锚定子串匹配）

| 事件 | 当前正则 | 锚定 | 来源 |
|---|---|---|---|
| M4F_RUN | `M4F_RUN\s*:` (re.I) | **未锚定**——子串匹配 | controller.py:158 PAT_M4F |
| TASK_COMPLETED review | `TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-review` (re.I) | **未锚定**——子串匹配 | handoff_watcher_v2.py:56 |
| TASK_COMPLETED fix | `TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-fix` (re.I) | **未锚定**——子串匹配 | handoff_watcher_v2.py:58 |
| TASK_COMPLETED verify | `TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-verify` (re.I) | **未锚定**——子串匹配 | handoff_watcher_v2.py:60 |
| VERDICT | `re.search(r"(?mi)^\s*VERDICT\s*=\s*(PASS\|FAIL\|BLOCKED)\s*$", body)` | **行级锚定**——`(?mi)` 使 `^/$` 按行匹配 + 忽略大小写；但 body 可包含其他行 | controller.py:502 |

**注意**：当前 M4F_RUN / TASK_COMPLETED 正则均为未锚定子串匹配。自然语言中包含对应子串**会**触发。VERDICT 正则有行级锚定但允许 body 中存在其他行。

### 7.2 M5-0 冻结严格契约（M4F_ONLY_MODE=1 独立解析器）

M5-0 Candidate 使用独立严格解析函数（不影响 Legacy M3 路径的现有解析行为）。

**M4F_RUN 严格解析**：
- marker 必须位于 body **开头**（`body.startswith("M4F_RUN:")` 或 `body.startswith("M4F_RUN:")`）
- marker 后只能是一个 JSON object（`json.loads(body[len("M4F_RUN:"):].strip())`）
- JSON 通过 `m4f_ingress.validate_event` schema 校验
- trailing prose、第二个 marker、额外顶层字段均拒绝
- 解析失败 → stage_events status='ERROR'（不进入 M4F_PENDING）

**review/fix handoff 严格解析**（fullmatch）：
- body 必须完整匹配 `TASK_COMPLETED: <run_id>-review`（无 trailing prose）
- body 必须完整匹配 `TASK_COMPLETED: <run_id>-fix`（无 trailing prose）
- run_id 必须符合 M4F_RUN_PREFIX + `[A-Za-z0-9._:-]` 字符集

**verify handoff 严格解析**（fullmatch + 独立 VERDICT 行）：
- body 必须恰好两行：`TASK_COMPLETED: <run_id>-verify\nVERDICT=PASS|FAIL|BLOCKED`
- 或完整匹配 `TASK_COMPLETED: <run_id>-verify` + 含恰好一个独立行 `^\s*VERDICT\s*=\s*(PASS|FAIL|BLOCKED)\s*$`
- 多个 VERDICT 行 → 拒绝
- 无 VERDICT 行 → 等待（PARTIAL，不推进）

**通用规则**：
- 普通自然语言、引用文本、代码块和多个 marker **不得触发** M5-0 严格解析器
- 解析失败写 ERROR/HOLD 审计，不得静默忽略或继续推进
- 测试脚本不得使用 Agent token 模拟 Agent 输出
- evidence 必须记录真实 sender Matrix user_id 和 event_id

**M5-0 严格解析测试覆盖**（正例/负例）：
- 正例：纯 marker body；M4F_RUN + 合法 JSON；verify 两行格式
- 负例：surrounding prose（marker 前后自然语言）；duplicate marker；code fence 包裹；trailing text；错误 JSON；额外顶层字段；多个 VERDICT 行

## 8. 配置兼容矩阵（生产兼容 + Candidate fail-closed）

### 8.1 双模式默认值

| env | M4F_ONLY_MODE=0（生产） | M4F_ONLY_MODE=1（Candidate） |
|---|---|---|
| MATRIX_USER | 默认 `"admin"`（向后兼容，不崩溃） | **必须显式设置**；不得为 `admin`；推荐 `m5-0-ctrl`；缺失/空/`admin` → 启动失败 |
| CONTROLLER_CONSUMER_NAME | 默认 `"controller"`（保持生产游标） | **必须显式设置**；不得等于 `controller`；推荐 `m5-0-candidate`；缺失/空/`controller` → 启动失败 |
| M4F_ENABLED | 默认 `0` | **必须 = `1`** |
| M4F_LIVE_MODE | 默认 `0` | **必须 = `1`** |
| M4F_ONLY_MODE | `0` | `1` |
| M4F_ALLOWED_ROOMS | 默认空（不处理 M4F_RUN from /sync） | **必须非空** |
| M4F_ALLOWED_SENDERS | 默认空（拒绝所有 sender） | **必须非空** |
| M4F_RUN_PREFIX | 默认空（不校验前缀） | **必须非空**（推荐 `m5live-`） |
| RESERVED_RUN_PREFIXES | 默认空（旧部署兼容）；M5-0 cutover 前须显式设为 Candidate 的 M4F_RUN_PREFIX | 默认空（Candidate 不使用此项；由 Production 排除） |
| MATRIX_SERVER_NAME | `matrix-local.hiclaw.io:18080` | 同左 |
| GATEWAY_ROLE | 默认空 → gateway_client 回退 `"coordinator"`（生产零改动） | **必须 = `m5coordinator`**（v2.4 勘误，见 §21） |
| GATEWAY_TOKEN | 默认空 → gateway_client 回退 `COORDINATOR_TOKEN`（生产零改动） | **必须显式设置**；为 Candidate 专用独立 token，**不等于** COORDINATOR_TOKEN，**不是** GitHub PAT |

**关键**：M4F_ONLY_MODE=0 时，生产 Controller 保持现有启动行为（ADMIN=admin, consumer_name=controller），**不得因新配置读取而崩溃**。

### 8.2 RESERVED_RUN_PREFIXES cutover 门

1. 普通模式默认空，保证旧部署兼容。
2. 启动 M5-0 Candidate **前**，生产 Controller 必须显式配置 `RESERVED_RUN_PREFIXES=m5live-`。
3. Candidate start script 必须执行 **cutover preflight**：
   - 验证生产 Controller 的 RESERVED_RUN_PREFIXES 包含 Candidate 的 M4F_RUN_PREFIX
   - 只输出 `"cutover: match"` 或 `"cutover: mismatch"`，不输出完整环境变量
   - 不匹配 → Candidate **不启动**
4. Production 和 Candidate prefix 必须完全一致。
5. prefix 为空、重叠、包含 SQL wildcard（`%`/`_`）或非法字符 → 启动失败。
6. 生产 Controller 必须先完成 reserved-prefix 配置和健康重启，Candidate 才可启动。
7. 对应反例已纳入验收表（§20）。

## 9. 五层隔离模型

| 层 | Candidate | Production | 隔离机制 |
|---|---|---|---|
| 1. Matrix user | m5-0-ctrl（MATRIX_USER） | admin（硬编码） | 不同 /sync session |
| 2. consumer_name | m5-0-candidate（CONTROLLER_CONSUMER_NAME） | controller（硬编码） | 独立游标行 |
| 3. room/sender | M4F_ALLOWED_ROOMS / SENDERS | 所有非保留 | allowlist filter |
| 4. run prefix | LIKE 'm5live-%' | NOT LIKE 'm5live-%' | 参数化 SQL WHERE |
| 5. advisory lock | pg_try_advisory_lock（独占） | 不获取 | 同时最多 1 个 candidate |

## 10. Candidate advisory lock

```sql
SELECT pg_try_advisory_lock(hashtextextended('mergepilot:m5-0-candidate', 0))
```

- 类型：**session-level**（进程生命期持有）
- 连接：独立 PG 连接（不复用主连接）
- 获取失败：立即 sys.exit(1)，不 login Matrix，不消费事件
- 连接断开：Candidate 检测到（tick 开头 SELECT 1）→ exit(1)
- 正常退出：pg_advisory_unlock + close
- 异常退出：PG session 断开自动释放
- STARTUP_CHECK_ONLY：获取 → 释放 → exit 0
- Production Controller：**不获取此锁**
- 不产生数据库持久对象（PG 运行时原语）

## 11. Candidate 主循环伪代码（M4F_ONLY_MODE=1）

```python
def run_forever():
    startup_assert()
    # advisory lock（独立连接）
    _lock_conn = psycopg2.connect(PG_DSN)
    if not _lock_conn.cursor().execute(
        "SELECT pg_try_advisory_lock(hashtextextended('mergepilot:m5-0-candidate',0))").fetchone()[0]:
        sys.exit(1)

    ensure_matrix_login()
    while True:
        # 检查 lock 连接健康
        if _lock_conn.closed: sys.exit(1)
        # 域 A：scoped M4-F
        drain_m4f_events_scoped(run_prefix=M4F_RUN_PREFIX)
        reconcile_m5_skill_to_review(run_prefix=M4F_RUN_PREFIX)
        reconcile_m5_handoffs(run_prefix=M4F_RUN_PREFIX)
        # 禁止：initiate_l2_pending, drain_l2_outbox, reconcile_l2,
        #        process_rollback, process_rollback_advance
        # 域 B：scoped Matrix
        consume_m5_live_events(allowed_rooms, allowed_senders, consumer_name)
        drain_outbox_scoped(run_prefix=M4F_RUN_PREFIX)
        time.sleep(POLL_INTERVAL)
```

**明确禁止**：L2、rollback、普通 TASK_SUBMITTED、非 m5live TASK_COMPLETED、非前缀 dispatch。

## 12. Production/Candidate 双向工作队列分区

### dispatch_outbox

| Controller | SQL WHERE | 参数 |
|---|---|---|
| Candidate | `status IN ('PENDING','RETRY') AND next_retry_at<=now() AND run_id LIKE %s` | `'m5live-%'` |
| Production | `status IN ('PENDING','RETRY') AND next_retry_at<=now() AND run_id NOT LIKE %s` | `'m5live-%'` |

### stage_events（M4F_RUN claim）

| Controller | SQL WHERE |
|---|---|
| Candidate | `event_type='M4F_RUN' AND (status='M4F_PENDING' OR ...) AND run_id LIKE %s` |
| Production | M4F_ENABLED=0 → return 0（不 claim） |

**SQL 全部参数化**（`%s` 占位，不拼接前缀字符串）。

### 跨前缀安全性

| 场景 | Candidate | Production | 交叉 |
|---|---|---|---|
| m5live dispatch | LIKE ✅ | NOT LIKE ❌ | 0 |
| 普通 dispatch | LIKE ❌ | NOT LIKE ✅ | 0 |
| m5live M4F_RUN | LIKE ✅ | M4F_ENABLED=0 ❌ | 0 |
| 普通 M4F_RUN | LIKE ❌ | M4F_ENABLED=0 ❌ | 0 |

**prefix 为空或重叠 → 启动 fail-closed。**

## 13. DAG→handoff 原子桥

```
单事务（幂等）：
1. 判断：6 expected skills 全 SUCCEEDED
   SELECT count(*) FROM skill_job_outbox WHERE run_id=%s AND status='SUCCEEDED' → 6
   SELECT count(*) FROM skill_invocations WHERE output_schema_validated=true → 6
2. 任一非 SUCCEEDED → 不创建 review stage（保持等待或超时 HOLD）
3. 全 SUCCEEDED：
   INSERT INTO stage_runs(run_id,stage,agent,attempt,status)
     VALUES(%s,'review','reviewer',1,'PENDING_DISPATCH')
     ON CONFLICT(run_id,stage,attempt) DO NOTHING
   INSERT INTO dispatch_outbox(idempotency_key,run_id,...)
     VALUES('m5-'+run_id+'-review-dispatch',%s,...)
     ON CONFLICT(idempotency_key) DO NOTHING
   UPDATE task_runs SET current_stage='m4f_await_review' WHERE run_id=%s AND current_stage='m4f'
```

- 固定 idempotency_key：`m5-<run_id>-review-dispatch` / `-fix-dispatch` / `-verify-dispatch`
- 重复 reconcile：ON CONFLICT DO NOTHING → 不重复派发
- 双重派发计数 = 0

## 14. 唯一调度权威

- **Controller + dispatch_outbox 是 M5-0 唯一调度器**
- handoff_watcher_v2 / handoff_watcher 排除 m5live-* 前缀（`if prefix.startswith("m5live-"): continue`）
- Manager 只发 M4F_RUN（入口事件），不直接 @mention Worker
- 重复 TASK_COMPLETED：stage_events PK 幂等 + dispatch_outbox idempotency_key 幂等 → 不产生第二 dispatch

## 15. Matrix send 幂等

- txn = `"c_" + sha256(f"{room_id}:{user}:{text}").hexdigest()[:16]`（controller.py:219）
- 相同输入 → 相同 txn → Matrix PUT 返回相同 event_id（spec 保证）
- 崩溃恢复：send 成功但 UPDATE DISPATCHED 未提交 → 重启 → 重发 → 相同 txn → 相同 event_id → UPDATE
- **不产生第二个 Matrix event。不需要修改 txn id 生成。**

## 16. 身份和凭据

| 规则 | 实现 |
|---|---|
| 完整 Matrix user_id 校验 | verify_m5_sender(raw_sender, allowed_localparts)：校验 `localpart:server_name` 格式，server_name 必须 = MATRIX_SERVER_NAME；完整 raw_sender 持久化到 stage_events.sender（v2.4 Fix 1） |
| Manager/Worker allowlist | config/m5-0-allowlist.yaml（deploy-owned） |
| Worker 不持 PAT | mcporter → Policy Gateway → mcp-backend-net 隔离 |
| **Candidate 独立最小权限 Gateway 身份**（v2.4） | GATEWAY_ROLE=`m5coordinator` + 独立 GATEWAY_TOKEN；Gateway policy `m5coordinator: {classes: [read]}` 仅授权 `pull_request_read`/`list_branches`/`list_pull_requests`/`get_file_contents`/`get_commit`/`list_commits`；详见 §21 |
| Candidate runner 不读 COORDINATOR_TOKEN | 不接收、不读取生产 COORDINATOR_TOKEN；Gateway 鉴权走独立 GATEWAY_TOKEN |
| Controller 独占 Gateway token | 生产 controller env COORDINATOR_TOKEN；Candidate env GATEWAY_TOKEN（二者不同值、不同角色） |
| GitHub PAT 只在 github-mcp | mcp-backend-net 网络隔离；Candidate 经 m4f_ingress→gateway_client 用 m5coordinator 只读身份访问，绝不持 PAT |

## 17. 真实 GitHub 边界

| 项 | 冻结 |
|---|---|
| 仓库 | nghqqa/MergePilot-e2e-fixture |
| 分支 | fix/m5live-<run_id> |
| 只读 | diff-parse / risk-classify / sast-scan / case-retrieval |
| 创建 PR | pr-lifecycle create_branch + push_files + create_pull_request |
| Merge | **不自动 merge** |
| 清理 | 每次 run 后 close PR + delete branch |
| 10 次策略 | 9 次 EXISTING（同 idempotency_key）+ 1 次 CREATED + 0 merge |
| provenance | mcp_calls.target_repo + git_sha + run_id 一致 |

## 18. 状态映射

**只使用数据库和代码中已存在的状态值。**

### task_runs

| 触发 | status | current_stage | skill_data_state |
|---|---|---|---|
| 初始 | RUNNING | m4f | ACTIVE |
| 六 Skill 全成 | RUNNING | m4f_await_review | ACTIVE |
| Reviewer 完成 | RUNNING | m4f_await_fix | ACTIVE |
| Fixer 完成 | RUNNING | m4f_await_verify | ACTIVE |
| Verifier PASS | **HOLD** | **m5_verify_passed** | ACTIVE |
| Verifier FAIL | **HOLD** | **m5_verify_failed** | ACTIVE |
| 任一 Skill FAIL | **HOLD** | m4f_skill_failed | ACTIVE |

注意：task_runs.status **没有 COMPLETED**。成功终态使用已有 `HOLD` + current_stage 自由 TEXT。

### stage_runs

| stage | 初始 status | 终态 |
|---|---|---|
| review | PENDING_DISPATCH | COMPLETED |
| fix | PENDING_DISPATCH | COMPLETED |
| verify | PENDING_DISPATCH | COMPLETED + verdict=PASS/FAIL |

### skill_job_outbox

PENDING → LEASED → SUCCEEDED（6/6）

### stage_events

M4F_RUN: RECEIVED → M4F_PENDING → M4F_RUNNING → PROCESSED
TASK_COMPLETED review/fix/verify: RECEIVED → PROCESSED

## 19. hiclaw_live=true 的 22 项机器公式

```python
hiclaw_live = all([
    # 因果链（v2.1）
    real_matrix_event,              # event_id 来自真实 Matrix /sync
    manager_event_verified,         # sender=@manager, M4F_RUN 合法
    reviewer_handoff_verified,      # sender=@reviewer, TASK_COMPLETED review
    fixer_handoff_verified,         # sender=@fixer, TASK_COMPLETED fix
    verifier_handoff_verified,      # sender=@verifier, TASK_COMPLETED verify + VERDICT=PASS
    sender_role_allowlist,          # 全部 sender 在 allowlist
    controller_consumed_handoffs,   # stage_events 全部 handoff PROCESSED
    # 基础能力（v2.1）
    real_gateway,                   # mcp_calls AUDIT_DSN 真实
    not fake_github_mcp,            # UPSTREAM_URL 含 github-mcp
    six_skills_succeeded,           # skill_job_outbox 6/6 SUCCEEDED
    provenance_complete,            # revision_bindings + mcp_calls 完整
    negative_cases_passed,          # 3 条反例通过
    consecutive_live_runs >= 10,
    secret_leaks == 0,
    # /sync 与隔离（v2.1/v2.2）
    m4f_run_observed_by_sync,       # consume_events 拾取（非直接 process_event）
    candidate_consumer_isolated,    # consumer_name != 'controller'
    six_skill_to_review_bridge_committed,
    authoritative_dispatch_only,    # handoff_watcher 排除 m5live-*
    no_handoff_watcher_duplicate,   # 双重派发计数 = 0
    manager_output_not_test_injected,
    worker_outputs_not_test_injected,
    full_matrix_sender_verified,    # server_name 校验通过
])
```

任一项 false → **hiclaw_live = false**。该值由 runner 从权威数据源自动计算，不手工写死。

## 20. 验收门和反例

### 正向门（7 组）

| 组 | 门 | 权威源 | PASS 条件 |
|---|---|---|---|
| A | 真实 M4F_RUN /sync + 4 handoff | stage_events + Matrix event_id | 全部 PROCESSED |
| B | 六 Skill + Gateway + Provenance | skill_job_outbox + mcp_calls | 6/6 + audit |
| C | 幂等 + 3 负向 | stage_events + mcp_calls | 重复不增生 + 负向 fail-closed |
| D | 真实 GitHub | mcp_calls + fixture repo | ≤1 CREATED + 0 merge |
| E | 10 次稳定 | live evidence | 10/10 SUCCEEDED |
| F | 安全 + 残留 | Docker / Matrix API | containers=0 + rooms 如实 |
| G | M4-F 回归 | run_all.sh + legacy | 17/17 + 6/6 |

### 关键反例（合并 v2.1-v2.3）

1. 真实 M4F_RUN 经 /sync 处理
2. 非 allowlisted sender 被拒
3. 非 allowlisted room 被拒
4. 同名不同 homeserver sender 被拒
5. 双 Controller 不抢 offset
6. cross_claim=0（dispatch_outbox + stage_events）
7. 两 Candidate 第二个启动失败（advisory lock denied）
8. Candidate lock 连接断开 → exit
9. 六 Skill 未齐不 dispatch
10. 重复 reconcile 只派一次
11. Reviewer 缺失超时 HOLD
12. Gateway DENY fail-closed
13. Matrix txn 重试幂等（不重复 event）
14. watcher duplicate dispatch=0
15. 测试伪造 Agent marker 不计 hiclaw_live
16. 删除任一 Agent 事件 → hiclaw_live=false
17. 10/10 live full-chain
18. M4-F 17/17 离线不回归
19. legacy 6/6 不回归
20. Candidate 不处理普通 M3 事件
21. Production 不处理 m5live 事件
22. advisory lock 不留持久对象

## 21. 实现增量

| 增量 | 范围 | 独立验收 |
|---|---|---|
| **M5-0A** | 身份 allowlist + Candidate Controller + advisory lock + /sync M4F_RUN 路由 + 5 层隔离 + 双向分区 | Candidate /sync 拾取 Manager 真实 M4F_RUN → stage_events 有记录 → drain → 6 jobs enqueued |
| **M5-0B** | DAG→handoff 桥接事务 + reconcile_m5_handoffs + Agent SOUL/prompt + watcher 排除 m5live-* | 六 Skill 完成后 review stage 创建 + Reviewer/Fixer/Verifier TASK_COMPLETED 推进 → PASS/HOLD |
| **M5-0C** | 真实 Gateway/github-mcp 桥 + 10 次 live + 3 负向 + PR/branch 清理 | 10/10 pass + 3/3 fail-closed + PR residue=0 |
| **M5-0D** | live evidence + hiclaw_live 公式 + 离线 17/17 回归 + legacy 6/6 + tag/push | hiclaw_live=true（22 项全满足）+ G1/G2 不回归 |

每个增量独立可验。不得提前声明下一增量完成。

---

## 22. v2.4 极窄勘误：Candidate Gateway 身份冲突

### 22.1 冲突描述（v2.3 遗留阻断项）

`m4f_ingress.stage_agentteams_event` 必须经 Policy Gateway 调用 `gateway_read_pr`/`gateway_get_pr_diff`/`gateway_get_pr_files`（PR 数据权威来源）。但 v2.3 §16 同时规定 **Candidate runner 不读 COORDINATOR_TOKEN**，而 `gateway_client.py` 硬编码用 `COORDINATOR_TOKEN` 鉴权到 `/coordinator/sse`。二者矛盾：Candidate 要么违反 §16（塞回生产 token），要么无法读 PR（DAG 断链）。

### 22.2 代码级审计结论

Gateway（`tools/policy-gateway/gateway.py`，**禁改**）已原生支持任意独立身份，**无需任何代码改动**：

- `ROLE_TOKENS`（env JSON）→ `TOKEN_TO_ROLE` 反向映射
- `Route("/{role}/sse", ...)` 通用路由接受任意 role 字符串
- `handle_sse` 校验 `TOKEN_TO_ROLE[token] == path_role`（token 角色须与路径声明一致）

因此只需 **配置增量**（策略 yaml + env），不需改 gateway.py / m4f_ingress.py / DB 契约。

### 22.3 勘误方案：Candidate 专用 `m5coordinator` 角色

| 项 | 值 |
|---|---|
| Gateway 角色 | `m5coordinator`（新增，与生产 `coordinator` 同 `classes: [read]` 但独立 token + 独立身份） |
| Candidate env | `GATEWAY_ROLE=m5coordinator` + `GATEWAY_TOKEN=<独立随机>`（运行时生成，不写入源码/evidence） |
| Gateway policy | `m5coordinator: {classes: [read]}`——仅 `pull_request_read`/`list_branches`/`list_pull_requests`/`get_file_contents`/`get_commit`/`list_commits`（M4F ingress 所需只读集） |
| Gateway ROLE_TOKENS | `{"coordinator":"<prod>", "m5coordinator":"<candidate>"}`——两 token 不同值、映射不同 role |
| gateway_client.py | `GATEWAY_ROLE` env（默认 `"coordinator"`，向后兼容）+ `GATEWAY_TOKEN` env（默认回退 `COORDINATOR_TOKEN`）；SSE 路径 `f"/{GATEWAY_ROLE}/sse"` |

**关键不变量**：
1. Candidate 不接收、不读取生产 `COORDINATOR_TOKEN`。
2. `GATEWAY_TOKEN` ≠ `COORDINATOR_TOKEN`（不同值）。
3. `GATEWAY_TOKEN` **不是 GitHub PAT**（Candidate 经 Gateway → github-mcp 访问，PAT 只在 github-mcp 网络隔离内）。
4. `m5coordinator` 仅 `read` 类，无 `comment`/`fix`/`l2` 权限。
5. 生产 Controller 不设 `GATEWAY_ROLE`/`GATEWAY_TOKEN` → 回退 `coordinator` + `COORDINATOR_TOKEN`，**生产零改动**。

### 22.4 修改边界（v2.4 允许改动的文件集）

| 文件 | 改动 | 禁止 |
|---|---|---|
| `tools/workflow-controller/gateway_client.py` | 参数化 `GATEWAY_ROLE`/`GATEWAY_TOKEN`（向后兼容默认） | 不改鉴权协议、不改 Gateway 业务语义 |
| `tools/start-m5-0-candidate.sh` | 传 `GATEWAY_ROLE=m5coordinator` + `GATEWAY_TOKEN`；前缀 charset + overlap 预检 | 不传 COORDINATOR_TOKEN |
| `tools/policy-gateway/gateway.py` | **不改** | — |
| `tools/workflow-controller/m4f_ingress.py` | **不改** | — |
| DB 契约（m4f1_state.sql 等） | **不改** | 无新表/列/函数/索引/CHECK |

### 22.5 v2.4 验收门（追加到 §20 正向门 H 组）

| 门 | 权威源 | PASS 条件 |
|---|---|---|
| H1 | gateway_client.py | `GATEWAY_ROLE` 默认 `"coordinator"`（生产零改动）；Candidate 设 `m5coordinator` 时 SSE 路径 = `/{GATEWAY_ROLE}/sse` |
| H2 | Candidate 容器 env | 无 `COORDINATOR_TOKEN`；有独立 `GATEWAY_TOKEN` 且 ≠ 生产 coordinator token |
| H3 | Gateway policy fixture | `m5coordinator: {classes: [read]}` 存在，无 `fix`/`comment`/`l2` |
| H4 | Gateway audit（mcp_calls） | Candidate PR 读取审计记录 role=`m5coordinator`（非 `coordinator`） |
| H5 | line-level hygiene | start-m5-0-candidate.sh 实际被扫描；`GATEWAY_TOKEN="$GATEWAY_TOKEN"` 行被行级豁免（非字面量），真 secret 仍被捕获 |

## 23. v2.5 极窄勘误：Candidate provenance 角色校验

### 23.1 实测冲突

v2.4 对 Gateway 通用路由和 token-role 映射的审计是正确的，但遗漏了
`gateway.py` 的 M4-F provenance 专用校验：只要请求携带
`mergepilot_run_id`，代码就额外要求 `role == "coordinator"`。因此
`m5coordinator` 即使仅有 `read` policy，也会在
`pull_request_read(method=get)` 上被 `M4F_PROVENANCE_CONTEXT_DENIED` 拒绝，
六 Skill DAG 无法创建。

### 23.2 极窄修正

仅将 provenance 允许角色从 `coordinator` 扩为
`{coordinator, m5coordinator}`。其余限制保持不变：

1. 带 `mergepilot_run_id` 的调用仍只能是 `pull_request_read`。
2. `method` 仍必须精确等于 `get`；`get_diff`、`get_files` 等继续拒绝。
3. role policy 仍在后续执行；`m5coordinator` 仍只有 `read` 类，无
   `comment`、`fix` 或 `l2`。
4. `mergepilot_run_id` 格式校验不变。
5. 生产 `coordinator` 行为和凭据不变。

### 23.3 修改与验收边界

- 允许极窄修改 `tools/policy-gateway/gateway.py` 的 provenance role 条件。
- 正向门：真实 Gateway 记录 `caller_agent=m5coordinator`，事件进入
  `PROCESSED`，精确创建六个预期 Skill job。
- 反例门：`m5coordinator` 携带 `mergepilot_run_id` 调用
  `pull_request_read(method=get_diff)` 必须返回
  `M4F_PROVENANCE_CONTEXT_DENIED`。

## 24. 修改边界

### 预计修改文件

| 文件 | 增量 | 变更 |
|---|---|---|
| controller.py:852 | A | body 过滤新增 M4F_RUN（条件 M4F_LIVE_MODE=1） |
| controller.py:322 | A | sender ADMIN → allowlist |
| controller.py:832,858 | A | consumer_name env |
| controller.py:200 | A | MATRIX_USER env |
| controller.py:850 | A | verify_sender + server_name |
| controller.py:843 | A | room filter |
| controller.py:781-784 | A | dispatch_outbox run_id LIKE |
| controller.py:691-710 | A | M4F claim run_id LIKE |
| controller.py:2010-2058 | A | M4F_ONLY_MODE 主循环分支 + advisory lock |
| controller.py:675 区间 | B | 六 Skill 完成检测 + review stage + dispatch 桥 |
| controller.py:458 区间 | B | m5live TASK_COMPLETED 阶段推进 |
| handoff_watcher_v2.py:54-60 | B | 排除 m5live- 前缀 |
| handoff_watcher.py:64-71 | B | 排除 m5live- 前缀 |
| start-controller-container.sh | A | 6+ env 变量 |
| config/m5-0-allowlist.yaml（新建） | A | user_id allowlist |
| tests/m5_0/（新建） | A-D | live runner + fixtures + evidence |
| check_hygiene.py | D | TARGETS 加 tests/m5_0/ + evidence/m4/m4f-live/ |
| Manager SOUL | A | M4F_RUN 生成指令 |
| Reviewer/Fixer/Verifier SOUL | B | TASK_COMPLETED/VERDICT 格式确认 |

### 禁止修改

m4f1_state.sql / m4f1_hotfix_1.sql / m4f_skill_worker.py / m4f_controller.py / m4f_ingress.py / skills/** / run_all.sh / 已发布 evidence / 已发布 tag / controller.py M3 L2/drain/rollback 核心语义 / DB schema（表/列/函数/索引/CHECK）。`gateway.py` 仅允许 §23 所述 provenance role 条件极窄修正，其他业务语义禁止修改。

### 数据库契约

**不变。** 无新表、新列、新函数。所有隔离通过参数化 WHERE + 代码分支 + PG advisory lock 实现。
