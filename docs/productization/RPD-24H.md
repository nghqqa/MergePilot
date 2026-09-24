# RPD-24H 展示视图（24h 自主交付轮）

**轮次**: RPD-24H-20260923 ｜ **开始**: 2026-09-23T15:09:04Z ｜ **BASE_HEAD**: `b79cd39` ｜ **远端 pushed_head**: `2e98d4e`（2026-09-24 校准轮收口时 git ls-remote/gh pr view 实读）
**分支**: `feat/rpd-24h-delivery`（PR #233 → main，OPEN 未 merge；校准轮收口提交后 local 领先 pushed 1 提交，未 push） ｜ 状态源: [RPD-24H.yaml](RPD-24H.yaml) ｜ 机器态: [execution-state.json](execution-state.json)
（HEAD 分层：校准轮起点=`74a2782`；并行 CASE2-B 人工补丁会话同窗追加 ec66209/81e2b25/2e98d4e 并经其自身授权 push——夜轮 D-E 通道；校准轮自身零 GitHub 写入）

## 任务板

| ID | 任务 | 状态 | 优先级 | 证据 | 备注 |
|---|---|---|---|---|---|
| RPD-01 | 启动门禁和基线记录 | **DONE** | P0 | [startup-report](../evidence/rpd-24h/rpd-01/startup-report.md) | BASE_HEAD 复现✓ 工作树干净✓ 无秘密✓ |
| RPD-02 | pgvector 隔离证据复核 | **DONE** | P0 | [reverify](../evidence/rpd-24h/rpd-02/reverify.md) | 新一次性实例重放 11/11,实例已销毁 |
| RPD-03 | case_retrieval 接线契约 | **DONE** | P1 | [wiring](../evidence/rpd-24h/rpd-03/wiring.md) | validate_env --preflight 正/负双向实测 |
| RPD-04 | TicketStore 和人工门闭环 | **DONE** | P1 | [closure](../evidence/rpd-24h/rpd-04/closure.md) | smoke 13/13 + 28 单测；禁真实决策/派发 |
| RPD-05 | 后端回归 | **DONE** | P0 | [failure-ledger](../evidence/rpd-24h/rpd-05/failure-ledger.md) | 本地 282/0；PG 门控 25/26（TEST-DEBT 口径保持） |
| RPD-06 | 有限前端契约对齐 | **DONE** | P2 | [scope](../evidence/rpd-24h/rpd-06/scope.md) | gate_display 八态纯映射（5/5 测试） |
| RPD-07 | PR 交付 | **DONE** | P0 | [pr-record](../evidence/rpd-24h/rpd-07/pr-record.md) | **PR #233** OPEN（head 演进至 2e98d4e，2026-09-24 校准轮核验；main 未动） |
| RPD-08 | 最终报告 | **DONE** | P0 | [final-report](../evidence/rpd-24h/rpd-08/final-report.md) | **MANUAL-REQUIRED** |

## 进度快照（每 30 分钟追加）

| 时间(UTC) | 当前任务 | commit | 测试 | 请求数/token | 阻塞 | 下一步 |
|---|---|---|---|---|---|---|
| 15:09 | RPD-01 | b79cd39 | — | 0/0 | 无 | 门禁+RPD 初始化 |
| 15:14 | RPD-02 | b79cd39 | — | 0/0 | 无 | pgvector 证据复核 |
| 15:30 | RPD-03 | b79cd39 | smoke 重放 11/11 | 0/0 | 无 | case_retrieval 接线契约 |

## PR 范围审计（2026-09-24 复核轮）

**PR_SCOPE_ACCEPTABLE = true**：157/157 文件全部归属 R3..RPD-08 各轮（后端 74/测试 45/文档 19/RPD 证据 17/仓库配置 2，+19013/−67）；
零无关文件、零生成物、零 .env/私钥、零生产配置、零共享数据、秘密扫描净（3 处命中=脱敏测试夹具）。
拆分方案 A（保留综合 PR，推荐）/ B（四段拆分）待用户选择——详见 [pr-scope-report](../evidence/rpd-24h/pr-audit/pr-scope-report.md)。

## 执行判定（2026-09-24 执行指令轮）

指令要求"按已批准子项继续执行"，但 **RPD user_reply 五项全空、指令与仓库均无"批准 D-X"明确形式**
→ 判定=**批复缺失**：保持 MANUAL-REQUIRED，零外部动作（未 push db62d1c/9dcf3f6、未注入环境、未启用审批、未执行 CASE2）。
等待明确批复：`批准 D-A` / `批准 D-B` / `批准 D-C A1-A5` / `批准 D-E` / `修改条件：…`。

