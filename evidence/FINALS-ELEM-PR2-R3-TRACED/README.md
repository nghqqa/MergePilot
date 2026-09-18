# FINALS-ELEM-PR2-R3-TRACED — PR #2 全链闭环（R3, AgentLoop 全程追踪版）

> 结论:**REAL_EXECUTED_AGENTTEAMS_COPAW + REAL_TRACED_AGENTLOOP — 干净链路 + 全程 OTel span 直连导出达成**。
> kickoff → Reviewer 独立审查（非预设 SPEC）→ 人工安全门（操作员现场批准）→ **Leader 10 秒后发出全新 fix 委派事件** →
> Fixer 最小修复 → Verifier 独立验证 **VERIFIED（首次通过）** → 项目 completed。PR #2 保持 OPEN，零 GitHub 写入。
> run_id:`run-elem-pr2r3t-20260916-01` · project:`elemiso-pr2r3t-gate` · head `1dedf5e1992c950557064d8f4fb9039d1523deb3`
> 墙钟 **20 分 29 秒**（15:35:31Z kickoff → 15:56:00Z 终报）· 全部 4 容器一次创建后零重启（15:27:41Z 建，16:21:02Z 停）

## 一、本轮与 R2 的差异（为什么要 R3-TRACED）

| | R2（FINAL-ELEM-PR2-LIVE-…-R2） | R3（本包） |
|---|---|---|
| OTel 埋点 | 无 | **每容器 7 类 patch 全程生效**：tool.<name>（Toolkit 稠点）、matrix.receive/send、agent.session.run、genai.llm.call/request、taskflow.notify_assignment |
| 导出 | 无 | **直连 SLS endpoint（无中继），239 批次全部 SUCCESS、0 失败** |
| 镜像 | copaw-worker:223ddc2-build1 | **223ddc2-agentloop**（v2.2 延迟 patching 设计，image ID `8a6b8995ccc2`，同 ID 双 tag `-agentloop`/`-agentloop-v22`） |
| 门后派发 | Leader 新委派事件（R2 关键验收点） | 同样达成：批准 15:53:52Z → **fix 委派事件 15:54:02Z** `$tVLWTi47TF-rbghrGcjjPryWn2SNRa9UG75SCNklCTk`（10 秒，操作员零介入） |

## 二、真实事件链（event_id，可由房间导出复核）

| 阶段 | event_id / 时间(Z) |
|---|---|
| kickoff（admin→Leader DM，含 SPEC REVIEW 全文） | `$wqTKbyLTVEWmTKRJaRleCE-sviPN58eqew8j9AQlx0Y`（15:35:31） |
| **Leader 委派 pr2r3t-review-1**（kickoff 后 5 秒） | `$n_sZQTvviLOvpAW0YY6GGoulRcD6v2uDgvcFuc5ChS8`（15:35:36） |
| Reviewer 提交（FINDING_CONFIRMED / HIGH / CWE-22 / HVR:YES） | `$IQNqM4dP6ZzUy…`（15:48:03，meta submitted_at 15:48:00） |
| Leader 停门报告（"尚未委派 pr2r3t-fix-1"） | `$OAzmJ8rnQjNeD…`（15:48:12） |
| **人工门批准（操作员现场决策）** | 记录落盘 + DM `$xgWT-EXMDWXJGmC2TdWyRdbsfgAJICO060-tk7b6dNU` / 团队房 `$LUCV4ilKfewaOWLwtlmfCIj1MoSTWhabkmLLT9sETrk`（15:53:52） |
| **Leader 全新 fix 委派**（批准后 10 秒，R2 验收口径） | `$tVLWTi47TF-rbghrGcjjPryWn2SNRa9UG75SCNklCTk`（15:54:02） |
| Fixer 提交（+13/−7 最小修复） | `$KZ7Cf19PTU2ut…`（15:54:34） |
| Leader 委派 pr2r3t-verify-1 | `$0KgmZFQN84t8EdtBIcAmJGa04uCtK_C5bMsHvtQAkoI`（15:54:42） |
| Verifier 提交 **VERIFIED（PASS，首次通过）** | `$P6EWeBeFqNay5…`（15:55:47） |
| Leader 终报（项目 completed） | DM `$e6eRj7pKgmuRo…`（15:56:00） |

## 三、结果与核验

- Reviewer：FINDING_CONFIRMED / HIGH / CWE-22（自主定性；真实 PoC：`name=../outside-secret.txt`→200 泄露、`../../../etc/hostname`→200 任意文件读）；**额外独立发现**：PR 自带测试#2 失败是 fixture/payload 错位（`../../../` 越过 tmp 到 `/tmp`），非缓解；正确 payload `../../deep-secret.txt`→200 `DEEP-SECRET`。工件：`tasks/pr2r3t-review-1/{result.md,workspace/findings.md}`。
- Fixer：`tasks/pr2r3t-fix-1/attempt-1.diff`（单文件，realpath+commonpath 包含性校验），**sha256 `674356fc9a661faa…c0116081` 与 R1/R2 独立产出完全一致**（确定性修复互证）；测试冻结；零 GitHub 写入。
- Verifier：独立干净 clone、patch 字节一致 + sha256 复现一致、**修复前 5 组逃逸向量全部 200+泄露（含新增前导 `/` 绝对路径 `name=/etc/hostname`）→ 修复后全部 400**、合法 200/缺失 404、PR 测试断言反转如实记录。工件：`tasks/pr2r3t-verify-1/workspace/*`。
- 项目终态：controller `agt get projects` = **completed**（`controller-project.json`）；plan 三行全 `[x]`（`project/plan.md`）。
- GitHub：`github-branches-after.txt` = git ls-remote 复核，两分支 head SHA 与运行前后一致（PR state API 因匿名限流不可用，以分支 SHA 未变 + 全程 worker 无 GitHub 凭据为零写入佐证）。

