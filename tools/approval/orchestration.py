# -*- coding: utf-8 -*-
"""orchestration — 确定性建票控制面(2026-09-24 建票所有权冻结轮)。

架构决策:
  - 票据创建决定权从 Leader/LLM 收归控制面:action 选择是**配置化纯函数**,
    输入只有结构化 ReviewOutcome + ApprovalPolicy;模型自由文本、result.md
    文案或 marker 缺失都不会导致 HIGH finding 丢票(marker 只是兼容信号)。
  - ensure_gate_ticket 幂等:同 (run_id, action, finding_id) 活动票唯一
    (存储层 partial UNIQUE INDEX 跨进程强制);重放返回同一张票;桥崩溃
    恢复后重新执行 ensure 收敛同一结果。
  - 顺序纪律:同一 finding 的 run_poc → generate_patch 有序推进,不并存
    两张无序活动票(本 finding 已有其他 action 的活动票时拒绝新建)。
  - 旧 head 失效:同 repo 活动票绑定的 head ≠ 当前 head → 一律
    invalidate_for_new_head(确定性,规格 §1 四问3)。
  - 创建 ≠ 批准:本模块**绝不**调用 authorize_approval/decide;自动建票
    终态是 PENDING,决策只能来自具名审批人经 CAS(gate_cli/console)。

模式(feature flag,允许回滚但不恢复模型票据所有权):
  - "auto"    :确定性建票(默认);
  - "observe" :只读观察——完整计算将要创建的票,但不写任何状态。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .approval import Binding, canonical_hash
from .policy import ApprovalPolicy, UnconfiguredPolicy

GATE_SEVERITIES = ("HIGH", "CRITICAL")
POC_ACTION = "run_poc"
PATCH_ACTION = "generate_patch"
# 永久禁止进入选择词表的动作(即便未来 ALLOWED_ACTIONS 扩容也不得选入)
FORBIDDEN_ACTIONS = frozenset({"push_branch", "merge", "close", "revert"})
FORBIDDEN_ACTIONS_IS_FROZEN = True   # 冻结声明:移除须显式架构决策

CREATOR_ID = "control-plane:ensure"
MODE_AUTO = "auto"
MODE_OBSERVE = "observe"


@dataclass(frozen=True)
class EnsureTicketResult:
    """ensure_gate_ticket 的确定性结果(纯数据,无副作用语义)。"""
    ok: bool
    reason: str = ""                     # OK/OBSERVE_MODE/POLICY_NOT_CONFIGURED/...
    ticket_id: Optional[str] = None
    created: bool = False
    action: Optional[str] = None
    marker_status: str = "ABSENT"        # ABSENT/COMPATIBLE/CONFLICT:<detail>
    outcome_digest: Optional[str] = None
    invalidated: List[str] = field(default_factory=list)   # 被新 head 失效的旧票


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _plus_hours_iso(now_iso: str, hours: int) -> str:
    base = dt.datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    return (base + dt.timedelta(hours=hours)).isoformat()


def policy_fingerprint(policy: Any) -> str:
    """政策版本:显式 policy_version 优先;否则内容规范哈希(确定性)。"""
    version = getattr(policy, "policy_version", "")
    if version:
        return str(version)
    body = getattr(policy, "to_dict", lambda: {"configured": False})()
    return canonical_hash(body)[:12]


def validate_outcome_shape(outcome: Any) -> Optional[str]:
    """建票前置最小形状校验(数据源侧已校验;此处防编程错误,纯内存)。"""
    if not isinstance(outcome, dict):
        return "outcome not object"
    if outcome.get("schema_version") != "review-outcome.v1":
        return "outcome schema_version mismatch"
    for key in ("run_id", "repo", "head_sha", "finding_validation"):
        if not outcome.get(key):
            return "outcome binding incomplete: %s" % key
    import re
    if not re.fullmatch(r"[0-9a-f]{40}", str(outcome["head_sha"])):
        return "outcome head_sha not 40hex"
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", str(outcome["repo"])):
        return "outcome repo must be owner/name"
    if outcome["finding_validation"] == "CONFIRMED" and not outcome.get("findings"):
        return "CONFIRMED outcome without findings"
    return None


def select_action(outcome: Dict[str, Any], policy: Any) -> Tuple[Optional[str], str]:
    """确定性动作选择(纯函数;HIGH/CRITICAL + CONFIRMED 才进本函数语义)。

    - CONFIRMED + 无本 run/head 的结构化独立验证 → run_poc;
    - CONFIRMED + 已有本 run/head 结构化独立验证 → generate_patch;
    - publish_result 当前未启用;push_branch/merge/close/revert 永久禁止;
    - INCONCLUSIVE 不自动建 generate_patch 票(也不建 run_poc)。
    返回 (action|None, reason)。"""
    if not getattr(policy, "configured", False):
        return None, "POLICY_NOT_CONFIGURED"
    fv = outcome.get("finding_validation")
    if fv != "CONFIRMED":
        return None, "NO_CONFIRMED_FINDING:%s" % fv
    findings = outcome.get("findings") or []
    if not findings:
        return None, "NO_FINDING"
    severity = findings[0].get("severity")
    if severity not in GATE_SEVERITIES:
        return None, "SEVERITY_BELOW_GATE:%s" % severity
    action = PATCH_ACTION if has_current_run_validation(outcome) else POC_ACTION
    if action in FORBIDDEN_ACTIONS:
        return None, "ACTION_FORBIDDEN:%s" % action
    if not policy.allows_action(action):
        return None, "ACTION_NOT_ENABLED:%s" % action
    approvers = (getattr(policy, "approver_map", {}) or {}).get(outcome["repo"]) or []
    if not approvers:
        return None, "NO_APPROVER_CONFIGURED:%s" % outcome["repo"]
    return action, "OK"


def has_current_run_validation(outcome: Dict[str, Any]) -> bool:
    for v in outcome.get("validations") or []:
        if (v.get("run_id") == outcome.get("run_id")
                and v.get("head_sha") == outcome.get("head_sha")):
            return True
    return False


def reconcile_marker(outcome: Dict[str, Any], marker: Optional[Dict[str, Any]]) -> str:
    """marker 兼容裁决(纯函数):缺失=ABSENT;一致=COMPATIBLE;不符=CONFLICT。

    结构化 outcome 永远权威;冲突只记录,不执行 marker 请求的额外动作。"""
    if not marker:
        return "ABSENT"
    m_sev = marker.get("severity")
    findings = outcome.get("findings") or []
    o_sev = findings[0].get("severity") if findings else None
    gate = (outcome.get("finding_validation") == "CONFIRMED"
            and o_sev in GATE_SEVERITIES)
    if gate and m_sev == o_sev:
        return "COMPATIBLE"
    return "CONFLICT:marker_severity=%s,outcome=%s/%s" % (
        m_sev, outcome.get("finding_validation"), o_sev or "none")


def ensure_gate_ticket(review_outcome: Dict[str, Any], policy: Any, store: Any,
                       *, now: Optional[str] = None, mode: str = MODE_AUTO,
                       marker: Optional[Dict[str, Any]] = None,
                       ttl_hours: Optional[int] = None,
                       expected_head_sha: Optional[str] = None) -> EnsureTicketResult:
    """结构化 ReviewOutcome → 幂等审批票据(确定性控制面;创建≠批准)。

    - 同 (run_id, action, finding_id) 活动票唯一;重放返回同一张票;
    - marker 缺失不阻塞;marker 冲突仅记录(outcome 权威);
    - policy 未配置 / 动作未启用 / 绑定不完整 → 拒绝(ok=False);
    - 同 repo 旧 head 活动票先失效(锚 = expected_head_sha,来自投递行的
      可信当前 head;未提供时退化为 outcome.head_sha——调用方必须已在
      数据源侧对 write-once manifest 校验过);
    - expected_head_sha 提供且与 outcome 不符 → STALE_HEAD_OUTCOME 拒绝
      (旧 outcome 重放不得失效当前票);
    - 每次 ensure 落 append-only 审计(创建与重放都留痕);
    - mode="observe":只计算不落库(回滚观察模式)。"""
    marker_status = reconcile_marker(review_outcome if isinstance(review_outcome, dict)
                                     else {}, marker)
    digest = (review_outcome or {}).get("outcome_digest") if isinstance(review_outcome, dict) else None
    try:
        digest = digest or canonical_hash(review_outcome)
    except Exception:
        digest = None

    if mode == MODE_OBSERVE:
        action, reason = select_action(review_outcome, policy) if isinstance(
            review_outcome, dict) else (None, "outcome not object")
        return EnsureTicketResult(ok=False, reason="OBSERVE_MODE:no_state_written;"
                                  "would_action=%s(%s)" % (action, reason),
                                  action=action, marker_status=marker_status,
                                  outcome_digest=digest)

    why = validate_outcome_shape(review_outcome)
    if why:
        return EnsureTicketResult(ok=False, reason=why, marker_status=marker_status,
                                  outcome_digest=digest)

    # 新鲜度锚:可信当前 head(投递行)与 outcome 不符 → 旧 outcome 重放,拒绝。
    # 不失效任何票——失效方向必须由"当前 head"驱动,不能由历史文档驱动。
    current_head = expected_head_sha or review_outcome["head_sha"]
    if expected_head_sha and review_outcome["head_sha"] != expected_head_sha:
        return EnsureTicketResult(ok=False, reason="STALE_HEAD_OUTCOME",
                                  marker_status=marker_status, outcome_digest=digest)

    action, reason = select_action(review_outcome, policy)
    if action is None:
        return EnsureTicketResult(ok=False, reason=reason, marker_status=marker_status,
                                  outcome_digest=digest)

    # 旧 head 失效先于顺序纪律:同 repo 活动票 head ≠ 当前(可信)head → invalidate
    invalidated: List[str] = []
    for t in store.active_by_repo(review_outcome["repo"]):
        if t.binding.head_sha != current_head:
            r = store.transition(t.ticket_id, "invalidate_for_new_head",
                                 actor=str(review_outcome["head_sha"]))
            if r.ok:
                invalidated.append(t.ticket_id)

    # 顺序/漂移纪律(仅本 run 同 finding 的活动票):
    #   其他 action 活动票 → 拒绝(run_poc → generate_patch 有序,不无序并存);
    #   同 action 不同 finding_id → outcome 漂移,拒绝(同一 run 不换票)。
    finding_id = review_outcome["findings"][0]["finding_id"]
    for other in _active_for_run(store, review_outcome):
        if other.binding.finding_id != finding_id:
            if other.binding.action == action:
                return EnsureTicketResult(
                    ok=False, reason="OUTCOME_DRIFT:active finding_id differs",
                    action=action, marker_status=marker_status,
                    outcome_digest=digest, invalidated=invalidated)
            continue   # 同 run 的其他 finding(理论上单 finding;不干预)
        if other.binding.action != action:
            return EnsureTicketResult(
                ok=False, reason="SEQUENTIAL_GATE_REQUIRED:active=%s" % other.binding.action,
                action=action, marker_status=marker_status, outcome_digest=digest,
                invalidated=invalidated)

    finding = review_outcome["findings"][0]
    pf = policy_fingerprint(policy)
    params = {"action": action, "finding_id": finding["finding_id"],
              "finding_validation": review_outcome["finding_validation"],
              "pr_number": review_outcome.get("pr_number"),
              "severity": finding["severity"], "policy_version": pf}
    binding = Binding(run_id=review_outcome["run_id"], repo=review_outcome["repo"],
                      head_sha=review_outcome["head_sha"], action=action,
                      params_hash=canonical_hash(params),
                      finding_fingerprint=finding["fingerprint"],
                      finding_id=finding["finding_id"])
    now_iso = now or _utc_now_iso()
    ttl = int(ttl_hours if ttl_hours is not None else getattr(policy, "ttl_hours", 24) or 24)
    ticket, created = store.create(binding, attempt_no=1, created_at=now_iso,
                                   created_by_run=review_outcome["run_id"],
                                   approval_expires_at=_plus_hours_iso(now_iso, ttl))
    # append-only 审计:创建与重放都留痕(request_hash 存可读事件串,与
    # transition 的 reason 用法一致;outcome 摘要单独成列值以便复核)
    store.record_event(
        ticket.ticket_id, None, ticket.status, CREATOR_ID,
        "ENSURE_%s|action=%s|marker=%s|outcome=%s|invalidated=%d|policy=%s"
        % ("CREATED" if created else "REPLAYED", action, marker_status,
           (digest or "?")[:16], len(invalidated), pf))
    return EnsureTicketResult(ok=True, reason="OK", ticket_id=ticket.ticket_id,
                              created=created, action=action,
                              marker_status=marker_status, outcome_digest=digest,
                              invalidated=invalidated)


def _active_for_run(store: Any, outcome: Dict[str, Any]) -> List[Any]:
    """同 run 的全部活动票(跨 action/finding;供顺序与漂移纪律判定)。"""
    active = store.active_by_repo(outcome["repo"])
    return [t for t in active if t.binding.run_id == outcome["run_id"]]
