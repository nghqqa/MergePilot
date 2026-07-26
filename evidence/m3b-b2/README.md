# M3-B2 证据 · 最小权限矩阵(deny-by-default)+ B2.1 / B2.2 hardening

> **least privilege verified · search-escape closed · 12 tools disabled · field whitelist**
> 验证日期:2026-07-26(B2 / B2.1 / B2.2)
> policy: `b2.2-20260726-v1` / hash `d9ec07a7d9b4767f`
> 标签:`m3b-b2-closed`(07af4cc)/ `m3b-b2-hardened`(743070f)/ `m3b-b2.2-closed`(本提交,security-closed)

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
B2.1 hardening 已闭合:**3 个绕过修复 + update_pull_request 字段白名单 + 5 个工具全角色禁用 + repo allowlist 读覆盖 + search scope 约束**。
待做:B3(不可变审计强化 + 写/L2 fail-closed-on-audit)、B4(审批票据 + action_outbox)、B5(负向证据全集)。

---

## B2.1 hardening(代码审查后补强,22/22 PASS)

代码审查发现 3 个真实绕过 + 2 项策略不足,全部修复:

### 3 个绕过修复

| # | 绕过 | 根因 | 修复 |
|---|---|---|---|
| A | `create_branch(from_branch=evil)` 放行 | 代码查 `args.get("from")`,真实参数是 `from_branch` | 改 `args.get("base") or args.get("from_branch")` |
| B | coordinator 可无约束调 `create_or_update_file` 等 | coordinator 继承 `fix` 类且 `write_checks=false` | coordinator 收敛为 `[read, comment, l2]`,不再继承 fix |
| C | reviewer/verifier 可读/搜任意仓库 | repo allowlist 只在 fixer 写校验里 | 提为 call_tool 步骤 2 全局校验:所有带 owner+repo 的工具(含读)统一受 allowlist 约束 |

### update_pull_request 字段白名单(混合风险工具,参数级授权)

不归入任何工具类,通过 `extra_tools` 单独授权 + 角色字段规则:

| 角色 | 允许字段 | 其他字段 |
|---|---|---|
| fixer | `title` / `body` | `PR_FIELD_NOT_ALLOWED` |
| fixer/coordinator | `state`(任意值) | `L2_TICKET_REQUIRED`(B4 票据) |
| coordinator | 仅 identity | `PR_FIELD_NOT_ALLOWED` |
| reviewer/verifier | — | `TOOL_NOT_ALLOWED` |

未来上游新增字段自动拒绝(白名单语义,deny-by-default)。

### 5 个工具全角色禁用(disabled 类)

| 工具 | 理由 |
|---|---|
| `create_repository` | 无业务用例(MergePilot 处理现有 allowlist 仓库);资源/所有权/治理问题 |
| `fork_repository` | 可复制私有代码,数据扩散风险 |
| `search_repositories` | 全局搜索,当前流程不需要 |
| `search_users` | 全局搜索,当前流程不需要 |
| `assign_copilot_to_issue` | 启动外部 Copilot coding agent,可能产生真实代码/PR |

理由:审批票据只证明"有人批准",不能替代"业务是否需要这个能力"。最小权限 = 没有业务用例就不授予,即使有票据也不授予。

### search scope 约束

`search_code` / `search_commits` / `search_issues` / `search_pull_requests` 的 `query` 必须含 `repo:<allowlist>` 限定符,且所有 `repo:` 限定符都在 allowlist 内(防 `repo:allowlist-X` 前缀绕过)。无 scope → `SEARCH_SCOPE_NOT_ALLOWED`。

### B2.1 新增验证(在 22/22 内)

