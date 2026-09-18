# Phase 14.2H-WD-COPAW-HIGH-RISK-FIX-AUDIT — 执行报告

- 时间窗: 2026-08-29 16:58 – 18:20 本地（08:58 – 10:20 UTC）
- 前置: PHASE14-WINDOWS-COPAW-MATRIX-SYNC-AUDIT-20260829-164840（根因 A/B 定位）
- 设计文档: 同目录 DESIGN.md（存储路径设计审计 + Fix A/B 设计 + 测试矩阵）

## 裁决

**COPAW_HIGH_RISK_FIX_VERIFIED** — 修复实现、八项必测通过、实弹闭环成功；
全部阶段成功条件满足。人工安全门**未进入**，Fixer/Verifier **未派发**。

## 一、修复内容（源码：D:\goai\p12\agentteams-v123\copaw）

### Fix A — task 存储 project 命名空间隔离（task.py / taskflow.py / projectflow.py）
1. 新规范布局 `shared/projects/<pid>/tasks/<tid>/{meta.json,spec.md,result.md}`；
   `FileSystemTaskStore._task_dir(task_id, project_id)` + 公共 `task_shared_path()`。
2. 读取解析 `_resolve_task_meta`：显式 project → 新路径（平铺回退强校验 project 归属）；
   无 project → 全项目扫描 + 平铺候选；0/多候选分别报 not-found / ambiguous（多项目同 task_id 不再静默串号）。
3. 写入一律经 `meta.project_id` 路由到 project 布局；平铺目录只读不再写入（sandbox 旧数据零改动）。
4. `prepare_task` / `commit_task_assignment` 内部读全部作用域化。
5. `delegate_task` 复用分支双重护栏：project 作用域读 + **平铺命中不可信**（project 布局缺失即视为陈旧残留）+ `event_id` 非空才允许 reused；prepare 的 existing-assigned 捷径同样不信任平铺命中。
6. ack/submit/check：接受可选 projectId；`_sync_pull_task_paths` 容错拉取（无 project 时先拉整棵 `shared/projects/` 树，防陈旧平铺遮蔽新委派）；"先鉴权后 filesync" 语义保留；`validate_task_result` 放行两种前缀。

### Fix B — Matrix 委派 mention 与历史恢复语义（matrix_channel.py）
1. `matrix_seen_events` 追加式去重账本（cap 10000）：`_enqueue` 成功后记录 event_id，入口查重 → 跨重启 exactly-once。
2. `_load_sync_token` 数值 token 回退 `AGENTTEAMS_MATRIX_REPLAY_EVENTS`（默认 200，0 关闭）个流位置 → 重启后窗口内历史由 homeserver 重投 + 账本去重；解决"token 推进与消费解耦 → 重启永久跳过历史"。
3. `_resolve_is_dm`：`room.users` 为空（恢复/全量同步期）时经 `joined_members` 实查成员数，修复 DM 误判导致的静默丢弃（文本 + 媒体两条回调路径统一）。

### 次要修复
- `test_taskflow_tool.py` 10 处断言同步新语义（路径/拉取次数/错误消息），零回归。

## 二、必测结果（tests/test_fix_audit.py，12 测试）

| # | 测试 | 结果 |
|---|---|---|
| T1 | project_id/task_id 隔离（双项目同名 review-1 物理隔离 + ambiguous 语义 + 跨项目不可读） | PASS |
| T1b | 平铺遗留仅无 project 时可见；显式他项目不可见 | PASS |
| T2 | 复用分支行为（异项目/陈旧不复用；同项目+event_id 复用；event_id 缺失必重发） | PASS |
| T2c | 审计bug原样场景：同项目平铺陈旧 assigned meta 不被信任 → 必发新事件 | PASS |
| T3/T3b | 不同 project 同 task_id 回归（各自新 event + ack/submit 走 project 路径） | PASS |
| T4/T4b | enqueue↔token：enqueue 失败不记账本（可重放）、成功才记账本；重启后重放恰好一次 | PASS |
| T5/T5b | mention MXID 过滤：m.mentions/链接/正文三层命中；群无 mention 落 history；DM 白名单无 mention 投递 | PASS |
| T6 | since token 回放：25396→25196（窗口200）；0 关闭；非数值不变；下限钳 0 | PASS |
| T7 | 重复/幂等：同 event 二次回调不重复 enqueue；账本跨"重启"生效 | PASS |
| T8 | DM 判定：users 空→joined_members=2 判 DM；=3 群聊；查询异常安全回退 | PASS |

全仓回归：与基线（原始代码同环境）逐项对比 **零新增失败**；
基线上失败的 11 个必测在补丁后全部通过。既有失败 89 个为基线环境性失败，与本修复无关。

测试环境：build1 派生的一次性 scratch 容器（新隔离 sandbox，约束 3）；
对役容器零影响的纯副本注入。

## 三、build2 镜像与灰度

