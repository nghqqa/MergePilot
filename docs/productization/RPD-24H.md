# RPD-24H 展示视图（24h 自主交付轮）

**轮次**: RPD-24H-20260923 ｜ **开始**: 2026-09-23T15:09:04Z ｜ **BASE_HEAD**: `b79cd39`
**分支**: `feat/backend-pg-storage` ｜ 状态源: [RPD-24H.yaml](RPD-24H.yaml)

## 任务板

| ID | 任务 | 状态 | 优先级 | 证据 | 备注 |
|---|---|---|---|---|---|
| RPD-01 | 启动门禁和基线记录 | **DONE** | P0 | [startup-report](../evidence/rpd-24h/rpd-01/startup-report.md) | BASE_HEAD 复现✓ 工作树干净✓ 无秘密✓ |
| RPD-02 | pgvector 隔离证据复核 | **DONE** | P0 | [reverify](../evidence/rpd-24h/rpd-02/reverify.md) | 新一次性实例重放 11/11,实例已销毁 |
| RPD-03 | case_retrieval 接线契约 | TODO | P1 | — | 共享注入需要时→WAITING_HUMAN |
| RPD-04 | TicketStore 和人工门闭环 | TODO | P1 | — | 禁真实决策/派发 |
| RPD-05 | 后端回归 | TODO | P0 | — | 保留 TEST-DEBT 口径 |
| RPD-06 | 有限前端契约对齐 | TODO | P2 | — | 仅八种门状态展示 |
| RPD-07 | PR 交付 | TODO | P0 | — | 一个 feature PR;结果不明即停 |
| RPD-08 | 最终报告 | TODO | P0 | — | 到点即停 |

## 进度快照（每 30 分钟追加）

| 时间(UTC) | 当前任务 | commit | 测试 | 请求数/token | 阻塞 | 下一步 |
|---|---|---|---|---|---|---|
| 15:09 | RPD-01 | b79cd39 | — | 0/0 | 无 | 门禁+RPD 初始化 |
| 15:14 | RPD-02 | b79cd39 | — | 0/0 | 无 | pgvector 证据复核 |
| 15:30 | RPD-03 | b79cd39 | smoke 重放 11/11 | 0/0 | 无 | case_retrieval 接线契约 |

## 已知事实 → 待证据复核映射

| 已知事实 | 复核任务 | 命令 |
|---|---|---|
| pgvector smoke 11/11 | RPD-02 | 复核脚本+commit 记录 |
| TicketStore 13/13 + 26 单测 | RPD-04 | gate_ticket_smoke.py + pytest |
| 回归 281 / PG 25-26 | RPD-05 | pytest 四目录 + MERGEPILOT_PG_CONTRACT=1 |
| case_retrieval 接线 9/11→待重验 | RPD-03 | pytest test_case2_fixes + validate_env |
