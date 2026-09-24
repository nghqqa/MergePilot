# Productized E2E Case 1 — 脱敏证据索引（2026-09-24）

本文件是**唯一入库**的证据记录；原始工件全部本地保留（路径见 §4），
不入库原因：含未脱敏运行日志与完整模型产出。入库内容仅限结构化事实
ID、哈希与摘要，可逐项复核（来源 = 本地工件 + GitHub/台账外部事实）。

## 1. 运行绑定（不可变事实）

| 项 | 值 |
|---|---|
| 目标仓库 | nghqqa/fastapi-boilerplate-demo，PR #2 |
| 触发分支 | demo/high-risk-human-gate |
| 旧 head → 新 head | `42ed17879becbc02e31551938afbbf689351df96` → `26ed8f1e4ca28692933df15ae6c2fd2cdf633a9b`（空提交） |
| delivery_id | `e077b300-b7e9-11f1-84ea-7ae7528bd79a` |
| run_id | `run-gh-pr2-26ed8f1e-073224` |
| project_id | `elemiso-gh-pr2-26ed8f1e` |
| task_id | `gh-pr2-26ed8f1e-review-1` |
| manifest（本地 run-manifest.json） | 见 §4，sha256 前 16 位 `2f5a48445fa15202` |
| 模型 | agentteams-gateway/deepseek-flash（leader+reviewer） |
| model catalog @ dispatch | `[deepseek-flash, deepseek-v4-pro]`（实测探针） |
| rag snapshot @ dispatch | `fd34c304e1da487f…`（lexical-zh-en-v1，service reachable） |
| Check Run | `107543692249`（mergepilot/review，**neutral**，head=26ed8f1e） |
| delivery 终态 | ERROR `TIMEOUT(manual)`；publish=ok |

## 2. 审查结论（结构化令牌，非自然语言推断）

- reviewer findings.md 令牌：`STATUS: FINDING_CONFIRMED` / `SEVERITY: HIGH` /
  `HUMAN_VERIFICATION_REQUIRED: YES`；CWE-22（demo_download 路径穿越）。
- 独立 PoC：`name=../outside-secret.txt` → HTTP 200（逃逸 base 目录）；
  `/etc/hostname` → HTTP 200。复现命令记录于原始 findings（本地）。
- token 化结论已由确定性解析器复验（本轮整改 tests/gh_bridge/
  test_ticket_orchestration.py 使用同格式夹具）。

## 3. RAG / Skill 事实

- rag-live 审计流（本地 rag-tool-spans.jsonl 副本）：本 run 窗口内
  **零新 rag_retrieve 调用记录** → 实时消费**不可确认**（findings 引用
  组织标准不构成调用证据）。
- 确定性 skills：diff_parse ×1、sast_scan ×1（findings 中带请求 ID 引用；
  SAST 0 findings = AST 模式固有盲区，不推翻人工/复现结论）。
- case_retrieval：SCOPE_MISSING（当时镜像无 scope-file 回退；已在
  RAG-IMAGE-SYNC 轮修复，v6scope 镜像已部署）。

## 4. 本地工件与保留策略

位置：`D:\goai\r3work\evidence\productized-e2e-case1\`
（已移出业务仓库克隆；保留策略：随案例库长期保留，不随仓库分发）。

| 文件 | sha256（前16） | 说明 |
|---|---|---|
| run-manifest.json | `2f5a48445fa15202` | write-once 派发清单 |
| run-context.json | `cc622aeea3a51a1f` | 八字段可信 run 上下文 |
| receipt.json | `08585f23cf4ae273` | check-run 回执（adopted=false） |
| reviewer-result.md | `43f5fdde85b85881` | leader 转发的审查结论 |
| reviewer-findings.md | `1a4d029887ac951c` | reviewer 结构化发现（含 PoC） |
| case1b-bridge-run.log | `a6d8cf102b105ce3` | 桥全量日志（未脱敏，本地） |

## 5. 关键结论 → 证据落点

| 结论 | 落点 |
|---|---|
| HIGH/CWE-22 复确认 | §2 令牌 + 本地 reviewer-findings.md（sha256 §4） |
| rag 消费不可确认 | §3 + 本地审计流副本 |
| neutral check-run=超时收口 | §1 + 本地 bridge-run.log（20min 截止） |
| 零 approve/fixer/verifier/patch/merge | 台账（github_deliveries ERROR TIMEOUT）+ 无 patch 工件 |
| 空提交绕过本地 hook 触发 | 本轮登记（E2E-CASE1.md 口径校准节）；触发 head=26ed8f1e |

## 6. 后续轮次（R2，占位）

R2（建票可靠性整改后的复跑）证据将按同一格式追加：
新 head/delivery/run/ticket_id（PENDING）/check-run ID/模型用量事实/零确认清单。
