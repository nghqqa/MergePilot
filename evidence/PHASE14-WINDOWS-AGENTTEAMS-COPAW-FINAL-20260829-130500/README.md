# Phase 14.2H-WD-FINAL-SUBMISSION-LOCK — 最终不可变复赛提交包

- 日期：2026-08-29 13:05 → 13:12 (+08:00)
- 裁决：**`PHASE14_2H_WINDOWS_AGENTTEAMS_COPAW_SUBMISSION_READY`** ✅
- 性质：三组证据合并核对后的最终锁定包（只读归档，零 runtime 改动，零消息）

## 合并核对的三组证据源（全部 SHA256SUMS 校验通过）

| 源目录 | 校验 |
|---|---|
| PHASE14-WINDOWS-COPAW-E2E-FULL-20260829-122526 | 20/20 OK |
| PHASE14-WINDOWS-COPAW-PROMOTION-A-FINAL-20260829-124400 | 17/17 OK |
| PHASE14-WINDOWS-CREDENTIAL-ROTATION-20260829-130500 | 20/20 OK |

## 提交状态

```text
CREDENTIAL_ROTATION = ROTATED_AND_VERIFIED
COPAW_E2E           = VERIFIED
PROMOTION_A         = APPROVED
OPENCLAW_MAINLINE   = PRESERVED
PR_WRITE_ACTIONS    = NOT_AUTHORIZED_AND_NOT_EXECUTED
```

## 活体复核（审计时点）

- 10/10 容器 running、RestartCount=0、StartedAt 恒定（180s 锁定窗 7 采样全 YES）
- copaw-sandbox **completed**（copaw 文件存储全 [x]）；wd1-pr1-bootstrap **active**（主线保留）
- 三任务 result.md 全 SUCCESS；kickoff/retry 账本一致（copaw: 1+2，均消费保留；无重复）
- 无 reconcile 删除/重建循环；无手工 delegate；无跨 runtime 连接

## 凭据

- DeepSeek provider key：已轮换为操作员提供的新 key（旧 key 平台已失效 401）
- 网关 consumer keys：5/5 已轮换（console==worker 文件==MinIO 三方一致）
- 秘密扫描：0 真实命中（收紧模式含 sk- 词边界、syt_、64-hex）

## 边界声明

AgentTeams=协作框架；CoPaw=Agent 运行时；p14h2-copaw=隔离演示 runtime；p14h2-wd=OpenClaw 主线。
sandbox 结果不归功主线；操作员播种/修复未伪装成 Agent 行为；无 PR 写操作声明。

## 文件（18 + SHA256SUMS）

final-architecture.md / competition-requirements-map.json / agent-role-matrix.json /
autonomous-timeline.json / task-results.json / self-repair-evidence.json / runtime-boundary.json /
openclaw-copaw-comparison.json / credential-rotation-final.json / stability-final.json /
pr-state-final.json / human-approval-final.json / secret-handling-audit.json /
redaction-report.json / verdict.json / README.md / SHA256SUMS
