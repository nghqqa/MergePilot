# -*- coding: utf-8 -*-
"""structured_gate — 结构化人工门建票通道(CL-02)。

与 gate_ticket.open_gate_ticket(依赖 leader marker 文件)互补:本通道由
**执行编排器(bridge/iso executor)**在 run 终局后,依据**自己采集并核验
过的证据**直接建票——不依赖 leader 自然语言、不依赖写文件指令。

身份与归属(证据绑定,非字符串认证):
  - 执行身份 = 编排器自身(它持有 run manifest、delivery、审计流的权威副本);
  - run/head/task 绑定来自执行器自有记录(manifest/run-context),与
    reviewer 产出的 result 证据交叉核对(结构化结论行 + 审计流中
    reviewer skill 调用共存);
  - reviewer 文本只是证据之一:severity 取自 result 证据的结构化结论行
    (STATUS/SEVERITY/HUMAN_VERIFICATION_REQUIRED),缺任一段即拒绝。

幂等:同 (run_id, action, finding_id) 活动票唯一(存储层 partial
UNIQUE INDEX 强制);重复到达收敛同一张票。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Optional, Tuple

RESULT_LINE = re.compile(
    r"STATUS:\s*(FINDING_CONFIRMED|NOT_CONFIRMED).*?"
    "SEVERITY:\\s*(HIGH|MEDIUM|LOW).*?"
    r"HUMAN_VERIFICATION_REQUIRED:\s*(YES|NO)", re.S)


class StructuredGateError(Exception):
    def __init__(self, subcode: str, detail: str = ""):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail


def parse_reviewer_verdict(result_text: str) -> Dict[str, str]:
    """从 reviewer result 证据解析结构化结论行。

    找不到完整三段式结论行 → StructuredGateError(VERDICT_MISSING)。"""
    m = RESULT_LINE.search(result_text or "")
    if not m:
        raise StructuredGateError("VERDICT_MISSING",
                                  "no structured verdict line in reviewer result")
    return {"status": m.group(1), "severity": m.group(2),
            "human_verification_required": m.group(3)}


def _plus_hours_iso(now_iso: str, hours: int) -> str:
    base = dt.datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    return (base + dt.timedelta(hours=hours)).isoformat()


def open_structured_gate_ticket(store, *, executor_id: str,
                                run: Dict[str, Any],
                                reviewer_result_text: str,
                                audit_records: list,
                                task_id: str, ttl_hours: int = 24,
                                now: str = None) -> Tuple[Any, bool, str]:
    """执行器证据绑定建票(幂等)。返回 (ticket, created, reason)。

    必填输入(全部为执行器自有/核验过的记录):
      run            {run_id, repo, head_sha, manifest_id}
      reviewer_result_text  reviewer result 证据全文(结构化结论行所在)
      audit_records  本 run 审计流记录(至少含 reviewer 的 skill 调用——
                     证据共存校验;无审计=拒绝,不凭文本建票)
    校验失败 → (None, False, reason);通过 → 幂等建票(PENDING, 24h TTL)。"""
    try:
        import gate_ticket as gt
    except ImportError:
        import sys as _sys
        gt = _sys.modules.get("approval_pkg.gate_ticket")
        if gt is None:
            return None, False, "gate_ticket module unavailable"

    if not executor_id or not str(executor_id).strip():
        return None, False, "EXECUTOR_ID_REQUIRED"
    try:
        verdict = parse_reviewer_verdict(reviewer_result_text)
    except StructuredGateError as e:
        return None, False, e.subcode
    if verdict["status"] != "FINDING_CONFIRMED":
        return None, False, "NOT_A_GATE: status=%s" % verdict["status"]
    if verdict["human_verification_required"] != "YES":
        return None, False, "NOT_A_GATE: human verification not required"

    run_id = run.get("run_id")
    head = run.get("head_sha")
    repo = run.get("repo")
    if not (run_id and repo and head):
        return None, False, "BINDING_INCOMPLETE: run/repo/head missing"
    skill_calls = [a for a in (audit_records or [])
                   if str(a.get("tool", "")).startswith("skill_")]
    if not skill_calls:
        return None, False, "EVIDENCE_COEXISTENCE_FAILED: no reviewer skill calls"

    binding = gt.marker_binding(
        {"version": 1, "severity": verdict["severity"],
         "task_id": task_id, "requested_by": "reviewer-evidence",
         "requested_at": now or ""},
        run_id=run_id, repo=repo, head_sha=head, task_id=task_id)
    now_iso = now or dt.datetime.now(dt.timezone.utc).isoformat()
    ticket, created = store.create(
        binding, attempt_no=1, created_at=now_iso, created_by_run=run_id,
        approval_expires_at=_plus_hours_iso(now_iso, ttl_hours))
    return ticket, created, ""
