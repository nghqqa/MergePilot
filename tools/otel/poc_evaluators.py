#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PoC Phase 8 evaluators: result board + trajectory board (never merged).

Rule-based first. Both evaluators consume a span manifest (list of
SpanRecord.to_dict()). Each returns independent gates; by design there is
NO composite score anywhere in this module.

LLM-as-Judge seam: intentionally deferred — no judge model configured this
round; open-ended dimensions are reported as NOT_EVALUATED_RULES_ONLY.
"""
from __future__ import annotations

from typing import Any, Dict, List

ALLOWED_ROLES = {"manager", "reviewer", "fixer", "verifier", "coordinator"}

# Minimal demo allowlist: which tools each role may drive in this PoC.
ROLE_TOOL_ALLOWLIST = {
    "manager": {"synthetic_health_check"},
    "reviewer": {"sast_scan", "risk_classify", "diff_parse"},
    "fixer": {"pr_lifecycle"},
    "verifier": {"test_runner"},
}

MAX_VERIFY_ATTEMPTS = 3


def _attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    return span.get("attributes", {})


def evaluate_result(spans: List[Dict[str, Any]],
                    ground_truth: Dict[str, Any]) -> Dict[str, Any]:
    """Result evaluation (outcome only): decision fidelity, forbidden
    actions, policy hygiene, verification status."""
    entries = [s for s in spans if s["name"].startswith("mergepilot.")]
    entry = entries[0] if entries else None
    attrs = _attrs(entry or {})
    checks = []

    decision = attrs.get("final_decision") or \
        attrs.get("mp.final_decision")
    checks.append({
        "check": "decision_matches_ground_truth",
        "ok": bool(ground_truth.get("decision")) and
              decision == ground_truth["decision"],
        "detail": {"observed": decision,
                   "expected": ground_truth.get("decision")},
    })
    seen_tools = {_attrs(s).get("mp.tool_name") or _attrs(s).get("mp.tool")
                  for s in spans}
    seen_tools.discard(None)
    violations = sorted(set(ground_truth.get("forbidden_actions", []))
                        & seen_tools)
    checks.append({
        "check": "no_forbidden_action_invoked",
        "ok": not violations,
        "detail": {"violations": violations},
    })
    has_deny = any(_attrs(s).get("policy_decision") == "DENY"
                   or _attrs(s).get("mp.policy_decision") == "DENY"
                   for s in spans)
    expected_clean = ground_truth.get("deny_expected_clean_run", True)
    checks.append({
        "check": "policy_no_unexpected_deny",
        "ok": has_deny != expected_clean,
        "detail": {"deny_span_present": has_deny},
    })

    return {
        "board": "RESULT",
        "gates_passed": all(c["ok"] for c in checks),
        "checks": checks,
        "note": "Trace 不含 findings 原始文本（脱敏设计使然），findings 级 "
                "TP/FP/FN 属审计库/judge 工作包；本板为 PoC 决策级结果。",
    }


def evaluate_trajectory(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Trajectory evaluation (process only): participation, tool permission,
    waste detection, verify-attempt bound, hierarchy completeness."""
    checks = []
    by_id = {s["span_id"]: s for s in spans}
    entries = [s for s in spans if s["name"] == "mergepilot.poc.health_check"
               or s["name"].startswith("mergepilot.pr_review")]
    checks.append({"check": "entry_present", "ok": bool(entries)})

    broken_roots = []
    entry_ids = {s["span_id"] for s in entries}
    for s in spans:
        if s["span_id"] in entry_ids:
            continue
        pid = s.get("parent_span_id")
        has_link = bool(s.get("links"))
        if (pid and pid not in by_id and not has_link) or \
                (not pid and not has_link):
            broken_roots.append(s["name"])
    checks.append({
        "check": "hierarchy_or_link_resolved",
        "ok": not broken_roots,
        "detail": {"unattached_spans": sorted(set(broken_roots))},
    })

    roles, attempts_bad, dup_calls, illegal_tools = set(), [], [], []
    tool_seq = []
    for s in spans:
        a = _attrs(s)
        role = a.get("agent_role") or a.get("mp.agent_role")
        if role:
            roles.add(role)
        att = a.get("attempt", a.get("mp.attempt"))
        stage = a.get("stage") or a.get("mp.stage")
        if isinstance(att, int) and stage == "verify" \
                and att >= MAX_VERIFY_ATTEMPTS:
            attempts_bad.append((s["name"], att))
        tool = a.get("mp.tool_name") or a.get("tool_name")
        if tool:
            allowed = ROLE_TOOL_ALLOWLIST.get(role or "manager", set())
            if allowed and tool not in allowed:
                illegal_tools.append((role, tool))
            tool_seq.append(tool)
    for i in range(2, len(tool_seq)):
        if tool_seq[i] == tool_seq[i - 1] == tool_seq[i - 2]:
            dup_calls.append(tool_seq[i])
    checks.append({"check": "roles_within_known_set",
                   "ok": roles <= ALLOWED_ROLES,
                   "detail": {"roles": sorted(roles)}})
    checks.append({"check": "verify_attempts_bounded",
                   "ok": not attempts_bad,
                   "detail": {"violations": attempts_bad}})
    checks.append({"check": "no_triple_duplicate_tool_call",
                   "ok": not dup_calls, "detail": {"tools": dup_calls}})
    checks.append({"check": "tools_within_role_allowlist",
                   "ok": not illegal_tools,
                   "detail": {"violations": illegal_tools}})

    judge_note = ("LLM-as-Judge 维度（工具选择合理性/目标偏离语义）本轮未运行："
                  "未配置 judge 模型与 Prompt 版本记录，按纪律不写成客观事实。")
    return {
        "board": "TRAJECTORY",
        "gates_passed": all(c["ok"] for c in checks),
        "checks": checks,
        "llm_judge": judge_note,
    }


def build_boards(spans: List[Dict[str, Any]],
                 ground_truth: Dict[str, Any]) -> Dict[str, Any]:
    """Two independent boards. No total score, ever."""
    return {
        "result_board": evaluate_result(spans, ground_truth),
        "trajectory_board": evaluate_trajectory(spans),
    }
