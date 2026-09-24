# RPD-24H 最终报告（2026-09-23，轮次收口）

## 最终状态判定：MANUAL-REQUIRED
存在需用户授权/人工决策事项（CASE2 门决策、D-1/D-2、正式 controller 注入、真实 CASE2），
已安全停止；证据、commit 与复现命令已保存。必要隔离验收全部 DONE，但"真实审批/CASE2"
不可由 AI 完成，故不判 PROMOTE-READY。

## 交付物（PR #233：feat/rpd-24h-delivery → main @ 84ad9af）
| RPD | 任务 | 状态 | 关键证据 |
|---|---|---|---|
| RPD-01 | 启动门禁+基线 | DONE | startup-report.md（BASE_HEAD=b79cd39，工作树干净） |
| RPD-02 | pgvector 证据复核 | DONE | 新一次性实例重放 **11/11** 后销毁 |
| RPD-03 | case_retrieval 接线契约 | DONE | validate_env `--preflight` 正/负实测（0/5）；路径深度 bug 修复 |
| RPD-04 | TicketStore 人工门闭环 | DONE | gate_ticket_smoke **13/13** + 28 单测 |
| RPD-05 | 后端回归 | DONE | 本地 **282/0**；PG 门控 25/26（race=基线既有，TEST-DEBT 保持） |
| RPD-06 | 前端契约对齐 | DONE | gate_display 八态纯映射 + 审批响应字段（5/5 测试） |
| RPD-07 | PR 交付 | DONE | PR #233（branch push + 唯一 PR，均显式核验） |
| RPD-08 | 最终报告 | DONE | 本文件 |

## GitHub 写入声明
仅两项：push feature branch `feat/rpd-24h-delivery` + 创建 PR #233。
无 main push、无 merge、无 check-run、无 issue/其他对象。
（首次 push 403 权限拒绝=明确失败，改用 gh 凭据助手后成功，远端核验一致。）

## 边界遵守声明
零真实审查；零付费模型调用；零空提交推送（业务仓库）；零共享 case-pg/生产 MinIO/
AgentTeams 配置修改；零 fixer/verifier 唤醒；零 embedding 模型下载（D-7 未变）；
TicketStore 未接真实部署；approve/reject 仍为本地隔离接口。

## 复现命令
```bash
# 回归（本地）
python -X utf8 -m pytest tests/approval/ tests/gh_bridge/ tests/model_gateway/ tests/skills/ -q -p no:cacheprovider
# PG 门控（隔离实例）
MERGEPILOT_PG_CONTRACT=1 python -X utf8 -m pytest tests/approval/test_store_pg.py tests/approval/test_migration_runner.py -q -p no:cacheprovider
# TicketStore smoke
python -X utf8 tools/approval/gate_ticket_smoke.py
# pgvector 全查询 smoke（需一次性 pgvector 实例；本轮重放 11/11）
# 配置校验
python -X utf8 tools/case_retrieval/deploy/validate_env.py --mode repo
```

## 回滚命令
```bash
# PR 层：关闭 PR #233 + 删除远端分支即完全回滚（main 未动）
gh pr close 233; git push origin --delete feat/rpd-24h-delivery
# 本地：回到基线
git checkout feat/backend-pg-storage && git reset --hard b79cd39
# 运行副本回滚点：r3work/rollback-20260923-222605/（CASE2 实测版三文件）
```

## 人工决策点（WAITING_HUMAN）
1. CASE2 人工门：HIGH finding 批准或拒绝
2. D-1/D-2：真实审批动作子集与审批人身份
3. 正式 controller/Worker 环境注入（共享变更）
4. 真实 CASE2 执行（新 head 空提交+预算门+单 check-run，见 CASE2-CANDIDATES §7）
5. gh CLI 凭据 vs git 凭据管理器权限不一致（本轮以 gh 通道完成授权内推送；是否统一凭据由用户决定）

## 资源清理
- 一次性 pgvector 容器已销毁（两轮）；口令文件已删除
- 本地 Docker 栈（elemiso/mp-*）保持原状（未新启长期资源）

---

# 后续轮次追述（2026-09-24 状态校准轮补记；上文 2026-09-23 原始收口保留不改）

> 以下为 RPD-24H 收口后至校准轮之间真实发生的后续批次，及校准轮本身的最终叙述。
> 历史事实不回写、不倒签；各批次的详细证据在各自目录。

## 后续批次事实链（按发生顺序）

1. **复核/执行判定轮（2026-09-24 早期）**：PR #233 范围审计 `PR_SCOPE_ACCEPTABLE=true`
   （157/157 文件归属各轮，秘密扫描净）；执行指令轮判定"批复缺失"→保持 MANUAL-REQUIRED 零外部动作。
