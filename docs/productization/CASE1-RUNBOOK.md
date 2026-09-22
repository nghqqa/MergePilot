# 首个受控真实案例运行手册（CASE1-RUNBOOK）

**日期**：2026-09-22 ｜ **待确认**：见文末确认单 ｜ **执行模型：加固旧链路**；`MERGEPILOT_REVIEW_V3=shadow` 仅在派发边界记录计划/状态，**不驱动任何审查决策**。

## 1. 旧链路实际启动的角色与调用范围（勘误后口径）

**不存在"TRIVIAL 所以单审查器"的推断**——风险分级是 v3 逻辑，旧链路不做分级。旧链路固定角色：

| 角色 | 本轮行为 | 模型调用 |
|---|---|---|
| `leader` | 接收 kickoff，delegate 单一 review 任务，等待并汇报 | **有**（编排轮次） |
| `reviewer` | clone→checkout head→merge-base diff→独立审查；skill_diff_parse 必调；rag_retrieve/skill_case_retrieval/skill_sast_scan 参考性调用；可对检出树写自有 PoC/跑 PR 自带测试（仅限自己工作区） | **有**（主体消耗） |
| `fixer` / `verifier` | plan 中存在但被 kickoff 标记 N/A；**仅人工门批准后才会激活——本轮门保持关闭** | 唤醒但空闲 |

调用范围（SPEC 约束，kickoff 原文）：不改仓库、零 GitHub 写、只在自己工作区；结论必须含 STATUS/SEVERITY/HUMAN_VERIFICATION_REQUIRED。HIGH+需人工 → 门停等（本轮保留等待，不自动批准）。

## 2. 定向认领（本轮新增，已测试）

`MERGEPILOT_TARGET_PR=9` + `MERGEPILOT_TARGET_HEAD=<新SHA>`（两者都设才激活；SQL 层过滤；非目标行**保持 PENDING 不动**；不触碰 already_processed）。格式非法直接拒绝启动。

## 3. 预算与凭证（现实口径）

- 凭证链：worker → **本地网关**（elemiso-controller:8080，64 位 bearer）→ 上游供应商。**本地网关 key 不是计费凭证**；累计消费硬上限只能在 **provider 控制面**设定（需用户提供）。
- 仅共享凭证可用时的影响：全部 worker 调用共享同一上游配额；失控 agent 消耗共享额度；桥 20min 截止只停编排不停已发出的 agent 调用；唯一即时切断手段=停止 reviewer 容器（见 §4）。
- 限频/事后计量/桥观察超时**均不算硬预算**（明确记录）。

## 4. 取消与清理（已本地演练：停/启可逆）

- **桥超时（20min）只结束编排与台账**，不会停止已在运行的 reviewer agent。
- 实际切断 = `docker stop elemiso-worker-reviewer`（已演练：停→0 容器→启→Up，可逆）。
- **独占条件**：执行前核对任务流无其他活动项目（case 期间栈专用于本案例）；停止仅针对 reviewer 单容器，不动 leader/proxy/ctrl/case-pg。案例结束后恢复容器原状态。

## 5. 两类验收（不得混同）

| 类 | 内容 | 判定 |
|---|---|---|
| A 正常案例 | 真实 Agent 执行、结论绑正确 head、manifest 全字段、GitHub 发布成功、控制台/台账状态一致 | 每项独立证据 |
| B RAG 消费 | rag_retrieve 实际被调用、返回来源正确、snapshot 匹配、结论引用/使用检索证据 | PR #9 若无安全知识命中，**合法空结果≠失败**；也不得据单案例宣称 RAG 有效性验证完成 |

## 6. 执行序列（获批后）

```
0. prerun gate 全绿（git 固定/副本 sha/快照/模式=off→案例中改 shadow/唯一执行者/容器/预算确认/投递前置）
1. 启动 rag-live（:4184，运行语料）→ /health 核对
2. 设 MERGEPILOT_REVIEW_V3=shadow + MERGEPILOT_TARGET_PR=9 + MERGEPILOT_TARGET_HEAD=<新SHA>
3. 推空提交（唯一外部写触发）→ webhook → 台账新 PENDING 行
4. 运行副本桥 run --once --timeout-min 20
5. 全程证据：桥日志、RunStore、台账行、rag 审计尾行、check-run 记录、控制台读模型
6. 结束：停 rag-live、恢复 reviewer 容器、台账/证据归档（不删除审计记录）
```

## 7. 确认单（唯一待确认项，回复即执行）

1. **触发与写入**：是否允许向 `feat/skill-exercise`（PR #9）推一个空提交，并经 reporter 向新 head 发布**至多 1 个** check-run？
2. **预算**：provider 侧 gateway 上游 key 能否设置本次消费硬上限（数值由你定）？不能的话，接受"20min 截止 + reviewer 容器停止 + 人工停止信号"为兜底？
3. **服务权限**：启动 rag-live（案例期间）、停止/恢复 reviewer 容器、运行同步副本桥（off→shadow）——是否均获准？
4. **凭证**：确认现网凭证维持现状（未轮换）可满足本次真实运行条件，或明确轮换后再执行。
