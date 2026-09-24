"""阶段状态与 run 级 outcome(ARCHITECTURE-V3 §3)。

维度状态(每 run × 每维度)与 run 级 outcome 是两个正交枚举:
总状态绝不覆盖维度状态;部分完成/降级必须显式可见。
时间以字符串注入(ISO),不隐式取系统时钟。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── 维度状态 ────────────────────────────────────────────────────────────────
PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"
SKIPPED = "SKIPPED"
NOT_APPLICABLE = "NOT_APPLICABLE"
CANCELLED = "CANCELLED"

TERMINAL = frozenset({SUCCEEDED, FAILED, TIMEOUT, SKIPPED, NOT_APPLICABLE, CANCELLED})
UNFINISHED_BAD = frozenset({TIMEOUT, FAILED, SKIPPED})   # 影响"完整通过"的终态

_VALID_TRANSITIONS = {
    PENDING: {RUNNING, SKIPPED, NOT_APPLICABLE, CANCELLED},
    RUNNING: {SUCCEEDED, FAILED, TIMEOUT, CANCELLED},
    FAILED: {RUNNING},    # 仅限预算内重试;耗尽后停留(停留态即报告态)
    TIMEOUT: {RUNNING},   # 降级策略 delay 的预算内重入
    SUCCEEDED: set(), SKIPPED: set(), NOT_APPLICABLE: set(), CANCELLED: set(),
}

# ── run 级 outcome(与维度状态正交) ─────────────────────────────────────────
REVIEW_COMPLETED = "REVIEW_COMPLETED"
REVIEW_PARTIAL = "REVIEW_PARTIAL"
CONCLUSION_PUBLISHED = "CONCLUSION_PUBLISHED"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
FIXING = "FIXING"
PATCH_VALIDATING = "PATCH_VALIDATING"
WRITEBACK_OK = "WRITEBACK_OK"
WRITEBACK_FAILED = "WRITEBACK_FAILED"
MANUAL_ATTENTION = "MANUAL_ATTENTION"


@dataclass
class StageRecord:
    status: str = PENDING
    attempts: int = 0
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict:
        return {"status": self.status, "attempts": self.attempts,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "error": self.error, "detail": self.detail}


@dataclass
class RunStages:
    """一个 run 的全部维度状态。critical_reviewers 里的维度超时/失败
    → 不得发布为通过(只能 REVIEW_PARTIAL / MANUAL_ATTENTION)。"""
    critical_reviewers: tuple = ("security",)
    stages: Dict[str, StageRecord] = field(default_factory=dict)

    def record(self, dimension: str) -> StageRecord:
        return self.stages.setdefault(dimension, StageRecord())

    def transition(self, dimension: str, new_status: str, at: Optional[str] = None,
                   error: Optional[str] = None) -> bool:
        """守卫转移:非法转移返回 False(不抛,调用方记录);终态不可逆。"""
        rec = self.record(dimension)
        if new_status not in _VALID_TRANSITIONS.get(rec.status, set()):
            return False
        rec.status = new_status
        if new_status == RUNNING:
            rec.attempts += 1
            rec.started_at = at
        if new_status in TERMINAL:
            rec.ended_at = at
        if error is not None:
            rec.error = error[:200]
        return True

    def unfinished_dimensions(self) -> List[str]:
        return [d for d, r in self.stages.items() if r.status in UNFINISHED_BAD]

    def to_dict(self) -> Dict:
        return {d: r.to_dict() for d, r in sorted(self.stages.items())}


def derive_outcome(stages: RunStages,
                   conclusion_published: bool = False,
                   gate_approved: Optional[bool] = None,
                   fix_started: bool = False,
                   patch_validating: bool = False,
                   writeback_ok: Optional[bool] = None,
                   now: Optional[str] = None) -> Dict:
    """显式派生 run 级 outcome。规则(摘要):
    - 任一非豁免维度 TIMEOUT/FAILED/SKIPPED → 不得 REVIEW_COMPLETED;
    - 关键安全维度 TIMEOUT/FAILED → MANUAL_ATTENTION 或 REVIEW_PARTIAL,绝不通过;
    - CANCELLED(如 PR 更新使旧 run 失效)→ MANUAL_ATTENTION,不得视为完成;
    - 审查完成 ≠ 已发布 ≠ 回写成功,逐段独立判定。"""
    dims = stages.stages
    review_dims = [d for d, r in dims.items() if r.status != NOT_APPLICABLE]
    cancelled = [d for d, r in dims.items() if r.status == CANCELLED]
    reviewed = bool(review_dims) and all(
        dims[d].status == SUCCEEDED for d in review_dims)
    bad = stages.unfinished_dimensions()
    critical_bad = [d for d in bad
                    if d.split("review:", 1)[-1] in stages.critical_reviewers]

    if critical_bad or cancelled:
        outcome = MANUAL_ATTENTION
    elif not review_dims or any(dims[d].status == PENDING for d in review_dims):
        outcome = MANUAL_ATTENTION  # 尚未跑完也不得伪装
    elif bad:
        outcome = REVIEW_PARTIAL
    elif reviewed:
        outcome = REVIEW_COMPLETED
    else:
        outcome = MANUAL_ATTENTION

    if writeback_ok is True:
        outcome = WRITEBACK_OK
    elif writeback_ok is False:
        outcome = WRITEBACK_FAILED
    elif patch_validating:
        outcome = PATCH_VALIDATING
    elif fix_started:
        outcome = FIXING
    elif gate_approved is True:
        outcome = FIXING
    elif gate_approved is False:
        outcome = AWAITING_APPROVAL if outcome != MANUAL_ATTENTION else outcome
    elif conclusion_published and outcome in (REVIEW_COMPLETED, REVIEW_PARTIAL):
        outcome = CONCLUSION_PUBLISHED if outcome == REVIEW_COMPLETED else outcome

    note = ("部分完成:关键维度未成功,不得视为完整通过" if critical_bad
            else ("run 已失效(CANCELLED): %s" % ", ".join(cancelled) if cancelled
                  else ("覆盖不足: %s" % ", ".join(bad) if bad
                        else "全部维度成功" if reviewed else "审查未完成")))
    return {"outcome": outcome,
            "review_complete": outcome == REVIEW_COMPLETED,
            "coverage_missing": bad,
            "critical_failures": critical_bad,
            "cancelled_dimensions": cancelled,
            "note": note}
