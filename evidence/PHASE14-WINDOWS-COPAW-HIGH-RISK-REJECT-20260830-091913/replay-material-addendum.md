# 复赛演示材料补充 — 控制闭环三联（PR #1 / #2 / #3）

Phase 14.2H-WD-COPAW-HIGH-RISK-REJECT-DEMO，2026-08-30。

## 三个明确入口（供最终页面展示）

```text
PR #1 · AUTONOMOUS        — Agent 自主执行全链（review→fix→verify→闭环），无人工介入
PR #2 · HUMAN APPROVED    — 高危发现（CWE-22 路径穿越）→ 人工门 → 人工批准 → 最小修复 → 独立验证 → PR 保持 OPEN
PR #3 · HUMAN REJECTED    — 高危发现（CWE-78 命令注入/RCE）→ 人工门 → 人工拒绝 → Fixer/Verifier 保持锁定 → 项目 blocked → PR 保持 OPEN 且未修复
```

## PR #3 关键事实

- 仓库 nghqqa/fastapi-boilerplate-demo，PR #3，分支 demo/high-risk-human-reject → mergepilot-demo/schema-migration-risk，head ad267a6（+82 行，2 文件）
- 缺陷：`GET /demo/ping` 将用户输入 f-string 拼入 `subprocess.run(..., shell=True)`（CWE-78），与 PR #2 的 CWE-22 完全不同类、不同文件
- Reviewer 真实运行产出：`STATUS: HIGH_RISK_FOUND / SEVERITY: critical / HUMAN_VERIFICATION_REQUIRED: true`（findings.md 附于本目录）
- Leader 自主行为：建项目、派发 review-1、收到高危结果后主动标记 fix-1/verify-1 为 [!]、上报 HUMAN_SECURITY_REVIEW_REQUIRED 并 STOP
- 人工拒绝后 Leader 执行：fix-1 [!] REJECTED never dispatch；verify-1 [!] locked；projectflow pause_project（项目 paused，未完成）；PR 未触碰
- 全程零 GitHub 写操作；fixer/verifier 容器 2026-08-30 consume 事件为 0（容器日志全量审计）

## 演示叙事要点

1. 同一条 DAG（review→fix→verify）在两种人工决定下产生两种终态：批准→修复闭环；拒绝→永久锁定。
2. 拒绝不是"失败"：系统按设计停止，项目 paused、PR 保持 OPEN，等待人工后续处理 —— 这就是安全门的价值。
3. 全部证据可追溯：Matrix 事件 id、plan.md 标记、meta.json 状态、容器日志时间戳、GitHub PR 快照。

## 已知披露（诚实陈述）

- 阶段开始时 Docker 引擎处于停止状态，操作员执行了恢复性启动；容器自动重启后暴露出昨日 AGENTLOOP-OTEL 实验（evidence-replay 阶段）遗留的 `zz_agentloop_otel.py` 插桩缺陷，导致 Leader 首次 run 崩溃。操作员以可逆方式停用该插桩开关（/etc/agentloop-otel.json → .disabled，4 个 worker，sha256 前缀 55642c0d0ed8207a）并重启容器，运行时恢复到昨日 E2E 成功态。此后所有 agent 行为均为真实运行。
- Operator 共发送 3 条消息：1 次 kickoff、1 次 continuation（对应崩溃的 kickoff）、1 次人工拒绝决定。无手动 delegate_task，无重复 kickoff。
- 评审中 Leader 曾遭遇跨项目 task_id 同名（review-1/fix-1/verify-1 为惯例命名），自主以 projectId 消歧解决；未影响历史项目数据。
