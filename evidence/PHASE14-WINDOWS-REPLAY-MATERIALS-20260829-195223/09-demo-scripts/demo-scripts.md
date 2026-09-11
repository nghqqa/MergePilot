# 评委可读演示脚本（3 / 8 / 15 分钟）

> 约定：`[LIVE]` 为现场实操，`(备用)` 为失败时的录制/截图回退。
> 素材：Element Web（http://127.0.0.1:18088，admin 登录）、四个 worker 控制台
> （manager :18682 / reviewer :14678 / fixer :18933 / verifier :11743）、
> 任务存储 `shared/projects/copaw-high-risk-human-gate/tasks/`。

---

## 3 分钟版（核心叙事）

| 时间 | 内容 |
|---|---|
| 0:00–0:30 | **问题**：多智能体改代码，谁来为"高危漏洞"负责？演示双案例：同一套 Agent 团队，PR #1 走自主闭环，PR #2 触发人工安全门 |
| 0:30–1:10 | **PR #1 自主闭环**（30 秒讲 + 团队房间滚动截图）：review→fix→verify 三棒 @mention 委派、全链 TASK_COMPLETED `[LIVE]` Element 房间回放 `(备用: 05-timeline.md)` |
| 1:10–2:20 | **PR #2 高危门**：Reviewer 独立确认 CWE-22（读 review 结论：FINDING_CONFIRMED/HIGH/HUMAN_VERIFICATION_REQUIRED: YES）→ ⛔ 系统停等 → 操作员批准（展示批准记录）→ Fixer 最小修复（前后探针：200 泄密→404 拒绝）→ Verifier 独立重 clone 验证（VERIFICATION_PASSED/NONE）`[LIVE]` reviewer/fixer 控制台 + fix.patch |
| 2:20–3:00 | **边界与可信**：PR #2 保持 OPEN（不自动 merge）；如实披露三项工程事件与未实现项（PolarDB RAG/DB Branch/AgentLoop-OTel 未接入）；全部材料有 SHA256SUMS |

## 8 分钟版（+ 工程深度）

在 3 分钟版基础上扩展：

| 时间 | 增补内容 |
|---|---|
| 3:00–4:00 | **架构**：官方组件栈对照（controller/Tuwunel/MinIO/Higress/Element），四容器由 controller reconcile 管理（非手工编排）——回应"是否真基于 AgentTeams" |
| 4:00–5:00 | **协议**：委派幂等（txn_id + event_id）、mentions 双层校验、project-scoped 任务存储、result.md 顶层协议标记（展示 `check_task` JSON：status/resultStatus/effective）`[LIVE]` |
| 5:00–6:30 | **审计→修复故事**（工程自证亮点）：首次高危委派静默丢失的存储级取证（stale event_id vs 房间历史）→ 双根因 → 12 项单测基线全败→修复后全过、全仓零回归 → build2 重放闭环成功 |
| 6:30–7:30 | **角色矩阵与状态机**（04-dag.md 讲解）：pending→assigned→in_progress→submitted→completed；人工门的"权限拓扑"本质 |
| 7:30–8:00 | 收束：诚实披露清单（07）+ 未实现边界（08） |

## 15 分钟版（+ 平台设计与答辩弹药）

在 8 分钟版基础上扩展：

| 时间 | 增补内容 |
|---|---|
| 8:00–10:00 | **现场全链重放**：Element 登录 → 团队房间按时间线回放两案例 → 逐条对照事件 ID（$IDofCrGE→review、$JU09kIgw→fix、$t0dXkuWw→verify）；打开 verifier 控制台看独立验证轨迹 `[LIVE]` |
| 10:00–11:30 | **探针实证**：before/after 探针原始输出（200 泄 TOP-SECRET-OUTSIDE-BASE → 404）；仓库自带漏洞测试"转红"的正确性解释（漏洞闭合的预期结果） |
| 11:30–13:00 | **演示平台设计**（10-demo-platform.md）：页面区块、状态字段 JSON Schema、与 check_task/Matrix 事件的字段映射、完整性面板（SHA256SUMS） |
| 13:00–14:00 | **失败工程学**：MinIO 清空事件、tool_guard 会话清理、状态枚举兼容——每个事件的取证、处置与残余风险（07） |
| 14:00–15:00 | **边界与展望**：明确未实现（PolarDB RAG/DB Branch/AgentLoop-OTel）；下一步：平台化状态页、审批流接入 Element、OTel |

## 演示守则

1. 任何时刻被问"这是不是真跑了"：出示事件 ID + 房间历史 + 任务存储三处对照。
2. 被问"能不能自动 merge"：明确回答不能也不应——人工门授权范围止于"修复+验证"。
3. 被问"RAG/数据库分支/OTel"：如实回答未实现，并指向 08-scope-boundary.md。
