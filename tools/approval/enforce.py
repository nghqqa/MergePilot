# -*- coding: utf-8 -*-
"""enforce — D-B 正式审批的策略执行点(批准前收口新增)。

把 policy.py 的配置接到 approve/派发 两个动作上;所有检查 fail-closed:
  - policy 未配置(UnconfiguredPolicy) → 一律拒绝;
  - 动作不在 D-1 启用子集 → 拒绝;
  - 审批人不在该 repo 的具名审批人集 → 拒绝(D-2,稳定身份=GitHub login);
  - 可选 head 新鲜度: 提供 tip 时与票据绑定 head 不符 → 拒绝(不产生 CAS);
  - 通过后仍走 store 的 CAS approve(先到先得,过期 EXPIRED,重复 NOOP)。

本模块不做外部副作用;真正的执行(fixer 派发)另经 dispatch.dispatch_fixer,
其同样要求 CONFIGURED policy 才放行(见 authorize_dispatch)。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class AuthorizationDenied(Exception):
    def __init__(self, subcode: str, detail: str = ""):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail


def _policy_configured(policy) -> bool:
    return bool(getattr(policy, "configured", False))


def authorize_approval(store, policy, ticket_id: str, actor: str, *,
                       head_tip: str = None,
                       now=None, reason: str = None) -> Dict[str, Any]:
    """策略校验 + CAS approve。返回 {ok, status, reason, ticket_id}。

    任一策略检查失败 → 不触碰票据状态(无副作用拒绝)。"""
    t = store.get(ticket_id)
    if t is None:
        return {"ok": False, "reason": "TICKET_NOT_FOUND"}
    if not _policy_configured(policy):
        return {"ok": False, "reason": "POLICY_NOT_CONFIGURED",
                "ticket_id": ticket_id}
    if not policy.allows_action(t.binding.action):
        return {"ok": False, "reason": "ACTION_NOT_ENABLED:%s" % t.binding.action,
                "ticket_id": ticket_id}
    if not policy.can_approve(actor, t.binding.repo):
        return {"ok": False, "reason": "APPROVER_NOT_AUTHORIZED",
                "ticket_id": ticket_id}
    if head_tip is not None and head_tip.lower() != t.binding.head_sha.lower():
        return {"ok": False, "reason": "STALE_HEAD",
                "ticket_id": ticket_id}
    kw = {"actor": actor}
    if now:
        kw["now"] = now
    r = store.transition(ticket_id, "approve", **kw)
    return {"ok": r.ok, "status": r.status, "reason": r.reason,
            "ticket_id": ticket_id}


def authorize_reject(store, policy, ticket_id: str, actor: str, *,
                     now=None, reason: str = None) -> Dict[str, Any]:
    """策略校验 + CAS reject。reject 是收紧方向,策略要求同 approve
    (未配置政策下同样拒绝——不开放无政策的手工否决)。"""
    t = store.get(ticket_id)
    if t is None:
        return {"ok": False, "reason": "TICKET_NOT_FOUND"}
    if not _policy_configured(policy):
        return {"ok": False, "reason": "POLICY_NOT_CONFIGURED",
                "ticket_id": ticket_id}
    if not policy.allows_action(t.binding.action):
        return {"ok": False, "reason": "ACTION_NOT_ENABLED:%s" % t.binding.action,
                "ticket_id": ticket_id}
    if not policy.can_approve(actor, t.binding.repo):
        return {"ok": False, "reason": "APPROVER_NOT_AUTHORIZED",
                "ticket_id": ticket_id}
    kw = {"actor": actor}
    if now:
        kw["now"] = now
    if reason:
        kw["error"] = reason        # 拒绝原因落票据 error 字段(既有约定)
    r = store.transition(ticket_id, "reject", **kw)
    return {"ok": r.ok, "status": r.status, "reason": r.reason,
            "ticket_id": ticket_id}


def authorize_dispatch(policy, ticket) -> Dict[str, Any]:
    """派发前策略闸(dispatch 调用;ticket 须为 APPROVED)。

    未配置政策 → DISPATCH_CLOSED(fail-closed: feature flag 关闭时无法
    创建真实执行路径)。动作未启用 → 同样拒绝。"""
    if not _policy_configured(policy):
        return {"ok": False, "reason": "DISPATCH_CLOSED:POLICY_NOT_CONFIGURED"}
    if not policy.allows_action(ticket.binding.action):
        return {"ok": False,
                "reason": "DISPATCH_CLOSED:ACTION_NOT_ENABLED:%s"
                          % ticket.binding.action}
    return {"ok": True}
