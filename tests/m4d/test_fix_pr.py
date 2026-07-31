"""Fix PR creation, reconciliation, and idempotency tests."""
from __future__ import annotations

import copy

import pytest

from skills.pr_lifecycle import core

from .conftest import BASE_SHA, FakeAdapter, fix_input, trusted_env


def _branch(adapter, env, inp):
    config = core.load_trusted_config(inp["action"], env)
    changes = core._validate_changes(inp["changes"])
    return core._binding(config, {**inp, "changes": changes})[0]


def test_13_fix_pr_creates_one_branch_commit_and_pr(adapter):
    out = core.run(fix_input(), adapter=adapter, trusted_env=trusted_env())
    assert out["outcome"] == "CREATED"
    assert out["head_branch"].startswith("fix/run-123-")
    assert out["changed_paths"] == ["src/app.py"]
    assert adapter.calls.count("create_branch") == 1
    assert adapter.calls.count("push_files") == 1
    assert adapter.calls.count("create_pull_request") == 1


def test_14_same_payload_replay_returns_existing_without_writes(adapter):
    inp = fix_input()
    first = core.run(inp, adapter=adapter, trusted_env=trusted_env())
    calls_after_first = list(adapter.calls)
    second = core.run(inp, adapter=adapter, trusted_env=trusted_env())
    assert first["head_branch"] == second["head_branch"]
    assert second["outcome"] == "EXISTING"
    assert adapter.calls == calls_after_first + [
        "list_branches",
        "list_pull_requests",
        "read_pull_request",
        "list_pull_request_files",
        "read_pull_request",
        "get_file",
    ]


def test_15_same_idempotency_key_different_payload_is_conflict(adapter):
    inp = fix_input()
    core.run(inp, adapter=adapter, trusted_env=trusted_env())
    writes_before = [x for x in adapter.calls if x in {
        "create_branch", "push_files", "create_pull_request"
    }]
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(changes=[{"path": "src/app.py", "content": "different\n"}]),
                 adapter=adapter, trusted_env=trusted_env())
    assert exc.value.subcode == core.IDEMPOTENCY_CONFLICT
    writes_after = [x for x in adapter.calls if x in {
        "create_branch", "push_files", "create_pull_request"
    }]
    assert writes_after == writes_before


def test_16_existing_base_branch_is_reused_and_pushed(adapter):
    inp = fix_input()
    env = trusted_env()
    branch = _branch(adapter, env, inp)
    adapter.branches[branch] = BASE_SHA
    adapter.branch_history[branch] = [BASE_SHA]
    adapter.branch_files[branch] = {}
    out = core.run(inp, adapter=adapter, trusted_env=env)
    assert out["outcome"] == "CREATED"
    assert "create_branch" not in adapter.calls
    assert adapter.calls.count("push_files") == 1


def test_17_pushed_matching_branch_without_pr_is_reconciled(adapter):
    inp = fix_input()
    first = core.run(inp, adapter=adapter, trusted_env=trusted_env())
    adapter.prs.clear()
    adapter.next_pr = 1
    out = core.run(inp, adapter=adapter, trusted_env=trusted_env())
    assert out["outcome"] == "CREATED"
    assert out["head_branch"] == first["head_branch"]
    assert adapter.calls.count("push_files") == 1
    assert adapter.calls.count("create_pull_request") == 2


def test_18_unknown_branch_content_is_not_overwritten(adapter):
    inp = fix_input()
    env = trusted_env()
    branch = _branch(adapter, env, inp)
    adapter.branches[branch] = "a" * 40
    adapter.branch_history[branch] = ["a" * 40, BASE_SHA]
    adapter.branch_files[branch] = {"src/app.py": "untrusted\n"}
    adapter.commits["a" * 40] = {
        "sha": "a" * 40,
        "files": [{"path": "src/app.py", "status": "modified"}],
    }
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(inp, adapter=adapter, trusted_env=env)
    assert exc.value.subcode == core.IDEMPOTENCY_CONFLICT
    assert "push_files" not in adapter.calls


