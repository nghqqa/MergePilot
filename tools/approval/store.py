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

    def active_by_repo(self, repo: str) -> list: ...
        # 确定性编排只读查询(2026-09-24):旧 head 失效/顺序纪律判定。

    def record_event(self, ticket_id: str, from_status: Optional[str],
                     to_status: Optional[str], actor: Optional[str],
                     request_hash: Optional[str]) -> None: ...
        # append-only 审计事件(非状态转移,如 ENSURE_CREATED/ENSURE_REPLAYED)。

    def transition(self, ticket_id: str, event: str, **kw: Any) -> TransitionResult: ...

    def close(self) -> None: ...


class PostgreSQLTicketStore:
    """PostgreSQL 实现(2026-09-22 设计契约 e247b80 §5.4 落地,见 pg_store.py)。

    保留原占位签名兼容:空 DSN → ValueError;实现本体在 tools/approval/pg_store.py。
    """

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQLTicketStore 需要 DSN")
        from .pg_store import PostgreSQLTicketStore as _Impl
        self._impl = _Impl(dsn)
        self.dsn = dsn

    def create(self, binding, attempt_no: int = 1, created_at: str = "",
               created_by_run: str = "",
               approval_expires_at: Any = None):
        return self._impl.create(binding, attempt_no, created_at,
                                 created_by_run, approval_expires_at)

    def get(self, ticket_id: str):
        return self._impl.get(ticket_id)

    def active_for(self, binding):
        return self._impl.active_for(binding)

    def active_by_repo(self, repo):
        return self._impl.active_by_repo(repo)

    def record_event(self, ticket_id, from_status, to_status, actor, request_hash):
        self._impl.record_event(ticket_id, from_status, to_status, actor,
                                request_hash)

    def transition(self, ticket_id: str, event: str, **kw: Any):
        return self._impl.transition(ticket_id, event, **kw)

    def close(self):
        self._impl.close()
