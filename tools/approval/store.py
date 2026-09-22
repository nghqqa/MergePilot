"""TicketStore 接口——票据存储的抽象边界(P-1 后续演化,2026-09-22)。

V0 = SQLiteTicketStore(单实例);未来多 Controller/多用户/共享部署必须
PostgreSQLTicketStore,不得继续扩展 SQLite(边界见 ARCHITECTURE-V3 §6)。
接口固定为五个操作,任何实现都复用 approval.transition 的同一状态机语义:
  create(binding, ...) -> (Ticket, created)   幂等:活动票唯一
  get(ticket_id) -> Ticket | None
  active_for(binding) -> Ticket | None
  transition(ticket_id, event, **kw) -> TransitionResult   CAS,先到先得
  close() -> None
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .approval import Binding, Ticket, TransitionResult


@runtime_checkable
class TicketStore(Protocol):
    """票据存储接口。实现必须保证:
    - create 跨进程幂等(同 run/action/finding 活动票唯一,并发收敛同一张票);
    - transition 为 CAS:前置状态不匹配时返回 INVALID_TRANSITION,不覆盖;
    - 崩溃后重开保留已提交状态;
    - 状态机语义与 approval.transition 完全一致(不得自行重写转移规则)。"""

    def create(self, binding: Binding, attempt_no: int = 1, created_at: str = "",
               created_by_run: str = "",
               approval_expires_at: Any = None) -> tuple:  # -> (Ticket, bool)
        ...

    def get(self, ticket_id: str) -> Optional[Ticket]: ...

    def active_for(self, binding: Binding) -> Optional[Ticket]: ...

    def transition(self, ticket_id: str, event: str, **kw: Any) -> TransitionResult: ...

    def close(self) -> None: ...


class PostgreSQLTicketStore:
    """PostgreSQL 实现占位(多 Controller/多用户/共享部署的目标形态)。

    迁移设计(不在 V0 实现):
    - 表形状沿用 tools/approval/store_sqlite.py 的 tickets 列;
    - 活动票唯一:`CREATE UNIQUE INDEX ... ON tickets(run_id, action, finding_id)
      WHERE status IN ('PENDING','APPROVED','EXECUTING')`(PG 部分索引,同形);
    - 事务:`SELECT ... FOR UPDATE` 或 `UPDATE ... WHERE status=<prev>`(rowcount CAS),
      与 SQLite 版同一语义;连接 DSN 形状与 case-pg/receiver 惯例一致;
    - 演进触发条件:多 Controller(cutover)、多部署实例或共享部署立项时实现,
      届时 M2 语义单测(approval.transition 层)与 store 契约测试全部复用。
    """

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQLTicketStore 需要 DSN")
        self.dsn = dsn
        raise NotImplementedError(
            "PostgreSQLTicketStore 是迁移占位:多 Controller/共享部署时实现"
            "(见 ARCHITECTURE-V3 §6);V0 使用 SQLiteTicketStore")
