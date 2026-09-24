# RPD-04 TicketStore 人工门闭环复核（2026-09-23 ~16:10 UTC）

| 契约项 | 状态 | 证据 |
|---|---|---|
| marker → pending ticket 幂等 | ✅ | smoke a（重复 marker 同票，created=False） |
| run/repo/head 绑定 + task/severity（哈希） | ✅ | smoke b + 单测 binding |
| approve/reject/expire CAS 先到先得 | ✅ | smoke c（跨连接竞争唯一赢家） |
| 24h TTL | ✅ | smoke e（内部转移持久化 EXPIRED）+ test_ttl_default_is_24h |
| append-only audit（每次尝试留痕） | ✅ | smoke g（3 行：拒/成/拒） |
| 重复决策不覆盖历史 | ✅ | smoke d + 单测 approve_then_reject_refused |
| bridge 只建票 | ✅ | smoke h（结构性断言）+ CASE2 轮桥代码 |
| approve → APPROVED_PLAN_READY（计划数据，不派发） | ✅ | smoke e2e |
| reject → BLOCKED / expire → CLOSED_EXPIRED | ✅ | smoke e2e |
| 无身份 fail-closed（IDENT_REQUIRED） | ✅ | smoke f |

原始输出：smoke-output.txt / pytest-output.txt（28 passed）
禁止项复核：本轮零真实 approve/reject、零 fixer/verifier 唤醒、零共享票据修改、零 GitHub 写入。
