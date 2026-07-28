#!/bin/bash
# approve.sh — B4d L2 审批 CLI(host-only,mergepilot_approver 账号)。
#
# 用法:
#   approve.sh list                 列 PENDING 票据(经 l2_pending_list,只读)
#   approve.sh show <ticket_id>     展示单张 PENDING 票据(repo/PR/head SHA/action/TTL 剩余)
#   approve.sh approve <ticket_id>  审批 PENDING→APPROVED(approved_by 自动取 id -un@hostname)
#
# 安全边界(设计 docs/M3-B4 §B4d):
#   - 仅以 mergepilot_approver 连 audit-pg(EXECUTE-only l2_pending_list/l2_approve;
#     无任何表 SELECT/INSERT/UPDATE —— 见 m3b-b4-create-roles.sh 自检)。
#   - approved_by 由 CLI 从 $(id -un)@$(hostname) 派生,**不接收任何参数**(防伪造审批人)。
#   - 密码从 /home/ngh/.config/mergepilot/b4-roles.env 读,经 PGPASSWORD 传 psql,**绝不回显/入日志**。
#   - 拒绝路径由 l2_approve CAS 兜底:非 PENDING / 过期(approval_expires_at<=now) / 重复 / 不存在 → FALSE。
#   - ticket_id 走 psql 变量 :'tkt' 自动转义 + 格式白名单,防注入。
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
    echo "PENDING L2 tickets (as $APV_USER @ audit-pg):"
    apsql -c "SELECT ticket_id, action, repo || '#' || pr_number AS repo_pr,
                     created_at, approval_expires_at,
                     date_trunc('second', approval_expires_at - now()) AS ttl_remaining
              FROM l2_pending_list() ORDER BY created_at;"
    ;;

  show)
    TKT="${2:-}"
    valid_ticket "$TKT" || { echo "ERROR: ticket_id 非法(期望 tkt-<uuid>)" >&2; exit 2; }
    # l2_pending_list 只返 PENDING;非 PENDING/不存在 → 0 行。TKT 已格式校验,直嵌安全。
    ROW=$(apsql -A -F $'\t' -t -c "SELECT ticket_id, run_id, action, repo, pr_number, expected_head_sha, target_branch, canonical_payload::text, args_hash, attempt_no, created_at, approval_expires_at, date_trunc('second', approval_expires_at - now()) FROM l2_pending_list() WHERE ticket_id = '$TKT';")
    if [ -z "$ROW" ]; then
      echo "ticket $TKT 不在 PENDING 列表(不存在 / 已审批 / 已过期 / 已失败)。approver 仅可见 PENDING。"
      exit 1
    fi
    IFS=$'\t' read -r tid run action repo pr head target payload ahash attempt created expires ttl <<< "$ROW"
    echo "ticket:    $tid"
    echo "run:       $run"
    echo "action:    $action"
    echo "repo:      $repo"
    echo "PR:        #$pr"
    echo "head SHA:  $head"
    echo "target:    $target"
    echo "payload:   $payload"
    echo "args_hash: $ahash"
    echo "attempt:   $attempt"
    echo "created:   $created"
    echo "expires:   $expires  (PENDING 审批期 24h)"
    echo "TTL:       $ttl remaining"
    ;;

  approve)
    TKT="${2:-}"
    valid_ticket "$TKT" || { echo "ERROR: ticket_id 非法(期望 tkt-<uuid>)" >&2; exit 2; }
    # approved_by 由 CLI 派生,不接受参数(防伪造);校验仅含安全字符后直嵌(防注入)
    APPROVED_BY="$(id -un)@$(hostname)"
    [[ "$APPROVED_BY" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo "ERROR: derived approved_by 含非法字符: $APPROVED_BY" >&2; exit 2; }
    RES=$(apsql -A -t -c "SELECT l2_approve('$TKT', '$APPROVED_BY')::text;")
    # ::text 给规范 "true"/"false";无 ::text 时 psql 显示 "t"/"f"。两者都认。
    if [[ "$RES" =~ ^(t|true)$ ]]; then
      echo "✓ APPROVED $TKT"
      echo "  approved_by = $APPROVED_BY  (派生自 id -un@hostname,不可参数伪造)"
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
    echo "  approved_by 自动取 \$(id -un)@$(hostname),不可参数注入。" >&2
    exit 2
    ;;
esac