## CL-08（2026-09-24 深夜阻塞 → 同日已决）：**deepseek-flash reasoning 耗尽单请求上限 → 已选择人工修复**

**模型失败根因（保留）**：实测（usage 真实计费）fixer 提示词下 `completion_tokens=8000` 全部为
`reasoning_tokens`，`content=""`；thinking 禁用后 diff 的 `@@` 头损坏（`@` 占位），git apply 2/2 候选失败。
**2026-09-24 校准轮用户决策：三选一已决 = 人工修复**——本轮起不再调用真实模型、不增加模型 token，
模型路线关闭。人工补丁已生成并验证（见 CASE2-B 节）；CL-08=RESOLVED_MANUAL_FIX。
链上其余组件（结构化建票/iso 审批/派发 fencing+outbox/预算守卫/网关桥）验证可用的事实保持。
历史消耗：~6 请求 / ~67.5k tokens 保守入账（200k 预算内）；**本轮新增模型请求=0、token=0**。

## CASE2-B 收口与人工决策（2026-09-24 复核轮）

**证据复核 14/14**（manifest_sha 权威重算一致；零 fix/verify 任务；秘密扫描净）——[case2b-evidence-recheck](../evidence/rpd-24h/pr-audit/case2b-evidence-recheck.md)

| 决定/任务 | 状态 | 说明 |
|---|---|---|
| **CASE2-B-HIGH-DECISION** | **APPROVED_PLAN_READY**（2026-09-24 用户批复 B，校准轮重申） | 仅处置意向：批准进入**人工** fix 计划，auto_dispatch=false；**非 TicketStore 正式 approve**（正式审批仍需 D-B+合法 ticket+具名身份+24h TTL） |
| **CASE2-B 人工修复** | **MANUAL_FIX_VERIFIED**（2026-09-24 校准轮） | 补丁 `patch.diff`（SHA256 `6d9e9905…`，目标 `42ed1787`，3 文件 +167/−30）：git apply --check exit=0；修复后 **13 passed/1 skipped（symlink=Windows 特权）/0 failed**；未修复对照主 PoC 复现泄漏→修复后同向量 400 阻断；合法路径回归 3/3；[verify-report](../evidence/rpd-24h/case2b-fix/verify-report.md)。**人工复核≠生产 Verifier；补丁未推送目标仓库；正式派发/合并仍待 D-B+合法票据** |
| RAG-IMAGE-SYNC | **历史 DONE（2026-09-24）；本轮 FROZEN／NO_NEW_ACTION** | v6scope 镜像+agt 更新+容器内验收全过（真实 case-pg scope 查询 OK 4 found/107ms）；[exec-report](../evidence/rpd-24h/rag-image-sync/exec-report.md)。校准轮零镜像构建/零 agt 更新/零容器操作，不改写为未完成 |
| D-D | WAITING_FOR_CASE2_TICKET | leader 未写 marker→无合法 ticket，不补造 |
| D-B | **WAITING_HUMAN / DISABLED** | 未批；正式 approve/reject 未启用。**不阻塞人工漏洞修复的事实记录**，但产品化决策面因此保持 MANUAL-REQUIRED |
| 生产 Fixer/Verifier | **零启动（保持）** | 人工路线已验证；生产角色在 D-B+合法票据（或用户明确指示）前不得启动 |
| CL-08 | **RESOLVED_MANUAL_FIX** | 三选一已决=人工修复；模型路线本轮关闭（根因保留见 CL-08 节） |

处置意向决定 ≠ TicketStore 正式 approve/reject（后者需 D-B+合法 ticket+具名身份+24h TTL）。

## 人工决策登记（2026-09-23 复核轮）

| decision_id | 内容 | 状态 | 用户答复 |
|---|---|---|---|
| D-A | controller/Worker 环境注入 + case-pg 只读账号 + Worker 重启 | **APPROVED_EXECUTED**（夜轮批准；k8s 原生 PUT 完成，生产 preflight PASS；校准轮补登记，不重新执行） | 夜轮授权（见 night_round） |
| D-B | D-1/D-2 真实审批（动作子集/具名审批人/24h TTL/真实决策面/仅出计划） | **WAITING_HUMAN / DISABLED**（校准轮重申保持） | 无 |
| D-C | CASE2 运行授权（A1 空提交 / A2 flash 审查 / A3 rag-live / A4 单 check-run / A5 费用风险） | **APPROVED_EXECUTED**（夜轮批准 A1-A5；CASE2-B 已执行完毕；校准轮补登记，不重新执行） | 夜轮授权（见 night_round） |
| D-D | 具体票据的人工门决策 | **WAITING_FOR_CASE2_TICKET** | 无（票据未产生） |
| D-E | gh CLI 凭据通道（限本仓库 feature branch/禁输出 token/禁 push main/禁 merge） | **APPROVED_EXECUTED**（夜轮批准；NR-01 已 push，远端 tip=74a2782 核验；校准轮零 GitHub 写入） | 夜轮授权（见 night_round） |

