# M3-B4d / B4d.1 证据 · approve CLI(host-only,mergepilot_approver,session_user 强身份)

> **L2 审批 CLI:list / show / approve;approved_by 由 DB `session_user` 写入(不可伪造);approver 仅 EXECUTE 2 函数。**
> 验证日期:2026-07-28 | 标签:`m3b-b4d-closed`(B4d,1df86cf)→ **`m3b-b4d.1-closed`(B4d.1 hardening)**
> 关联设计:[M3-B4 §B4d](../../docs/M3-B4-审批票据与Action-Outbox设计.md) | 关联代码:`tools/approve.sh` + `tools/audit-db/m3b_b4d1.sql`

## B4d.1 hardening(针对 B4d 复审 P1/P2)

B4d 的 `approved_by` 由 CLI 从 `id -un@hostname` 派生并作参数传入 `l2_approve(tkt, by)` —— 持 approver 密码者可绕过 CLI 直调 `l2_approve('tkt','evil')` 冒名。B4d.1 修复:

- **`m3b_b4d1.sql`**:`l2_approve` 函数体改写 `approved_by = session_user`(SECURITY DEFINER 下 = 认证登录角色),**忽略** `p_approved_by`(保留参数 + 加 DEFAULT NULL,**签名不变** ⇒ B4a frozen allowlist 仍有效)。
- ⇒ 直调 `l2_approve('tkt','EVIL@FORGED')` 只能记自己的 `session_user`;冒名需受害者的 DB 密码。**逐人身份 = 每人一个 LOGIN 角色授予 EXECUTE**(测试用第二角色 `mergepilot_approver_alt` 演示)。
- `approve.sh`:移除 `id/hostname` 派生,`approve` 调 `l2_approve('$TKT')`(1-arg);显示 `session_user` 作为记录的身份。
- **P2 严格参数**:`approve <tkt>` 多余参数(含 `--by`)→ `exit 2`,票保持 PENDING。
- **P1 测试完整性**:缺票显式 `bad`(不静默跳过);结尾 `[ PASS -eq 18 ] && [ FAIL -eq 0 ]`。
- **P2 fixture 清理**:trap 经 `gh.exe`(WSL→Windows interop)关 fixture 上所有 open B4d PR + 删分支。
- **文档/格式**:修 README 断链、修 `:'tkt'` 注释(实际校验后直嵌)、`list` 把过期 PENDING 标 `EXPIRED`(不可审批)、证据文件去尾随空白。

## 落地

### `tools/approve.sh`(host-only,经 `mergepilot_approver` 连 audit-pg)
- **list**:`l2_pending_list()` + `approvable` 列(`approval_expires_at > now`?过期标 `EXPIRED`)。
- **show <ticket_id>**:单票详情(repo/PR/head SHA/action/TTL/approvable);非 PENDING/不存在 → 告不可见。
- **approve <ticket_id>**:`SELECT l2_approve(:ticket)`;**严格 1 参数**;`approved_by = session_user`(DB 写,CLI 不传、不可伪造)。
- 拒绝由 `l2_approve` CAS 兜底:非 PENDING / 过期 / 重复 / 不存在 → FALSE(exit 1);非法 ticket_id → exit 2。
- 凭证:`PGPASSWORD` 传 psql,绝不回显(凭证扫描确认)。

### 安全边界(`mergepilot_approver`,见 `m3b-b4-create-roles.sh`)
LOGIN + NOINHERIT/NOSUPERUSER/NOBYPASSRLS;全 membership/表/序列权限 REVOKE;仅 `EXECUTE l2_pending_list(), l2_approve(text,text)`。

## 验证(18/18 PASS · fixture 隔离)

`tools/m3b-b4d-approve.sh`:source `e2e-lib.sh` + `e2e_guard`,fixture 仓经 `policy-gw-e2e` 建真实 PR → `l2_create_ticket` 产 PENDING 票,再 exercise `approve.sh`。

| # | 场景 | 关键断言 |
|---|---|---|
| 1 | list/show/approve 正向(6) | list 列出;show repo+approvable=yes;approve→APPROVED;DB 状态;**approved_by=mergepilot_approver(session_user)**;expires_at |
| 2 | 身份不可伪造(2) | 直调 `l2_approve('tkt','EVIL@FORGED')` → approved_by 仍为 session_user;第二角色审批 → approved_by=mergepilot_approver_alt |
| 3 | 拒绝路径(5) | 重复 / 过期(状态仍 PENDING)/ list 标 EXPIRED / 不存在 / 非法格式 |
| 4 | 严格参数(1) | `approve <tkt> --by=…` → exit 2 且票保持 PENDING |
| 5 | approver 边界(3) | SELECT/INSERT denied;EXECUTE pending_list OK |
| 6 | 凭证扫描(1) | list/show 输出不含密码 |

**结尾门**:`[ PASS -eq 18 ] && [ FAIL -eq 0 ]`(防"少跑仍绿")。

### 证据文件
- `b4d-test.out`:18/18,含 list/show/approve 原始输出 + DB 断言 + session_user 验证。
- `list-out.txt` / `show-out.txt`:approve.sh 输出样例(凭证扫描输入)。
- `credential-scan.txt`:无泄漏结论。

## 后续
- **B4e**:review→fix→verify→**approve**→drain→merge 总 E2E + 崩溃恢复录像(approve CLI 接入正向链)。
- **B5**:负向证据 8 项。
