# Wave 3 — 四 Agent 审查管线（MU 真实执行面）

## 链路

```
GitHub webhook（HMAC 验签 + delivery 去重）
  → event_sync job（入队需 repository_binding active）
  → tick/worker 执行（MU_FIXTURES=0 生产路径；执行前复查 installation/binding）
  → PR 快照落库
  → review_run 幂等创建（UNIQUE tenant+repo+pr+head_sha；新 head 必建新 run）
  → deterministic Reviewer（八类规则；expected_head_sha 防 TOCTOU）
  → Leader 策略裁定（确定性，非 LLM）
      clean → COMPLETED
      P2/P3 → needs_human（保持 REVIEWED，人工入口）
      P0/P1 → fix_required
      protection 未知/未配置 → BLOCKED（fail-closed）
  → [fix_required] FXV Fixer dry-run（真子进程；禁改 workflows/权限/secrets）
  → 独立 Verifier（fresh fetch@head + digest 复核 + apply + harness）
      PASS → Leader 终裁 COMPLETED
      FAIL → REWORK_REQUIRED → 回派 Fixer（MAX_FIX_ROUNDS=2 有界）
      超限 → BLOCKED + dead-letter（只存引用，不存正文）
  → review_record / orchestration_decision / audit 全程留痕
  → 只读 API（/api/mu/runs）+ 前端管线视图（MultiUserPage PR 详情）
```

## Provider 三态

| Provider | 用途 | 默认 |
|---|---|---|
| `deterministic` | 规则审查（reviewer-rules 八类）/ Leader 策略 | **默认启用** |
| `mock` | 测试注入（`__setGhProviderForTests` / `__WAVE3_TEST_DEPS`） | 仅测试 |
| `llm` | LLM Reviewer（`agents/llm.mjs`） | **fail-closed**：`MU_LLM_ENDPOINT/MU_LLM_MODEL/MU_LLM_API_KEY` 三者齐备才可用；缺一即 `LLM_NOT_CONFIGURED`，绝不静默外调。输出过 JSON schema 校验 + 16KiB 上限；只记 digest/usage；无 citation 不得 verified |

## 安全红线

- **修复仅 dry-run**：不写 GitHub、不建 commit、不 push、不改 `.github/workflows/**`/权限/branch protection/deployment secrets
- **无自动 approve / 自动 merge / GitHub required review 计入**——AI 结果永不构成 GitHub 侧审查通过
- **凭据纪律**：installation token / LLM key 仅内存；不入库、不入日志、不入审计、不进 finding
- **脱敏**：finding 只存打码摘要（`summary_masked`）；diff/源码正文仅内存流经；dead-letter 只存引用
- **TOCTOU**：`expected_head_sha` 校验——PR 推进即中止（stale_head），旧 head 结果绝不写入新 head
- **跨租户**：复合 FK（tenant+repo+pr+run）数据库层拒绝一切跨组合注入

## 配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `MU_FXV_TEST_CMD` | Verifier harness 命令（无 shell，按空格拆分） | `node -e process.exit(0)`（平凡通过——**真实部署必须配置**，默认值会在 evidence 中如实存在） |
| `MU_LLM_*` | LLM 三件套（endpoint/model/key） | 未设 → LLM 通道关闭 |

## 定位声明

四 Agent 已接入 Developer Edition Beta 的 MU 真实执行面。**不代表 Enterprise Production Ready**：不包含 RLS、SSO、SCIM、HA、配额限流、自动审批、自动合并、仓库写入或 branch protection 绕过。