批准格式：`批准 D-A` / `批准 D-C A1-A5` / `修改条件：…`；模糊表达（"继续/可以/开始"）不构成批准。
部分批准只执行对应范围，其余保持 WAITING_HUMAN，整体状态保持 MANUAL-REQUIRED。
D-E 明确决定前：**零新的 GitHub 写入**（本复核轮的 RPD 更新仅本地提交，不推送）。

## D-A 解锁（2026-09-24 调查修复轮）：**正式接线完成，PRODUCTION PREFLIGHT PASS**

根因=**agt apply CLI 丢弃 spec.env**（controller 本身支持：223ddc2 `member_reconcile.go:923` 调用
`mergeUserEnv(workerEnv, m.Spec.Env, ...)`）。修复=经 kube-apiserver 原生 PUT 写入 spec.env 两键
（D-A 已批准的注入内容），controller 检测 Env 变化自动重建 reviewer 容器，env 注入实测=2，
容器内 preflight 全绿（只读角色/超时/表能力/scope 信任）。**本轮按指令未执行 CASE2**；
A1-A5 授权保留。回滚=apiserver PUT 移除 spec.env 两键+容器重建。

## 夜轮结果（2026-09-24 收口）：BLOCKED → 后继轮已按新授权完成 CASE2-B（见上节）

## 状态校准轮（2026-09-24T02:54Z）：文档收口 + CASE2-B 人工修复验证

范围=仅状态/证据索引/报告文件，零业务代码修改，不触碰人工补丁文件本体。动作：
① CASE2-B 补丁人工验证（临时一次性 clone @42ed1787，验证后销毁；git apply --check exit=0；
修复后 13 passed/1 skipped/0 failed；未修复对照=主 PoC 复现泄漏→修复后同向量 400 阻断）→
[verify-report](../evidence/rpd-24h/case2b-fix/verify-report.md)；② 用户决策登记（HIGH-DECISION 重申/
CL-08 人工修复/D-B 保持/RAG 冻结/D-A·D-C·D-E 补登记）；③ 三源同步（yaml/json/md）+ final-report 追述；
④ HEAD 分层实读登记——校准轮起点 **74a2782**；**并行人工补丁会话**（CASE2-B 修复，自身授权=夜轮 D-E 通道）同窗追加 ec66209/81e2b25/2e98d4e 并已 push（含隔离容器验证证据 fix-report/poc-rerun/test-output），与本轮临时 clone 独立复核（verify-report）构成**双验证链**、结论一致；远端 pushed_head=**2e98d4e**；校准轮自身零 GitHub 写入，收口提交后 local 领先 pushed；⑤ 模型请求/token 本轮新增 **0**（并行会话亦为人工路线零模型）。


## 最终状态（2026-09-24 校准轮口径）：**MANUAL-REQUIRED**

- CASE2-B 人工修复验证完成（子任务级 **MANUAL_FIX_VERIFIED**：apply 干净 + PoC 阻断 + 合法回归 + 13/1s/0f，见 [verify-report](../evidence/rpd-24h/case2b-fix/verify-report.md)）；
- 但 **D-B 正式审批启用仍待决** → 整个产品化决策面继续 **MANUAL-REQUIRED**，不为变绿隐藏待决项；
- 仍待人工决策：①D-B（真实审批启用）②D-D（等合法票据）③补丁去向（推送目标仓库/仅存档/关闭语义，需用户明确指示）④PR #233 拆分方案 A/B 与 merge 决定；
- 本轮（校准）：零模型请求/零 token；未 push、未 merge、未改 PR；生产 Fixer/Verifier 零启动；RAG-IMAGE-SYNC 冻结零操作。
- PR：https://github.com/nghqqa/MergePilot/pull/233（OPEN，pushed_head=2e98d4e；本轮收口提交未 push，local 领先 1 提交）

## 已知事实 → 待证据复核映射（全部已复核）

| 已知事实 | 复核任务 | 命令 |
|---|---|---|
| pgvector smoke 11/11 | RPD-02 | 复核脚本+commit 记录 |
| TicketStore 13/13 + 26 单测 | RPD-04 | gate_ticket_smoke.py + pytest |
| 回归 281 / PG 25-26 | RPD-05 | pytest 四目录 + MERGEPILOT_PG_CONTRACT=1 |
| case_retrieval 接线 9/11→待重验 | RPD-03 | pytest test_case2_fixes + validate_env |