```
create_branch(from_branch=evil)       → BASE_NOT_ALLOWED       ✅
update_pull_request(state=open)       → L2_TICKET_REQUIRED     ✅
update_pull_request(base=evil)        → PR_FIELD_NOT_ALLOWED   ✅
update_pull_request(draft=true)       → PR_FIELD_NOT_ALLOWED   ✅
update_pull_request(title=...)        → ALLOW(字段白名单通过) ✅
coordinator create_or_update_file     → TOOL_NOT_ALLOWED       ✅
fixer assign_copilot_to_issue         → TOOL_NOT_ALLOWED       ✅
reviewer 读非 allowlist repo          → REPO_NOT_ALLOWED       ✅
reviewer search_code 无 repo:         → SEARCH_SCOPE_NOT_ALLOWED ✅
reviewer search_code repo:allowlist   → ALLOW                  ✅
create_repository / fork_repository 对 coordinator 不可见        ✅
```

### 角色可见工具数(B2.1 后)

| 角色 | 可见数 | 变化 |
|---|---:|---|
| reviewer | 24 | -2(移除 search_repositories/users) |
| fixer | 37 | -3(assign_copilot + search_repos/users),+update_pull_request(extra) |
| coordinator | 32 | -12(移除整个 fix 类 + create/fork_repository) |

### B4 待绑

`update_pull_request` / `create_pull_request` 的 `pullNumber` 应绑定到当前 `run_id` 创建的修复 PR(B4),否则 fixer 仍能修改 allowlist 仓库中的其他 PR。

---

## B2.2 hardening(第二轮代码审查后补强,25/25 PASS)

复审发现 1 个确定的搜索逃逸 + 一组残留过权,全部修复。

### 阻断项 1:搜索 Boolean 逃逸(已闭合)

`_search_scoped_ok` 只验 `repo:` 存在且在 allowlist,但 GitHub 支持 `OR`/`NOT`/括号,`repo:allowlist OR password` 能逃出 scope。

**修复:不信任调用者 scope。** query 只允许纯术语:
- 含冒号(任何 `word:` 限定符)→ `SEARCH_QUALIFIER_FORBIDDEN`
- 含括号 → `SEARCH_QUALIFIER_FORBIDDEN`
- 含 `OR`/`NOT`/`AND` → `SEARCH_OPERATOR_NOT_ALLOWED`
- **gateway 自己注入** `repo:<allowlist>` 后转发(`_inject_search_scope`)

```
password                          → ALLOW(gateway 注入 repo:nghqqa/MergePilot)
repo:nghqqa/MergePilot password   → SEARCH_QUALIFIER_FORBIDDEN  (用户自带 scope)
repo:allowlist OR password        → SEARCH_QUALIFIER_FORBIDDEN  (布尔逃逸,闭合)
password OR secret                → SEARCH_OPERATOR_NOT_ALLOWED
repo:allowlist OR repo:evil/x     → SEARCH_QUALIFIER_FORBIDDEN
```

### 阻断项 2:残留过权(已禁用)

| 工具 | 原归属 | 禁用理由 |
|---|---|---|
| `issue_write` / `sub_issue_write` | fix | 改 Issue 状态/层级,当前修复流程不用,未绑 run_id |
| `update_pull_request_branch` | fix | 真实更新 PR 分支,未绑 PR+head SHA(B4 绑定后可重开) |
| `get_teams` / `get_team_members` | read | 组织级读,当前流程不需要 |
| `list_issue_fields` / `list_issue_types` | read | 可省略 repo 的元数据读,组织级范围 |

disabled 类共 12 个工具,对所有角色不可见。

### 测试脚本加固

- 末尾 `[ "$FAIL" -eq 0 ] || exit 1`:FAIL>0 非零退出(CI/证据收集不可接受失败)。
- 审计按 `ts > 测试起点` 过滤,不再累计历史 DENY。

### B4 绑定范围(修正)

`create_pull_request` 无 `pullNumber` 输入。正确流程:`head=fix/<run_id>-* → 建 PR → 记录 pullNumber + head SHA → 后续 update/comment/review/merge 全部绑定该 PR`。B4 须覆盖所有带 PR/Issue 编号的写操作,不只 `update_pull_request`。

### 角色可见工具数(B2.2 后)

| 角色 | 可见数 |
|---|---:|
| reviewer | 19 |
| fixer | 31 |
| coordinator | 28 |
