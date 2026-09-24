# -*- coding: utf-8 -*-
"""enforce — D-B 正式审批的策略执行点(批准前收口新增)。

把 policy.py 的配置接到 approve/派发 两个动作上;所有检查 fail-closed:
  - policy 未配置(UnconfiguredPolicy) → 一律拒绝;
  - 动作不在 D-1 启用子集 → 拒绝;
  - 审批人不在该 repo 的具名审批人集 → 拒绝(D-2,稳定身份=GitHub node ID);
  - 可选 head 新鲜度: 提供 tip 时与票据绑定 head 不符 → 拒绝(不产生 CAS);
  - 通过后仍走 store 的 CAS approve(先到先得,过期 EXPIRED,重复 NOOP)。

身份模型(2026-09-24 收口收紧):
  * `actor_id` = 稳定 GitHub node ID(如 `MDQ6VXNlcjM1OTg3NDg=`),唯一且不可变;
  * `actor_login` = 可变 GitHub login(仅展示);两者同时传入时审计双记;
  * 策略比较 (`can_approve`) **只用 actor_id**,绝不比较 login;
  * 未提供 actor_id → 拒绝(fail-closed: 不可用 login 代替 node_id 授权)。

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


def authorize_approval(store, policy, ticket_id: str, *, actor_id: str,
                       actor_login: str = None, head_tip: str = None,
                       now=None, reason: str = None) -> Dict[str, Any]:
    """策略校验 + CAS approve。返回 {ok, status, reason, ticket_id}。

    actor_id: 稳定 GitHub node ID(必填,授权主键);
    actor_login: 可变 login(仅展示,审计双记)。
    任一策略检查失败 → 不触碰票据状态(无副作用拒绝)。"""
    t = store.get(ticket_id)
    if t is None:
        return {"ok": False, "reason": "TICKET_NOT_FOUND",
                "ticket_id": ticket_id}
    if not _policy_configured(policy):
        return {"ok": False, "reason": "POLICY_NOT_CONFIGURED",
                "ticket_id": ticket_id}
    if not policy.allows_action(t.binding.action):
        return {"ok": False, "reason": "ACTION_NOT_ENABLED:%s" % t.binding.action,
                "ticket_id": ticket_id}
    if not actor_id or not str(actor_id).strip():
        return {"ok": False, "reason": "ACTOR_ID_REQUIRED",
                "ticket_id": ticket_id}
    if not policy.can_approve(actor_id, t.binding.repo):
        return {"ok": False, "reason": "APPROVER_NOT_AUTHORIZED",
                "ticket_id": ticket_id}
    if head_tip is not None and head_tip.lower() != t.binding.head_sha.lower():
        return {"ok": False, "reason": "STALE_HEAD", "ticket_id": ticket_id}
    kw: Dict[str, Any] = {"actor": actor_id}
    if now:
        kw["now"] = now
    r = store.transition(ticket_id, "approve", **kw)
    return {"ok": r.ok, "status": r.status, "reason": r.reason,
            "ticket_id": ticket_id, "actor_id": actor_id,
            "actor_login": actor_login}


def authorize_reject(store, policy, ticket_id: str, *, actor_id: str,
                     actor_login: str = None, head_tip: str = None,
                     now=None, reason: str = None) -> Dict[str, Any]:
    """策略校验 + CAS reject。reject 是收紧方向,策略要求同 approve
    (未配置政策下同样拒绝——不开放无政策的手工否决)。"""
    t = store.get(ticket_id)
    if t is None:
        return {"ok": False, "reason": "TICKET_NOT_FOUND",
                "ticket_id": ticket_id}
    if not _policy_configured(policy):
        return {"ok": False, "reason": "POLICY_NOT_CONFIGURED",
                "ticket_id": ticket_id}
    if not policy.allows_action(t.binding.action):
        return {"ok": False, "reason": "ACTION_NOT_ENABLED:%s" % t.binding.action,
                "ticket_id": ticket_id}
    if not actor_id or not str(actor_id).strip():
        return {"ok": False, "reason": "ACTOR_ID_REQUIRED",
                "ticket_id": ticket_id}
    if not policy.can_approve(actor_id, t.binding.repo):
        return {"ok": False, "reason": "APPROVER_NOT_AUTHORIZED",
                "ticket_id": ticket_id}
    kw: Dict[str, Any] = {"actor": actor_id}
    if now:
        kw["now"] = now
    if reason:
        kw["error"] = reason
    r = store.transition(ticket_id, "reject", **kw)
    return {"ok": r.ok, "status": r.status, "reason": r.reason,
            "ticket_id": ticket_id, "actor_id": actor_id,
            "actor_login": actor_login}


def authorize_dispatch(policy, ticket) -> Dict[str, Any]:
    """派发前策略闸(dispatch 调用;ticket 须为 APPROVED)。

    未配置政策 → DISPATCH_CLOSED(fail-closed: feature flag 关闭时无法
    创建真实执行路径)。动作未启用 → 同样拒绝。
    REJECTED/EXPIRED/FAILED/INVALIDATED 状态 → 派发关闭(仅 APPROVED/EXECUTING 可)。"""
    if not _policy_configured(policy):
        return {"ok": False, "reason": "DISPATCH_CLOSED:POLICY_NOT_CONFIGURED"}
    if not policy.allows_action(ticket.binding.action):
        return {"ok": False,
                "reason": "DISPATCH_CLOSED:ACTION_NOT_ENABLED:%s"
                          % ticket.binding.action}
    status = getattr(ticket, "status", "")
    if status in ("REJECTED", "EXPIRED", "FAILED", "INVALIDATED"):
        return {"ok": False,
                "reason": "DISPATCH_CLOSED:TICKET_%s" % status}
    return {"ok": True}
