# -*- coding: utf-8 -*-
"""gate_display — 人工门状态 → 前端展示契约(有限对齐,2026-09-24)。

允许的展示状态(八种,固定枚举,不新增):
  pending / action_required / approved_plan_ready / rejected / blocked /
  expired / backend_unavailable / scope_missing

规则:
  * 票据状态为主映射;ENVIRONMENTAL 的 backend_unavailable / scope_missing
    由显式标志置位并覆盖票据映射(基础设施问题优先展示);
  * action_required 仅在票据 PENDING 且 check-run 已发布(action_required)
    时出现;未发布时为 pending;
  * 未映射状态一律 ValueError(编程错误,前端不猜)。
"""
from __future__ import annotations

GATE_DISPLAY_STATES = (
    "pending", "action_required", "approved_plan_ready", "rejected",
    "blocked", "expired", "backend_unavailable", "scope_missing",
)


def gate_display(ticket_status: str, *, published: bool = False,
                 backend_ok: bool = True, scope_ok: bool = True) -> str:
    if not backend_ok:
        return "backend_unavailable"
    if not scope_ok:
        return "scope_missing"
    if ticket_status == "PENDING":
        return "action_required" if published else "pending"
    if ticket_status in ("APPROVED", "EXECUTING", "USED"):
        return "approved_plan_ready"
    if ticket_status == "REJECTED":
        return "rejected"
    if ticket_status == "EXPIRED":
        return "expired"
    if ticket_status in ("FAILED", "INVALIDATED"):
        return "blocked"
    raise ValueError("unmapped ticket status for gate display: %r" % (ticket_status,))
