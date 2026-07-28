# 标签 SHA 映射与冻结策略

> 维护日期:2026-07-28
> 用途:记录一次历史整理(commit 指针整体变化)的原因与旧→新 SHA 映射,并冻结现有标签。

## 背景:为什么 B1–B3.2 标签的 commit SHA 变了

历史上有若干 commit 在 message 末尾带了 `Co-Authored-By: Claude <noreply@anthropic.com>` trailer,GitHub 据此把 "claude" 显示为 contributor。为彻底清除 AI 标识,做了一次**两步历史整理**:

1. **filter-branch 重写**:剥离全部 commit message 中的 trailer(以及任何 `🤖`/`Claude Code`/`anthropic.com` 描述)。这一步改变几乎所有 commit 的 SHA。
2. **删除仓库 + 重建**:filter-branch 只重写分支/标签引用,**旧 commit 对象作为悬空对象仍留在 GitHub 对象库约 90 天**(直链可访问,仍显示原 trailer 的 co-author badge)。唯一能立即清空悬空对象的方法是删除仓库并从干净本地重建。这一步使重建后推送的标签指向**与 filter-branch 输出相同的 SHA**(本地未再变更的部分),整体与"整理前记录的 SHA"不一致。

因此:任何在 B1–B3.2 时代记录的 SHA(包括部分 commit body 里互相引用的旧 SHA)现在都已**失效**。本文件给出权威的当前映射。

## 当前标签 → commit SHA(权威,2026-07-26 重建后)

标签均为 annotated tag,有 **tag object SHA** 与指向的 **peeled commit SHA** 两个值。下表"commit SHA"列是 **peeled commit**(里程碑代码实际所在);`git rev-parse <tag>` 给的是 tag object,`git rev-list -1 <tag>` 给的是 peeled commit。

| 标签 | tag object SHA | **peeled commit SHA** | 备注 |
|---|---|---|---|
| `v0.4.0` | `4eb285f` | `d13eaae` | M2 收口 |
| `m3a-e2e-pass` | `e045b6d` | `8db5b9f` | M3-A E2E 闭合 |
| `m3a-evidence-closed` | `b84a541` | `72a7005` | M3-A 证据闭合 |
| `m3b-b1-closed` | `dfa3f88` | `710ac0a` | B1 封闭旁路 + 角色 token |
| `m3b-b2-closed` | `5020094` | `de6f09d` | B2 最小权限矩阵 |
| `m3b-b2-hardened` | `ee99059` | `eca5510` | B2.1 hardening |
| `m3b-b2.2-closed` | `7a68eb3` | `61c2d87` | B2.2 搜索逃逸 + 残留过权 |
| `m3b-b3-closed` | `6837ca3` | `618ac81` | B3 INSERT-only 审计 + 写 fail-closed |
| `m3b-b3.1-closed` | `286ae14` | `081dcd6` | B3.1 phase CHECK + 恢复幂等 + fail-fast |
| `m3b-b3.2-closed` | `661ebd1` | `9a4287c` | B3.2 set -e 回归修复 |
| `m3b-b4a-closed` | `b4e649e` | `4e4f766` | B4a DB + 函数 + 账号 |
| `m3b-b4a.1-closed` | `5e92ed7` | `5027271` | B4a.1 owner + payload 一致性 + 漂移收敛 |
| `m3b-b4a.2-closed` | `3ed91ab` | `d98df28` | B4a.2 pgvector owner 恢复 + payload/hash/TTL 封闭 + GRANT 顺序 + 精确 ACL |
| `m3b-b4a.3-closed` | `f8f1c84` | `242f24e` | B4a.3 完整签名 allowlist + pg_depend pgvector + JSON 类型校验 |
| `m3b-b4b-closed` | `365f873` | `1d30874` | B4b Gateway L2 claim 流 + 故障路径覆盖 + FAULT_INJECT test_mode 门(15+13/28) |
| `m3b-b4c-closed` | `ee298b6` | `e6fbfb5` | B4c Controller L2 审批闭环(发现→建票→drain→对账)+ 并发/崩溃/E2E(42/42) |
| `m3b-b4d-closed` | `465c35c` | `1df86cf` | B4d approve CLI(host-only,mergepilot_approver,approved_by 不可伪造,16/16 fixture) |
| `m3b-b4d.1-closed` | `785258f` | `b4456da` | B4d.1 l2_approve session_user 强身份(approved_by 真不可伪造)+ 严格参数/测试完整性/fixture 清理(18/18) |
| `m3b-b4c.1-closed` | `5c0d603` | `6fe8fcb` | B4c.1 收敛调度加固:确定性拒绝 + typed outcome + 公平调度 + 工作预算 + Gateway circuit breaker/降级(26/26 fixture) |
| `m3b-b4c.1.1-closed` | `4d02117` | `5c8349e` | B4c.1.1 修正(B4c.1 复审 7 P1):RETRY 重排/reconcile 公平/breaker 全覆盖/预算硬边界/deny CAS/reason 正则/预检 migration(16/16 fixture) |
| `m3b-b4c.1.2-closed` | `c2fd828` | `bd3a07c` | B4c.1.2 修正(B4c.1.1 复审 3 P1):deny 完整 CAS(锁 task 先)/breaker 覆盖 initiate+reconcile(helpers 透传 Unavailable+Denied)/硬预算(_gw_timeout_for≤剩余+read_branch deadline+config 校验)(13/13,fixture 0 PR/0 branch 硬门) |