## 四、AgentLoop 追踪证据（本轮核心增量）

- **span 产出**（`agentloop/span-summary.json`，PR2 窗口快照 @15:57Z）：leader 187 / reviewer 143 / fixer 94 / verifier 100，计 **524 span**；类型覆盖 `tool.taskflow`(7)/`tool.projectflow`(8)/`taskflow.notify_assignment`(3)/`matrix.receive`(…)/`matrix.send`/`agent.session.run`/`genai.llm.call`+`request`(79+79)/`tool.execute_shell_command` 等。
- **导出**：直连 `…cn-hangzhou.log.aliyuncs.com/apm/trace/opentelemetry/v1/traces`（公网 endpoint，无中继），各容器 `OTEL_EXPORT SUCCESS` 批次合计 239、失败 0（`agentloop/audit-*-snapshot-1557Z.log`）。
- **显式 HTTP 200 探针**（`agentloop/direct-probe-*.json`，各容器内独立 POST）：leader trace_id `2d4105035d1ce8cf…`、reviewer `1fddd4846732d9b6…`、fixer `51ec9810c992dc7a…`、verifier `2c14de8b8ea76604…`（AgentLoop 控制台可检索）。
- 埋点实现：`image/zz_agentloop_otel.py`（v2.2 延迟 patching：.pth 导入零副作用、watcher 轮询开关、90s 宽限、仅 patch 已加载模块、稠点均为运行时按名查找——与注册时捕获引用的 taskflow/message 工具函数及 nio 回调 `_on_room_event` 解耦）。
- **控制台证据**：`agentloop-console-screenshot-shared.png`（操作员现场截图，PR3 窗口 6 条 Trace，逐秒交叉验证见
  `FINALS-ELEM-PR3-R2-TRACED/README.md` 第四节；PR2 窗口 trace 可按 direct-probe trace_id 检索：
  leader `2d4105035d1ce8cf…`、reviewer `1fddd4846732d9b6…`、fixer `51ec9810c992dc7a…`、verifier `2c14de8b8ea76604…`）。

## 五、用量与口径

- PR2 窗口（15:35–15:56:30Z）：**90 次调用 · 输入 6,218,954（98% 缓存命中）· 输出 30,647**（`usage-summary.json`；含 4 条 smoke 探活）。按 R2 同口径估算 **≈¥1.5–3.3**（点估计可能略超 ≤¥2 上界，与 R2 同性质，如实披露；权威数字以 DeepSeek 控制台为准；网关 ai_log 的 `model=deepseek-flash` 为供应商响应侧命名，请求侧为 deepseek-chat）。
- 采集顺序披露：`agentloop/`、`tasks/`、`project/` 工件于 **容器停止（16:21:02Z）之前**实时采集（span/audit 直接 cat 自运行容器；MinIO 工件经 mc 读取）——修正 R1 的"停机后恢复"顺序问题。
- 停止语义披露：`agt update --state Stopped` 由 controller 移除 worker 容器与其 auth 卷（CR 保留 Stopped，`elemiso-ctrl-data` 卷/Matrix/MinIO 数据全部保留）；停止前最后一次模型调用 16:18:04Z，停机后网关零新调用（行数恒定 1450）。

## 六、文件清单

| 路径 | 内容 |
|---|---|
| kickoff.json / kickoff-as-sent.txt | kickoff 事件与逐字原文（SPEC 非预设：未点名任何漏洞类别） |
| gate-approval-sent.json / project/human-gate-approval.md | 门批准记录（派发前落盘） |
| project/{meta,plan,result}.md(json) | 项目存储官方路径产物（agt project create + Leader 双树落盘） |
| tasks/pr2r3t-{review,fix,verify}-1/ | 三角色 spec/meta/result/工件（MinIO 权威副本） |
| team-room-messages.json / leader-dm-messages.json | Matrix 全量导出（645/406 事件，含全部 event_id；与 PR3 包共享） |
| agentloop/* | span/audit 日志、直连探针 JSON、span-summary |
| canary-ab/ | 金丝雀 A/B：无埋点派发 → 有埋点派发 → 工具路径（canary3-audit/spans/房间导出） |
| image/* | v2.2 Dockerfile + 埋点模块 + .pth + 开关文件（副本；权威副本在 release/agentloop-copaw-image/） |
| scripts/* | kickoff/gate/matrix/monitor/enable_otel/probe 运行脚本 |
| higress-gateway-log-final.log / usage-summary.json | 网关全量访问+ai_log（用量权威） |
| controller-project.json / github-branches-after.txt | controller 视图与 GitHub 只读核验 |
| SHA256SUMS | 全包指纹锁定 |
