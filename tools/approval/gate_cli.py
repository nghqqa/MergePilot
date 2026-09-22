"""审批票据 CLI——M2 票据的操作面(签发/批准/拒绝/查看)。

定位:D-1/D-2/D-3 拍板前,这是票据机制的**可测试操作工具**,不是产品审批入口;
它不接任何真实执行路径(执行方必须经 check_execution 且启用集非空)。
未来门 Web 页消费同一 SqliteTicketStore,语义与状态机完全一致。

用例:
  python gate_cli.py new  --db t.db --run run-1 --repo o/r --head <40hex> \
      --action generate_patch --params '{"k":"v"}' --patch-fp <64hex> \
      [--finding-id F1] [--expires "2026-09-23T12:00:00+00:00"]
  python gate_cli.py approve --db t.db --ticket <id> --actor alice
  python gate_cli.py reject  --db t.db --ticket <id> --actor alice
  python gate_cli.py show    --db t.db [--ticket <id>]
  python gate_cli.py check   --db t.db --ticket <id> --run run-1 --repo o/r \
      --head <40hex> --params '{"k":"v"}' --patch-fp <64hex>   # 执行前校验(红线)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from .approval import Binding, ExecutionRequest, canonical_hash, check_execution
from .store_sqlite import SqliteTicketStore


def _now_iso():
    """真实 UTC 时间(ISO 字符串;定长格式下与票据 expires 的字典序=时间序)。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _expires_or_none(args):
    return args.expires if getattr(args, "expires", None) else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gate_cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_db(p):
        p.add_argument("--db", required=True)

    p = sub.add_parser("new")
    add_db(p)
    p.add_argument("--run", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--params", required=True, help="JSON 对象文本")
    p.add_argument("--patch-fp", default=None)
    p.add_argument("--finding-fp", default=None)
    p.add_argument("--finding-id", default=None)
    p.add_argument("--expires", default=None, help="ISO-8601,如 2026-09-23T12:00:00+00:00")
    p.add_argument("--attempt", type=int, default=1)

    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        add_db(p)
        p.add_argument("--ticket", required=True)
        p.add_argument("--actor", required=True)

    p = sub.add_parser("show")
    add_db(p)
    p.add_argument("--ticket", default=None)

    p = sub.add_parser("check")
    add_db(p)
    p.add_argument("--ticket", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--params", required=True)
    p.add_argument("--patch-fp", default=None)

    a = ap.parse_args(argv)
    store = SqliteTicketStore(a.db)
    try:
        if a.cmd == "new":
            params = json.loads(a.params)
            b = Binding(run_id=a.run, repo=a.repo, head_sha=a.head,
                        action=a.action, params_hash=canonical_hash(params),
                        patch_fingerprint=a.patch_fp,
                        finding_fingerprint=a.finding_fp,
                        finding_id=a.finding_id)
            t, created = store.create(b, attempt_no=a.attempt,
                                      approval_expires_at=_expires_or_none(a))
            print(json.dumps({"ticket_id": t.ticket_id, "status": t.status,
                              "created": created,
                              "note": "未批准;启用动作集须 D-1 拍板(规格 §6)"},
                             ensure_ascii=False))
        elif a.cmd in ("approve", "reject"):
            r = store.transition(a.ticket, a.cmd, now=_now_iso(), actor=a.actor)
            print(json.dumps({"ok": r.ok, "status": r.status, "reason": r.reason},
                             ensure_ascii=False))
            return 0 if r.ok else 1
        elif a.cmd == "show":
            if a.ticket:
                t = store.get(a.ticket)
                if t is None:
                    print(json.dumps({"error": "NOT_FOUND"}))
                    return 1
                print(json.dumps({"ticket_id": t.ticket_id, "status": t.status,
                                  "binding": vars(t.binding) | {"action": t.binding.action},
                                  "approved_by": t.approved_by,
                                  "expires_at": str(t.approval_expires_at),
                                  "missing_fields": ["真实审批人映射(D-2)未配置"]},
                                 ensure_ascii=False, default=str))
            else:
                print(json.dumps({"note": "逐票查询(--ticket);列表页属门 Web 工作项"},
                                 ensure_ascii=False))
        elif a.cmd == "check":
            t = store.get(a.ticket)
            if t is None:
                print(json.dumps({"ok": False, "reason": "NOT_FOUND"}))
                return 1
            params = json.loads(a.params)
            req = ExecutionRequest(
                ticket_id=a.ticket, run_id=a.run, repo=a.repo, head_sha=a.head,
                params_hash=canonical_hash(params),
                patch_fingerprint=a.patch_fp or t.binding.patch_fingerprint,
                finding_fingerprint=t.binding.finding_fingerprint)
            r = check_execution(t, req, now=None)
            print(json.dumps({"ok": r.ok, "reason": r.reason}, ensure_ascii=False))
            return 0 if r.ok else 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
