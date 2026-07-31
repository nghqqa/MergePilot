"""Policy Gateway adapter normalization and failure-boundary tests."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from skills.pr_lifecycle import core
from skills.pr_lifecycle.adapters import policy_gateway
from skills.pr_lifecycle.adapters.policy_gateway import PolicyGatewayAdapter

from .conftest import BASE_SHA, trusted_env


def _adapter():
    return PolicyGatewayAdapter(
        core.load_trusted_config("ensure_fix_pr", trusted_env())
    )


def _result(payload, *, is_error=False):
    return SimpleNamespace(
        is_error=is_error,
        content=[SimpleNamespace(text=json.dumps(payload))],
    )


def test_41_list_branches_uses_fixed_repo_and_pagination(monkeypatch):
    adapter = _adapter()
    seen = {}

    def fake(tool, args, timeout_ms, write=False):
        seen.update({"tool": tool, "args": args, "timeout_ms": timeout_ms,
                     "write": write})
        return _result({"branches": [{"name": "main", "sha": BASE_SHA}]})

    monkeypatch.setattr(adapter, "_invoke", fake)
    assert adapter.list_branches(page=2, per_page=50, timeout_ms=1234) == [
        {"name": "main", "sha": BASE_SHA}
    ]
    assert seen["tool"] == "list_branches"
    assert seen["args"] == {
        "owner": "example",
        "repo": "project",
        "page": 2,
        "perPage": 50,
    }
    assert seen["write"] is False


def test_42_pull_request_normalization_includes_binding_fields():
    raw = {
        "number": 7,
        "state": "open",
        "title": "Fix",
        "body": "body",
        "merged": False,
        "draft": True,
        "head": {
            "ref": "fix/run-x",
            "sha": "a" * 40,
            "repo": {"full_name": "example/project"},
        },
        "base": {"ref": "main"},
        "merge_commit_sha": None,
        "html_url": "https://example.test/pull/7",
    }
    out = PolicyGatewayAdapter._normalize_pr(raw)
    assert out["title"] == "Fix"
    assert out["head_repo_full_name"] == "example/project"
    assert out["draft"] is True


def test_43_malformed_upstream_payload_is_schema_failure(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        adapter,
        "_invoke",
        lambda *args, **kwargs: _result({"branches": "not-a-list"}),
    )
    with pytest.raises(core.GatewayFailure) as exc:
        adapter.list_branches(page=1, per_page=100, timeout_ms=1000)
    assert exc.value.kind == "SCHEMA"


def test_44_gateway_reason_code_is_allowlisted():
    result = SimpleNamespace(
        is_error=True,
        content=[SimpleNamespace(text="reason_code=L2_TICKET_DENIED details omitted")],
    )
    with pytest.raises(core.GatewayFailure) as exc:
        PolicyGatewayAdapter._raise_result_error(result, write=True)
    assert exc.value.kind == "DENIED"
    assert exc.value.reason_code == "L2_TICKET_DENIED"
    assert exc.value.forwarded is False


def test_45_started_write_failure_is_unknown_not_retryable(monkeypatch):
    adapter = _adapter()

    async def failing(tool, args, state):
        state["call_started"] = True
        raise RuntimeError("connection lost after forwarding")

    monkeypatch.setattr(adapter, "_async_call", failing)
    with pytest.raises(core.GatewayFailure) as exc:
        adapter._call_result("merge_pull_request", {}, 1000, write=True)
    assert exc.value.kind == "UNKNOWN"
    assert exc.value.forwarded is True


def test_46_transport_ignores_proxy_environment_and_redirects(monkeypatch):
    seen = {}

    class Client:
        pass

    def fake_client(**kwargs):
        seen.update(kwargs)
        return Client()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=fake_client))
    result = policy_gateway._isolated_httpx_client(
        headers={"Authorization": "Bearer fixture"},
        timeout=3,
        auth=None,
    )
    assert isinstance(result, Client)
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False


def test_47_completed_result_survives_transport_close(monkeypatch):
    adapter = _adapter()
    marker = object()

    async def completed_then_close(tool, args, state):
        state["call_started"] = True
        state["call_completed"] = True
        state["result"] = marker
        raise RuntimeError("SSE close failed after result")

    monkeypatch.setattr(adapter, "_async_call", completed_then_close)
    assert adapter._call_result("list_branches", {}, 1000, write=False) is marker


def test_54_merge_write_normalizes_authoritative_sha(monkeypatch):
    adapter = _adapter()
    expected = "b" * 40
    monkeypatch.setattr(
        adapter,
        "_invoke",
        lambda *args, **kwargs: _result({
            "sha": expected,
            "merged": True,
            "message": "Pull Request successfully merged",
        }),
    )
    out = adapter.merge_pull_request(
        7,
        "tkt-00000000-0000-4000-8000-000000000001",
        "squash",
        "Merge fix",
        "",
        timeout_ms=1000,
    )
    assert out == {"sha": expected}
