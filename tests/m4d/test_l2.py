"""Coordinator-only merge/close and M3 ticket/effect mapping tests."""
from __future__ import annotations

import pytest

from skills.pr_lifecycle import core

from .conftest import close_input, merge_input, trusted_env


def test_33_merge_pr_confirms_result_sha(adapter):
    adapter.seed_pr()
    out = core.run(
        merge_input(), adapter=adapter,
        trusted_env=trusted_env(role="coordinator", action="merge_pr"),
    )
    assert out["outcome"] == "MERGED"
    assert len(out["result_sha"]) == 40
    assert out["effect_state"] == "CONFIRMED"
    assert "L2_CONFIRMED" in out["phases"]


def test_34_merge_already_merged_is_read_only(adapter):
    pr = adapter.seed_pr(merged=True, state="closed")
    out = core.run(
        merge_input(pull_number=pr["number"]), adapter=adapter,
        trusted_env=trusted_env(role="coordinator", action="merge_pr"),
    )
    assert out["outcome"] == "ALREADY_MERGED"
    assert "merge_pull_request" not in adapter.calls


def test_35_close_pr_confirms_closed_state(adapter):
    adapter.seed_pr()
    out = core.run(
        close_input(), adapter=adapter,
        trusted_env=trusted_env(role="coordinator", action="close_pr"),
    )
    assert out["outcome"] == "CLOSED"
    assert "L2_CONFIRMED" in out["phases"]


def test_36_close_already_closed_is_read_only(adapter):
    adapter.seed_pr(state="closed")
    out = core.run(
        close_input(), adapter=adapter,
        trusted_env=trusted_env(role="coordinator", action="close_pr"),
    )
    assert out["outcome"] == "ALREADY_CLOSED"
    assert "close_pull_request" not in adapter.calls


def test_37_close_merged_is_not_misreported_as_close(adapter):
    adapter.seed_pr(merged=True, state="closed")
    out = core.run(
        close_input(), adapter=adapter,
        trusted_env=trusted_env(role="coordinator", action="close_pr"),
    )
    assert out["outcome"] == "ALREADY_MERGED"


def test_38_invalid_ticket_is_rejected_before_network(adapter):
    adapter.seed_pr()
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(
            merge_input(approval_ticket="tkt-not-a-uuid"), adapter=adapter,
            trusted_env=trusted_env(role="coordinator", action="merge_pr"),
        )
    assert exc.value.subcode == core.INVALID_INPUT
    assert adapter.calls == []


def test_39_policy_denial_does_not_claim_github_write(adapter):
    adapter.seed_pr()
    adapter.fail("merge_pull_request", "DENIED", "L2_TICKET_DENIED")
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(
            merge_input(), adapter=adapter,
            trusted_env=trusted_env(role="coordinator", action="merge_pr"),
        )
    assert exc.value.subcode == core.POLICY_DENIED
    assert exc.value.output["effect_state"] == "ATTEMPTED"
    assert not any(item["type"] == "github_write" for item in exc.value.effects)


def test_40_post_write_disconnect_is_effect_unknown(adapter):
    adapter.seed_pr()
    adapter.fail("merge_pull_request", "UNKNOWN", "OUTCOME_UNKNOWN", forwarded=True)
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(
            merge_input(), adapter=adapter,
            trusted_env=trusted_env(role="coordinator", action="merge_pr"),
        )
    assert exc.value.subcode == core.EFFECT_UNKNOWN
    assert exc.value.retryable is False
    assert exc.value.output["effect_state"] == "UNKNOWN"
    assert any(item["type"] == "github_write" for item in exc.value.effects)


def test_52_merge_success_sha_avoids_stale_post_write_read(monkeypatch, adapter):
    class DelayedMerge(type(adapter)):
        def __init__(self):
            super().__init__()
            self.post_merge_reads = 0

        def merge_pull_request(self, pull_number, ticket, merge_method, commit_title,
                               commit_message, *, timeout_ms):
            result = super().merge_pull_request(
                pull_number, ticket, merge_method, commit_title, commit_message,
                timeout_ms=timeout_ms,
            )
            return result

        def read_pull_request(self, pull_number, *, timeout_ms):
            result = super().read_pull_request(pull_number, timeout_ms=timeout_ms)
            if result["merged"]:
                self.post_merge_reads += 1
                result["merged"] = False
                result["state"] = "open"
                result["merge_commit_sha"] = None
            return result

    monkeypatch.setattr(core, "SETTLE_SLEEP_SECONDS", 0)
    delayed = DelayedMerge()
    delayed.seed_pr()
    out = core.run(
        merge_input(),
        adapter=delayed,
        trusted_env=trusted_env(role="coordinator", action="merge_pr"),
    )
    assert out["outcome"] == "MERGED"
    assert delayed.calls.count("merge_pull_request") == 1
    assert delayed.post_merge_reads == 0


def test_53_close_visibility_delay_is_read_only_reconciled(monkeypatch, adapter):
    class DelayedClose(type(adapter)):
        def __init__(self):
            super().__init__()
            self.hidden_close_reads = 0

        def close_pull_request(self, pull_number, ticket, *, timeout_ms):
            result = super().close_pull_request(
                pull_number, ticket, timeout_ms=timeout_ms,
            )
            self.hidden_close_reads = 6
            return result

        def read_pull_request(self, pull_number, *, timeout_ms):
            result = super().read_pull_request(pull_number, timeout_ms=timeout_ms)
            if self.hidden_close_reads:
                self.hidden_close_reads -= 1
                result["state"] = "open"
            return result

    monkeypatch.setattr(core, "SETTLE_SLEEP_SECONDS", 0)
    delayed = DelayedClose()
    delayed.seed_pr()
    out = core.run(
        close_input(),
        adapter=delayed,
        trusted_env=trusted_env(role="coordinator", action="close_pr"),
    )
    assert out["outcome"] == "CLOSED"
    assert delayed.calls.count("close_pull_request") == 1
