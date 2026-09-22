"""SQLite(WAL) 票据存储——M2 门票据的跨进程 CAS 落地(P-1 工程决策,2026-09-22)。

为什么不是 MinIO(原提案 B):mc 客户端无条件写原语,"单写者约定"只是约定,
拿不出并发正确性证明;V0 门页多请求并发批准/拒绝是真实场景。
为什么是 SQLite:同一 SQL 级 CAS(BEGIN IMMEDIATE + 状态守卫 UPDATE)、
WAL 崩溃恢复、零新服务、单文件可备份;V0 单团队同机部署下足够,
Controller cutover 时可平移到服务器 PG(同一表结构形状)。

关键设计:**状态机不重写**——从 approval.transition(纯逻辑,34 单测)复用;
存储层只负责 (1) BEGIN IMMEDIATE 串行化 (2) 按前置状态守卫写回(CAS)
(3) 活动票唯一性由 partial UNIQUE INDEX 强制(跨进程幂等创建)。
时间统一 ISO-8601 字符串(定长格式下字典序=时间序)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any, Optional

from .approval import (ACTIVE_STATES, Binding, Ticket, TransitionResult,
                       create_ticket, transition)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    repo             TEXT NOT NULL,
    head_sha         TEXT NOT NULL,
    action           TEXT NOT NULL,
    params_hash      TEXT NOT NULL,
    patch_fingerprint TEXT,
    finding_fingerprint TEXT,
    finding_id       TEXT,
    attempt_no       INTEGER NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT '',
    created_by_run   TEXT NOT NULL DEFAULT '',
    approval_expires_at,
    approved_by      TEXT,
    approved_at,
    result_fingerprint TEXT,
    error            TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_ticket
    ON tickets(run_id, action, finding_id)
    WHERE status IN ('PENDING','APPROVED','EXECUTING');
"""

_COLS = ("ticket_id,run_id,repo,head_sha,action,params_hash,patch_fingerprint,"
         "finding_fingerprint,finding_id,attempt_no,status,created_at,"
         "created_by_run,approval_expires_at,approved_by,approved_at,"
         "result_fingerprint,error")


def _row_to_ticket(row) -> Ticket:
    b = Binding(run_id=row[1], repo=row[2], head_sha=row[3], action=row[4],
                params_hash=row[5], patch_fingerprint=row[6],
                finding_fingerprint=row[7], finding_id=row[8])
    return Ticket(ticket_id=row[0], binding=b, attempt_no=row[9], status=row[10],
                  created_at=row[11], created_by_run=row[12],
                  approval_expires_at=row[13], approved_by=row[14],
                  approved_at=row[15], result_fingerprint=row[16], error=row[17])


