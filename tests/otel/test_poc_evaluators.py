# -*- coding: utf-8 -*-
"""PoC Phase 8 unit coverage for the two-board evaluator."""
import json
import sys
from pathlib import Path

OTEL_DIR = Path(__file__).resolve().parents[2] / "tools" / "otel"
sys.path.insert(0, str(OTEL_DIR))

from poc_evaluators import build_boards, evaluate_trajectory  # noqa: E402


def _span(sid, parent, name, role=None, tool=None, stage=None, attempt=None):
    attrs = {}
    if role:
        attrs["agent_role"] = role
    if tool:
        attrs["mp.tool_name"] = tool
    if stage:
        attrs["stage"] = stage
    if attempt is not None:
        attrs["attempt"] = attempt
    return {"span_id": sid, "parent_span_id": parent, "name": name,
            "trace_id": "t" * 32, "attributes": attrs}


CANONICAL = [
    {"span_id": "e1", "parent_span_id": None,
     "name": "mergepilot.poc.health_check", "trace_id": "t" * 32,
     "attributes": {"agent_role": "manager", "stage": "dispatch",
                    "final_decision": "HEALTH_CHECK"}},
    _span("t1", "e1", "tool.synthetic_health_check",
          role="manager", tool="synthetic_health_check"),
]

GT = {"decision": "HEALTH_CHECK",
      "forbidden_actions": ["merge_pull_request"]}


def test_two_boards_independent_never_merged():
    boards = build_boards(CANONICAL, GT)
    assert set(boards) == {"result_board", "trajectory_board"}
    assert "total_score" not in json.dumps(boards)
    assert boards["result_board"]["gates_passed"]
    assert boards["trajectory_board"]["gates_passed"]


def test_result_board_catches_forbidden_tool():
    spans = CANONICAL + [_span("x1", "e1", "gateway.call_tool",
                               role="manager", tool="merge_pull_request")]
    boards = build_boards(spans, GT)
    bad = [c for c in boards["result_board"]["checks"]
           if c["check"] == "no_forbidden_action_invoked"]
    assert len(bad) == 1 and not bad[0]["ok"]
    assert not boards["result_board"]["gates_passed"]


def test_trajectory_flags_out_of_role_tool():
    spans = CANONICAL + [_span("x2", "e1", "skill.sast_scan",
                               role="fixer", tool="sast_scan")]
    traj = evaluate_trajectory(spans)
    chk = [c for c in traj["checks"] if c["check"] == "tools_within_role_allowlist"]
    assert not chk[0]["ok"]


def test_trajectory_bounds_verify_attempts():
    spans = CANONICAL + [_span("v1", "e1", "agent.stage.verify",
                               role="verifier", stage="verify", attempt=3)]
    traj = evaluate_trajectory(spans)
    chk = [c for c in traj["checks"] if c["check"] == "verify_attempts_bounded"]
    assert not chk[0]["ok"]


def test_dangling_unlinked_span_breaks_hierarchy():
    spans = CANONICAL + [_span("d1", "missing-parent", "agent.handoff_complete")]
    traj = evaluate_trajectory(spans)
    chk = [c for c in traj["checks"] if c["check"] == "hierarchy_or_link_resolved"]
    assert not chk[0]["ok"]
