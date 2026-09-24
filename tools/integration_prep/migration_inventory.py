"""迁移源盘点(migration inventory):SQLite → PostgreSQL 迁移的源侧检查。

对给定 SQLite 文件输出:表清单、列/约束(PRAGMA)、建表 DDL、行数。
用于迁移前核对源字段/约束/数据量;输出 JSON 供设计窗口对照正式契约。
只读:仅 PRAGMA 与 SELECT,不写源文件。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List


def inventory(path: str) -> Dict[str, Any]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out: Dict[str, Any] = {"source": os.path.basename(path),
                               "tables": {}}
        for t in tables:
            cols = [dict(r) for r in cur.execute("PRAGMA table_info(%s)" % t)]
            ddl = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()[0]
            count = cur.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            idx = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL", (t,))]
            out["tables"][t] = {"columns": cols, "ddl": ddl, "row_count": count,
                                "explicit_indexes": idx}
        return out
    finally:
        conn.close()


def compatibility_notes(inv: Dict[str, Any]) -> List[str]:
    """源侧兼容性注意点(供迁移工具实现时对照)。"""
    notes = []
    for t, info in inv.get("tables", {}).items():
        for c in info["columns"]:
            if c["type"].upper() == "" and c["name"] in ("approval_expires_at",
                                                         "approved_at"):
                notes.append("%s.%s: 无类型声明列(ISO 字符串)→ PG 迁移为 timestamptz 需校验格式"
                             % (t, c["name"]))
        ddl = info.get("ddl", "")
        if "WHERE status IN" in ddl:
            notes.append("%s: 部分唯一索引(活动票唯一)→ PG 同形支持已由 spike 验证" % t)
    return notes


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    inv = inventory(sys.argv[1])
    print(json.dumps(inv, ensure_ascii=False, indent=1))
    for n in compatibility_notes(inv):
        print("NOTE:", n)