class SQLiteTicketStore:
    """TicketStore 的 V0 单实例实现(接口见 store.py)。

    **边界**:单 Controller、单部署实例、WAL 单文件——不承诺多实例高可用,
    不承诺多用户 SaaS;多 Controller/共享部署迁 PostgreSQLTicketStore(store.py)。
    同文件多连接/多线程安全:
    - 本实例内多线程:check_same_thread=False + 实例锁串行化事务;
    - 跨进程:BEGIN IMMEDIATE 写锁排队(busy_timeout)+ 前置状态守卫 UPDATE;
    - create 并发兜底:partial UNIQUE INDEX,冲突方捕获后返回既有票。"""

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

    class _Txn:
        """显式 BEGIN IMMEDIATE 事务(isolation_level=None 下 with conn 是空操作)。"""

        def __init__(self, store):
            self.store = store

        def __enter__(self):
            self.store._lock.acquire()
            self.store._conn.execute("BEGIN IMMEDIATE")
            return self.store._conn

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.store._conn.execute("COMMIT")
                else:
                    self.store._conn.execute("ROLLBACK")
            finally:
                self.store._lock.release()
            return False

    def _txn(self):
        return SQLiteTicketStore._Txn(self)

    # ── 幂等创建(活动票唯一由 partial UNIQUE INDEX 跨进程强制) ───────────
    def create(self, binding: Binding, attempt_no: int = 1, created_at: str = "",
               created_by_run: str = "", approval_expires_at: Any = None):
        """返回 (ticket, created)。活动票已存在 → 返回既有票(created=False)。"""
        probe = create_ticket(binding, attempt_no=attempt_no)  # 形状校验(不入库)
        del probe
        with self._txn() as conn:
            cur = conn.execute(
                "SELECT %s FROM tickets WHERE run_id=? AND action=? "
                "AND finding_id IS ? AND status IN ('PENDING','APPROVED','EXECUTING')"
                % _COLS, (binding.run_id, binding.action, binding.finding_id))
            row = cur.fetchone()
            if row:
                return _row_to_ticket(row), False
            t = create_ticket(binding, attempt_no=attempt_no,
                              created_at=created_at, created_by_run=created_by_run,
                              approval_expires_at=approval_expires_at)
            try:
                conn.execute(
                    "INSERT INTO tickets (%s) VALUES (%s)" % (_COLS, ",".join("?" * 18)),
                    (t.ticket_id, binding.run_id, binding.repo, binding.head_sha,
                     binding.action, binding.params_hash, binding.patch_fingerprint,
                     binding.finding_fingerprint, binding.finding_id, t.attempt_no,
                     t.status, t.created_at, t.created_by_run,
                     t.approval_expires_at, t.approved_by, t.approved_at,
                     t.result_fingerprint, t.error))
            except sqlite3.IntegrityError:
                # 跨进程竞争输家:UNIQUE INDEX 拒绝了第二张活动票 → 读回赢家
                cur = conn.execute(
                    "SELECT %s FROM tickets WHERE run_id=? AND action=? "
                    "AND finding_id IS ? AND status IN ('PENDING','APPROVED','EXECUTING')"
                    % _COLS, (binding.run_id, binding.action, binding.finding_id))
                row = cur.fetchone()
                if row:
                    return _row_to_ticket(row), False
                raise
            return t, True

    def get(self, ticket_id: str) -> Optional[Ticket]:
        cur = self._conn.execute("SELECT %s FROM tickets WHERE ticket_id=?" % _COLS,
                                 (ticket_id,))
        row = cur.fetchone()
        return _row_to_ticket(row) if row else None

    def active_for(self, binding: Binding) -> Optional[Ticket]:
        cur = self._conn.execute(
            "SELECT %s FROM tickets WHERE run_id=? AND action=? "
            "AND finding_id IS ? AND status IN ('PENDING','APPROVED','EXECUTING')"
            % _COLS, (binding.run_id, binding.action, binding.finding_id))
        row = cur.fetchone()
        return _row_to_ticket(row) if row else None

    # ── CAS 转移:复用纯逻辑状态机 + 前置状态守卫写回 ─────────────────────
    def transition(self, ticket_id: str, event: str, **kw) -> TransitionResult:
        with self._txn() as conn:  # 写锁排队:竞争在此串行,先到先得
            cur = conn.execute(
                "SELECT %s FROM tickets WHERE ticket_id=?" % _COLS, (ticket_id,))
            row = cur.fetchone()
            if row is None:
                return TransitionResult(False, "?", "NOT_FOUND")
            t = _row_to_ticket(row)
            prev = t.status
            result = transition(t, event, **kw)
            if result.ok:
                updated = conn.execute(
                    "UPDATE tickets SET status=?, approved_by=?, approved_at=?, "
                    "result_fingerprint=?, error=? "
                    "WHERE ticket_id=? AND status=?",
                    (t.status, t.approved_by, t.approved_at,
                     t.result_fingerprint, t.error, ticket_id, prev))
                if updated.rowcount == 0:
                    return TransitionResult(False, prev, "INVALID_TRANSITION:%s" % prev)
            return result
