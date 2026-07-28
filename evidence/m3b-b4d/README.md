# M3-B4d 证据 · approve CLI(host-only,mergepilot_approver 账号)

> **L2 审批命令行入口:list / show / approve;approved_by 不可参数伪造;approver 仅 EXECUTE 2 函数。**
> 验证日期:2026-07-28 | 标签:`m3b-b4d-closed`(SHA 见 [标签SHA映射](../../docs/标签SHA映射.md))
> 关联设计:[M3-B4 §B4d](../M3-B4-审批票据与Action-Outbox设计.md) | 关联代码:`tools/approve.sh`

## 落地

### `tools/approve.sh`(host-only,经 `mergepilot_approver` 连 audit-pg)
- **list**:`SELECT ... FROM l2_pending_list()` → PENDING 票据表(只读,经 SECURITY DEFINER 函数,不暴露表 SELECT)。
- **show <ticket_id>**:从 `l2_pending_list()` 过滤单票,展示 run/action/repo/PR/head SHA/target/payload/args_hash/attempt/created/expires/**TTL 剩余**。非 PENDING/不存在 → 仅告 "不在 PENDING 列表"。
- **approve <ticket_id>**:`SELECT l2_approve(:ticket, :approved_by)`;**approved_by 硬派生自 `$(id -un)@$(hostname)`,CLI 无任何参数入口**(`--by` 不存在;多余参数被忽略 → 实测伪造无效)。
- **拒绝路径由 DB CAS 兜底**:非 PENDING / 过期(`approval_expires_at<=now`)/ 重复 / 不存在 → `l2_approve` 返回 FALSE → CLI 退出 1;非法 ticket_id 格式 → CLI 退出 2(防注入)。
- **凭证**:密码从 `b4-roles.env` 读,经 `PGPASSWORD` 传 psql,**绝不回显/入日志**(凭证扫描确认)。

### 安全边界(沿用 B4a 的 `mergepilot_approver`,见 `m3b-b4-create-roles.sh` 自检)
- LOGIN + NOINHERIT/NOSUPERUSER/NOBYPASSRLS;全部 membership/表/序列权限 REVOKE。
- 仅 `GRANT EXECUTE ON l2_pending_list(), l2_approve(text,text)`。
- 反向:approver 调 `l2_claim_ticket` / 表 SELECT / INSERT → denied。

## 验证(16/16 PASS · fixture 隔离)

测试 `tools/m3b-b4d-approve.sh`:source `e2e-lib.sh` + `e2e_guard`,在 **fixture 仓** `nghqqa/MergePilot-e2e-fixture` 经 **测试 Gateway** `policy-gw-e2e` 建真实 fix PR → `l2_create_ticket` 产 PENDING 票,再 exercise `approve.sh`。

| # | 场景 | 关键断言 |
|---|---|---|
| 1 | list / show / approve 正向 | list 列出 PENDING;show 展示 repo/PR/head SHA/TTL;approve → APPROVED;DB 状态 + approved_by + 执行期 expires_at 全对 |
| 2 | approved_by 不可参数伪造 | 源码硬派生(无 `--by`/无 `$2..$9` 作 approved_by)+ 实测传 `evil@forged --by=admin@fake` 仍写入 host 身份 |
| 3 | 拒绝路径 | 重复审批(已 APPROVED)/ 过期 / 不存在 → exit 1;非法格式 → exit 2 |
| 4 | approver 权限边界 | SELECT/INSERT approvals → denied;EXECUTE l2_pending_list → OK |
| 5 | 凭证扫描 | approve.sh list/show 输出不含密码、无 PGPASSWORD/PASS 字样 |

### 证据文件
- `b4d-test.out`:16/16,含 list/show/approve 原始输出 + DB 断言。
- `list-out.txt` / `show-out.txt`:approve.sh 输出样例(凭证扫描输入)。
- `credential-scan.txt`:无泄漏结论。

## 后续
- **B4e**:review→fix→verify→**approve(approve.sh)**→drain→merge 总 E2E + 崩溃恢复录像(approve CLI 在此接入正向链)。
- **B5**:负向证据 8 项。