## 旧 SHA 引用 → 当前 peeled commit(commit body 里互相引用的、现已失效的)

| commit body 里引用的旧 SHA | 对应标签 | 当前实际 peeled commit |
|---|---|---|
| `m3b-b2-closed (07af4cc)` | m3b-b2-closed | `de6f09d` |
| `m3b-b2-hardened (743070f)` | m3b-b2-hardened | `eca5510` |
| `m3b-b3-closed (07f5480)` | m3b-b3-closed | `618ac81` |
| `m3b-b3.1-closed (fa5fa70)` | m3b-b3.1-closed | `081dcd6` |

这些旧 SHA 出现在某些 commit 的 message 文本里(如"Does NOT move m3b-b3-closed (07f5480)"),仅作历史说明,指向的对象已不存在。**追溯里程碑请用标签名**(标签名未变且权威),不要用 body 里的旧 SHA。

## 冻结策略(从 2026-07-26 起)

- **现有标签(m3a-* / m3b-* / v0.4.0)冻结,不再移动**。每个 closed 里程碑对应一个不可变标签;后续修正用**新增标签**(如 `m3b-b4a.1-closed`),不动旧标签。
- 后续如发现遗漏的 AI 标识需要再次整理历史,**优先在本地修复后重新整体推送**,并**同步更新本文件的 SHA 映射**;不再用 filter-branch + delete 这种重操作(已确保后续 commit 不带 AI 标识,见 `no-ai-identifiers-in-commits` 记忆)。
- 里程碑追溯:**以标签名为准**;SHA 仅供精确检索。
| `m3b-b4c.1.3-closed` | `69e826e` | `e78874d` | B4c.1.3:共享每-tick预算(_tick_take)+deny 3字段CAS+breaker恢复(_l2_gw_ok)+统一_gw_timeout_for+expiry/stranded LIMIT(6/6) |
| `m3b-b4c.1.4-closed` | `865e958` | `4be419b` | B4c.1.4:空阶段不消耗预算(_tick_ok/_tick_use)+expiry LIMIT 50+timeout max(rem,0)(7/7) |
| `m3b-b4c.1.5-closed` | `1ce8c69` | `cdd3337` | B4c.1.5:expiry deadline guard + L2_EXPIRY_BATCH 参数化+校验(6/6) |
| `m3b-b4c.1.6-closed` | `d90c3d4` | `4e5813c` | B4c.1.6:mid-batch deadline guard(expiry/stranded 批内逐项检查,5/5) |
| `m3b-b4c.1.6a-closed` | `356d004` | `330d752` | B4c.1.6a:evidence-only — mid-batch deadline 测试确定性命中(monkeypatch;不动 1.6) |
| `m3b-b4e-closed` | `26d0984` | `63301e8` | B4e:总 E2E(review→fix→verify→approve→drain→MERGED)+崩溃恢复 + UNKNOWN/EXECUTING 对账 + Gateway 降级→恢复(breaker)+ Matrix 非 L2 循环存活(43/43,fixture 隔离,4×L2_CLAIMED/4×L2_COMPLETE 无双 claim) |
