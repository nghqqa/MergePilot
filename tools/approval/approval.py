"""M2 审批票据核心:绑定五元组 + CAS 状态机 + 执行前校验。

设计约束(规格 §1–§3,红线="批准了 A、执行了 B"):
- 绑定不可变:票据创建后 run/repo/head_sha/指纹/params_hash 不再变更;
- 转移单方向 CAS:PENDING 是唯一分支态,approve/reject 先到先得;
- 校验纯函数:任何拒绝都发生在副作用之前,校验器自身无 IO。
时间以参数注入(now),不隐式取系统时钟——便于竞争与过期测试。
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional

# ── 状态与动作 ────────────────────────────────────────────────────────────
PENDING = "PENDING"
APPROVED = "APPROVED"
EXECUTING = "EXECUTING"
USED = "USED"            # 终态:批准的动作已完成且结果指纹已回填(≠ 旧 merge 语义)
REJECTED = "REJECTED"    # 终态
EXPIRED = "EXPIRED"      # 终态
FAILED = "FAILED"        # 终态,允许新 attempt
INVALIDATED = "INVALIDATED"  # 终态:PR 出现新 head(规格 §1 四问3),允许新 run 的新 attempt

TERMINAL_STATES = frozenset({USED, REJECTED, EXPIRED, FAILED, INVALIDATED})
ACTIVE_STATES = frozenset({PENDING, APPROVED, EXECUTING})
EXECUTABLE_STATES = frozenset({APPROVED, EXECUTING})

# V0 候选动作集(规格 §1)。启用子集是决策项 D-1;merge/close/revert 明确剥离。
ALLOWED_ACTIONS = frozenset({"generate_patch", "run_poc", "publish_result"})

_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_hash(value: Any) -> str:
    """规范序列化 sha256(执行参数/补丁指纹统一用此)。键排序、紧凑分隔符。"""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── 数据模型 ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Binding:
    """票据绑定的执行绑定(规格 §2 五元组+上下文)。不可变。"""

    run_id: str
    repo: str            # owner/name 全名
    head_sha: str        # 被审查 commit 完整 SHA
    action: str
    params_hash: str     # 执行参数规范哈希(canonical_hash)
    patch_fingerprint: Optional[str] = None      # 补丁内容 sha256;无补丁动作为 None
    finding_fingerprint: Optional[str] = None    # 无补丁动作(如 run_poc)绑 finding 指纹
    finding_id: Optional[str] = None


@dataclass
class Ticket:
    """审批票据。binding 不可变;status 与审计字段随转移演进。"""

    ticket_id: str
    binding: Binding
    attempt_no: int
    status: str = PENDING
    created_at: str = ""          # ISO 文本,签发方填
    created_by_run: str = ""
    approval_expires_at: Any = None   # 可比较对象(datetime 等),签发方定(D-3 参数化)
    approved_by: Optional[str] = None
    approved_at: Any = None
    result_fingerprint: Optional[str] = None
    error: Optional[str] = None

    def snapshot(self) -> "Ticket":
        return copy.deepcopy(self)


@dataclass(frozen=True)
class ExecutionRequest:
    """执行方出示的请求(规格 §3)。五元组必须与票据绑定逐项相等。"""

    ticket_id: str
    run_id: str
    repo: str
    head_sha: str
    params_hash: str
    patch_fingerprint: Optional[str] = None
    finding_fingerprint: Optional[str] = None


@dataclass(frozen=True)
class TransitionResult:
    """转移结果。状态竞争仲裁用返回值,不用异常控制流。"""

    ok: bool
    status: str
    reason: str = ""    # OK / NOOP / INVALID_TRANSITION:<from> / EXPIRED / IDENT_REQUIRED


# ── 形状校验(create 前置) ────────────────────────────────────────────────
def validate_binding_shape(b: Binding) -> None:
    """违反即 ValueError(编程错误,非运行时竞争)。"""
    if not b.run_id or not isinstance(b.run_id, str):
        raise ValueError("BINDING: run_id 必须非空字符串")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", b.repo or ""):
        raise ValueError("BINDING: repo 必须是 owner/name 全名")
    if not _HEAD_SHA_RE.fullmatch(b.head_sha or ""):
        raise ValueError("BINDING: head_sha 必须 40hex 完整 SHA")
    if b.action not in ALLOWED_ACTIONS:
        raise ValueError("BINDING: action 必须 ∈ %s(merge/close/revert 已剥离)" % sorted(ALLOWED_ACTIONS))
    if not _HASH64_RE.fullmatch(b.params_hash or ""):
        raise ValueError("BINDING: params_hash 必须 64hex(canonical_hash)")
    for name, fp in (("patch_fingerprint", b.patch_fingerprint),
                     ("finding_fingerprint", b.finding_fingerprint)):
        if fp is not None and not _HASH64_RE.fullmatch(fp):
            raise ValueError("BINDING: %s 必须 64hex 或省略" % name)
    if b.patch_fingerprint is None and b.finding_fingerprint is None:
        raise ValueError("BINDING: patch_fingerprint 与 finding_fingerprint 至少其一")


def create_ticket(binding: Binding, attempt_no: int = 1, created_at: str = "",
                  created_by_run: str = "", approval_expires_at: Any = None) -> Ticket:
    validate_binding_shape(binding)
    if attempt_no < 1:
        raise ValueError("attempt_no 必须 ≥1")
    return Ticket(
        ticket_id="tkt-" + uuid.uuid4().hex,
        binding=binding,
        attempt_no=attempt_no,
        created_at=created_at,
        created_by_run=created_by_run,
        approval_expires_at=approval_expires_at,
    )


def _expired(ticket: Ticket, now: Any) -> bool:
    """now 与 expires 需同一时序域:ISO 字符串与 aware datetime 均可(统一解析为 UTC)。"""
    if ticket.approval_expires_at is None or now is None:
        return False
    now_v = _coerce_dt(now)
    exp_v = _coerce_dt(ticket.approval_expires_at)
    if now_v is None or exp_v is None:
        return False
    return now_v >= exp_v


def _coerce_dt(v: Any) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    if isinstance(v, str):
        try:
            d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return None


# ── 状态机(CAS 转移) ─────────────────────────────────────────────────────
def transition(ticket: Ticket, event: str, now: Any = None,
               actor: Optional[str] = None,
               result_fingerprint: Optional[str] = None,
               error: Optional[str] = None) -> TransitionResult:
    """单步 CAS 转移。成功时原地更新 ticket 并返回 ok=True。

    仲裁规则(规格 §1 四问4):先到先得;对已处目标态的幂等重放返回 NOOP;
    其余前置态不匹配返回 INVALID_TRANSITION,不生效、不覆盖。
    审批期限(approval_expires_at)只约束授权边界事件(approve/start_exec):
    已开始的执行允许收尾(complete/fail 永远可走,这是安全方向)。
    """
    if (event == "start_exec" and _expired(ticket, now)):
        if ticket.status in ACTIVE_STATES:
            transition(ticket, "expire", now=now)
        return TransitionResult(False, ticket.status, "EXPIRED")

    if event == "approve":
        if ticket.status == APPROVED:
            return TransitionResult(True, APPROVED, "NOOP")  # 幂等重放,不触发过期
        if ticket.status != PENDING:
            return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)
        if _expired(ticket, now):
            transition(ticket, "expire", now=now)
            return TransitionResult(False, ticket.status, "EXPIRED")
        if not actor or not str(actor).strip():
            return TransitionResult(False, PENDING, "IDENT_REQUIRED")  # D-2:不得匿名放行
        ticket.status = APPROVED
        ticket.approved_by = actor
        ticket.approved_at = now
        return TransitionResult(True, APPROVED, "OK")

    if event == "reject":
        if ticket.status == REJECTED:
            return TransitionResult(True, REJECTED, "NOOP")
        if ticket.status != PENDING:
            return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)
        ticket.status = REJECTED
        ticket.approved_by = actor  # 记录拒绝人(可空:拒绝是收紧,不要求身份非空)
        return TransitionResult(True, REJECTED, "OK")

    if event == "start_exec":
        if ticket.status != APPROVED:
            return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)
        ticket.status = EXECUTING
        return TransitionResult(True, EXECUTING, "OK")

    if event == "complete":
        if ticket.status != EXECUTING:
            return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)
        if result_fingerprint is None:
            return TransitionResult(False, EXECUTING, "RESULT_FP_REQUIRED")
        ticket.status = USED
        ticket.result_fingerprint = result_fingerprint
        return TransitionResult(True, USED, "OK")

    if event == "fail":
        if ticket.status != EXECUTING:
            return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)
        ticket.status = FAILED
        ticket.error = error
        return TransitionResult(True, FAILED, "OK")

    if event == "expire":
        if ticket.status in (PENDING, APPROVED):
            ticket.status = EXPIRED
            return TransitionResult(True, EXPIRED, "OK")
        if ticket.status == EXPIRED:
            return TransitionResult(True, EXPIRED, "NOOP")
        return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)

    if event == "invalidate_for_new_head":
        new_head = str(actor or "")
        if ticket.binding.head_sha == new_head:
            return TransitionResult(False, ticket.status, "NOT_A_NEW_HEAD")
        if ticket.status in ACTIVE_STATES:
            ticket.status = INVALIDATED
            ticket.error = "new head %s" % new_head[:12]
            return TransitionResult(True, INVALIDATED, "OK")
        if ticket.status == INVALIDATED:
            return TransitionResult(True, INVALIDATED, "NOOP")
        return TransitionResult(False, ticket.status, "INVALID_TRANSITION:%s" % ticket.status)

    return TransitionResult(False, ticket.status, "UNKNOWN_EVENT:%s" % event)


# ── 执行前校验(红线防线,规格 §3) ────────────────────────────────────────
def check_execution(ticket: Ticket, request: ExecutionRequest,
                    now: Any = None) -> TransitionResult:
    """执行方在任何副作用前调用。ok=False 时 reason 指明拒绝项。

    期限语义:APPROVED(未开始)受 approval_expires_at 约束——期限前必须开始;
    EXECUTING(已开始)的时长由编排侧执行期限管理(M1 watch deadline / exec TTL),
    不再用审批期限拦截,避免误杀在途执行。
    """
    if request.ticket_id != ticket.ticket_id:
        return TransitionResult(False, ticket.status, "TICKET_MISMATCH")
    if ticket.status not in EXECUTABLE_STATES:
        return TransitionResult(False, ticket.status, "NOT_EXECUTABLE:%s" % ticket.status)
    if ticket.status == APPROVED and _expired(ticket, now):
        return TransitionResult(False, ticket.status, "EXPIRED")
    b = ticket.binding
    pairs = (
        ("run_id", request.run_id, b.run_id),
        ("repo", request.repo, b.repo),
        ("head_sha", request.head_sha, b.head_sha),
        ("params_hash", request.params_hash, b.params_hash),
    )
    if b.patch_fingerprint is not None:
        pairs += (("patch_fingerprint", request.patch_fingerprint, b.patch_fingerprint),)
    if b.finding_fingerprint is not None:
        pairs += (("finding_fingerprint", request.finding_fingerprint, b.finding_fingerprint),)
    for fname, got, want in pairs:
        if got != want:
            return TransitionResult(False, ticket.status, "BINDING_MISMATCH:%s" % fname)
    return TransitionResult(True, ticket.status, "OK")


# ── 参考存储(幂等创建 + 串行化转移) ──────────────────────────────────────
class InMemoryTicketStore:
    """单进程参考实现:演示幂等创建与 CAS 仲裁的串行语义。
    生产存储(PG/MinIO)在门 Web 化工作项接入,须保持同样语义。"""

    def __init__(self) -> None:
        self._tickets: Dict[str, Ticket] = {}

    @staticmethod
    def _key(binding: Binding) -> str:
        return "|".join([binding.run_id, binding.action, binding.finding_id or ""])

    def create(self, binding: Binding, **kw: Any):
        """同 (run_id, action, finding_id) 有活动票 → 返回既有票(幂等,created=False)。"""
        validate_binding_shape(binding)
        for t in self._tickets.values():
            if self._key(t.binding) == self._key(binding) and t.status in ACTIVE_STATES:
                return t, False
        t = create_ticket(binding, **kw)
        self._tickets[t.ticket_id] = t
        return t, True

    def get(self, ticket_id: str) -> Optional[Ticket]:
        return self._tickets.get(ticket_id)

    def transition(self, ticket_id: str, event: str, **kw: Any) -> TransitionResult:
        t = self._tickets.get(ticket_id)
        if t is None:
            return TransitionResult(False, "?", "NOT_FOUND")
        return transition(t, event, **kw)

    def active_for(self, binding: Binding) -> Optional[Ticket]:
        key = self._key(binding)
        for t in self._tickets.values():
            if self._key(t.binding) == key and t.status in ACTIVE_STATES:
                return t
        return None
