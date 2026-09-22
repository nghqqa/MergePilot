"""PostgreSQLTicketStore — TicketStore 接口的 PostgreSQL 实现(设计契约 e247b80 §5.4)。

语义与 SQLiteTicketStore 完全一致(同一纯逻辑状态机 approval.transition):
  - create 幂等(部分唯一索引 NULLS NOT DISTINCT 强制活动票唯一);
  - transition 为 CAS(SELECT FOR UPDATE + 前置状态守卫 UPDATE);
  - 票据变更与 ticket_audit 追加同事务(原子);
  - 连接级失败抛 StorageUnavailable(与业务拒绝严格分离);
  - 时间一律时区明确的 UTC datetime(ISO 字符串自动解析)。

错误分类(要求⑦):OperationalError/InterfaceError → StorageUnavailable;
UniqueViolation 在 create 内部消化为"返回既有票";其余 psycopg2 错误原样抛出。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from .approval import Binding, Ticket, TransitionResult, create_ticket, transition


class StorageUnavailable(RuntimeError):
    """连接/事务基础设施失败——与业务拒绝(INVALID_TRANSITION)严格分离。"""


def _connect(dsn: str):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        raise StorageUnavailable("PG 连接失败: %s" % str(e)[:160]) from e


def _as_utc_dt(value: Any, field: str) -> dt.datetime:
    """统一为时区明确的 UTC datetime(ISO 字符串自动解析;naive 视为 UTC)。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        v = value
    elif isinstance(value, str):
        v = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("%s 必须是 ISO 字符串或 datetime,得到 %r" % (field, value))
    if v.tzinfo is None:
        v = v.replace(tzinfo=dt.timezone.utc)
    return v.astimezone(dt.timezone.utc)


_COLS = ("ticket_id, run_id, repo_id, head_sha, action, params_hash, "
         "patch_fingerprint, finding_fingerprint, finding_id, attempt_no, "
         "status, created_at, created_by_run, approval_expires_at, "
         "approved_by, approved_at, result_fingerprint, error")

_INSERT = ("INSERT INTO approval.tickets (%s) VALUES (%s)"
           % (_COLS, ",".join(["%s"] * 18)))


