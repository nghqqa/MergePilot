# 案例二：PR #2（demo/high-risk-human-gate）— 高危安全门案例

> 定位：在 PR #1 的协同管道上叠加**高危人工安全门**：Reviewer 确认高危后，
> 系统强制停下等人工批准；批准后 Fixer 最小修复、Verifier 独立验证；
> **PR #2 全程保持 OPEN，未 merge/push/close/reopen**。

## 案例设定

- 仓库：`nghqqa/fastapi-boilerplate-demo`，**PR #2**，分支 `demo/high-risk-human-gate`
- 预置缺陷（演示用）：`backend/src/interfaces/api/v1/demo_high_risk.py` 的 `demo_download`
  将用户可控 `name` 直接 `os.path.join` 到 `DEMO_FILES_DIR`，`../` 可逃逸基目录，
  经 `FileResponse` 任意文件读取（**CWE-22**）
- DAG：`review-1 → ⛔人工安全门 → fix-1 → verify-1`

## 完整闭环（UTC，全部真实事件）

### 1) 独立审查 → HIGH_RISK_FOUND

| 时刻 | 事件 | 证据 |
|---|---|---|
| 09:49:35 | Leader（修复版 taskflow）委派 review-1：event `$IDofCrGE0os3RryOyFkF6R9qdX4cvU7bXL9nj1aiJac`，`m.mentions` 命中 reviewer | 房间事件 + manager meta.json |
| 09:49:35 | reviewer 消费（`Created queue`） | verifier/fixer 同款日志链 |
| 09:49:37 | reviewer `ack_task`（acknowledged_at） | 任务 meta |
| ~10:19 | reviewer 提交：review-report 结论 | `TASK_COMPLETED: review-1` @manager（event `$0CmSNq-…`） |

**Review 结论（review-1/result.md + workspace/review-report.md）**：

```text
STATUS: SUCCESS
SUMMARY: Confirmed HIGH path traversal / arbitrary file read (CWE-22) ...
         STATUS: FINDING_CONFIRMED, SEVERITY: HIGH, HUMAN_VERIFICATION_REQUIRED: YES
```

### 2) ⛔ 人工安全门（系统强制暂停点）

Review 结论为 `FINDING_CONFIRMED + SEVERITY: HIGH` ⇒ 计划图进入人工门：
Fixer/Verifier **不派发**，直至操作员批准。

**人工门记录**（`shared/projects/copaw-high-risk-human-gate/human-gate-approval.md`，
快照 artifacts/human-gate-approval.md）：操作员确认 HIGH_RISK_FOUND 与 CWE-22 定性，
授权 Leader 派发 fix-1/verify-1、授权最小修复+本地测试与独立验证；
**明确禁止 merge/push/close/reopen，PR #2 保持 OPEN**。

### 3) 人工批准后：Fixer 修复

| 时刻 | 事件 | 证据 |
|---|---|---|
| 10:06:24 | fix-1 委派：event `$JU09kIgwjvVJmEUSVSlNR8Va2-zhxsvtmFaZ3RIHuTM` | 房间事件 |
| 10:06→10:22 | fixer：clone 分支 → 修复前探针（**traversal 200 泄露**）→ 最小修复 → 修复后探针（**404 拒绝**） | workspace/test-evidence.md |
| ~11:0x | fix-1 提交：`STATUS: SUCCESS`、`FIX_APPLIED`、`TESTS_PASSED` | fix-1/result.md |

补丁（artifacts/fix.patch，12+/4-，单文件）：
`Path.resolve()` 归一化 + `is_relative_to(DEMO_FILES_DIR)` 包含性校验，越界返回 404。

### 4) Verifier 独立验证

| 时刻 | 事件 | 证据 |
|---|---|---|
| 11:06:25 | verify-1 委派：event `$t0dXkuWwmjdkA-Ao6H0Z3KhuYJLJ0nQPCq7w0Jr1aU8` | 房间事件 |
| 11:06→11:26 | verifier **独立重 clone**、应用 fix.patch（字节级一致）、自设计探针复测、仓库测试回归 | workspace/verification-report.md |
| ~11:26 | verify-1 提交 | verify-1/result.md |

**验证结论（verify-1/result.md）**：

```text
STATUS: VERIFICATION_PASSED
SEVERITY: NONE
HUMAN_VERIFICATION_REQUIRED: NO
SUMMARY: FIX_INDEPENDENTLY_VERIFIED REGRESSION_CHECK_PASSED - ...
```

探针对照（原始输出 artifacts/verify-1.before/after_probe_raw.txt）：

| 请求 `name=../outside/outside-secret.txt` | 修复前 | 修复后 |
|---|---|---|
| HTTP | 200 | **404** |
| 泄露外部内容 | TOP-SECRET-OUTSIDE-BASE | 无 |

### 5) PR #2 状态（合规声明）

- **PR #2 保持 OPEN**；未执行 merge / push / close / reopen
- Fixer/Verifier 无远端凭证，spec 明令"仅本地作业"；修复以补丁交付物形态存在
- 修复是否进入远端分支，属仓库维护者的后续人工决策，不在本系统授权范围内

## 与 PR #1 案例的对照总结

| | PR #1 | PR #2 |
|---|---|---|
| 触发人工门 | 否（结论非高危） | 是（SEVERITY: HIGH） |
| 派发时机 | Leader 自主 | 人工批准后 Leader 自主 |
| Fixer 边界 | 常规修复 | 最小修复 + 本地测试，禁远端操作 |
| 验证强度 | 常规回归 | 独立重 clone + 自设计探针 + 字节级补丁核对 |
