# FXV 生产编排（Finding → Ticket → Fixer → Test → Verifier → Audit）

> 2026-09-26 核心能力闭环轮引入。代码：`console/backend/lib/fxv/`；测试：
> `console/backend/test/fxv-unit.test.mjs`（19 项，内存替身）+
> `console/backend/test/fxv-pg.integration.mjs`（18 项，真 PG + 真 git + 本地 bare remote，
> 覆盖 11 个端到端回归场景）。

## 配置（fail-closed）

| 环境变量 | 默认 | 语义 |
|---|---|---|
| `FXV_REPO_ALLOWLIST` | （无） | **必填**。空=拒绝运行一切修复动作 |
| `FXV_BRANCH_ALLOWLIST` | `*` | 分支白名单 |
| `FXV_DRY_RUN` | `1`（开） | 默认只做隔离应用+验证，不产生真实写 |
| `FXV_GITHUB_WRITE` | `disabled` | 必须显式 `authorized` 才可能进入真实提交段 |
| `FXV_STEP_TIMEOUT_MS` | 60000 | 单步超时 → TIMEOUT 终态 |
| `FXV_APPROVAL_TTL_MS` | 24h | 审批/流程 TTL → EXPIRED 终态 |
| `FXV_GRANT_TTL_MS` | 1h | 写入授权 TTL |
| `FXV_MAX_RETRIES` | 2 | 步骤重试上限 |

## 状态机（每步持久化 + 全量审计 `fxv.audit_events`）

```
FILED → AWAITING_APPROVAL →(人工) APPROVED → PATCH_GENERATING → PATCH_READY
→ DRY_RUN_APPLY → DRY_RUN_VERIFIED → DRY_RUN_COMPLETE（dry-run 默认终态）
                                     ↘（dry_run=0）AWAITING_GITHUB_GRANT →(一次性授权) GRANTED
                                       → COMMITTING → COMMITTED → TEST_RUNNING → VERIFIED
终态：REJECTED / EXPIRED / STALE_HEAD / DIGEST_DRIFT / TEST_FAILED / ROLLED_BACK /
      TIMEOUT / ERROR_FATAL / DRY_RUN_COMPLETE / VERIFIED
人工等待：AWAITING_APPROVAL / AWAITING_GITHUB_GRANT / MANUAL_WAIT（handler 缺失/恢复需人工）
安全终态：EXPIRED / STALE_HEAD / DIGEST_DRIFT 可从任意非终态进入（恢复期与绑定违约）
```

## 不变量（绑定与门禁）

1. **立案绑定**：ticket/finding/repo/branch/base_head_sha/patch_digest 在 FILED 固化；
   之后任何转迁携带的 head/digest 与立案不符 → 自动转 STALE_HEAD / DIGEST_DRIFT 终态。
2. **仓库/分支白名单**：管线入口强制；越权 → ERROR_FATAL。
3. **补丁凭据形状闸**：补丁文本含真实凭据形状（ghp_/AKIA/私钥块/赋值）→ ERROR_FATAL
   （与 `scripts/secret-scan.sh` 同族模式，文档示例豁免）。
4. **dry-run 默认**：DRY_RUN_VERIFIED 后默认终态 DRY_RUN_COMPLETE；真实写需显式
   `FXV_DRY_RUN=0` + `FXV_GITHUB_WRITE=authorized` + 一张有效 grant。
5. **GitHub 写双闸**：配置闸（authorized）+ 授权闸（fxv.grants 具名操作员+repo+TTL，
   FOR UPDATE SKIP LOCKED 原子一次性消费）；disabled 模式即使存在可用授权也不消费。
6. **幂等**：同转迁重放返回 idempotent 不重复副作用；fileAttempt 以 ticket_id 幂等。
7. **并发**：乐观转迁（state=expected 条件更新），竞争者恰好一个非幂等赢家。
8. **fail-closed**：handler 未配置 → MANUAL_WAIT（绝不伪成功）；步骤异常/超时 → ERROR_FATAL/TIMEOUT。
9. **重启恢复**：`recover()` 重校验 TTL（EXPIRED）与 head（STALE_HEAD），可续跑的保留原状态；
   每个恢复动作审计。
10. **回滚**：COMMITTED/TEST_RUNNING 段失败且配置 rollback handler → 推回 base head → ROLLED_BACK。

## PG schema（console 首个自持命名空间 `fxv`，幂等初始化）

- `fxv.attempts` — 立案绑定 + 状态 + state_detail + attempts_count + expires_at
- `fxv.audit_events` — 全转迁/终态/人工等待/恢复审计（BIGSERIAL seq）
- `fxv.grants` — GitHub 写入显式授权（operator/repo/branch/expires_at/used_at）

## 集成回归 11 场景（`fxv-pg.integration.mjs`，18 断言全绿）

S1 成功链（dry-run，真 git 应用）/ S2 attempt 不存在 / S3 stale head / S4 越权仓库 /
S5 digest 漂移 / S6 隔离测试失败 / S7 并发 claim / S8 重启恢复（过期→EXPIRED、head 漂移→
STALE_HEAD、可续跑续至完成）/ S9 真实提交本地 bare 后失败→回滚（bare main 回到 base head）/
S10 补丁含凭据形状→拒绝 / S11 GitHub 写边界（无授权不可达真实写；disabled 不消费授权）。

**真实性边界**：测试的 git remote 为本地 bare 仓库路径，绝不触达 GitHub；
真实 GitHub 写路径的授权/消费逻辑已测，但对 github.com 的实际推送未在本测试中执行
（需人工授权 + 真实仓库，见能力分级）。
