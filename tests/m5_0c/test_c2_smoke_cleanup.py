#!/usr/bin/env python3
"""Unit tests for c2_smoke cleanup_gh retry + propagation verification.

Root cause fixed: GitHub PR-close is eventually-consistent; the bridge refuses
branch delete while a PR is open (open_pr_exists). cleanup_gh now closes PRs,
waits a fixed propagation delay, then deletes branches with a light retry on
open_pr_exists. After a successful delete, _verify_branches_gone bounded-polls
list_branches to confirm the branch has actually disappeared (GitHub delete is
also eventually-consistent — a branch may linger in list_branches for seconds
after the delete API returns success).

These tests exercise the retry + verification policy via injected callables
(no stack, no WSL, no network).
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
    """No propagation delay -> 1 delete, no close, no sleep."""
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
    """Bridge refuses open_pr_exists -> re-close PR -> sleep -> retry -> deleted."""
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
    """pr_num None + refused open_pr_exists -> never calls close (nothing to close)."""
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
    """A refusal that is NOT open_pr_exists (e.g. forbidden) -> no re-close, just retry."""
    closes = []
    d = S._delete_branch_with_retry(
        "cfg", "fix/abc", "rk", 42, attempts=2, delay=0.001,
        delete_fn=lambda c, b, k: _resp({"reason": "forbidden_403", "refused": True}),
        close_fn=lambda c, p: closes.append(p) or _resp({"state": "closed"}),
        sleep=lambda s: None)
    _x(len(closes) == 0, "no re-close for non-open_pr_exists refusal")
    _x(d.get("reason") == "forbidden_403", "returns the refusal reason")


# ── _verify_branches_gone ──

def test_verify_gone_first_poll():
    """Branch absent on first list_branches query -> confirmed immediately."""
    sleeps = []
    ok, residue, reason = S._verify_branches_gone(
        "cfg", ["fix/abc", "feature/c2-src-x"],
        list_fn=lambda c: ["main", "other"],
        max_polls=3, delay=0.01,
        sleep=lambda s: sleeps.append(s))
    _x(ok is True, "confirmed absent on first poll")
    _x(residue == [], "no residue")
    _x(len(sleeps) == 0, "no sleep needed")


def test_verify_gone_present_then_absent():
    """Branch present on poll 1, absent on poll 2 -> confirmed after 1 sleep."""
    poll = {"i": 0}

    def list_fn(c):
        names = ["main", "fix/abc"] if poll["i"] == 0 else ["main"]
        poll["i"] += 1
        return names

    sleeps = []
    ok, residue, reason = S._verify_branches_gone(
        "cfg", ["fix/abc"],
        list_fn=list_fn,
        max_polls=3, delay=0.01,
        sleep=lambda s: sleeps.append(s))
    _x(ok is True, "confirmed absent on poll 2")
    _x(len(sleeps) == 1, "slept once between polls")


def test_verify_gone_still_present_fail_closed():
    """Branch never disappears -> fail-closed after max_polls."""
    ok, residue, reason = S._verify_branches_gone(
        "cfg", ["fix/abc"],
        list_fn=lambda c: ["main", "fix/abc"],
        max_polls=3, delay=0.001,
        sleep=lambda s: None)
    _x(ok is False, "fail-closed when branch persists")
    _x("fix/abc" in residue, "residue includes the stale branch")


def test_verify_gone_query_none_fail_closed():
    """list_fn returns None (query failure) -> fail-closed immediately."""
    ok, residue, reason = S._verify_branches_gone(
        "cfg", ["fix/abc"],
        list_fn=lambda c: None,
        max_polls=3, delay=0.001,
        sleep=lambda s: None)
    _x(ok is False, "fail-closed on None query result")
    _x("fix/abc" in residue, "residue preserved on query failure")


def test_verify_gone_query_exception_fail_closed():
    """list_fn raises -> fail-closed."""
    ok, residue, reason = S._verify_branches_gone(
        "cfg", ["fix/abc"],
        list_fn=lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
        max_polls=3, delay=0.001,
        sleep=lambda s: None)
    _x(ok is False, "fail-closed on query exception")
    _x("fix/abc" in residue, "residue preserved on exception")


# ── cleanup_gh wiring (with verification) ──

def test_cleanup_gh_closes_sleeps_deletes_verifies():
    """cleanup_gh closes both PRs, sleeps once (propagation), deletes both
    branches, then verifies both gone. Verifies ordering."""
    S.CL = {"pr": 7, "src_pr": 8, "branch": "fix/rk1-aaaaaaaaaaaa",
            "src_branch": "feature/c2-src-rk1"}
    events = []
    real_sleep = S.time.sleep
    real_close = S.close_pr_mcp
    real_del = S._delete_branch_with_retry
    real_verify = S._verify_branches_gone
    S.time.sleep = lambda s: events.append(("sleep", s))  # type: ignore
    S.close_pr_mcp = lambda cfg, pr: events.append(("close", pr)) or _resp({"state": "closed"})  # type: ignore
    S._delete_branch_with_retry = lambda cfg, br, rk, pr, **kw: events.append(("delete", br, pr)) or {"deleted": True, "already_absent": False}  # type: ignore
    S._verify_branches_gone = lambda cfg, branches, **kw: events.append(("verify", tuple(branches))) or (True, [], "mock")  # type: ignore
    try:
        result = S.cleanup_gh({"net": "x"}, "rk1")
    finally:
        S.time.sleep, S.close_pr_mcp, S._delete_branch_with_retry, S._verify_branches_gone = real_sleep, real_close, real_del, real_verify  # type: ignore
    kinds = [e[0] for e in events]
    _x(kinds == ["close", "close", "sleep", "delete", "delete", "verify"],
       "close both PRs -> one sleep -> delete both -> verify (got %s)" % kinds)
    _x(result.get("clean") is True, "cleanup clean when verify confirms")


def test_cleanup_gh_verify_fails_returns_unclean():
    """Delete succeeds but verify reports branch still present -> clean=False."""
    S.CL = {"pr": 7, "src_pr": None, "branch": "fix/rk1-aaa", "src_branch": None}
    _saved = (S._verify_branches_gone, S._delete_branch_with_retry,
              S.close_pr_mcp, S.time.sleep)
    S._verify_branches_gone = lambda cfg, branches, **kw: (False, list(branches), "still present")  # type: ignore
    S._delete_branch_with_retry = lambda cfg, br, rk, pr, **kw: {"deleted": True}  # type: ignore
    S.close_pr_mcp = lambda cfg, pr: _resp({"state": "closed"})  # type: ignore
    S.time.sleep = lambda s: None  # type: ignore
    try:
        result = S.cleanup_gh({"net": "x"}, "rk1")
    finally:
        S._verify_branches_gone, S._delete_branch_with_retry, S.close_pr_mcp, S.time.sleep = _saved  # type: ignore
    _x(result.get("clean") is False, "unclean when verify fails")
    _x("fix/rk1-aaa" in result.get("residue", []), "residue includes stale branch")


def test_cleanup_gh_already_absent_still_verifies():
    """already_absent delete result still triggers verification."""
    S.CL = {"pr": 7, "src_pr": None, "branch": "fix/rk1-aaa", "src_branch": None}
    verified = {"called": False}
    _saved = (S._verify_branches_gone, S._delete_branch_with_retry,
              S.close_pr_mcp, S.time.sleep)

    def track_verify(cfg, branches, **kw):
        verified["called"] = True
        return (True, [], "mock")
    S._verify_branches_gone = track_verify  # type: ignore
    S._delete_branch_with_retry = lambda cfg, br, rk, pr, **kw: {"already_absent": True}  # type: ignore
    S.close_pr_mcp = lambda cfg, pr: _resp({"state": "closed"})  # type: ignore
    S.time.sleep = lambda s: None  # type: ignore
    try:
        S.cleanup_gh({"net": "x"}, "rk1")
    finally:
        S._verify_branches_gone, S._delete_branch_with_retry, S.close_pr_mcp, S.time.sleep = _saved  # type: ignore
    _x(verified["called"] is True, "verify called even for already_absent delete")


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n); fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
