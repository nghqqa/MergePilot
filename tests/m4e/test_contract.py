"""Schema, production handle, and CLI envelope tests."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from skills.case_retrieval import core
from skills.case_retrieval import run as skill_run
from skills.common.runtime import errors


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def valid_output():
    return {
        "schema_version": "1",
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "embedding_version": "1.0.0",
        "complete": True,
        "results": [],
        "stats": {
            "total_found": 0,
            "returned": 0,
            "trusted_available": 0,
            "repo_scope": "repo-alpha",
            "knowledge_base_size": 0,
        },
        "degraded": [],
    }


def request(inp=None):
    return {
        "contract_version": "1",
        "request_id": "m4e-contract",
        "trace_id": "m4e-contract",
        "timeout_ms": 1000,
        "input": inp or {"query": "test"},
    }


def test_handle_uses_production_schema_and_validates_output(monkeypatch):
    monkeypatch.setattr(core, "run", lambda *args, **kwargs: valid_output())
    result = skill_run.handle({"input": {"query": "test"}, "deadline": None})
    assert result == {"status": "OK", "output": valid_output()}


def test_handle_rejects_schema_invalid_output(monkeypatch):
    monkeypatch.setattr(core, "run", lambda *args, **kwargs: {"complete": True})
    with pytest.raises(errors.SkillError) as raised:
        skill_run.handle({"input": {"query": "test"}, "deadline": None})
    assert "schema-invalid" in str(raised.value)


def test_handle_public_message_is_subcode_only(monkeypatch):
    def fail(*args, **kwargs):
        raise core.CaseRetrievalError(core.DB_UNAVAILABLE, "postgresql://user:secret@host/db")

    monkeypatch.setattr(core, "run", fail)
    with pytest.raises(errors.SkillError) as raised:
        skill_run.handle({"input": {"query": "test"}, "deadline": None})
    assert str(raised.value) == core.DB_UNAVAILABLE
    assert "secret" not in str(raised.value)


def test_output_schema_rejects_unbounded_or_unknown_degradation():
    validator = skill_run._output_validator()
    out = valid_output()
    out["degraded"] = [{"type": "arbitrary", "reason": "anything"}]
    assert list(validator.iter_errors(out))


def test_output_schema_caps_results():
    validator = skill_run._output_validator()
    out = valid_output()
    result = {
        "case_id": "x",
        "score": 0.5,
        "category": "quality",
        "severity": "low",
        "issue_summary": "i",
        "fix_summary": "f",
        "citation": {
            "source_id": "unknown",
            "source_type": "unknown",
            "source_url": None,
            "verifiable": False,
        },
        "source_version": "1",
        "created_at": "2026-01-01T00:00:00Z",
        "stale": False,
        "untrusted": True,
    }
    out["results"] = [result] * 21
    out["stats"]["returned"] = 21
    assert list(validator.iter_errors(out))


def _run_cli(req, extra_env):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("MERGEPILOT_CR_"):
            env.pop(key)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "skills.case_retrieval.run"],
        cwd=ROOT,
        input=json.dumps(req),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )


def test_cli_missing_scope_is_denied_and_subcode_only():
    proc = _run_cli(request(), {"MERGEPILOT_CR_PG_DSN": "redacted"})
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 4
    assert envelope["error_code"] == "DENIED"
    assert envelope["message"] == core.SCOPE_MISSING
    assert envelope["side_effects"] == []
    assert proc.stderr == ""


def test_cli_missing_dsn_is_dependency_unavailable():
    proc = _run_cli(request(), {})
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 5
    assert envelope["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert envelope["message"] == core.DB_UNAVAILABLE
    assert proc.stderr == ""


def test_input_schema_rejects_trusted_fields():
    validator = skill_run._input_validator()
    for forbidden in ("repo_scope", "dsn", "sql", "table", "model", "endpoint", "credential"):
        assert list(validator.iter_errors({"query": "x", forbidden: "bad"}))


def test_schemas_are_draft_2020_12_meta_valid():
    for validator in (skill_run._input_validator(), skill_run._output_validator()):
        jsonschema.Draft202012Validator.check_schema(validator.schema)


def test_output_schema_rejects_untrusted_false():
    validator = skill_run._output_validator()
    out = valid_output()
    result = {
        "case_id": "x",
        "score": 0.5,
        "category": "quality",
        "severity": "low",
        "issue_summary": "i",
        "fix_summary": "f",
        "citation": {
            "source_id": "u",
            "source_type": "pr",
            "source_url": "https://x.test/p/1",
            "verifiable": True,
        },
        "source_version": "1",
        "created_at": "2026-01-01T00:00:00Z",
        "stale": False,
        "untrusted": False,
    }
    out["results"] = [result]
    out["stats"]["returned"] = 1
    assert list(validator.iter_errors(out))


def test_e2e_evidence_versions_and_delivery_digest_are_reproducible():
    path = Path(ROOT) / "evidence/m4/m4e/pgvector-e2e.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    assert set(evidence["versions"]) == {
        "postgresql", "pgvector", "fastembed", "psycopg2"
    }
    digest = hashlib.sha256()
    paths = []
    for base in (Path(ROOT) / "skills/case_retrieval", Path(ROOT) / "tests/m4e"):
        for item in base.rglob("*"):
            if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                paths.append(item)
    for item in sorted(paths, key=lambda value: value.relative_to(ROOT).as_posix()):
        digest.update(item.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    assert evidence["delivery_digest"] == digest.hexdigest()
