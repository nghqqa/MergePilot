# M3-B3 证据 · 不可变审计强化(INSERT-only 账号 + 写 fail-closed)+ B3.1 加固

> **gateway INSERT-only DB account · write fail-closed · append-only events · phase CHECK · fail-fast init**
> 验证日期:2026-07-26(B3 + B3.1)
> 标签:`m3b-b3-closed`(07f5480)/ `m3b-b3.1-closed`(本提交,证据闭合)

## 落地项

### 1. 独立数据库账号 `policy_gateway_audit`(INSERT-only)

- 仅授 `INSERT mcp_calls` + `CONNECT`/`USAGE schema`;显式 `REVOKE SELECT, UPDATE, DELETE, TRUNCATE`。
- 不碰 `approvals`/`policy_action_outbox`(B4 用单独账号)。
- 密码在 `/home/ngh/.config/mergepilot/audit-db.env`(chmod 600);`run-policy-gateway.sh` 读取构建 AUDIT_DSN。
- gateway 不再用 `mergepilot` 超管连审计库。

权限(authoritative `role_table_grants`):仅 `INSERT`。

### 2. 写操作审计 fail-closed

调用顺序(写工具 = comment ∪ fix ∪ l2 ∪ update_pull_request):
```
策略 ALLOW → 持久化 INTENT(必须成功)→ 调 GitHub → 追加 RESULT/ERROR
```
- INTENT 写失败 → `POLICY_DENIED reason_code=AUDIT_UNAVAILABLE`,**绝不在 INTENT 未持久化时调 GitHub**。
- 只读 / `tools/list` / 认证拒绝:保持 fail-open(尽力记,失败也放行)。

### 3. 追加式事件 + correlation_id

`mcp_calls` 加 `correlation_id` + `phase`(INTENT | RESULT | ERROR)。一次调用的 INTENT 与 RESULT 共享同一 correlation_id;**永远 INSERT,不 UPDATE 原行**。

### 4. 多仓库启动断言

`_inject_search_scope` 当前只支持单仓库 allowlist。lifespan 启动断言:
```python
if len(_GLOBAL_REPOS) != 1:
    raise RuntimeError("search scope requires exactly one allowlisted repo")
```
多仓库需结构化范围组合(未实现),启动即拒绝,避免静默放行未限定 query。

## B3 验收(11/11 PASS)— `b3-test.txt`

| # | 检查 | 结果 |
|---|---|---|
| 1 | policy_gateway_audit INSERT 成功 | ✅ |
| 2 | UPDATE/DELETE/TRUNCATE 被权限拒绝 | ✅ |
| 3 | 授权仅 INSERT(role_table_grants) | ✅ |
| 4 | create_branch 执行(INTENT 先于 GitHub) | ✅ |
| 5 | INTENT+RESULT 共享 correlation_id | ✅ |
| 6 | 审计无原始内容/token(只 args_hash,16 hex) | ✅ |
| 7 | L2 仍 L2_TICKET_REQUIRED(B3 不放行) | ✅ |
| 8 | 坏-DSN 下只读(get_me)仍可执行 | ✅ |
| 9 | 坏-DSN 下写 → AUDIT_UNAVAILABLE | ✅ |
| 10 | 坏-DSN 下未转发 GitHub(日志 0 forward) | ✅ |
| 11 | GitHub 上无该分支(确认无副作用) | ✅ |
| — | 测试失败非零退出 | ✅ exit 0 |

坏-DSN 测试用独立容器 `policy-gw-noaudit`(AUDIT_DSN 指向不可达 host),不破坏生产审计库;测完即停删。

## 证据样本 — `audit-evidence.txt`

```
corr=1959e448  INTENT   fixer  create_branch  ALLOW  POLICY_ALLOW
corr=1959e448  RESULT   fixer  create_branch  ALLOW  UPSTREAM_RESULT
grants(policy_gateway_audit on mcp_calls): INSERT
```

## 关键修正(踩坑)

| # | 问题 | 修复 |
|---|---|---|
| ① | DDL 用 `current_setting('pw')` 但 `-v pw=` 设的是 psql var 不是 GUC;DO block 内也不插值 → 角色没建成,密码却落盘 | 非引用 heredoc 让 shell 展开 `$PW`(token_urlsafe 安全字符集),`ON_ERROR_STOP=1` |
| ② | 自检 `INSERT ... RETURNING` 失败:RETURNING 需 SELECT 权限(已 revoke) | 自检去掉 RETURNING,直接验 INSERT 成功 |
| ③ | gateway 容器持有旧密码(首次 DDL 失败时的)→ 审计连不上 → 写全 fail-closed | DDL 修复后必须重建 gateway 读新 audit-db.env |
| ④ | mcp_calls 自检 `head -1` 被 wsl 噪声污染 → 假"allowed" | 改 grep 全输出 + 权威 `role_table_grants` 检查 |

