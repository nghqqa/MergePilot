# -*- coding: utf-8 -*-
"""gate_ticket — 人工门标记 → 审批票据的最小闭环(2026-09-24)。

定位:把 CASE2 落地的结构化 gate marker(human-gate-required.json,桥已校验
归属)接入既有 M2 票据机制,补上"审批记录必须可审计"的一环。**不发明新票据
字段**——全部复用 approval.Binding/Ticket 既有形状与 TicketStore 协议:
  - action = "generate_patch"(批准即授权 fix 阶段生成补丁;merge/close/revert
    本就剥离于 ALLOWED_ACTIONS);
  - run 级审批:finding_id=None(target_key='_run_' 哨兵由存储层派生);
  - params_hash = canonical_hash(marker 载荷);finding_fingerprint =
    canonical_hash(task/severity/head 绑定)——既有字段,只做值派生。

不变式(与 D-1/D-2/D-3 一致):
  - marker 只是 Agent 请求:创建票据≠批准;批准必须经 CAS 且 D-2 要求
    非空身份(状态机 IDENT_REQUIRED);
  - 幂等:同 (run_id, action, finding_id) 活动票唯一(存储层 partial
    UNIQUE INDEX 跨进程强制),重复 marker 收敛同一张票;
  - fail-closed:marker 归属不符 / severity 非法 / 缺 repo、head → 拒绝创建;
    重复决策 NOOP/INVALID_TRANSITION(先到先得,不覆盖历史);过期 EXPIRED;
  - 审批记录可审计:决策经 store.transition,审计与状态写回同事务
    (SQLite/PG 均落 ticket_audit);
  - 本模块对 run 的终态只读:状态映射是纯函数,不写 run 存储、不改
    已完成 run 的终态、不绕过 PG/CAS/fencing。

approve 之后的产物是**派发计划数据**(纯数据,不派发):fixer/verifier 仍
默认禁用,真实派发属后续授权。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional, Tuple

from .approval import Binding, canonical_hash

GATE_ACTION = "generate_patch"   # 批准人工门 = 授权进入 fix(generate_patch)阶段

_SEVERITIES = ("HIGH", "MEDIUM", "LOW")


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _plus_hours_iso(now_iso: str, hours: int) -> str:
    base = dt.datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    return (base + dt.timedelta(hours=hours)).isoformat()


def validate_marker_for_ticket(marker: Dict[str, Any], run_id: str,
                               task_id: str) -> Optional[str]:
    """创建前最小区校验(桥已做归属校验;此处二次防编程错误)。

    返回 None=通过;否则返回拒绝原因。任何不符 → 调用方必须放弃创建。"""
    if not isinstance(marker, dict):
        return "marker not object"
    if marker.get("version") != 1:
        return "marker version mismatch"
    if marker.get("run_id") != run_id:
        return "marker run_id mismatch (attribution refused)"
    if task_id is not None and marker.get("task_id") != task_id:
        return "marker task_id mismatch"
    if marker.get("severity") not in _SEVERITIES:
        return "marker severity invalid"
    if marker.get("requested_by") != "leader":
        return "marker requested_by must be leader"
    return None


def marker_binding(marker: Dict[str, Any], run_id: str, repo: str,
                   head_sha: str, task_id: str) -> Binding:
    """marker + 投递上下文 → Binding(既有字段;值派生自可信桥上下文)。

    repo/head_sha 必须来自投递行/manifest(可信),不得来自 marker 文本。"""
    payload = {
        "gate_version": marker.get("version"),
        "severity": marker.get("severity"),
        "task_id": task_id,
        "requested_by": marker.get("requested_by"),
        "requested_at": marker.get("requested_at"),
    }
    finding_fp = canonical_hash({"task_id": task_id, "severity": marker.get("severity"),
                                 "repo": repo, "head_sha": head_sha})
    return Binding(run_id=run_id, repo=repo, head_sha=head_sha,
                   action=GATE_ACTION, params_hash=canonical_hash(payload),
                   finding_fingerprint=finding_fp, finding_id=None)  # run 级审批


def open_gate_ticket(store, marker: Dict[str, Any], run_id: str, repo: str,
                     head_sha: str, task_id: str, ttl_hours: int = 24,
                     now: Optional[str] = None) -> Tuple[Any, bool, str]:
    """marker → pending ticket(幂等)。返回 (ticket, created, reason)。

    fail-closed:校验不符时返回 (None, False, reason),不创建任何记录。
    TTL(D-3,默认 24h——对齐 policy.py 既有默认与 AUTH-DECISION-PACKAGE l2
    惯例):批准边界;过期由状态机 approve→EXPIRED。"""
    why = validate_marker_for_ticket(marker, run_id, task_id)
    if why:
        return None, False, why
    binding = marker_binding(marker, run_id, repo, head_sha, task_id)
    now_iso = now or _utc_now_iso()
    ticket, created = store.create(
        binding, attempt_no=1, created_at=now_iso, created_by_run=run_id,
        approval_expires_at=_plus_hours_iso(now_iso, ttl_hours))
    return ticket, created, ""


def decide(store, ticket_id: str, decision: str, actor: Optional[str],
           now: Optional[str] = None, reason: Optional[str] = None):
    """人工决策(approve/reject)。CAS:先到先得,重复决策不覆盖历史。

    D-2:approve 无身份 → 状态机 IDENT_REQUIRED 拒绝(不匿名放行)。
    reason 经审计 request_hash 落库(append-only)。"""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve|reject")
    kw: Dict[str, Any] = {"now": now} if now else {}
    if actor:
        kw["actor"] = actor
    if reason:
        kw["error"] = reason     # 拒绝原因落在票据 error 字段(既有形状)
    result = store.transition(ticket_id, decision, **kw)
    if result.ok and decision == "reject" and reason:
        # reject 的 reason 已入票;此处再补一条带 reason 的审计痕迹由存储层
        # request_hash 承载(result.reason),无需二次写。
        pass
    return result


def expire_if_due(store, ticket_id: str, now: Optional[str] = None):
    """到期票据显式过期(桥周期任务可选调用;状态机保证只对 PENDING/APPROVED 生效)。"""
    kw: Dict[str, Any] = {"now": now} if now else {}
    return store.transition(ticket_id, "expire", **kw)


# ── gate 状态映射(纯函数;对 run 终态只读,不写任何 run 存储) ─────────────
def run_gate_state(ticket_status: Optional[str]) -> Dict[str, Any]:
    """票据状态 → run 级门状态 + 后续动作计划(纯数据,不派发)。

    pending ticket    → GATE_WAIT(run 保持 action_required/GATE_WAIT);
    approve           → APPROVED_PLAN_READY(生成 fix/verify 派发计划数据,
                        blocked-by-policy:不自动派发 fixer/verifier);
    reject            → BLOCKED;
    expire            → CLOSED_EXPIRED;
    EXECUTING/USED    → EXECUTING/DONE(后续阶段,本轮不进入);
    FAILED/INVALIDATED→ FAILED/INVALIDATED(允许新一轮的新票据)。
    """
    if ticket_status is None:
        return {"gate_state": "NO_TICKET", "dispatch_plan": None,
                "auto_dispatch": False}
    if ticket_status == "PENDING":
        return {"gate_state": "GATE_WAIT", "dispatch_plan": None,
                "auto_dispatch": False}
    if ticket_status == "APPROVED":
        return {"gate_state": "APPROVED_PLAN_READY",
                "dispatch_plan": {
                    "fix": {"action": GATE_ACTION, "auto_dispatch": False,
                            "note": "plan-only; fixer dispatch requires separate authorization"},
                    "verify": {"action": "verify", "auto_dispatch": False}},
                "auto_dispatch": False}
    if ticket_status == "REJECTED":
        return {"gate_state": "BLOCKED", "dispatch_plan": None,
                "auto_dispatch": False}
    if ticket_status == "EXPIRED":
        return {"gate_state": "CLOSED_EXPIRED", "dispatch_plan": None,
                "auto_dispatch": False}
    return {"gate_state": ticket_status, "dispatch_plan": None,
            "auto_dispatch": False}
