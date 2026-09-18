# Phase 14.2H-WD-CREDENTIAL-ROTATION — 凭据轮换（全部完成）

- 日期：2026-08-29 13:05 → 13:31 (+08:00)
- 裁决：**`CREDENTIAL_ROTATION_VERIFIED`**（完整：consumer 5/5 + DeepSeek provider）

## consumer 轮换（5/5，两阶段零停机）

| Consumer | 旧值撤销 | 新 key 网关 completion |
|---|---|---|
| manager | ✅ | 200 / 0.53s |
| worker-p14h2-wd-worker-manager | ✅ | 200 / 0.47s |
| worker-p14h2-wd-worker-reviewer | ✅ | 200 / 1.00s |
| worker-p14h2-wd-worker-fixer | ✅ | 200 / 0.36s |
| worker-p14h2-wd-worker-verifier | ✅ | 200 / 0.59s |

## DeepSeek provider key（操作员提供新 key，已完成）

- 新 key 由操作员在 platform.deepseek.com 创建，经受保护文件（D:\goai\secrets\deepseek_api_key.txt）
  提供，stdin 直传容器，全程不回显不落盘
- Higress deepseek provider token 已刷新（console API PUT），digest 前后变化已记录
- 直连 DeepSeek：200 / 0.58s；经网关端到端：completion 200
- **旧 key（会话回显暴露的 DeepSeek key，前缀略）已在平台失效（401）**——暴露 key 不再有效

## 四方一致性（终态快照）

每个 worker：本地 openclaw.json apiKey == MinIO 对象 == console consumer value（digest 逐一对齐），
网关 completion 全 200。runtime 自身的网关凭据动态管理（会自行轮换/同步）与本次操作员轮换已收敛。

## 稳定性

180s 窗：10/10 容器 running、RestartCount=0、认证错误 0、project 两态不变
（copaw-sandbox completed / wd1-pr1-bootstrap active）。

## 文件（20）

rotation-authorization / runtime-freeze / provider-before / consumer-inventory-before /
credential-generation-audit / provider-update-result / consumer-rotation-results /
digest-change-audit / auth-validation-matrix / llm-completion-validation / matrix-validation /
minio-validation / stability-180s / old-credential-revocation / four-way-consistency-snapshot /
secret-handling-audit / redaction-report / verdict / README / SHA256SUMS
