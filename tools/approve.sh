#!/bin/bash
# approve.sh — B4d/B4d.1 L2 审批 CLI(host-only,mergepilot_approver 账号)。
#
# 用法:
#   approve.sh list                 列 PENDING 票据(经 l2_pending_list,只读;标记是否可审批)
#   approve.sh show <ticket_id>     展示单张 PENDING 票据(repo/PR/head SHA/action/TTL/可审批)
#   approve.sh approve <ticket_id>  审批 PENDING→APPROVED(approved_by = DB session_user,不可伪造)
#
# 安全边界(设计 docs/M3-B4 §B4d + B4d.1 hardening):
#   - 仅以 mergepilot_approver 连 audit-pg(EXECUTE-only l2_pending_list/l2_approve;
#     无任何表 SELECT/INSERT/UPDATE —— 见 m3b-b4-create-roles.sh 自检)。
#   - **approved_by 由 DB 函数 l2_approve 用 session_user(认证登录角色)写入**(B4d.1);CLI 不传、
#     不接受任何参数。持 approver 密码者直调 l2_approve('tkt','evil') 也只能记自己的 session_user。
#     逐人身份 = 每人一个 LOGIN 角色授予 EXECUTE l2_approve。
#   - approve 严格参数:多余参数(含 --by 等)→ exit 2,票据保持 PENDING。
#   - 密码从 /home/ngh/.config/mergepilot/b4-roles.env 读,经 PGPASSWORD 传 psql,**绝不回显/入日志**。
#   - 拒绝路径由 l2_approve CAS 兜底:非 PENDING / 过期(approval_expires_at<=now) / 重复 / 不存在 → FALSE。
#   - ticket_id 格式白名单 ^tkt-[0-9a-f-]+$ + 校验后直嵌(防注入;psql -c 不做 :'var' 替换,故不依赖它)。
set -uo pipefail

ENVF=/home/ngh/.config/mergepilot/b4-roles.env
[ -f "$ENVF" ] || { echo "ERROR: 缺 $ENVF(先跑 m3b-b4-create-roles.sh)" >&2; exit 1; }
APV_USER=$(grep '^MERGEPILOT_APPROVER_USER=' "$ENVF" | cut -d= -f2-)
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' "$ENVF" | head -1 | cut -d= -f2-)
[ -n "$APV_PW" ] || { echo "ERROR: b4-roles.env 无 MERGEPILOT_APPROVER_PASS" >&2; exit 1; }
PG_DB=$(grep '^PG_DATABASE=' /home/ngh/.config/mergepilot/controller.env 2>/dev/null | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=${PG_DB:-mergepilot_audit}

# approver 视角的 psql(只准调两个函数;输出绝不包含密码)
apsql(){ docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U "$APV_USER" -d "$PG_DB" -P pager=off "$@"; }

valid_ticket(){ [[ "$1" =~ ^tkt-[0-9a-f-]+$ ]]; }

cmd="${1:-}"
case "$cmd" in
  list)
    echo "PENDING L2 tickets (as $APV_USER @ audit-pg; approvable = approval_expires_at>now):"
    apsql -c "SELECT ticket_id, action, repo || '#' || pr_number AS repo_pr,
                     (CASE WHEN approval_expires_at > now() THEN 'yes' ELSE 'EXPIRED' END) AS approvable,
                     created_at,
                     date_trunc('second', approval_expires_at - now()) AS ttl_remaining
              FROM l2_pending_list() ORDER BY created_at;"
    ;;

  show)
    TKT="${2:-}"
    valid_ticket "$TKT" || { echo "ERROR: ticket_id 非法(期望 tkt-<uuid>)" >&2; exit 2; }
    # l2_pending_list 只返 PENDING;非 PENDING/不存在 → 0 行。TKT 已格式校验,直嵌安全。
    ROW=$(apsql -A -F $'\t' -t -c "SELECT ticket_id, run_id, action, repo, pr_number, expected_head_sha, target_branch, canonical_payload::text, args_hash, attempt_no, created_at, approval_expires_at, (CASE WHEN approval_expires_at > now() THEN 'yes' ELSE 'EXPIRED-not-approvable' END), date_trunc('second', approval_expires_at - now()) FROM l2_pending_list() WHERE ticket_id = '$TKT';")
    if [ -z "$ROW" ]; then
      echo "ticket $TKT 不在 PENDING 列表(不存在 / 已审批 / 已失败)。approver 仅可见 PENDING。"
      exit 1
    fi
    IFS=$'\t' read -r tid run action repo pr head target payload ahash attempt created expires approvable ttl <<< "$ROW"
    echo "ticket:     $tid"
    echo "run:        $run"
    echo "action:     $action"
    echo "repo:       $repo"
    echo "PR:         #$pr"
    echo "head SHA:   $head"
    echo "target:     $target"
    echo "payload:    $payload"
    echo "args_hash:  $ahash"
    echo "attempt:    $attempt"
    echo "created:    $created"
    echo "expires:    $expires  (PENDING 审批期 24h)"
    echo "TTL:        $ttl remaining"
    echo "approvable: $approvable"
    ;;

  approve)
    # 严格参数:仅 approve <ticket_id>。多余参数(含 --by 等)→ exit 2,票保持 PENDING。
    if [ $# -ne 2 ]; then
      echo "ERROR: approve 仅接受 1 个参数(ticket_id)。多余参数被拒 → 票据保持 PENDING(防伪造审批人)。" >&2
      echo "  用法: approve.sh approve <ticket_id>  (approved_by 由 DB session_user 写,无 --by 入口)" >&2
      exit 2
    fi
    TKT="$2"
    valid_ticket "$TKT" || { echo "ERROR: ticket_id 非法(期望 tkt-<uuid>)" >&2; exit 2; }
    # approved_by 由 DB 函数用 session_user 写(B4d.1),CLI 不传、不可伪造。1-arg 调用(p_approved_by DEFAULT NULL)。
    RES=$(apsql -A -t -c "SELECT l2_approve('$TKT')::text;")
    # ::text 给规范 "true"/"false";无 ::text 时 psql 显示 "t"/"f"。两者都认。
    if [[ "$RES" =~ ^(t|true)$ ]]; then
      WHO=$(apsql -A -t -c "SELECT session_user;")
      echo "✓ APPROVED $TKT"
      echo "  approved_by = $WHO  (DB session_user = 认证登录角色;不可参数伪造)"
      echo "  执行期默认 ~1h;Coordinator drain 将在 Gateway claim 后 merge/close。"
      exit 0
    else
      echo "✗ REFUSED $TKT (l2_approve 返回 FALSE)"
      echo "  原因:非 PENDING / 已过期(approval_expires_at<=now) / 已审批 / 不存在。"
      echo "  (approved_by 不会写入未迁移的票据。)"
      exit 1
    fi
    ;;

  *)
    echo "用法: approve.sh {list|show <ticket_id>|approve <ticket_id>}" >&2
    echo "  approved_by 由 DB session_user 写(B4d.1),CLI 无 --by 入口。" >&2
    exit 2
    ;;
esac
