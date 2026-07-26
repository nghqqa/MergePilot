# M3-B2 证据 · 最小权限矩阵(deny-by-default)

> **tool-layer least privilege verified · fixer write-arg constraints enforced · L2 gated**
> 验证日期:2026-07-26
> policy: `b2-20260726-v1` / hash `e029676319129893`

## 策略分层(B2 后,44 个上游工具)

| 角色 | 可见工具数 | 可见类 | 关键工具可见性 |
|---|---:|---|---|
| reviewer | 26 | read | get_me ✓ / create_branch ✗ / merge ✗ |
| verifier | 26 | read | 同 reviewer |
| fixer | 40 | read + comment + fix | create_branch ✓ / merge ✗ |
| coordinator | 44 | read + comment + fix + l2 | merge ✓ |

deny-by-default:任何上游新增工具不在 policy.yaml 显式 allow,对所有角色默认不可见。

## 已验证(B2 测试 10/10 PASS)— `b2-test.txt`

| # | 检查 | 结果 |
|---|---|---|
| A | reviewer list 不含 merge/create_branch | ✅ 26 工具 |
| B | reviewer get_me → ALLOW | ✅ 返回 login |
| C | fixer list 含 create_branch,不含 merge | ✅ 40 工具 |
| D | fixer create_branch 分支=evil/x → DENY | ✅ BRANCH_NOT_FIX_PREFIX |
| E | fixer 写 main → DENY | ✅ BRANCH_PROTECTED |
| F | fixer 写 .env → DENY | ✅ PATH_DENIED |
| G | fixer 写非 allowlist 仓库 → DENY | ✅ REPO_NOT_ALLOWED |
| H | coordinator list 含 merge | ✅ 44 工具 |
| I | coordinator merge → DENY(B2 占位) | ✅ L2_TICKET_REQUIRED |
| J | 审计 DENY 行 | ✅ 9 条 |

## fixer 写操作约束(`_check_write_args`)

- **repo allowlist**:`nghqqa/MergePilot` 之外全拒(REPO_NOT_ALLOWED)
- **base allowlist**:PR base / 分支起点只允许 `main`(BASE_NOT_ALLOWED)
- **fix 前缀**:写分支必须 `fix/` 开头(BRANCH_NOT_FIX_PREFIX / HEAD_NOT_FIX_BRANCH)
- **受保护分支**:禁止直接写 `main`(BRANCH_PROTECTED)
- **路径 denylist**:`.env*` / `*.pem` / `*.key` / `secrets/**` / `.github/workflows/**` / `credentials*`(PATH_DENIED)
- **close 即 L2**:`update_pull_request(state=closed)` → L2_TICKET_REQUIRED

## 工具级拒绝的返回形式

MCP 层拒绝返回结构化错误(不是 HTTP 403):
```
POLICY_DENIED reason_code=BRANCH_NOT_FIX_PREFIX tool=create_branch
<is_error=true>
```
HTTP 401/403 只用于认证失败(B1)。

## 设计说明(透明记录)

1. **MCP SDK input validation 在策略检查之前**:若调用者发来的 args 类型不合 schema(如 pullNumber 传了字符串),SDK 在 gateway handler 之前就拒掉,不会写 POLICY_DENIED 审计。这不是策略旁路——调用不会到达 GitHub。对合规 args,策略检查正常执行。测试探针已做数字字面量转换规避此点。
2. **审计仍 fail-open(B1 语义沿用)**:审计写入故障不阻断业务。写/L2 的 fail-closed-on-audit-failure 留待 B3。
3. **L2 占位**:B2 对 coordinator 的 l2 工具一律返回 L2_TICKET_REQUIRED(尚无票据机制)。B4 接审批票据后,带有效 ticket 才放行。

## 文件清单

| 文件 | 内容 |
|---|---|
| policy.yaml | 版本化权限矩阵(44 工具分类 + 4 角色 + 全局约束) |
| b2-test.txt | 10/10 PASS 完整输出 |
| mcp_calls-audit.txt | 策略 DENY 审计样本 |

## 范围

B2 已闭合:**工具级 deny-by-default 矩阵 + fixer 写操作 arg 校验 + L2 占位拒绝**。
待做:B3(不可变审计强化 + 写/L2 fail-closed-on-audit)、B4(审批票据 + action_outbox)、B5(负向证据全集)。
