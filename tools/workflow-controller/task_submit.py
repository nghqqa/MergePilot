#!/usr/bin/env python3
"""task_submit.py — channel-neutral TASK_SUBMITTED 三表落库(M8-GH-1)。

从 controller.process_event 的 TASK_SUBMITTED 分支提取的**唯一** production
实现(SQL 单实现,禁止双写):Matrix 路径与 GitHub delivery drain 路径都调用
本函数,调用方各自完成通道校验后进入。

合同(设计冻结 2026-08-18):
  * 事务所有权归调用方 —— 本模块绝不 commit/rollback。
  * 幂等键与状态值保持原样: task_runs ON CONFLICT(run_id);
    stage_runs ON CONFLICT(run_id,stage,attempt);
    dispatch_outbox ON CONFLICT(idempotency_key), key = f"{run_id}:review:1"。
  * duplicate 判定只比较**不可变身份字段**:
      task_runs(room_id, repo, pr_number, branch, approval_required)
      + stage_runs 结构键(agent='reviewer', attempt=1, stage='review')
      + dispatch_outbox 不可变载荷(target_agent, target_stage, attempt, body)
    绝不比较生命周期字段(stage_runs.status / dispatch_outbox.status /
    matrix_event_id / retry_count / dispatched_at / …)——DISPATCHED、
    RETRY、FAILED 等正常演进不影响幂等重放。
  * 不可变字段不一致 → SubmitTaskConflict(永久 RUN_ID_CONFLICT),
    冲突路径零 stage/outbox 补写、零状态回退。
  * 通道命名空间(纵深防御,调用方通道门为主):
      channel='github'  → run_id 必须 ^gh-[0-9a-f]{24}$(派生哈希由调用方复检)
      channel='matrix'  → run_id 禁止 gh- 前缀(RUN_ID_NAMESPACE_RESERVED)
  * mark_processed/update_event_meta 由调用方在本事务 stage_events 行
    刚插入(rowcount=1)后调用 —— 本模块不触碰 stage_events。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

_GH_RUN_ID_RE = re.compile(r"^gh-[0-9a-f]{24}$")
_GH_PREFIX = "gh-"


class SubmitTaskError(Exception):
    """永久提交错误(调用方记录终局,不重试)。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(code + ((" (%s)" % detail) if detail else ""))


class SubmitTaskConflict(SubmitTaskError):
    """不可变身份字段不一致(RUN_ID_CONFLICT)或命名空间违例。"""


class SubmitTaskTransient(Exception):
    """瞬时错误(调用方回滚并按通道策略重试)。"""


@dataclass(frozen=True)
class TaskSubmission:
    run_id: str
    room_id: str
    repo: Optional[str]
    pr_number: Optional[int]
    branch: Optional[str]
    approval_required: bool
    dispatch_body: str          # 通道语义在此(body 内嵌 repo/PR/branch)


@dataclass(frozen=True)
class EventSource:
    channel: str                # 'matrix' | 'github'
    event_id: str               # stage_events PK(调用方已插入 RECEIVED 行)
    sender_identity: str        # matrix: @local:server;github: github-app[<installation>]


@dataclass(frozen=True)
class SubmitResult:
    outcome: str                # 'created' | 'duplicate'
    run_id: str
    stage: str                  # 恒 'review'
    dispatch_key: str


def dispatch_key_for(run_id: str) -> str:
    """既有幂等键,精确保持: f"{run_id}:review:1"。"""
    return "%s:review:1" % run_id


def validate_channel_namespace(channel: str, run_id: str) -> None:
    """通道命名空间断言(纵深防御;主门在调用方)。"""
    if channel == "github":
        if not _GH_RUN_ID_RE.fullmatch(run_id or ""):
            raise SubmitTaskConflict(
                "RUN_ID_NAMESPACE_INVALID",
                "github run_id must match ^gh-[0-9a-f]{24}$")
    elif channel == "matrix":
        if (run_id or "").startswith(_GH_PREFIX):
            raise SubmitTaskConflict(
                "RUN_ID_NAMESPACE_RESERVED",
                "matrix TASK_SUBMITTED must not use the gh- run_id namespace")
    else:
        raise SubmitTaskError("CHANNEL_UNKNOWN", repr(channel))


