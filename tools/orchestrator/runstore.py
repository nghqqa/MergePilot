"""v3 run 记录持久化(SQLite,哑存储)。

边界:这是 run 记录的**持久化适配层**,不是状态机——所有维度状态转移
必须先经 stages.RunStages.transition(唯一状态机),此处只存取快照。
与审批票据库(TicketStore)分库分表,不共享状态,不构成第二套票据语义。

SQLite 边界沿用 DECISIONS P-1:单 Controller、单实例、非多租户、
不承诺高可用;多 Controller/共享部署迁 PostgreSQL(表形状已定,见 store.py)。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS v3_runs (
    run_id          TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT NOT NULL,
    base_sha        TEXT,
    mode            TEXT NOT NULL,             -- shadow | fixture | on
    risk_tier       TEXT,
    risk_json       TEXT,
    plan_json       TEXT,
    stages_json     TEXT,
    outcome_json    TEXT,
    coverage_missing TEXT,                     -- JSON 数组
    downgrade_reason TEXT,
    aggregates_json TEXT,
    finding_validation TEXT,
    patch_validation TEXT,
    rag_snapshot    TEXT,
    manifest_hash   TEXT,
    evidence_path   TEXT,
    superseded      INTEGER NOT NULL DEFAULT 0,  -- 1=被新 head 取代(CANCELLED)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v3_runs_pr ON v3_runs(repo, pr_number);
CREATE TABLE IF NOT EXISTS v3_hook_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL,
    error       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_COLS = ("run_id,repo,pr_number,head_sha,base_sha,mode,risk_tier,risk_json,"
         "plan_json,stages_json,outcome_json,coverage_missing,downgrade_reason,"
         "aggregates_json,finding_validation,patch_validation,rag_snapshot,"
         "manifest_hash,evidence_path,superseded,created_at,updated_at")


def evidence_hash(evidence: Dict[str, Any]) -> str:
    """证据内容寻址(规范化 sha256),供控制台/审计核对一致性。"""
    blob = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class RunStore:
    """v3 run 记录存取。同文件多连接安全(参数同 SQLiteTicketStore)。"""

    def __init__(self, path: str):
        self.path = path
        import threading
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=10,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)

    def close(self):
        self._conn.close()

    _JSON_MAP = {"risk_json": "risk", "plan_json": "plan",
                 "stages_json": "stages", "outcome_json": "outcome",
                 "aggregates_json": "aggregates",
                 "coverage_missing": "coverage_missing"}

    @staticmethod
    def _row_to_record(row) -> Dict[str, Any]:
        rec = dict(zip(_COLS.split(","), row))
        for jkey, plain in RunStore._JSON_MAP.items():
            if rec.get(jkey):
                try:
                    rec[plain] = json.loads(rec[jkey])
                except Exception:
                    pass
        return rec

    def save_run(self, record: Dict[str, Any]) -> None:
        """UPSERT(同 run_id 重写=幂等;updated_at 由调用方注入)。"""
        sql = ("INSERT INTO v3_runs (%s) VALUES (%s)"
               " ON CONFLICT(run_id) DO UPDATE SET %s"
               % (_COLS, ",".join("?" * 22),
                  ",".join("%s=excluded.%s" % (c, c) for c in _COLS.split(",")
                           if c not in ("run_id", "created_at"))))
        args = (record.get("run_id"), record.get("repo"), record.get("pr_number"),
                record.get("head_sha"), record.get("base_sha"), record.get("mode"),
                record.get("risk_tier"),
                _json_or_none(record.get("risk_json")),
                _json_or_none(record.get("plan_json")),
                _json_or_none(record.get("stages_json")),
                _json_or_none(record.get("outcome_json")),
                _json_or_none(record.get("coverage_missing")),
                record.get("downgrade_reason"),
                _json_or_none(record.get("aggregates_json")),
                record.get("finding_validation"),
                record.get("patch_validation"), record.get("rag_snapshot"),
                record.get("manifest_hash"), record.get("evidence_path"),
                1 if record.get("superseded") else 0,
                record.get("created_at"), record.get("updated_at"))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(sql, args)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT %s FROM v3_runs WHERE run_id=?" % _COLS, (run_id,))
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT %s FROM v3_runs ORDER BY updated_at DESC LIMIT ?" % _COLS,
            (limit,))
        return [self._row_to_record(r) for r in cur.fetchall()]

    def runs_for_pr(self, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT %s FROM v3_runs WHERE repo=? AND pr_number=? "
            "ORDER BY updated_at DESC" % _COLS, (repo, pr_number))
        return [self._row_to_record(r) for r in cur.fetchall()]

    def record_hook_error(self, delivery_id: str, error: str, at: str) -> None:
        """hook fail-soft 异常的持久痕迹(可观测;尽力而为,调用方不再抛)。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO v3_hook_errors(delivery_id, error, created_at) "
                    "VALUES (?,?,?)", (delivery_id, error[:300], at))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")

    def list_hook_errors(self, limit: int = 20) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT delivery_id, error, created_at FROM v3_hook_errors "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [{"delivery_id": r[0], "error": r[1], "created_at": r[2]}
                for r in cur.fetchall()]

    def mark_superseded(self, run_id: str, at: str) -> bool:
        """PR 更新使旧 run 失效(记录层标记;状态机侧 CANCELLED 由 adapter 完成)。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE v3_runs SET superseded=1, updated_at=? "
                "WHERE run_id=? AND superseded=0", (at, run_id))
            return cur.rowcount == 1


def _json_or_none(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
