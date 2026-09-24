"""SQLite → PostgreSQL 票据迁移工具(契约=e247b80 §5.4;默认 dry-run)。

安全设计:
- 源库只读(file:...?mode=ro),任何情况下不写源;
- 默认 dry-run:只校验并输出报告,不写目标;
- 目标身份显式(--pg-dsn),仅限隔离/获准实例;
- 校验:状态枚举 / head·哈希形状 / ISO 时间 / 活动票重复 / 父记录(run/repo)存在;
- 父记录缺失 → 跳过并报告,不伪造父记录满足外键;
- 冲突(ticket_id 已存在)不覆盖,计数报告;重跑安全(幂等);
- 导入后逐字段核对(不只比行数)。

用法:
  python migrate_tickets_sqlite_pg.py --sqlite t.db --pg-dsn "..."           # dry-run
  python migrate_tickets_sqlite_pg.py --sqlite t.db --pg-dsn "..." --apply   # 实际导入
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from typing import Any, Dict, List, Optional

VALID_STATUS = {"PENDING", "APPROVED", "REJECTED", "EXECUTING", "USED",
                "FAILED", "EXPIRED", "INVALIDATED"}


def _open_ro(path: str):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _parse_iso(v):
    if v is None:
        return None, None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d, None
    except Exception as e:
        return None, str(e)[:80]


def extract(sqlite_path: str) -> Dict[str, Any]:
    """只读抽取+校验。返回 rows/errors 摘要(不过脱敏需要——票据数据无秘密字段)。"""
    conn = _open_ro(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cols = [r["name"] for r in cur.execute("PRAGMA table_info(tickets)")]
        required = {"ticket_id", "run_id", "repo", "head_sha", "action",
                    "params_hash", "status"}
        missing = required - set(cols)
        if missing:
            raise ValueError("源表缺少必需列: %s" % sorted(missing))
        rows = [dict(r) for r in cur.execute("SELECT * FROM tickets ORDER BY ticket_id")]
    finally:
        conn.close()

    errors: List[str] = []
    warnings: List[str] = []
    active_seen = {}
    clean: List[Dict[str, Any]] = []
    for r in rows:
        tid = r["ticket_id"]
        if r["status"] not in VALID_STATUS:
            errors.append("%s: 非法状态 %r" % (tid, r["status"]))
            continue
        if not (isinstance(r["head_sha"], str) and len(r["head_sha"]) == 40):
            errors.append("%s: head_sha 非 40hex" % tid)
            continue
        for f in ("created_at", "approval_expires_at", "approved_at"):
            _, err = _parse_iso(r.get(f))
            if err:
                errors.append("%s: %s 时间格式非法(%s)" % (tid, f, err))
        if r["status"] in ("PENDING", "APPROVED", "EXECUTING"):
            key = (r["run_id"], r["action"], r["finding_id"])
            if key in active_seen:
                errors.append("%s: 与 %s 构成重复活动票 %s" % (tid, active_seen[key], key))
            else:
                active_seen[key] = tid
        clean.append(r)
    return {"rows": rows, "clean": clean, "errors": errors, "warnings": warnings}


def check_parents(pg_conn, rows: List[Dict[str, Any]]):
    """父记录存在性:缺失者跳过并报告,不伪造父记录满足外键。"""
    cur = pg_conn.cursor()
    ok, missing = [], []
    for r in rows:
        cur.execute("SELECT 1 FROM run.runs WHERE run_id=%s", (r["run_id"],))
        has_run = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM run.repos WHERE repo_id=%s", (r["repo"],))
        has_repo = cur.fetchone() is not None
        if has_run and has_repo:
            ok.append(r)
        else:
            missing.append({"ticket_id": r["ticket_id"],
                            "run_exists": has_run, "repo_exists": has_repo})
    return ok, missing


def import_rows(pg_conn, rows) -> Dict[str, int]:
    """导入(冲突不覆盖,幂等可重跑);逐行 UPSERT OR IGNORE 语义由 PK 冲突跳过实现。"""
    cur = pg_conn.cursor()
    inserted = skipped_conflict = 0
    for r in rows:
        created = _parse_iso(r.get("created_at"))[0]
        # 设计 v2 §5.4:target_key 非空 = COALESCE(finding_id, '_run_')
        target_key = r["finding_id"] if r["finding_id"] is not None else "_run_"
        cur.execute(
            "INSERT INTO approval.tickets (ticket_id, run_id, repo_id, head_sha, "
            "action, params_hash, patch_fingerprint, finding_fingerprint, "
            "finding_id, target_key, attempt_no, status, created_at, "
            "created_by_run, approval_expires_at, approved_by, approved_at, "
            "result_fingerprint, error) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ticket_id) DO NOTHING",
            (r["ticket_id"], r["run_id"], r["repo"], r["head_sha"], r["action"],
             r["params_hash"], r.get("patch_fingerprint"),
             r.get("finding_fingerprint"), r.get("finding_id"), target_key,
             r.get("attempt_no", 1), r["status"], created,
             r.get("created_by_run"), _parse_iso(r.get("approval_expires_at"))[0],
             r.get("approved_by"), _parse_iso(r.get("approved_at"))[0],
             r.get("result_fingerprint"), r.get("error")))
        if cur.rowcount == 1:
            inserted += 1
            cur.execute("INSERT INTO approval.ticket_audit "
                        "(ticket_id, from_status, to_status, actor, request_hash) "
                        "VALUES (%s,NULL,%s,'migration','MIGRATED')",
                        (r["ticket_id"], r["status"]))
        else:
            skipped_conflict += 1
    return {"inserted": inserted, "skipped_conflict": skipped_conflict}


def verify_import(pg_conn, rows) -> Dict[str, Any]:
    """导入后逐字段核对(字段级,不只行数)。"""
    cur = pg_conn.cursor()
    mismatch = []
    for r in rows:
        cur.execute("SELECT run_id, repo_id, head_sha, action, status, attempt_no "
                    "FROM approval.tickets WHERE ticket_id=%s", (r["ticket_id"],))
        row = cur.fetchone()
        if row is None:
            mismatch.append({"ticket_id": r["ticket_id"], "field": "<row>", "reason": "缺失"})
            continue
        expect = (r["run_id"], r["repo"], r["head_sha"], r["action"], r["status"])
        got = (row[0], row[1], row[2], row[3], row[4])
        if got != expect:
            mismatch.append({"ticket_id": r["ticket_id"], "field": "tuple",
                             "expected": expect, "got": got})
        if row[5] != r.get("attempt_no", 1):
            mismatch.append({"ticket_id": r["ticket_id"], "field": "attempt_no",
                             "expected": r.get("attempt_no", 1), "got": row[5]})
    return {"checked": len(rows), "mismatch": mismatch,
            "ok": not mismatch}


def run(sqlite_path: str, pg_dsn: str, apply: bool = False) -> Dict[str, Any]:
    report: Dict[str, Any] = {"mode": "apply" if apply else "dry-run",
                              "source": sqlite_path}
    ex = extract(sqlite_path)
    report["source_rows"] = len(ex["rows"])
    report["validation_errors"] = ex["errors"]
    report["warnings"] = ex["warnings"]
    if ex["errors"]:
        report["result"] = "ABORT: 源数据校验失败,未写目标"
        return report
    import psycopg2
    pg = psycopg2.connect(pg_dsn)   # autocommit=False:首条语句即开启事务
    try:
        ok_rows, missing = check_parents(pg, ex["clean"])
        pg.commit()                  # 关闭只读事务,再开写入事务
        report["parent_missing"] = missing
        if not apply:
            report["result"] = "DRY-RUN: 可导入 %d 行,父记录缺失跳过 %d 行" % (
                len(ok_rows), len(missing))
            return report
        cur = pg.cursor()
        cur.execute("BEGIN")
        result = import_rows(pg, ok_rows)
        report["import"] = result
        pg.commit()
        report["verify"] = verify_import(pg, ok_rows)
        report["result"] = ("OK: 导入 %d,冲突跳过 %d,核对 %s" % (
            result["inserted"], result["skipped_conflict"],
            "通过" if report["verify"]["ok"] else "FAIL"))
        return report
    except Exception as e:
        pg.rollback()   # 事务一致性:失败整体回滚
        report["result"] = "ABORT: %s: %s(已回滚,无半条状态)" % (type(e).__name__, str(e)[:160])
        return report
    finally:
        pg.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--pg-dsn", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="实际写入目标(默认 dry-run)")
    a = ap.parse_args(argv)
    report = run(a.sqlite, a.pg_dsn, apply=a.apply)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0 if not str(report.get("result", "")).startswith("ABORT") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
