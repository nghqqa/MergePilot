# SK5 轮证据包说明（2026-09-19 · 九轮运行 · 四包）

## 本轮核心增量

1. **skill_sast_scan 首次在正式运行中被 Agent 调用**：reviewer 在 PR1SK5（低风险路径）对 bootstrap 文件跑 SAST（span: `tool.skill_sast_scan=2`），诚实识别测试文件中 subprocess.run 为 false positive（非 shell=True）
2. **PR #1 低风险自动路径首次在当前栈打通**：NOT_CONFIRMED / LOW / 无人工门 → 自动 completed（62 秒）
3. **dual-reviewer 评审间信度实验**：两个独立 Reviewer（不同容器/不同账号/不同 session）对同一 PR #2 head SHA 得出完全一致结论（FINDING_CONFIRMED / HIGH / CWE-22 / HVR:YES）
4. **direct-probe 采集修复**：stdin 方式替代 docker cp（4 次同因失败后修复），4/4 非空（375-377 字节）
5. **delegation.link 数据层验证持续**：SK5 轮所有委派的 LINK_SPAN_TRACE trace_id 与 parent_trace 一致

## 四包概要

| 包 | 案例 | 结果 | 用量 |
|---|---|---|---|
| FINALS-ELEM-PR2-SK5-TRACED | PR #2 批准路径 | VERIFIED completed | 86 调用/15.64M(93%) |
| FINALS-ELEM-PR3-SK5-TRACED | PR #3 拒绝路径 | blocked 零派发 | 33 调用/3.88M(99%) |
| FINALS-ELEM-PR1-SK5-AUTO | PR #1 低风险自动 | NOT_CONFIRMED/LOW → auto completed | 45 调用/5.08M(99%) |
| DUAL-REVIEWER-EXP-20260919 | 双 Reviewer 信度 | A=B: FINDING_CONFIRMED/HIGH/CWE-22/HVR:YES | 31 调用/2.18M(98%) |

## dual-reviewer 对照表

| 维度 | Reviewer A | Reviewer B | 一致 |
|---|---|---|---|
| STATUS | FINDING_CONFIRMED | FINDING_CONFIRMED | ✓ |
| SEVERITY | HIGH | HIGH | ✓ |
| CWE | CWE-22 | CWE-22 | ✓ |
| HVR | YES | YES | ✓ |
| PoC | ../ 逃逸→200；/etc/hostname 绝对路径→200 | ../ 逃逸→200 TOP-SECRET（独立 TestClient 复现） | ✓ |

## SK5 span 汇总

| Worker | spans | skill 调用 | rag | link |
|---|---|---|---|---|
| leader | 280 | 0 | 0 | 0 |
| reviewer | 240 | 13（diff_parse 3, risk_classify 6, **sast_scan 2**, case_retrieval 2） | 5 | 5 |
| fixer | 118 | 0 | 1 | 5 |
| verifier | 129 | 2 | 1 | 5 |
| reviewer-b | 57 | 0 | 0 | 0 |
| **总计** | **824** | **15** | **7** | **15** |

## Skill 调用统计（SK5 全轮）

| Skill | 调用 | 说明 |
|---|---|---|
| skill_diff_parse | 4 | PR2 review + verify；PR1 review |
| skill_risk_classify | 7 | 各案例 review 阶段 |
| **skill_sast_scan** | **2** | PR1 低风险路径首次调用（bootstrap 文件 SAST，诚实 FP 识别） |
| skill_case_retrieval | 2 | PR2 review 阶段 |
| 总计 | **15** | （SK4 轮 6 → SK5 轮 15，sast_scan 为新增） |

## 三路径完整覆盖

| 路径 | 案例 | 门决策 | 终态 | 首次打通 |
|---|---|---|---|---|
| 批准 | PR #2 | APPROVED | VERIFIED completed | R2 轮 |
| 拒绝 | PR #3 | REJECTED | blocked 零派发 | PR3-LIVE 轮 |
| **自动（无门）** | **PR #1** | **无（LOW）** | **auto completed** | **SK5 轮 ✓** |
