#!/usr/bin/env python3
"""Unit tests for c2_smoke cleanup_gh retry logic.

Root cause fixed: GitHub PR-close is eventually-consistent; the bridge refuses
branch delete while a PR is open (open_pr_exists). cleanup_gh now closes PRs,
waits a fixed propagation delay, then deletes branches with a light retry on
open_pr_exists. (An earlier per-attempt polling version spawned ~130 containers
per run and exceeded C3_TIMEOUT.) These tests exercise the retry policy via
injected callables (no stack, no WSL, no network).
"""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import c2_smoke as S


def _x(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)


def _resp(d):
    return {"is_error": False, "content": json.dumps(d)}


# ── _delete_branch_with_retry ──

def test_delete_succeeds_first_try():
    """No propagation delay → 1 delete, no close, no sleep."""
    closes = []; sleeps = []
    d = S._delete_branch_with_retry(
        "cfg", "fix/abc", "rk", 42,
        delete_fn=lambda c, b, k: _resp({"deleted": True, "already_absent": False}),
        close_fn=lambda c, p: closes.append(p) or _resp({"state": "closed"}),
        sleep=lambda s: sleeps.append(s))
    _x(d.get("deleted") is True, "deleted on first try")
    _x(len(closes) == 0, "no re-close on success")
    _x(len(sleeps) == 0, "no sleep on success")


def test_delete_retries_after_open_pr_recloses():
    """Bridge refuses open_pr_exists → re-close PR → sleep → retry → deleted."""
    del_seq = [{"reason": "open_pr_exists", "refused": True},
               {"deleted": True, "already_absent": False}]
    state = {"i": 0}; reclosed = []

    def del_fn(c, b, k):
        r = del_seq[min(state["i"], len(del_seq) - 1)]
        state["i"] += 1
        return _resp(r)

    d = S._delete_branch_with_retry(
        "cfg", "fix/abc", "rk", 42, attempts=4, delay=0.01,
        delete_fn=del_fn,
        close_fn=lambda c, p: reclosed.append(p) or _resp({"state": "closed"}),
        sleep=lambda s: None)
    _x(d.get("deleted") is True, "deleted after re-close + retry")
    _x(len(reclosed) == 1, "re-closed the PR once (got %d)" % len(reclosed))


def test_delete_no_pr_does_not_reclose():
    """pr_num None + refused open_pr_exists → never calls close (nothing to close)."""
    closes = []
    d = S._delete_branch_with_retry(
        "cfg", "fix/abc", "rk", None, attempts=2, delay=0.001,
        delete_fn=lambda c, b, k: _resp({"reason": "open_pr_exists", "refused": True}),
        close_fn=lambda c, p: closes.append(p) or _resp({"state": "closed"}),
        sleep=lambda s: None)
    _x(len(closes) == 0, "no close attempted when pr_num is None")
    _x(d.get("reason") == "open_pr_exists", "returns the refusal after exhausting attempts")


def test_delete_already_absent_is_success():
    d = S._delete_branch_with_retry(
        "cfg", "fix/abc", "rk", 42,
        delete_fn=lambda c, b, k: _resp({"deleted": False, "already_absent": True}),
        close_fn=lambda c, p: _resp({"state": "closed"}),
        sleep=lambda s: None)
    _x(d.get("already_absent") is True, "already_absent treated as success")


def test_delete_non_open_pr_refusal_not_reclosed():
    """A refusal that is NOT open_pr_exists (e.g. forbidden) → no re-close, just retry."""
    closes = []
    d = S._delete_branch_with_retry(
        "cfg", "fix/abc", "rk", 42, attempts=2, delay=0.001,
        delete_fn=lambda c, b, k: _resp({"reason": "forbidden_403", "refused": True}),
        close_fn=lambda c, p: closes.append(p) or _resp({"state": "closed"}),
        sleep=lambda s: None)
    _x(len(closes) == 0, "no re-close for non-open_pr_exists refusal")
    _x(d.get("reason") == "forbidden_403", "returns the refusal reason")


# ── cleanup_gh wiring ──

def test_cleanup_gh_closes_sleeps_then_deletes():
    """cleanup_gh closes both PRs, sleeps once (propagation), then deletes both
    branches via _delete_branch_with_retry. Verifies ordering + single sleep."""
    S.CL = {"pr": 7, "src_pr": 8, "branch": "fix/rk1-aaaaaaaaaaaa", "src_branch": "feature/c2-src-rk1"}
    events = []
    real_sleep, real_close, real_del = S.time.sleep, S.close_pr_mcp, S._delete_branch_with_retry
    S.time.sleep = lambda s: events.append(("sleep", s))  # type: ignore
    S.close_pr_mcp = lambda cfg, pr: events.append(("close", pr)) or _resp({"state": "closed"})  # type: ignore
    S._delete_branch_with_retry = lambda cfg, br, rk, pr, **kw: events.append(("delete", br, pr)) or {"deleted": True, "already_absent": False}  # type: ignore
    try:
        S.cleanup_gh({"net": "x"}, "rk1")
    finally:
        S.time.sleep, S.close_pr_mcp, S._delete_branch_with_retry = real_sleep, real_close, real_del  # type: ignore
    # ordering: close fix, close src, sleep, delete fix, delete src
    kinds = [e[0] for e in events]
    _x(kinds == ["close", "close", "sleep", "delete", "delete"],
       "close both PRs -> one sleep -> delete both (got %s)" % kinds)
    _x(len([e for e in events if e[0] == "sleep"]) == 1, "exactly one propagation sleep")
    deletes = [e for e in events if e[0] == "delete"]
    _x(deletes == [("delete", "fix/rk1-aaaaaaaaaaaa", 7), ("delete", "feature/c2-src-rk1", 8)],
       "delete both branches with their bound PR")


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n); fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