2. **夜轮（2026-09-23 深夜，授权=D-E+D-A+D-C A1-A5）**：NR-01 push PR #233 本地提交；
   NR-02 D-A 正式接线被 `agt apply` 丢弃 `spec.env` 阻塞（生产 kine 存储证明）→ 当轮收口 BLOCKED。
3. **D-A 修复 + CASE2-B 执行轮（2026-09-24）**：D-A 经 kube-apiserver 原生 PUT 注入 spec.env 两键、
   容器重建、生产 preflight PASS；CASE2-B 按授权执行（run-gh-pr2-42ed1787-003205）：A1 空提交
   head=42ed1787、A2 deepseek-flash 真实审查、A3 rag-live **真实 RAG 检索 ×2**（引用
   cwe-22/path-containment 两条组织标准，第二次真实 RAG 消费）、A4 单 check-run 107446711189、
   终态 timeout（leader 未写 gate marker→**无合法 ticket**，不补造）。HIGH finding=CWE-22 任意文件读。
4. **CASE2-B 复核轮**：证据 14/14 复核通过；用户批复 B=**APPROVED_PLAN_READY**（仅处置意向）。
5. **RAG-IMAGE-SYNC 轮**：v6scope 镜像部署生产 reviewer 并验收（真实 case-pg scope 查询 OK）。
6. **closure 批次（CL-01..CL-08）**：CL-01..07 隔离链组件+确定性测试 DONE；CL-08 真实模型隔离链
   BLOCKED（deepseek-flash reasoning 耗尽 8000 上限；thinking 禁用后 diff @@ 头损坏 2/2）。

## 状态校准轮（2026-09-24T02:54Z，本节=当前最终叙述）

**范围**：仅状态/证据索引/报告文件；零业务代码修改；不触碰人工补丁文件本体（只读验证）。

**用户决策登记（本轮）**：
- CASE2-B-HIGH-DECISION 重申=批准进入**人工** fix 计划（处置意向，非 D-B/TicketStore 正式 approve）；
- CL-08 三选一已决=**人工修复**，本轮起零模型调用零 token（模型失败根因保留）；
- **D-B 保持 WAITING_HUMAN/DISABLED**；生产 Fixer/Verifier **零启动**；
- RAG-IMAGE-SYNC=历史 DONE 保留，本轮 **FROZEN/NO_NEW_ACTION**；
- D-A、D-C A1-A5、D-E 按已有真实证据登记 APPROVED_EXECUTED（夜轮批准+后继轮执行），不重新执行。

**CASE2-B 人工修复验证（→MANUAL_FIX_VERIFIED）**：补丁 `case2b-fix/patch.diff`
（SHA256 `6d9e9905…`，目标 `42ed1787`，3 文件 +167/−30）在一次性临时 clone 上验证：
`git apply --check` exit=0；修复后 **13 passed/1 skipped（symlink=Windows 特权）/0 failed**；
未修复对照=原始 PoC 主用例复现泄漏（200+TOP-SECRET，漏洞真实存在）→ 修复后同向量 400 阻断、
合法路径回归 3/3 通过；depth 对照用例失败=原始测试自身路径算术缺陷（补丁前已有，非补丁问题，如实登记）。
详见 [case2b-fix/verify-report.md](../case2b-fix/verify-report.md)。
**人工复核 ≠ 生产 Verifier 执行；补丁未推送目标仓库；正式派发/合并仍待 D-B+合法票据+用户明确指示。**

**HEAD 分层（git 实读）**：校准轮起点=`74a2782c…`；校准窗口内**并行 CASE2-B 人工补丁会话**追加
ec66209/81e2b25/2e98d4e 并经自身授权（夜轮 D-E 通道）push → 远端 pushed_head=`2e98d4e2…`
（PR #233 headRefOid 实读一致）；校准轮自身零 GitHub 写入；本轮收口提交后 local 领先 pushed 1 提交
（**local_head 与 pushed_head 分别记录，不混写**；main 未动、未 merge）。

**最终状态：MANUAL-REQUIRED**——CASE2-B 人工修复子任务=MANUAL_FIX_VERIFIED，但 D-B 正式审批启用
仍待决，产品化决策面整体保持 MANUAL-REQUIRED，不为变绿隐藏待决项。
仍待人工决策：①D-B（真实审批启用）②D-D（等合法票据）③已验证补丁的去向（推送/存档/关闭）
④PR #233 拆分方案 A/B 与 merge。

**本轮资源与边界声明**：新增模型请求 **0**、token **0**；未 push/未 merge/未改 PR/未发 check-run；
未动共享 case-pg/生产 MinIO/AgentTeams 配置；生产 Fixer/Verifier 零启动；验证用临时 clone 已销毁；
未提交任何未跟踪临时文件/密钥/数据库运行副本。
