from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTROLLER_DIR = ROOT / "tools/workflow-controller"
sys.path.insert(0, str(CONTROLLER_DIR))


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INGRESS = _load("m4f_ingress_test", CONTROLLER_DIR / "m4f_ingress.py")
GATEWAY = _load("gateway_client_m4f_test", CONTROLLER_DIR / "gateway_client.py")


def event_payload():
    return {
        "contract_version": "1",
        "run_id": "run-live-1",
        "trace_id": "trace-live-1",
        "repo": "example/project",
        "pr_number": 42,
        "risk_floor": "L1",
        "case_query": "parameterized database lookup",
        "test_runner": {
            "runner_key": "pytest",
            "test_paths": ["tests/m4f1/fixtures/demo_workspace/test_demo.py"],
            "expected_profiles_version": "1.0.0",
        },
        "pr_lifecycle": {
            "action": "ensure_fix_pr",
            "idempotency_key": "m4f.live.fix.1",
            "changes": [{"path": "src/app.py", "content": "print('safe')\n"}],
            "commit_message": "fix: safe query",
            "pr_title": "fix: safe query",
            "pr_body": "M4-F event fixture",
        },
    }


def pr_payload():
    return {
        "head_sha": "2" * 40,
        "base_sha": "1" * 40,
        "state": "open",
        "base": "main",
        "merged": False,
        "pr_number": 42,
        "head_ref": "fix/run-live-1",
        "head_repo_full_name": "example/project",
    }


def test_parse_event_is_strict_and_preserves_business_input():
    payload = event_payload()
    body = "M4F_RUN: " + json.dumps(payload, ensure_ascii=False)
    assert INGRESS.parse_event(body) == payload

    duplicate = body[:-1] + ',"run_id":"other"}'
    with pytest.raises(INGRESS.M4FIngressError, match="duplicate JSON key"):
        INGRESS.parse_event(duplicate)

    bad = dict(payload, base_sha="3" * 40)
    with pytest.raises(INGRESS.M4FIngressError, match="extra"):
        INGRESS.validate_event(bad)


def test_build_inputs_uses_only_gateway_revision_and_changed_files():
    diff = "diff --git a/src/app.py b/src/app.py\n+print('safe')\n"
    files = [
        {
            "filename": "src/app.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "+print('safe')\n",
        }
    ]
    inputs = INGRESS.build_skill_inputs(event_payload(), pr_payload(), diff, files)
    assert set(inputs) == {
        "diff-parse",
        "risk-classify",
        "sast-scan",
        "test-runner",
        "case-retrieval",
        "pr-lifecycle",
    }
    assert inputs["diff-parse"]["base_sha"] == "1" * 40
    assert inputs["diff-parse"]["head_sha"] == "2" * 40
    assert inputs["sast-scan"]["files"] == [
        {"path": "src/app.py", "content": "+print('safe')\n"}
    ]
    context = inputs["risk-classify"]["change_context"]
    assert context["files"][0]["path"] == "src/app.py"
    assert context["input_sha256"]


def test_gateway_revision_read_strips_no_authority_and_returns_base_sha(monkeypatch):
    seen = {}

    def fake_call(tool, args, timeout):
        seen.update({"tool": tool, "args": args, "timeout": timeout})
        return (
            json.dumps(
                {
                    "number": 42,
                    "state": "open",
                    "merged": False,
                    "head": {
                        "sha": "2" * 40,
                        "ref": "fix/run-live-1",
                        "repo": {"full_name": "example/project"},
                    },
                    "base": {"sha": "1" * 40, "ref": "main"},
                }
            ),
            None,
        )

    monkeypatch.setattr(GATEWAY, "gateway_call", fake_call)
    status, pr = GATEWAY.gateway_read_pr(
        "example", "project", 42, timeout=7, run_id="run-live-1"
    )
    assert status == "OK"
    assert pr["base_sha"] == "1" * 40
    assert seen["args"]["mergepilot_run_id"] == "run-live-1"


def test_gateway_files_rejects_non_list(monkeypatch):
    monkeypatch.setattr(GATEWAY, "gateway_call", lambda *args, **kwargs: ("{}", None))
    with pytest.raises(GATEWAY.GatewayUnavailable, match="invalid schema"):
        GATEWAY.gateway_get_pr_files("example", "project", 42)