- 镜像 `agentteams/copaw-worker:223ddc2-build2`（sha256:3309af97…，由 build1 派生，
  双 venv standard+lite 同步补丁；容器内 4 文件 sha256 与仓库源码一致）。
- 升级方式：补丁文件 cp 进 manager+reviewer 容器可写层 + docker restart（不经手任何 Secret）。
  **fixer/verifier 容器全程未动、未收到任何委派消息（约束 1/6）。**

## 四、实弹闭环（高风险 runtime，全只读验证）

| 时刻(UTC) | 事件 | 证据 |
|---|---|---|
| 09:49:35 | 修复版 delegate_task 直接执行（无 kickoff、无手发消息）：新路径落位、`m.mentions` 命中、commit 记录新 event | delegate 输出 + 房间事件 `$IDofCrGE0os3RryOyFkF6R9qdX4cvU7bXL9nj1aiJac` |
| 09:49:35 | reviewer `Created queue`→`Consumer started`→`_consume_with_tracker`→agent 启动（实时消费） | reviewer docker logs |
| 09:49:37 | reviewer ack（acknowledged_at），新路径 meta 写入 in_progress | reviewer 侧 meta.json |
| 09:49–09:59 | reviewer 抓取 PR#2 diff、分析、成文；期间 manager 领导环自动催办一次（"assigned but not running, please continue"），reviewer 续跑 | 房间事件 + logs |
| ~10:19 | reviewer 提交：status=submitted，`TASK_COMPLETED: review-1` @manager 进团队房间 | 房间事件 `$0CmSNq-…` |
| 10:2x | manager 侧 check_task：ok=true, status=submitted, resultStatus=SUCCESS, effective=true | check_task_mgr.py 输出 |

### 成功条件逐条对照
1. **不同 project 的 review-1 不再冲突** ✅ 高风险委派写入
   `shared/projects/copaw-high-risk-human-gate/tasks/review-1/`；平铺遗留
   （含审计时的污染数据，status=assigned/event=$nadwji）原样保留未污染新流程。
2. **delegate_task 必须产生新的有效 event** ✅ `$IDofCrGE…`（≠ 陈旧 `$nadwji…`），
   `reused` 未触发，homeserver 真实投递。
3. **event 包含目标 Reviewer MXID mention** ✅ `m.mentions.user_ids=[reviewer]` +
   正文首词 reviewer MXID（只读 GET 核实）。
4. **Reviewer 能消费 review-1** ✅ 实时消费（Created queue 09:49:35）+ ack + 完成提交。
5. **Reviewer 输出高危结果后才进入人工门/派发 Fixer** ✅
   result.md: `STATUS: SUCCESS`，SUMMARY 含 `FINDING_CONFIRMED, SEVERITY: HIGH,
   HUMAN_VERIFICATION_REQUIRED: YES`（task spec 第 13-14 行规定的输出协议；
   语义即 HIGH_RISK_FOUND 高危确认）；完整报告 `workspace/review-report.md`
   （`STATUS: FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES`，
   CWE-22 路径穿越确认）。
   **本阶段止步于此：未进入人工安全门，未派发 Fixer/Verifier。**

## 五、约束遵守清单
1. OpenClaw 主线（agentteams/worker-agent:223ddc2 四容器）零改动 ✅
2. copaw-sandbox 项目目录零改动；平铺遗留目录只读保留（作审计对照） ✅
3. 验证仅在高风险 runtime（manager+reviewer）与新隔离 scratch 容器 ✅
4. PR#2 业务代码零改动 ✅
5. 未发送新 kickoff（delegate 由修复版 taskflow 直接执行） ✅
6. Fixer/Verifier 未派发（无消息、无容器变更） ✅
7. 全程未输出/落盘 Secret（容器升级走可写层 cp，不经 env） ✅
8. 旧证据目录只读未覆盖；本阶段全部写入新目录 ✅

## 六、遗留事项（不阻塞，建议后续）
- `copaw/app/_app.py` 实例化时向 os.environ 写 `QWENPAW_WORKING_DIR`，
  会污染同进程后续测试（本套件已通过双变量设值免疫）；框架级修复属上游。
- entrypoint 每次启动 Matrix re-login 产生新 device（僵尸 device 累积）。
- `Re-bridge failed: 'FileSync' object has no attribute 'get_soul'` 未修（与本次根因无关）。
- Fixer/Verifier 容器仍在 build1 代码：进入人工门后的派发前，需以同样方式
  升级到 build2（其 ack/submit 依赖 Fix A 的新路径解析）。

## 七、证据清单
- DESIGN.md（存储路径设计审计 + 修复设计）、本 AUDIT.md、VERDICT.txt、SHA256SUMS
- 修复源码快照：task.py / taskflow.py / projectflow.py / matrix_channel.py
- 测试：test_fix_audit.py（12 用例）
- 实弹脚本：delegate_highrisk.py / check_task_mgr.py / probe_verify.py（只读）
- Dockerfile.build2（build1→build2 派生定义）
