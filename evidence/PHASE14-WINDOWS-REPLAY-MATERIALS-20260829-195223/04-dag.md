# 双案例 DAG 与状态机

## PR #1（普通协同，全自主）

```mermaid
graph TD
    S[Leader: create_project + plan_dag] --> R1
    R1[review-1 → @reviewer] -->|ack→work→submit| RF[Leader 验收<br/>check_task effective]
    RF --> F1[fix-1 → @fixer]
    F1 -->|ack→work→submit| FF[Leader 验收]
    FF --> V1[verify-1 → @verifier]
    V1 -->|ack→work→submit| DONE[PROJECT COMPLETE]
    style R1 fill:#dcfce7
    style F1 fill:#dcfce7
    style V1 fill:#dcfce7
```

无人工节点：review 结论非高危 ⇒ Leader 依 `ready_nodes`/依赖关系自主流转。

## PR #2（高危安全门）

```mermaid
graph TD
    S[Leader: project copaw-high-risk-human-gate<br/>PR #2 demo/high-risk-human-gate] --> R1
    R1[review-1 → @reviewer<br/>独立安全审查] --> RV{Review 结论}
    RV -->|SEVERITY: HIGH<br/>FINDING_CONFIRMED| GATE{{⛔ HUMAN SECURITY GATE<br/>人工安全门 · 受控停等}}
    RV -->|非高危| AUTO[按 PR#1 模式自主流转]
    GATE -->|操作员批准<br/>human-gate-approval.md| F1[fix-1 → @fixer<br/>最小修复 + 本地测试]
    GATE -->|拒绝/超时| STOP[停止并保留证据<br/>PR #2 保持 OPEN]
    F1 -->|fix.patch + 前后探针| FF[Leader 验收<br/>SUCCESS / TESTS_PASSED]
    FF --> V1[verify-1 → @verifier<br/>独立重 clone 验证]
    V1 -->|VERIFICATION_PASSED<br/>SEVERITY: NONE| OPEN[PR #2 保持 OPEN<br/>merge/push/close 禁止]
    style GATE fill:#fee2e2
    style OPEN fill:#e0e7ff
```

## 任务状态机

```mermaid
stateDiagram-v2
    [*] --> pending : plan_dag
    pending --> assigned : delegate_task(委派+event_id)
    assigned --> in_progress : ack_task(assignee)
    in_progress --> submitted : submit_task(result.md)
    submitted --> completed : Leader 验收 effective=true
    submitted --> failed : 验收失败(保留证据)
    assigned --> assigned : 重派(委派失败重试, txn 幂等)
```

## 消息/存储协议不变量

1. 委派必须产生**新的有效 Matrix 事件**且 `m.mentions` 命中目标（复用分支仅在
   同项目 + event_id 非空时允许）
2. 任务工件存放 `shared/projects/<pid>/tasks/<tid>/`（project 隔离；平铺旧路径只读兼容）
3. 接收端 event ledger + since-token 回放窗口 ⇒ 消息**至少一次投递 + 幂等消费**
4. 结果协议标记位于 result.md 顶层，Leader 以 `check_task` 的 `effective` 验收
