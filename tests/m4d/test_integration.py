"""Full M4-A runtime integration with injected Policy Gateway fixtures."""
from __future__ import annotations

import json

from skills.common.runtime import cli as common_cli
from skills.pr_lifecycle import run as skill_run

from .conftest import (
    BAD_SHA,
    PARENT_SHA,
    FakeAdapter,
    fix_input,
    merge_input,
    revert_input,
    trusted_env,
)


def _request(inp):
    return {
        "contract_version": "1",
        "request_id": "req-m4d",
        "trace_id": "trace-m4d",
        "input": inp,
        "timeout_ms": 60000,
    }


def _install(monkeypatch, adapter, env):
    monkeypatch.setattr(skill_run, "_ADAPTER_FACTORY", lambda: adapter)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_48_fix_pr_runs_through_common_runtime(monkeypatch):
    adapter = FakeAdapter()
    _install(monkeypatch, adapter, trusted_env())
    env, code = common_cli.run_request(
        _request(fix_input()),
        skill_run.handle,
        name=skill_run.SKILL_NAME,
        version=skill_run.SKILL_VERSION,
    )
    assert code == 0
    assert env["status"] == "OK"
    assert env["output"]["outcome"] == "CREATED"
    assert env["name"] == "pr-lifecycle"


def test_49_schema_injection_is_rejected_before_adapter(monkeypatch):
    adapter = FakeAdapter()
    _install(monkeypatch, adapter, trusted_env())
    inp = fix_input()
    inp["tool"] = "delete_file"
    env, code = common_cli.run_request(
        _request(inp),
        skill_run.handle,
        name=skill_run.SKILL_NAME,
        version=skill_run.SKILL_VERSION,
    )
    assert code == 2
    assert env["status"] == "ERROR"
    assert env["error_code"] == "INVALID_INPUT"
    assert adapter.calls == []


def test_50_revert_runtime_output_contains_paths_not_content(monkeypatch):
    adapter = FakeAdapter(base_sha=BAD_SHA)
    adapter.commits[BAD_SHA] = {
        "sha": BAD_SHA,
        "files": [{"path": "src/app.py", "status": "modified"}],
    }
    adapter.commit_sequences[BAD_SHA] = [BAD_SHA, PARENT_SHA]
    adapter.sha_files[PARENT_SHA] = {"src/app.py": "private parent content\n"}
    _install(monkeypatch, adapter, trusted_env(action="ensure_revert_pr"))
    env, code = common_cli.run_request(
        _request(revert_input()),
        skill_run.handle,
        name=skill_run.SKILL_NAME,
        version=skill_run.SKILL_VERSION,
    )
    text = json.dumps(env)
    assert code == 0
    assert env["output"]["changed_paths"] == ["src/app.py"]
    assert "private parent content" not in text


def test_51_effect_unknown_maps_to_dependency_unavailable(monkeypatch):
    adapter = FakeAdapter()
    adapter.seed_pr()
    adapter.fail("merge_pull_request", "UNKNOWN", "OUTCOME_UNKNOWN", forwarded=True)
    _install(
        monkeypatch,
        adapter,
        trusted_env(role="coordinator", action="merge_pr"),
    )
    env, code = common_cli.run_request(
        _request(merge_input()),
        skill_run.handle,
        name=skill_run.SKILL_NAME,
        version=skill_run.SKILL_VERSION,
    )
    assert code == 5
    assert env["status"] == "ERROR"
    assert env["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert env["message"] == "PRL_EFFECT_UNKNOWN"
    assert env["retryable"] is False
    assert env["output"]["effect_state"] == "UNKNOWN"
