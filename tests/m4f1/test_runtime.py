import hashlib
import importlib.util
import json
import os
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m4f_skill_worker", ROOT / "tools/m4f_skill_worker.py"
)
WORKER_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKER_MODULE
SPEC.loader.exec_module(WORKER_MODULE)

CONTROLLER_SPEC = importlib.util.spec_from_file_location(
    "m4f_controller", ROOT / "tools/workflow-controller/m4f_controller.py"
)
CONTROLLER_MODULE = importlib.util.module_from_spec(CONTROLLER_SPEC)
sys.modules[CONTROLLER_SPEC.name] = CONTROLLER_MODULE
CONTROLLER_SPEC.loader.exec_module(CONTROLLER_MODULE)


def _request(skill, input_, trace="trace-demo"):
    return {
        "contract_version": "1",
        "request_id": "req-1234567890abcdef12345678",
        "trace_id": trace,
        "input": input_,
    }


def _job(skill, request):
    raw = json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
    schema = ROOT / "skills" / WORKER_MODULE.SKILL_DIRS[skill] / "schema/output.schema.json"
    return WORKER_MODULE.Job(
        job_id="job-" + skill,
        run_id="run-demo",
        snapshot_id="snap-demo",
        trace_id=request["trace_id"],
        skill_name=skill,
        skill_version="1.0.0",
        request_digest=hashlib.sha256(raw).hexdigest(),
        request_bytes=raw,
        output_schema_digest=hashlib.sha256(schema.read_bytes()).hexdigest(),
    )


def _risk_context():
    files = [
        {
            "path": "src/auth.py",
            "old_path": None,
            "change_type": "M",
            "additions": 2,
            "deletions": 1,
            "binary": False,
            "mode_changed": False,
            "categories": ["source", "security_sensitive"],
            "hunks": [],
        }
    ]
    return {
        "schema_version": "1",
        "source": {"repo": "demo/repo"},
        "input_sha256": "0" * 64,
        "complete": True,
        "files": files,
        "modules_touched": ["src"],
        "change_categories": ["security_sensitive", "source"],
        "stats": {
            "files_changed": 1,
            "additions": 2,
            "deletions": 1,
            "hunks": 0,
            "binary_files": 0,
        },
    }


@pytest.fixture
def worker(tmp_path):
    trusted = {
        "test-runner": {
            "MERGEPILOT_TR_WORKSPACE": str(tmp_path),
            "MERGEPILOT_TR_EXECUTOR": "process",
            "MERGEPILOT_TR_TRUSTED_DEV": "true",
            "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
            "MERGEPILOT_TR_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        }
    }
    (tmp_path / "artifacts").mkdir()
    return WORKER_MODULE.SkillWorker(
        object(), repo_root=ROOT, trusted_skill_env=trusted
    )


def test_child_environment_does_not_inherit_control_credentials(worker, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "control-secret")
    monkeypatch.setenv("M4F_DATABASE_DSN", "postgresql://control")
    monkeypatch.setenv("SKILL_RUNNER_TOKEN", "runner-secret")
    env = worker._child_env("diff-parse")
    assert "PGPASSWORD" not in env
    assert "M4F_DATABASE_DSN" not in env
    assert "SKILL_RUNNER_TOKEN" not in env
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT)


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"b": 1, "a": {"n": None}}, '{"a":{"n":null},"b":1}'),
        ({"n": 0.0000001}, '{"n":1e-7}'),
        ({"n": 0.000001}, '{"n":0.000001}'),
        ({"n": -0.0}, '{"n":0}'),
        ({"a\\b": "x\ty"}, '{"a\\\\b":"x\\ty"}'),
        ({"\ue000": 1, "\U0001d54f": 2}, '{"\U0001d54f":2,"\ue000":1}'),
    ],
)
def test_controller_jcs_fixed_vectors(value, expected):
    assert CONTROLLER_MODULE.canonical_json(value) == expected


def test_controller_request_id_matches_frozen_vector():
    assert CONTROLLER_MODULE._request_identity(
        "tr1", "pa_run1", "diff-parse", {"f": 1}
    ) == "req-523b4899a7f81fd7ecb8e16c"


@pytest.mark.parametrize(
    "skill,input_,expected_status",
    [
        (
            "diff-parse",
            {
                "repo": "demo/repo",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "diff_format": "unified",
                "diff_text": (
                    "diff --git a/src/app.py b/src/app.py\n"
                    "--- a/src/app.py\n+++ b/src/app.py\n"
                    "@@ -1 +1 @@\n-print('old')\n+print('safe')\n"
                ),
                "pr_number": 42,
            },
            "OK",
        ),
        ("risk-classify", {"change_context": _risk_context()}, "OK"),
        (
            "sast-scan",
            {"mode": "inline", "files": [{"path": "src/app.py", "content": "print('safe')\n"}]},
            "OK",
        ),
        (
            "case-retrieval",
            {"query": "parameterized SQL injection fix", "top_k": 3},
            "ERROR",
        ),
        (
            "pr-lifecycle",
            {
                "action": "ensure_fix_pr",
                "idempotency_key": "demo.fix.1",
                "changes": [{"path": "src/app.py", "content": "print('safe')\n"}],
                "commit_message": "fix demo",
                "pr_title": "fix demo",
                "pr_body": "deterministic fixture",
            },
            "ERROR",
        ),
    ],
)
def test_real_skill_cli_and_schema_boundary(worker, skill, input_, expected_status):
    request = _request(skill, input_)
    job = _job(skill, request)
    input_validator, output_validator = worker._schema_validators(job)
    WORKER_MODULE._validate(input_validator, input_, "Skill input")
    response_bytes, _, _ = worker._execute(job, 30)
    response = WORKER_MODULE._strict_json(response_bytes)
    assert response["status"] == expected_status
    validated = worker._validate_response(job, response_bytes, request, output_validator)
    assert validated is (expected_status != "ERROR")


def test_real_test_runner_cli(worker, tmp_path):
    tests_dir = tmp_path / "demo_tests"
    tests_dir.mkdir()
    (tests_dir / "test_demo.py").write_text(
        "def test_demo():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    request = _request(
        "test-runner",
        {"runner_key": "pytest", "test_paths": ["demo_tests/test_demo.py"], "timeout_ms": 30000},
    )
    job = _job("test-runner", request)
    input_validator, output_validator = worker._schema_validators(job)
    WORKER_MODULE._validate(input_validator, request["input"], "Skill input")
    response_bytes, return_code, _ = worker._execute(job, 35)
    response = WORKER_MODULE._strict_json(response_bytes)
    assert return_code in (0, 10), json.dumps(response["output"], sort_keys=True)
    assert response["status"] == "OK"
    assert response["output"]["verdict"] in ("PASS", "FAIL")
    assert worker._validate_response(job, response_bytes, request, output_validator) is True
