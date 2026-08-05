#!/usr/bin/env python3
"""M5-0B handoff closed-loop — pure-logic unit tests (no DB / no Docker).

Covers the strict handoff classifier, dispatch-marker templates, the expected
six-Skill set, and the handoff_watcher m5live-* exclusion (plus legacy
non-regression). DB-backed scenarios (replay idempotency, concurrency,
stage advancement, HOLD/PARTIAL terminal behavior) live in the M5-0B
integration runner which exercises the real reconcile_* over a temp PG.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CTRL_DIR = ROOT / "tools" / "workflow-controller"
TOOLS_DIR = ROOT / "tools"
for d in (CTRL_DIR, TOOLS_DIR):
    d_str = str(d)
    if d_str not in sys.path:
        sys.path.insert(0, d_str)


def _import_controller():
    os.environ.setdefault("M4F_RUN_PREFIX", "m5live-")
    os.environ.setdefault("MATRIX_SERVER_NAME", "matrix-local.hiclaw.io:18080")
    if "controller" in sys.modules:
        del sys.modules["controller"]
    return importlib.import_module("controller")


@pytest.fixture(scope="module")
def ctrl():
    return _import_controller()


# ── _m5_classify_handoff: strict handoff classification (§7.2) ──

class TestClassifyHandoff:
    def test_review(self, ctrl):
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-review") == (
            "m5live-r1", "review", None)

    def test_fix(self, ctrl):
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-fix") == (
            "m5live-r1", "fix", None)

    def test_verify_partial_one_line(self, ctrl):
        # 1-line verify = PARTIAL (no verdict yet)
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-verify") == (
            "m5live-r1", "verify", None)

    def test_verify_pass(self, ctrl):
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-verify\nVERDICT=PASS") == (
            "m5live-r1", "verify", "PASS")

    def test_verify_fail(self, ctrl):
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-verify\nVERDICT=FAIL") == (
            "m5live-r1", "verify", "FAIL")

    def test_verify_blocked(self, ctrl):
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-verify\nVERDICT=BLOCKED") == (
            "m5live-r1", "verify", "BLOCKED")

    def test_verify_invalid_verdict_rejected(self, ctrl):
        # invalid VERDICT value -> REJECT
        res = ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-verify\nVERDICT=MAYBE")
        assert res[0] == "REJECT" and res[1] == "verify"

    def test_multiple_verdicts_rejected(self, ctrl):
        # >2 lines / two VERDICT lines -> not a strict verify
        res = ctrl._m5_classify_handoff(
            "TASK_COMPLETED: m5live-r1-verify\nVERDICT=PASS\nVERDICT=FAIL")
        assert res == (None, None, None) or res[0] == "REJECT"

    def test_trailing_prose_rejected(self, ctrl):
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-review 已完成") == (
            None, None, None)

    def test_code_fence_rejected(self, ctrl):
        assert ctrl._m5_classify_handoff(
            "```\nTASK_COMPLETED: m5live-r1-review\n```") == (None, None, None)

    def test_leading_prose_rejected(self, ctrl):
        assert ctrl._m5_classify_handoff(
            "结果如下\nTASK_COMPLETED: m5live-r1-review") == (None, None, None)

    def test_empty_body(self, ctrl):
        assert ctrl._m5_classify_handoff("") == (None, None, None)

    def test_non_handoff_body(self, ctrl):
        assert ctrl._m5_classify_handoff("hello world") == (None, None, None)

    def test_wrong_stage_suffix(self, ctrl):
        # -revert / -reverify are not M5 handoff stages
        assert ctrl._m5_classify_handoff("TASK_COMPLETED: m5live-r1-revert") == (
            None, None, None)


# ── expected six-Skill set (exact, not count) ──

class TestExpectedSkills:
    def test_exactly_six(self, ctrl):
        assert len(ctrl._M5_EXPECTED_SKILLS) == 6

    def test_is_the_frozen_set(self, ctrl):
        assert set(ctrl._M5_EXPECTED_SKILLS) == {
            "diff-parse", "risk-classify", "sast-scan",
            "test-runner", "case-retrieval", "pr-lifecycle",
        }

    def test_no_duplicates(self, ctrl):
        assert len(set(ctrl._M5_EXPECTED_SKILLS)) == len(ctrl._M5_EXPECTED_SKILLS)


# ── dispatch templates carry the exact completion marker ──

class TestDispatchTemplates:
    def test_review_template_marker(self, ctrl):
        tpl = ctrl._M5_DISPATCH_TPL["review"].format(run_id="m5live-r1")
        assert "TASK_COMPLETED: m5live-r1-review" in tpl

    def test_fix_template_marker(self, ctrl):
        tpl = ctrl._M5_DISPATCH_TPL["fix"].format(run_id="m5live-r1")
        assert "TASK_COMPLETED: m5live-r1-fix" in tpl

    def test_verify_template_two_lines(self, ctrl):
        tpl = ctrl._M5_DISPATCH_TPL["verify"].format(run_id="m5live-r1")
        assert "TASK_COMPLETED: m5live-r1-verify" in tpl
        assert "VERDICT=PASS|FAIL|BLOCKED" in tpl

    def test_stage_sender_map(self, ctrl):
        assert ctrl._M5_STAGE_SENDER == {
            "review": "reviewer", "fix": "fixer", "verify": "verifier"}


# ── reconcile guards: production (M4F_ONLY_MODE=0) is a no-op ──

class TestReconcileProductionNoOp:
    def test_skill_to_review_noop_in_production(self, ctrl, monkeypatch):
        monkeypatch.setattr(ctrl, "M4F_ONLY_MODE", False)
        assert ctrl.reconcile_m5_skill_to_review() == 0

    def test_handoffs_noop_in_production(self, ctrl, monkeypatch):
        monkeypatch.setattr(ctrl, "M4F_ONLY_MODE", False)
        assert ctrl.reconcile_m5_handoffs() == 0

    def test_skill_to_review_noop_without_prefix(self, ctrl, monkeypatch):
        monkeypatch.setattr(ctrl, "M4F_ONLY_MODE", True)
        monkeypatch.setattr(ctrl, "M4F_RUN_PREFIX", "")
        assert ctrl.reconcile_m5_skill_to_review() == 0


# ── handoff_watcher m5live-* exclusion + legacy non-regression ──

def _import_watcher(name):
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


class TestWatcherExclusionV2:
    def test_m5live_prefix_excluded(self):
        os.environ.pop("M5_WATCHER_EXCLUDE_PREFIXES", None)
        w = _import_watcher("handoff_watcher_v2")
        assert w._m5_excluded("m5live-run1") is True
        assert w._m5_excluded("m5live-") is True

    def test_legacy_prefix_not_excluded(self):
        os.environ.pop("M5_WATCHER_EXCLUDE_PREFIXES", None)
        w = _import_watcher("handoff_watcher_v2")
        assert w._m5_excluded("iso5-pr6") is False
        assert w._m5_excluded("normal-run1") is False
        assert w._m5_excluded("gh-pr1") is False

    def test_env_override(self):
        os.environ["M5_WATCHER_EXCLUDE_PREFIXES"] = "m5test-,m5live-"
        try:
            w = _import_watcher("handoff_watcher_v2")
            assert w._m5_excluded("m5test-run1") is True
            assert w._m5_excluded("m5live-run1") is True
            assert w._m5_excluded("normal-run1") is False
        finally:
            os.environ.pop("M5_WATCHER_EXCLUDE_PREFIXES", None)

    def test_v2_transitions_regex_captures_prefix(self):
        w = _import_watcher("handoff_watcher_v2")
        bodies = {
            "review": "TASK_COMPLETED: m5live-r1-review",
            "fix": "TASK_COMPLETED: m5live-r1-fix",
            "verify": "TASK_COMPLETED: m5live-r1-verify",
        }
        stage_to_idx = {"review": 0, "fix": 1, "verify": 2}
        for stage, body in bodies.items():
            tri = w.TRANSITIONS[stage_to_idx[stage]]
            m = tri[1].search(body)
            assert m is not None
            assert m.group(1) == "m5live-r1"
            assert w._m5_excluded(m.group(1)) is True


class TestWatcherExclusionV1:
    def test_m5live_prefix_excluded(self):
        os.environ.pop("M5_WATCHER_EXCLUDE_PREFIXES", None)
        w = _import_watcher("handoff_watcher")
        assert w._m5_excluded("m5live-run1") is True

    def test_legacy_prefix_not_excluded(self):
        os.environ.pop("M5_WATCHER_EXCLUDE_PREFIXES", None)
        w = _import_watcher("handoff_watcher")
        assert w._m5_excluded("iso5-pr6") is False
        assert w._m5_excluded("gh-pr1") is False

    def test_v1_transitions_regex_captures_prefix(self):
        w = _import_watcher("handoff_watcher")
        for stage, body in (("review", "TASK_COMPLETED: m5live-r1-review"),
                            ("fix", "TASK_COMPLETED: m5live-r1-fix"),
                            ("verify", "TASK_COMPLETED: m5live-r1-verify")):
            pat = [entry[0] for entry in w.TRANSITIONS if ("-" + stage) in entry[0].pattern][0]
            m = pat.search(body)
            assert m is not None
            assert m.group(1) == "m5live-r1"
            assert w._m5_excluded(m.group(1)) is True

    def test_v1_legacy_pattern_still_matches_legacy(self):
        w = _import_watcher("handoff_watcher")
        pat = [entry[0] for entry in w.TRANSITIONS if "-review" in entry[0].pattern][0]
        m = pat.search("TASK_COMPLETED: iso5-pr6-review")
        assert m is not None and m.group(1) == "iso5-pr6"
        assert w._m5_excluded(m.group(1)) is False


# ── P1-5: m5live- is CODE-FORCED; env can only APPEND — for every env config ──

class TestForcedExclusionV2:
    @pytest.mark.parametrize("env", [None, "", "m5test-", "m5test-,other-", "m5live-", ",,"])
    def test_m5live_always_excluded_under_any_env(self, env, monkeypatch):
        if env is None:
            monkeypatch.delenv("M5_WATCHER_EXCLUDE_PREFIXES", raising=False)
        else:
            monkeypatch.setenv("M5_WATCHER_EXCLUDE_PREFIXES", env)
        w = _import_watcher("handoff_watcher_v2")
        assert w._m5_excluded("m5live-run1") is True
        assert w._m5_excluded("m5live-") is True
        # legacy still not excluded
        assert w._m5_excluded("iso5-pr6") is False

    @pytest.mark.parametrize("env", [None, "", "m5test-", "m5test-,other-"])
    def test_m5live_always_excluded_v1(self, env, monkeypatch):
        if env is None:
            monkeypatch.delenv("M5_WATCHER_EXCLUDE_PREFIXES", raising=False)
        else:
            monkeypatch.setenv("M5_WATCHER_EXCLUDE_PREFIXES", env)
        w = _import_watcher("handoff_watcher")
        assert w._m5_excluded("m5live-run1") is True
        assert w._m5_excluded("iso5-pr6") is False


# ── P1-5: real watcher selection path (process_batch) — m5live => 0 sends ──

class TestWatcherProcessBatchV2:
    def test_m5live_zero_send_legacy_one_send(self, monkeypatch):
        w = _import_watcher("handoff_watcher_v2")
        monkeypatch.delenv("M5_WATCHER_EXCLUDE_PREFIXES", raising=False)
        w._M5_EXCLUDE_PREFIXES = w._build_exclude()  # rebuild with clean env
        sends = []
        rooms = [("!room1:hs", {"manager", "fixer", "verifier"})]
        events = [
            ("$m5live-review:hs", "reviewer", "TASK_COMPLETED: m5live-run1-review", 100),
            ("$m5live-fix:hs", "fixer", "TASK_COMPLETED: m5live-run1-fix", 101),
            ("$m5live-verify:hs", "verifier", "TASK_COMPLETED: m5live-run1-verify", 102),
            ("$legacy-review:hs", "reviewer", "TASK_COMPLETED: iso5-pr6-review", 103),
        ]
        monkeypatch.setattr(w, "discover_rooms", lambda t: rooms)
        monkeypatch.setattr(w, "recent", lambda t, rid, n=12: events)
        monkeypatch.setattr(w, "send_mention", lambda t, rid, user, msg: sends.append((user, msg)))
        n = w.process_batch("TOKEN", set(), set())
        # m5live handoffs produce ZERO sends; the one legacy review sends exactly once
        assert n == 1
        assert len(sends) == 1
        assert sends[0][0] == "fixer"  # review completion -> nudge fixer

    def test_env_cannot_disable_m5live_in_process_batch(self, monkeypatch):
        w = _import_watcher("handoff_watcher_v2")
        # operator tries to "disable" exclusion via env — must be ignored for m5live
        monkeypatch.setenv("M5_WATCHER_EXCLUDE_PREFIXES", "m5test-")
        w._M5_EXCLUDE_PREFIXES = w._build_exclude()
        sends = []
        monkeypatch.setattr(w, "discover_rooms", lambda t: [("!r:hs", {"manager", "fixer"})])
        monkeypatch.setattr(w, "recent", lambda t, rid, n=12: [
            ("$m:hs", "reviewer", "TASK_COMPLETED: m5live-run1-review", 1),
        ])
        monkeypatch.setattr(w, "send_mention", lambda t, rid, user, msg: sends.append(user))
        assert w.process_batch("T", set(), set()) == 0
        assert sends == []


class TestWatcherProcessBatchV1:
    def test_m5live_zero_send_legacy_one_send(self, monkeypatch):
        w = _import_watcher("handoff_watcher")
        monkeypatch.delenv("M5_WATCHER_EXCLUDE_PREFIXES", raising=False)
        w._M5_EXCLUDE_PREFIXES = w._build_exclude()
        sends = []
        watched = [("!room1:hs", {"manager", "reviewer", "fixer"})]
        events = [
            ("$m5live-review:hs", "reviewer", "TASK_COMPLETED: m5live-run1-review", 100),
            ("$legacy-fix:hs", "fixer", "TASK_COMPLETED: iso5-pr6-fix", 101),
        ]
        monkeypatch.setattr(w, "watched_rooms", lambda tok: watched)
        monkeypatch.setattr(w, "recent_msgs", lambda tok, rid, n=12: events)
        monkeypatch.setattr(w, "send", lambda tok, room, text: sends.append(text))
        n = w.process_batch("TOKEN", set(), "!mgr:hs", watched)
        assert n == 1
        assert len(sends) == 1  # only the legacy fix handoff -> nudge manager
