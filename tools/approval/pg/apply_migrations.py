"""迁移执行器(apply_migrations):按序应用、显式跟踪、严格形状、可重复。

- 跟踪表 approval.schema_migrations(version INT PK, applied_at);
- 已记录 → 跳过(幂等重跑安全);
- 未记录且表已存在(形状可疑,如夹具表占用正式表名) → 明确失败,
  不被 CREATE TABLE IF NOT EXISTS 静默掩盖;
- 每个迁移独立事务;失败即停,已应用保留(不回滚历史)。
用法: python apply_migrations.py --dsn ... [--dir migrations] [--mark N]
(--mark 仅用于收录"曾手工应用"的历史版本,不执行 SQL。)
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import psycopg2

_TRACKING_DDL = """
CREATE SCHEMA IF NOT EXISTS approval;
CREATE TABLE IF NOT EXISTS approval.schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def applied_versions(cur) -> set:
    cur.execute("SELECT version FROM approval.schema_migrations ORDER BY version")
    return {r[0] for r in cur.fetchall()}


def table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s", (schema, table))
    return cur.fetchone() is not None


def index_predicates(cur, schema: str, table: str) -> dict:
    cur.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname=%s AND tablename=%s", (schema, table))
    return {r[0]: r[1] for r in cur.fetchall()}


def apply_all(dsn: str, mig_dirs) -> dict:
    """mig_dirs:单目录或目录列表(跨域迁移按版本号全局排序合并)。"""
    if isinstance(mig_dirs, (str, Path)):
        mig_dirs = [mig_dirs]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    report = {"applied_now": [], "skipped": [], "errors": []}
    try:
        cur = conn.cursor()
        cur.execute(_TRACKING_DDL)
        conn.commit()
        applied = applied_versions(cur)
        files = []
        for d in mig_dirs:
            d = Path(d)
            files += list(d.glob("*.sql"))
        files = sorted(files, key=lambda p: int(re.match(r"(\d+)", p.name).group(1)))
        for f in files:
            version = int(re.match(r"(\d+)", f.name).group(1))
            if version in applied:
                report["skipped"].append(f.name)
                continue
            sql = f.read_text(encoding="utf-8")
            # 形状预检:所有 CREATE TABLE 目标若已存在且未登记 → 明确失败
            # (夹具/残留表占用正式表名时,不允许静默跳过或盲目覆盖)
            for m in re.finditer(
                    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                    r"((?:\w+\.)?\w+)\s*\(", sql, re.IGNORECASE):
                tbl = m.group(1)
                schema, _, name = tbl.partition(".")
                schema = schema or "public"
                if table_exists(cur, schema, name):
                    report["errors"].append(
                        "%s: 表 %s.%s 已存在但未登记为已应用(形状可疑,疑似夹具/残留表)。"
                        "请人工核对后 --mark %d,或删除残留表。" % (
                            f.name, schema, name, version))
            if report["errors"]:
                report["result"] = "FAIL:存在可疑既有表,已中止"
                return report
            try:
                cur.execute("BEGIN")
                cur.execute(sql)
                cur.execute("INSERT INTO approval.schema_migrations (version) "
                            "VALUES (%s)", (version,))
                conn.commit()
                report["applied_now"].append(f.name)
            except psycopg2.Error as e:
                conn.rollback()
                report["errors"].append("%s: %s: %s" % (
                    f.name, type(e).__name__, str(e)[:200]))
                report["result"] = "FAIL:迁移失败已回滚,保留既有合法数据"
                return report
        report["result"] = "OK"
        return report
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--dir", action="append", default=[], dest="dirs",
                    help="迁移目录(可多次);默认=approval+orchestrator 两域")
    ap.add_argument("--mark", type=int, action="append", default=[],
                    help="收录曾手工应用的历史版本号(不执行 SQL)")
    a = ap.parse_args(argv)
    conn = psycopg2.connect(a.dsn)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(_TRACKING_DDL)
    conn.commit()
    dirs = a.dirs or [str(Path(__file__).resolve().parents[1] /
                          "approval" / "pg" / "migrations"),
                      str(Path(__file__).resolve().parents[2] /
                          "orchestrator" / "pg" / "migrations")]
    already = applied_versions(cur)
    for v in a.mark:
        if v not in already:
            cur.execute("INSERT INTO approval.schema_migrations (version) "
                        "VALUES (%s) ON CONFLICT DO NOTHING", (v,))
            conn.commit()
            already.add(v)
            print("marked:", v)
    conn.close()
    report = apply_all(a.dsn, dirs)
    import json
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["result"] == "OK" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
