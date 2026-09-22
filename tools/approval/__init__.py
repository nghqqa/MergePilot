"""MergePilot M2 审批票据(toolkit)。

规格:docs/productization/M2-APPROVAL-SPEC.md(v2 备忘二.2a 四问)。
纯逻辑层,零第三方依赖;存储与 Web 门页在后续工作项接入。
"""
from .approval import (  # noqa: F401
    ALLOWED_ACTIONS,
    TERMINAL_STATES,
    Binding,
    ExecutionRequest,
    InMemoryTicketStore,
    Ticket,
    TransitionResult,
    canonical_hash,
    check_execution,
    create_ticket,
    transition,
    validate_binding_shape,
)