## 范围

B3 已闭合:**INSERT-only 审计账号 + 写 fail-closed + 追加式 correlation_id 事件 + 多仓库启动断言**。
B3.1 已闭合:**phase CHECK 约束 + 恢复幂等收敛 + init fail-fast + 测试逐项断言/凭证扫描**。
待做:B4(审批票据 + policy_action_outbox,B4 用单独账号不复用审计账号)、B5(负向证据全集)。

---

## B3.1 加固(复审后补强,19/19 PASS)

复审发现 3 个 P1 + 2 个 P2,全部修复。

### P1 修复

| # | 问题 | 修复 |
|---|---|---|
| ① | 运行库无 `phase` CHECK(`CREATE TABLE IF NOT EXISTS` 不改已有表,迁移只 ADD COLUMN)→ 可插入任意 phase | 幂等 `DO $$ ... ADD CONSTRAINT mcp_calls_phase_check CHECK (phase IS NULL OR phase IN ('INTENT','RESULT','ERROR'))` + 同理 `mcp_calls_decision_check` |
| ② | `m3b-create-audit-role.sh` 在 audit-db.env 已存在时跳过 GRANT/REVOKE → 卷重建/权限漂移后无法收敛 | 始终幂等执行角色 + 授权收敛;`--force` 仅轮换密码;env 复用时同步密码 `ALTER ROLE` |
| ③ | `run-policy-gateway.sh` 无 `set -e`、psql 无 `ON_ERROR_STOP` → init 失败仍替换 gateway | `set -euo pipefail` + schema psql `ON_ERROR_STOP=1` 显式失败检查;init 全过才 `docker rm/run` |

### P2 修复

| # | 问题 | 修复 |
|---|---|---|
| ④ | 权限断言把 UPDATE/DELETE/TRUNCATE 拼接后 grep 一次 → 任一被拒就 PASS(假绿) | `chk_priv` 逐项断言:SELECT/UPDATE/DELETE/TRUNCATE 各自必须 `permission denied` |
| ⑤ | 脚本打印密码前 8 位 | 只输出 `pass=<REDACTED> len=N`;轮换 audit 密码 + 重启 gateway |

非阻断:`REVOKE CREATE, TEMP ON DATABASE mergepilot_audit FROM PUBLIC`(收窄 PUBLIC)。

### B3.1 新增验证(在 19/19 内)

```
SELECT/UPDATE/DELETE/TRUNCATE 各自 → permission denied  (逐项,4 项)
mcp_calls_phase_check 约束存在                         ✅
INSERT phase='BOGUS' → violates check                  ✅
drift(GRANT SELECT)→ 角色脚本(无 --force)→ 收敛回 INSERT-only  ✅
坏 schema 变体 → 脚本 exit 1 + gateway 容器未替换(fail-fast)  ✅
Bearer 明文扫描 → 计入 FAIL                            ✅
```

### 约束证据 — `b3.1-constraints.txt`

```
mcp_calls_decision_check | CHECK (decision IN ('ALLOW','DENY','ERROR'))
mcp_calls_phase_check    | CHECK (phase IS NULL OR phase IN ('INTENT','RESULT','ERROR'))
policy_gateway_audit     | INSERT  (唯一权限)
```

### 关键坑

| # | 问题 | 修复 |
|---|---|---|
| ① | `current_setting('pw')` 读 GUC,但 `-v pw=` 设 psql var;DO block 内不插值 → 角色没建成密码却落盘 | 非引用 heredoc 展开 `$PW` + `ON_ERROR_STOP=1` |
| ② | `INSERT...RETURNING` 需 SELECT 权限(已 revoke)→ 自检假失败 | 自检去 RETURNING |
| ③ | 首次 DDL 失败后 gateway 持旧密码 → 审计连不上 → 写全 fail-closed | 修 DDL 后重建 gateway 读新 env |
| ④ | `chk_deny` 直接 pipe 在 set -e/pipefail 下显示假 ALLOWED | var-capture 再 grep(与测试 chk_priv 一致) |
