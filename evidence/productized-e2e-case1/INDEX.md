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

## 6. R2（建票可靠性整改后复跑，2026-09-24）— 已执行，最终状态 AWAITING_D_B_APPROVAL

代码依据：feat/e2e-ticket-orchestration @ `829d536`（= PR #233 head）；
桥自 worktree 直跑（matrix/run_context 与运行副本同哈希，零同步漂移）。

### 运行绑定

| 项 | 值 |
|---|---|
| 触发提交（空提交，普通 fast-forward push） | `0902e52aada63e1a8a17f395e7c8b374d106e5eb`（父 26ed8f1e） |
| delivery_id | `c3c133e0-b7f7-11f1-9b83-404c35e12127` |
| run_id | `run-gh-pr2-0902e52a-091043`（manifest sha256 前16 `1c7da52b947532b3`） |
| project / task | `elemiso-gh-pr2-0902e52a` / `gh-pr2-0902e52a-review-1` |
| ticket | **`tkt-9dc0fd7196ec4f2491ec18f938993e9e`，status=PENDING** |
| ticket 绑定 | run=上值；repo=nghqqa/fastapi-boilerplate-demo；head=0902e52a…；action=`run_poc`；finding_id=`find-10bb155e7f85`；finding_fp 前16=`10bb155e7f8561dd`；params_hash 前16=`f16dd6701719323a`；TTL 24h（至 2026-09-25T09:12:09Z） |
| ticket 创建者 | **确定性控制面**（audit：`control-plane:ensure` / `ENSURE_CREATED`，policy=db-2026-09-24）；`marker=ABSENT`（leader 未写 marker，票据照常成立） |
| Check Run | `107568712351`，**action_required**，title 携带 `ticket PENDING tkt-9dc0…（creator=control-plane; action=run_poc; marker=ABSENT）` |
| delivery 终态 | ERROR `GATE_WAIT(manual) verdict=gate; check_run=107568712351; ticket=tkt-9dc0…` |
| 时间线 | kickoff 17:10:47 → gate+建票 17:12:09（**82s**，无 20min 超时收口） |

### 审查事实

- reviewer findings（本 run 专属路径，自证 run_id/head 一致）：
  `FINDING_CONFIRMED / HIGH / HVR=YES`，CWE-22 路径穿越，独立 PoC。
- 结构化 outcome 源 = `reviewer-findings`（确定性令牌解析；head/run 绑定锚
  = write-once run-manifest）。
- **RAG 消费可确认**（与 E2E-1 不同）：rag-live 审计流窗口内
  `rag.retrieve ×2`（OK，document_count=3，org-standards/cwe-22-path-traversal）；
  另 diff_parse ×1、sast_scan ×1；窗口审计切片 sha256 前16 `11ba82b446d6b8cf`。

### 模型用量

- deepseek-flash（leader+reviewer），单次 reviewer 通过；
- 网关无逐请求计量 → 请求/token 精确值**不可观测**（如实登记）；
  窗口 82s 内无重试、无第二次派发；远低于本轮 6 请求/60k tokens 预算约束
  的量级（未超）。

### 零确认清单（R2）

- [x] approve/reject = 0（ticket 审计仅 ENSURE_CREATED；approved_by=None）
- [x] Fixer/Verifier 派发 = 0（容器 30min 窗口零任务日志；票据 PENDING 不可派发）
- [x] patch 生成/推送 = 0；历史人工补丁 f95d99d 未推送
- [x] merge = 0；最终 success Check Run = 0（发布为 action_required）
- [x] 共享 case-pg/MinIO 业务数据修改 = 0（桥仅写本 run 项目命名空间）
- [x] Worker CR/镜像/env 修改 = 0；embedding 下载 = 0

### R2 本地工件（不入库；路径 + sha256 前16）

位置：`D:\goai\r3work\evidence\productized-e2e-case1-r2\`

| 文件 | sha256（前16） |
|---|---|
| run-manifest.json | `1c7da52b947532b3` |
| run-context.json | `91f1a67a80c0b9c5` |
| receipt.json | `8bca67e4a79eb96d` |
| reviewer-findings.md | `04cf92bf18857b45` |
| bridge-run.log | `68da58a1abf372f7` |
| rag-audit-r2-window.jsonl | `11ba82b446d6b8cf` |

### 下一条可直接执行的具名审批命令（TTL 内；是否批准由操作员决定）

```bash
python tools/approval/gate_cli.py approve \
  --db ~/.mergepilot/gate-tickets.db \
  --ticket tkt-9dc0fd7196ec4f2491ec18f938993e9e \
  --actor 'MDQ6VXNlcjM1OTg3NDg='
```
（拒绝：`reject` 同参；或直接回复操作员决定，由会话执行。）