# 不可变身份字段元组(与既有 INSERT 列一一对应)
_IMMUTABLE_TASK_FIELDS = ("room_id", "repo", "pr_number", "branch",
                          "approval_required")


def submit_task(conn: Any, payload: TaskSubmission, source: EventSource) -> SubmitResult:
    """在调用方事务内原子写 task_runs + review stage_run + reviewer dispatch。

    返回 SubmitResult(outcome='created'|'duplicate')。任何违例抛
    SubmitTaskConflict/SubmitTaskError(永久)或 SubmitTaskTransient;
    回滚责任在调用方。
    """
    validate_channel_namespace(source.channel, payload.run_id)
    if not payload.run_id:
        raise SubmitTaskError("NO_RUN_ID")
    if not payload.room_id:
        raise SubmitTaskError("NO_ROOM_ID")

    key = dispatch_key_for(payload.run_id)
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO task_runs(run_id, room_id, repo, pr_number, branch,
                                     status, current_stage, approval_required)
               VALUES(%s, %s, %s, %s, %s, 'RUNNING', 'review', %s)
               ON CONFLICT(run_id) DO NOTHING""",
            (payload.run_id, payload.room_id, payload.repo, payload.pr_number,
             payload.branch, payload.approval_required))

        if cur.rowcount == 0:
            # ── duplicate 判定: 仅不可变身份字段(FOR UPDATE 锁定) ──
            cur.execute(
                """SELECT room_id, repo, pr_number, branch, approval_required
                   FROM task_runs WHERE run_id=%s FOR UPDATE""",
                (payload.run_id,))
            existing = cur.fetchone()
            if existing is None:
                raise SubmitTaskTransient("task_runs row vanished under lock")
            existing_map = dict(zip(_IMMUTABLE_TASK_FIELDS, existing))
            desired_map = {
                "room_id": payload.room_id,
                "repo": payload.repo,
                "pr_number": payload.pr_number,
                "branch": payload.branch,
                "approval_required": payload.approval_required,
            }
            differing = sorted(
                field for field in _IMMUTABLE_TASK_FIELDS
                if existing_map[field] != desired_map[field])
            if differing:
                raise SubmitTaskConflict(
                    "RUN_ID_CONFLICT",
                    "immutable field mismatch: %s" % ",".join(differing))

            # 结构键 + dispatch 不可变载荷(仅 SELECT,零补写)。
            cur.execute(
                """SELECT agent, attempt FROM stage_runs
                   WHERE run_id=%s AND stage='review' AND attempt=1""",
                (payload.run_id,))
            stage_row = cur.fetchone()
            if stage_row is None or tuple(stage_row) != ("reviewer", 1):
                raise SubmitTaskConflict(
                    "RUN_ID_CONFLICT",
                    "stage_runs immutable structure mismatch")

            cur.execute(
                """SELECT target_agent, target_stage, attempt, body
                   FROM dispatch_outbox WHERE idempotency_key=%s""",
                (key,))
            dispatch_row = cur.fetchone()
            if dispatch_row is None or tuple(dispatch_row) != (
                    "reviewer", "review", 1, payload.dispatch_body):
                raise SubmitTaskConflict(
                    "RUN_ID_CONFLICT",
                    "dispatch immutable payload mismatch")

            return SubmitResult(outcome="duplicate", run_id=payload.run_id,
                                stage="review", dispatch_key=key)

        # ── created 路径: 既有三条 SQL 原样(唯一实现) ──
        cur.execute(
            """INSERT INTO stage_runs(run_id, stage, agent, attempt, status, started_at)
               VALUES(%s, 'review', 'reviewer', 1, 'PENDING_DISPATCH', now())
               ON CONFLICT(run_id, stage, attempt) DO NOTHING""",
            (payload.run_id,))
        cur.execute(
            """INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id,
                                          target_agent, target_stage, attempt, body)
               VALUES(%s, %s, %s, 'reviewer', 'review', 1, %s)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (key, payload.run_id, payload.room_id, payload.dispatch_body))
        return SubmitResult(outcome="created", run_id=payload.run_id,
                            stage="review", dispatch_key=key)
    finally:
        cur.close()


__all__ = [
    "EventSource",
    "SubmitResult",
    "SubmitTaskConflict",
    "SubmitTaskError",
    "SubmitTaskTransient",
    "TaskSubmission",
    "dispatch_key_for",
    "submit_task",
    "validate_channel_namespace",
]