def test_19_multiple_matching_prs_fail_closed(adapter):
    inp = fix_input()
    env = trusted_env()
    branch = _branch(adapter, env, inp)
    adapter.branches[branch] = "a" * 40
    adapter.branch_history[branch] = ["a" * 40, BASE_SHA]
    adapter.branch_files[branch] = {"src/app.py": "print('fixed')\n"}
    adapter.commits["a" * 40] = {
        "sha": "a" * 40,
        "files": [{"path": "src/app.py", "status": "modified"}],
    }
    config = core.load_trusted_config("ensure_fix_pr", env)
    marker = core._binding(config, {**inp, "changes": core._validate_changes(inp["changes"])})[1]
    adapter.seed_pr(head=branch, title=inp["pr_title"], body=marker, head_sha="a" * 40)
    adapter.seed_pr(head=branch, title=inp["pr_title"], body=marker, head_sha="a" * 40)
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(inp, adapter=adapter, trusted_env=env)
    assert exc.value.subcode == core.IDEMPOTENCY_CONFLICT


def test_20_base_movement_is_not_force_overwritten(adapter):
    adapter.branches["main"] = "f" * 40
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=trusted_env())
    assert exc.value.subcode == core.IDEMPOTENCY_CONFLICT
    assert "create_branch" not in adapter.calls
    assert "push_files" not in adapter.calls


def test_21_l2_risk_creates_draft_pr(adapter):
    out = core.run(
        fix_input(),
        adapter=adapter,
        trusted_env=trusted_env(risk="L2"),
    )
    assert out["draft"] is True
    assert adapter.prs[0]["draft"] is True


def test_22_write_outcome_unknown_is_not_retryable(adapter):
    adapter.fail("push_files", "UNKNOWN", "UPSTREAM_OUTCOME_UNKNOWN", forwarded=True)
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=trusted_env())
    assert exc.value.subcode == core.EFFECT_UNKNOWN
    assert exc.value.retryable is False
    assert exc.value.output["effect_state"] == "UNKNOWN"
    assert any(item["type"] == "github_write" for item in exc.value.effects)


def test_23_branch_visibility_delay_is_read_only_reconciled(monkeypatch):
    class DelayedBranch(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.hide_once = False

        def create_branch(self, branch, from_branch, *, timeout_ms):
            result = super().create_branch(branch, from_branch, timeout_ms=timeout_ms)
            self.hide_once = True
            return result

        def list_branches(self, *, page, per_page, timeout_ms):
            items = super().list_branches(
                page=page, per_page=per_page, timeout_ms=timeout_ms
            )
            if self.hide_once:
                self.hide_once = False
                return [item for item in items if not item["name"].startswith("fix/")]
            return items

    monkeypatch.setattr(core, "SETTLE_SLEEP_SECONDS", 0)
    adapter = DelayedBranch()
    out = core.run(fix_input(), adapter=adapter, trusted_env=trusted_env())
    assert out["outcome"] == "CREATED"
    assert adapter.calls.count("create_branch") == 1


def test_24_pr_visibility_delay_does_not_duplicate_create(monkeypatch):
    class DelayedPR(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.hide_once = False

        def create_pull_request(self, head, base, title, body, draft, *, timeout_ms):
            result = super().create_pull_request(
                head, base, title, body, draft, timeout_ms=timeout_ms
            )
            self.hide_once = True
            return result

        def list_pull_requests(self, *, state, page, per_page, timeout_ms):
            items = super().list_pull_requests(
                state=state, page=page, per_page=per_page, timeout_ms=timeout_ms
            )
            if self.hide_once:
                self.hide_once = False
                return []
            return items

    monkeypatch.setattr(core, "SETTLE_SLEEP_SECONDS", 0)
    adapter = DelayedPR()
    out = core.run(fix_input(), adapter=adapter, trusted_env=trusted_env())
    assert out["outcome"] == "CREATED"
    assert adapter.calls.count("create_pull_request") == 1
