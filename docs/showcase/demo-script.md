# MergePilot 演示脚本（5 分钟）

> 配套材料：[README](../../README.md) · [架构图](architecture.svg) · [8 页面桌面截图](presentation/)（desktop-01…08 @2x）· [移动截图](presentation/)（mobile-01…04 @2x）
> 演示环境：隔离六容器栈（postgres / policy-gateway / controller / demo-console / console-edge / preflight），控制台地址 `http://127.0.0.1:8600`（仅 loopback）。
> **开场先读**：所有案例数据来自 **deterministic showcase seed —— 非外部客户数据、非生产证据**。

---

## 0:00–0:20 · 一句话定位与架构

**讲**："MergePilot 是一个可审计、可验证、fail-closed 的合并治理工作流：高风险变更没有 L2 人工审批不放行，revision drift 会被阻断并回滚，全程 INSERT-only 审计。"

指架构图（`docs/showcase/architecture.svg`）三个要点：

1. **内部后端网络**承载全部秘密（DSN 只进 postgres/demo-console），对外零发布；
2. **console-edge** 是唯一被发布的组件——无密钥 loopback 发布桥（强调：发布管线，不是第五个应用服务）；
3. deterministic seed（图中虚线）仅向审计库注入合成演示数据，**showcase-only**。

## 0:20–1:20 · Case A：Protected Merge Success

控制台切到 `run-showcase-a`（PR #101）。

1. **overview**（截图 desktop-01）：指 case 徽章 "Deterministic showcase seed"、run_id、PR #101、head SHA、最终状态 **MERGED**；
2. **timeline**（desktop-02）：review → fix → verify（**PASS**）→ merge（**MERGED**），按真实 `started_at` 排序；
3. **trace**（desktop-05）：网关决策链——INTENT `ALLOW`（`POLICY_PASS_L2_APPROVED`）→ RESULT `ALLOW`（`L2_TICKET_APPROVED`，ticket `tkt-showcase-a-l2`，携带 merge SHA）；
4. **evidence**（指 L2 表结构，详见 Case C 演示）：L2 审批票据与 audit_events 五步闭环（review/fix/verify/merge/close_pr）。

**讲**："受保护合并的完整放行链：策略允许 + 人工票据 + 验证通过 + 合并留痕。"

## 1:20–2:10 · Case B：Fail-Closed Policy Rejection

切到 `run-showcase-b`（PR #102）。

1. **overview**：最终状态 **FAIL** + Failure Reason 面板：`POLICY_DENY: write to protected path prefix (samples/); run blocked before merge (fail-closed)`；
2. **timeline**（mobile-02 同款画面）：review COMPLETED → fix **FAILED/DENIED**——**时间线在拒绝点终止**，没有任何 verify/merge 成功阶段；
3. **findings**（desktop-03）：Policy Rejection Facts 表——`create_or_update_file` · `DENY` · `PROTECTED_PATH_PREFIX` · "fail-closed: target path under protected prefix samples/"；
4. **evidence**：audit 摘要含 `policy_deny×1`，且**没有** L2 审批记录（拒绝流程不伪造批准）、没有 merge SHA。

**讲**："写受保护路径被网关直接拒绝，运行失败但审计证据完整保留——这就是 fail-closed。"

## 2:10–3:10 · Case C：Revision Drift Recovery

切到 `run-showcase-c`（PR #103）。

1. **overview**：Failure Reason `REVISION_DRIFT: observed head SHA differs from approved head SHA after merge`，最终 **ROLLED_BACK**；
2. **timeline**：review → fix → verify PASS → merge MERGED → **drift-check FAILED（REVISION_DRIFT）** → **rollback RECOVERED**；
3. **findings**：Drift & Rollback Facts 表——Reverted SHA（被回滚的 merge 提交）与 **Recovered SHA**（恢复后的最终提交）并列展示，re-verify PASS；
4. **safety**（desktop-06）：`rb-showcase-c-1 · RECOVERED · reverify PASS · reverted …c-merge… · recovered …c-recovered…`；
5. **evidence**（desktop-07）：L2 票据 `tkt-showcase-c-l2` + audit `drift_detected×1` + `rollback×1`。

**讲**："合并后 head 被偷换 → 与批准 SHA 不一致 → 阻断 → revision-cut 回滚 → 恢复一致状态。四个 SHA（approved/merge/drifted/recovered）互不相同，全程可对账。"

## 3:10–4:00 · 8 页面导航、live refresh 与 mobile

1. 依次走完 8 个导航项（01–08）：overview / timeline / findings / rag / trace / safety / evidence / benchmark；
2. **rag**（desktop-04）：诚实展示 `not_measured`——advisory-only、not adopted、untrusted，不伪造 RAG 结论；
3. **benchmark**（desktop-08）：诚实展示 `NOT_MEASURABLE_WITH_CURRENT_RUNTIME`，不虚构性能；
4. 指页面顶部横幅：状态 **OK**、poll 计数持续递增——live refresh 单定时器轮询两个只读端点；点 **Refresh now** 演示手动刷新（不产生第二个定时器）；
5. 切到移动视口 390×844（或展示 mobile-01…04）：overview/timeline/safety/evidence 单列布局，长 SHA 与失败原因文本换行显示、不撑宽页面。

## 4:00–5:00 · 测试数字、真实性边界与未完成范围

**讲**（收尾，对应 README 第八节）：

- 测试：PR-V2 案例套件 60 passed；完整回归 **1276 passed / 13 skipped / 0 failed**（ResourceWarning-as-error）；
- F1：audit seed 重放幂等——真实 PostgreSQL 实证 **12 → 12**；
- F2：recovered SHA 在 API / Desktop / Mobile 三侧可见；
- 组件级证据：5 服务 healthy + **PREFLIGHT_OK（10/10 门）**，三案例经 console-edge HTTP 200；
- **真实性边界（逐条读出）**：`application_integration_verified=false` · `database_verified=false` · `production_verified=false` · `revision_producer_contract=NOT_VERIFIED` · `audit_producer_contract=NOT_VERIFIED` · M8-A2-a 已通过隔离六容器 fixture 验证（非生产、非 producer contract）· M8-A1 不等于 revision producer integration；
- **未完成的生产化范围**：完整外部 producer integration、生产部署、真实外部客户验证均未完成；本演示不包含以上内容。

---

## 讲者备注

- 切换案例 = 以 `run-showcase-a/b/c` 重建 demo-console 容器（改 `MERGEPILOT_RUN_ID`），刷新页面即新案例，旧案例内容零残留；
- 若被问无障碍：键盘导航 / reduced-motion / 浏览器控制台监控为已披露的 residual validation（演示环境工具限制），**不声称 WCAG 合规**；
- 若被问数据真实性：所有页面事实（run_id/PR/SHA/阶段/决策/状态）均来自 live snapshot API；前端仅含 case 标签，无硬编码案例数据；
- 不得向观众声明：生产已部署、有真实客户、production ready、M8 完成、application/database/production verified=true。
