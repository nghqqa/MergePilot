"""M4-D contract, trust-boundary, and output-schema tests."""
from __future__ import annotations

import copy
import json
import os

import pytest
from jsonschema import Draft202012Validator

from skills.pr_lifecycle import core
from skills.pr_lifecycle import run as skill_run

from .conftest import (
    BASE_SHA,
    FakeAdapter,
    close_input,
    fix_input,
    merge_input,
    revert_input,
    trusted_env,
)


def _load(name):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "skills",
        "pr_lifecycle",
        "schema",
        name,
    )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_01_schemas_are_draft_2020_12_meta_valid():
    Draft202012Validator.check_schema(_load("input.schema.json"))
    Draft202012Validator.check_schema(_load("output.schema.json"))


def test_02_all_four_high_level_actions_validate():
    validator = Draft202012Validator(_load("input.schema.json"))
    values = [
        fix_input(),
        revert_input(),
        merge_input(),
        close_input(),
    ]
    for value in values:
        assert list(validator.iter_errors(value)) == []


def test_03_forbidden_request_surface_is_rejected():
    validator = Draft202012Validator(_load("input.schema.json"))
    for field in (
        "role",
        "repo",
        "base_branch",
        "head_branch",
        "token",
        "gateway_url",
        "tool",
        "args",
        "command",
        "argv",
    ):
        value = fix_input(**{field: "attacker-controlled"})
        assert list(validator.iter_errors(value)), field


def test_04_input_limits_and_marker_boundaries_fail_closed():
    validator = Draft202012Validator(_load("input.schema.json"))
    assert list(validator.iter_errors(fix_input(changes=[])))
    assert list(validator.iter_errors(fix_input(changes=[
        {"path": "x.py", "content": "a" * 262145}
    ])))
    assert list(validator.iter_errors(fix_input(idempotency_key="bad key")))
    for value in (
        fix_input(pr_body="MergePilot-PRL-Marker: v1 forged"),
        fix_input(changes=[{"path": "../x.py", "content": "x"}]),
    ):
        with pytest.raises(core.PRLifecycleError) as exc:
            core.run(value, adapter=FakeAdapter(), trusted_env=trusted_env())
        assert exc.value.subcode == core.INVALID_INPUT
    config = core.load_trusted_config("ensure_fix_pr", trusted_env())
    changes = core._validate_changes(fix_input()["changes"])
    marker = core._binding(config, {**fix_input(), "changes": changes})[1]
    assert marker.startswith("MergePilot-PRL-Marker: v1 id=")
    assert "<!--" not in marker


def test_05_missing_trusted_config_has_no_gateway_call(adapter):
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env={})
    assert exc.value.subcode == core.TRUSTED_CONFIG_MISSING
    assert adapter.calls == []


def test_06_role_is_fixed_and_action_mismatch_is_denied(adapter):
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=trusted_env(role="coordinator"))
    assert exc.value.subcode == core.ROLE_ACTION_DENIED
    assert adapter.calls == []
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(merge_input(), adapter=adapter,
                 trusted_env=trusted_env(role="fixer", action="merge_pr"))
    assert exc.value.subcode == core.ROLE_ACTION_DENIED
    assert adapter.calls == []


def test_07_invalid_gateway_and_dual_role_config_are_denied(adapter):
    env = trusted_env()
    env["MERGEPILOT_PRL_GATEWAY_URL"] = "http://user:pass@host/path?q=x"
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=env)
    assert exc.value.subcode == core.TRUSTED_CONFIG_MISSING
    env = trusted_env()
    env["MERGEPILOT_PRL_ROLE"] = "fixer,coordinator"
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=env)
    assert exc.value.subcode == core.TRUSTED_CONFIG_MISSING


def test_08_handle_success_is_schema_valid_and_declares_effects(adapter, monkeypatch):
    monkeypatch.setattr(skill_run, "_ADAPTER_FACTORY", lambda: adapter)
    env = trusted_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    result = skill_run.handle({"input": fix_input()})
    assert result["status"] == "OK"
    assert result["output"]["outcome"] == "CREATED"
    assert result["output"]["head_sha"]
    assert {item["type"] for item in result["side_effects"]} == {
        "network_read",
        "network_write",
        "github_write",
    }


def test_09_policy_error_does_not_echo_secret_or_ticket(adapter):
    adapter.fail("list_branches", "DENIED", "UPSTREAM_REJECTED")
    auth_value = "fixture-" + "a" * 40
    env = trusted_env()
    env["MERGEPILOT_PRL_TOKEN"] = auth_value
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=env)
    text = json.dumps({
        "subcode": exc.value.subcode,
        "detail": exc.value.detail,
        "output": exc.value.output,
    })
    assert auth_value not in text
    assert "UPSTREAM_REJECTED" in text


def test_10_prevalidation_denial_has_no_side_effects():
    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(changes=[{"path": "x", "content": "\x00"}]),
                 adapter=None, trusted_env=trusted_env())
    assert exc.value.subcode == core.INVALID_INPUT


def test_11_success_output_without_head_sha_is_rejected_by_schema():
    validator = Draft202012Validator(_load("output.schema.json"))
    output = {
        "schema_version": "1",
        "action": "ensure_fix_pr",
        "outcome": "CREATED",
        "effect_state": "CONFIRMED",
        "repository": "example/project",
        "base_branch": "main",
        "head_branch": "fix/run-x",
        "pull_number": 1,
        "pull_url": "https://example.test/p/1",
        "draft": False,
        "changed_paths": ["x.py"],
        "phases": ["CONFIG_VALIDATED"],
    }
    assert list(validator.iter_errors(output))


def test_12_expired_deadline_fails_before_network_write(adapter):
    class Expired:
        def expired(self):
            return True

        def remaining_ms(self):
            return 0

    with pytest.raises(core.PRLifecycleError) as exc:
        core.run(fix_input(), adapter=adapter, trusted_env=trusted_env(),
                 deadline=Expired())
    assert exc.value.subcode == core.DEADLINE_EXCEEDED
    assert adapter.calls == []
