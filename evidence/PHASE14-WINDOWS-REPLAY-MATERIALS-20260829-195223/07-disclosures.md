# 工程风险与事件披露（如实记录）

> 本文件按"事件 → 影响 → 根因 → 处置 → 残余风险"如实披露演示过程中发生的全部工程问题。
> 相关修复与测试证据见 PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-AUDIT-20260829-165813。

## 事件 1：MinIO shared 任务树异常清空（已恢复）

- **现象**：2026-08-29 11:26–11:41 UTC 之间，远端 MinIO
  `shared/projects/copaw-high-risk-human-gate/tasks/` 整树变为空（fix-1/verify-1/
  review-1 的 meta/result 短暂不可见）。
- **影响**：Leader 侧 `check_task` 短暂失败；三份任务结果在各 worker 本地副本
  **从未丢失**，数据完整性未受损。
- **根因（未完全定位）**：已排除 worker 侧覆盖（`push_local` 明确排除 `shared/` 前缀，
  任务目录仅经显式 `push_shared_path` 写入）；controller 日志无删除痕迹。疑似
  controller 侧 fs-view reconcile 或 MinIO 生命周期策略，列为**待查开放问题**。
- **处置**：以 worker 本地的提交原件（fixer 的 fix-1、verifier 的 verify-1、
  manager 的项目树）回推恢复；恢复后全部终检通过。
- **残余风险**：若删除者为周期性进程，可能复现；演示平台应内置"本地副本 + SHA256"
  的双保险展示（已在 10-demo-platform.md 的完整性面板体现）。

## 事件 2：tool_guard 审批超时导致会话清空

- **现象**：fixer（10:40:08Z）与 verifier（11:15:08Z）的 agent 会话被框架
  `tool_guard` 以"审批超时→拒绝"处理，并触发 `_cleanup_denied_session_memory`
  **清空会话记忆**。表现为：fixer 已完成全部工作却未正式提交；verifier 首轮空转。
- **根因**：copaw 框架的交互式审批设计（受保护工具需人工批准），与本次"无人值守
  自治运行"模式冲突；每轮失败后 consumer 空闲回收（600s），需新消息才能唤醒重试。
- **处置**：依据人工门授权（Fixer/Verifier 被明确许可执行修复与测试），将这两个
  worker 的 `security.tool_guard.enabled=false` 后重启；manager/reviewer 未改动。
  随后各一次 leader 催办即完成闭环。
- **残余风险**：关闭 guard 仅限这两个自治 worker 的演示场景；在有人值守模式应
  重新开启，并将"审批人=值班操作员"接入 Element 流程。

## 事件 3：状态枚举兼容问题

- **现象**：verifier 的 `result.md` 使用 `STATUS: VERIFICATION_PASSED`，而 store 的
  `validate_task_result` 白名单（RESULT_STATUSES）仅接受 SUCCESS 等字面值，
  `check_task` 对 verify-1 返回 `invalid result status`。
- **影响**：不影响验证证据本身（报告/探针原始输出完整）；仅协议字面值未入白名单。
- **处置**：如实记录；verify-1 结论以捕获的 result.md + verification-report.md 为准。
- **待办**：将 `VERIFICATION_PASSED|FAILED` 纳入白名单，或在协议中统一映射为
  `SUCCESS` + 附加字段。

## 事件 4：首次高危委派静默丢失（审计→修复，工程亮点）

- **现象**：06:25:18 Leader 对高风险项目委派 review-1 未产生任何 Matrix 事件，
  meta 记录了陈旧 event_id（08:09 操作员手工重投也因无 mention 不可达）。
- **根因**：双缺陷叠加——① taskflow 平铺任务命名空间使复用分支采信其他项目的
  陈旧 assigned meta 且不发送；② matrix 通道 since-token 高水位与消费解耦 +
  DM 判定缺陷，使"重启重放"结构性不可达。
- **处置**：只读审计定位（PHASE14-…-MATRIX-SYNC-AUDIT-…）→ project-scoped
  任务存储 + 复用分支三重护栏 + since-token 回放窗口/去重账本 + DM 判定加固 →
  12 项单元测试（基线全失败→修复后全通过、全仓零回归）→ build2 重放闭环成功。
- **残余风险**：`shared/` 整树拉取在大规模部署下的性能需优化；框架级
  `copaw/app/_app.py` 写环境变量、Element 容器僵尸 device 等上游问题已记录待修。

## 其他已知限制

- plan.md 存在历史重复区块（runtime 曾复写），不影响工具解析，建议清理。
- entrypoint 每次启动 Matrix re-login 产生新 device（僵尸 device 累积）。
- `Re-bridge failed: 'FileSync' object has no attribute 'get_soul'`（非阻塞缺陷）。