class PostgreSQLTicketStore:
    """TicketStore 的 PostgreSQL 实现(设计契约基线 e247b80 §5.4)。

    边界:实现 TicketStore 接口与既有纯逻辑状态机;不新增租约/执行票据语义;
    不修改真实审批动作、审批人或 TTL 政策(均为调用方输入)。
    """

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQLTicketStore 需要 DSN")
        self.dsn = dsn
        self._conn = _connect(dsn)

    def close(self):
        with contextlib.suppress(Exception):
            self._conn.close()

    # ── 内部 ─────────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_ticket(row) -> Ticket:
        b = Binding(run_id=row[1], repo=row[2], head_sha=row[3],
                    action=row[4], params_hash=row[5],
                    patch_fingerprint=row[6], finding_fingerprint=row[7],
                    finding_id=row[8])
        return Ticket(ticket_id=row[0], binding=b, attempt_no=row[9],
                      status=row[10], created_at=str(row[11]),
                      created_by_run=row[12] or "",
                      approval_expires_at=row[13], approved_by=row[14],
                      approved_at=row[15], result_fingerprint=row[16],
                      error=row[17])

    def _select(self, cur, where: str, args: tuple):
        cur.execute("SELECT %s FROM approval.tickets WHERE %s"
                    % (_COLS, where), args)
        rows = cur.fetchall()
        return [self._row_to_ticket(r) for r in rows]

    @staticmethod
    def _insert_args(t: Ticket, b: Binding) -> tuple:
        return (t.ticket_id, b.run_id, b.repo, b.head_sha, b.action,
                b.params_hash, b.patch_fingerprint, b.finding_fingerprint,
                b.finding_id, t.attempt_no, t.status, t.created_at or _now_utc(),
                t.created_by_run, t.approval_expires_at, t.approved_by,
                t.approved_at, t.result_fingerprint, t.error)

    # ── TicketStore 接口 ─────────────────────────────────────────────────
    def create(self, binding: Binding, attempt_no: int = 1, created_at: str = "",
               created_by_run: str = "",
               approval_expires_at: Any = None):
        """幂等创建:活动票唯一由部分唯一索引(NULLS NOT DISTINCT)跨进程强制;
        冲突 → 读回既有票(created=False)。票据创建与审计同事务。"""
        expires = _as_utc_dt(approval_expires_at, "approval_expires_at")
        probe = create_ticket(binding, attempt_no=attempt_no,
                              approval_expires_at=expires)   # 形状校验+到期入票
        try:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(_INSERT, self._insert_args(probe, binding))
                self._audit(cur, probe.ticket_id, None, probe.status,
                            created_by_run or None, None)
        except psycopg2.errors.UniqueViolation:
            self._conn.rollback()
            existing = self.active_for(binding)
            if existing is not None:
                return existing, False
            raise
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            raise StorageUnavailable(str(e)[:160]) from e
        return probe, True

    def get(self, ticket_id: str) -> Optional[Ticket]:
        try:
            rows = self._select(self._conn.cursor(), "ticket_id=%s", (ticket_id,))
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            raise StorageUnavailable(str(e)[:160]) from e
        return rows[0] if rows else None

    def active_for(self, binding: Binding) -> Optional[Ticket]:
        try:
            rows = self._select(
                self._conn.cursor(),
                "run_id=%s AND action=%s AND finding_id IS NOT DISTINCT FROM %s "
                "AND status IN ('PENDING','APPROVED','EXECUTING')",
                (binding.run_id, binding.action, binding.finding_id))
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            raise StorageUnavailable(str(e)[:160]) from e
        return rows[0] if rows else None

    def transition(self, ticket_id: str, event: str, **kw: Any) -> TransitionResult:
        """CAS 转移:SELECT FOR UPDATE 串行化 → 纯逻辑转移 → 前置状态守卫 UPDATE
        → 审计同事务。连接错误抛 StorageUnavailable(非业务拒绝)。
        now 接受 ISO 字符串(统一归一为 aware UTC datetime,与 TIMESTAMPTZ 可比)。"""
        if isinstance(kw.get("now"), str):
            try:
                kw["now"] = _as_utc_dt(kw["now"], "now")
            except ValueError as e:
                return TransitionResult(False, "?", "BAD_NOW:%s" % str(e)[:80])
        try:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute("SELECT %s FROM approval.tickets WHERE ticket_id=%%s "
                            "FOR UPDATE" % _COLS, (ticket_id,))
                rows = cur.fetchall()
                if not rows:
                    return TransitionResult(False, "?", "NOT_FOUND")
                t = self._row_to_ticket(rows[0])
                prev = t.status
                result = transition(t, event, **kw)
                if not result.ok:
                    return result      # 无写发生;事务提交(只读)
                cur.execute(
                    "UPDATE approval.tickets SET status=%s, approved_by=%s, "
                    "approved_at=%s, result_fingerprint=%s, error=%s "
                    "WHERE ticket_id=%s AND status=%s",
                    (t.status, t.approved_by, t.approved_at,
                     t.result_fingerprint, t.error, ticket_id, prev))
                if cur.rowcount == 0:
                    return TransitionResult(False, prev,
                                            "INVALID_TRANSITION:%s" % prev)
                self._audit(cur, ticket_id, prev, t.status,
                            kw.get("actor"), result.reason)
                return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            with contextlib.suppress(Exception):
                self._conn.rollback()
            raise StorageUnavailable(str(e)[:160]) from e

    @staticmethod
    def _audit(cur, ticket_id: str, from_status: Optional[str],
               to_status: str, actor: Optional[str], reason: Optional[str]):
        cur.execute(
            "INSERT INTO approval.ticket_audit "
            "(ticket_id, from_status, to_status, actor, request_hash) "
            "VALUES (%s,%s,%s,%s,%s)",
            (ticket_id, from_status, to_status, actor, reason))


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
