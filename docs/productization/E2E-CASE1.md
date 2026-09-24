# Productized E2E Case 1（结构化建票→有效审批→真实审查→D-B 门）

## 最终状态：**AWAITING_D_B_APPROVAL**（HIGH finding 确认，D-B ticket 未产生——leader 未写 marker）

## 执行摘要

| 项 | 值 |
|---|---|
| 目标仓库 | nghqqa/fastapi-boilerplate-demo |
| PR | #2（base: mergepilot-demo/schema-migration-risk） |
| 源分支 | demo/high-risk-human-gate |
| 旧 head | `42ed17879becbc02e31551938afbbf689351df96` |
| 新 head（空提交） | `26ed8f1e4ca28692933df15ae6c2fd2cdf633a9b` |
| delivery_id | `e077b300-b7e9-11f1-94c5-ff03aa78aa3c` |
| run_id | `run-gh-pr2-26ed8f1e-073224` |
| manifest_id | `99213dad4d2e…` |
| 模型 | deepseek-flash（leader+reviewer） |
| model catalog | [deepseek-flash, deepseek-v4-pro] ✓ |
| rag snapshot | `fd34c304…` / lexical-zh-en-v1 / **reachable at dispatch** |
| check-run | `107543692249`（mergepilot/review，neutral，head=26ed8f1e） |
| delivery | ERROR `TIMEOUT(manual) timeout; publish=ok` |

## 审查结论

**FINDING_CONFIRMED / HIGH / HUMAN_VERIFICATION_REQUIRED: YES**
CWE-22 任意文件读取（`demo_download` 无包含校验）。独立 PoC 复现。

## RAG/Skill 调用分析

### rag-live 审计（rag-tool-spans.jsonl）
- 本次 run 期间审计流**未记录新 rag_retrieve 调用**
- 审查员 findings 引用了组织标准 source_refs，但这些可能来自
  CoPaw 工作区的持久化上下文（前次 CASE2-B 运行遗留）而非
  本次 rag-live 的实时查询
- **无法确认本次发生了真实 rag_retrieve 网络调用**
- rag-live 服务本轮已启动且 reachable（manifest 确认）

### 结论
rag_retrieve 消费声明 = **不可确认**（审计无新调用记录，reviewer 文本引用不足以证明实时调用）。
与 CASE2-B 首次运行（rag-live 审计流记录了 2 次新调用）不同。

## 模型使用

| 项 | 值 |
|---|---|
| 模型 | deepseek-flash |
| 请求数 | 未精确计量（gateway 无实时计量） |
| token | 未精确计量 |
| provider 费用 | 不可实时观察；在途请求可能继续计费 |
| 模型预算 | 未设上限（deepseek-flash 按量计费） |

## 零确认清单

- [x] approve/reject 执行次数 = 0
- [x] Fixer/Verifier 生产角色启动 = 0
- [x] patch artifact = 0
- [x] 业务修复分支 push = 0
- [x] merge = 0
- [x] 最终成功 Check Run = 0
- [x] 共享 case-pg 业务数据修改 = 0

## D-B 票据状态

**未产生正式票据**。leader 未写 `human-gate-required.json` marker（与
CASE2-B 首次相同——deepseek-flash 指令遵循局限）。bridge 20min 截止
超时收口。ticket 创建路径已在 enforce.py 中实现并通过确定性测试，
但 leader 行为依赖是已知限制。

## 证据路径

| 文件 | 说明 |
|---|---|
| run-manifest.json | 派发清单（model catalog/rag snapshot/service probe） |
| run-context.json | 八字段可信 run 上下文 |
| receipt.json | check-run 回执（adopted=false） |
| reviewer-result.md | 审查员结构化结论 |
| reviewer-findings.md | 审查员完整发现报告 |
| bridge-run.log | 桥运行日志 |
| rag-tool-spans.jsonl | rag-live 审计流完整副本 |

## 回滚

```bash
# 业务仓库：回退空提交（如需完全撤销触发）
git push origin --delete demo/high-risk-human-gate  # 不推荐——会删除分支
# 或：push 一个 revert 空提交
# MergePilot：PR #233 无需变更（本轮无代码变更）
# rag-live：已停止
# bridge：已自然退出
```
