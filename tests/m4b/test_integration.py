"""M4-B integration tests: DiffParse -> RiskClassify end-to-end.

Pure compute pipeline (no network/GitHub/DB). Includes a real CLI subprocess
round-trip through the common runtime, and determinism of the full pipeline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from skills.diff_parse import core as dpc
from skills.risk_classify import core as rcc

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIX = os.path.join(_REPO_ROOT, "tests", "m4b", "fixtures")
SHA = "0" * 40
HEAD = "f" * 40


def _fixture(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _parse(text, **kw):
    return dpc.parse_diff(repo="o/r", base_sha=SHA, head_sha=HEAD,
                          diff_text=text, diff_format="unified", **kw)


def _classify(cc, floor="L0"):
    return rcc.classify(cc, risk_floor=floor)


def _pipeline(text, floor="L0"):
    return _classify(_parse(text), floor=floor)


def test_01_documentation_to_L0():
    out = _pipeline(_fixture("real-modified.diff"))  # source, not docs -- sanity L1
    assert out["risk_level"] == "L1"
    # a true docs-only change -> L0
    doc = "diff --git a/docs/g.md b/docs/g.md\n--- a/docs/g.md\n+++ b/docs/g.md\n@@ -1 +1,2 @@\n t\n+u\n"
    assert _pipeline(doc)["risk_level"] == "L0"


def test_02_source_to_L1():
    out = _pipeline(_fixture("real-modified.diff"))
    assert out["risk_level"] == "L1"


def test_03_dependency_to_L2():
    dep = "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1,2 @@\n click\n+flask\n"
    assert _pipeline(dep)["risk_level"] == "L2"


def test_04_migration_and_workflow_to_L2():
    wf = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1,2 @@\n jobs:\n+  build:\n"
    mig = "diff --git a/migrations/0002.sql b/migrations/0002.sql\nnew file mode 100644\n--- /dev/null\n+++ b/migrations/0002.sql\n@@ -0,0 +1 @@\n+ALTER TABLE t ADD c INT;\n"
    out = _pipeline(wf + mig)
    assert out["risk_level"] == "L2"
    assert "MIGRATION_SCHEMA" in out["matched_rules"]
    assert "WORKFLOW_CI" in out["matched_rules"]


def test_05_partial_context_conservative_upgrade():
    # build a small docs change but force DiffParse to return PARTIAL via max_files
    text = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n t\n+u\n"
        "diff --git a/extra.py b/extra.py\n--- a/extra.py\n+++ b/extra.py\n@@ -1 +1,2 @@\n x\n+y\n"
    )
    cc = _parse(text, options={"max_files": 1})
    assert cc["complete"] is False  # only first file parsed, second dropped
    out = _classify(cc)
    assert out["risk_level"] in ("L1", "L2")  # PARTIAL_CONTEXT forces at least L1
    assert "PARTIAL_CONTEXT" in out["matched_rules"]


def test_06_security_source_to_L2():
    sec = "diff --git a/src/auth/login.py b/src/auth/login.py\n--- a/src/auth/login.py\n+++ b/src/auth/login.py\n@@ -1 +1,2 @@\n def login():\n+    pass\n"
    out = _pipeline(sec)
    assert out["risk_level"] == "L2"
    assert "SECURITY_SENSITIVE_PATH" in out["matched_rules"]


def test_07_source_deletion_to_L2():
    out = _pipeline(_fixture("real-deleted.diff"))
    assert out["risk_level"] == "L2"
    assert "SOURCE_DELETION" in out["matched_rules"]


def test_08_full_pipeline_deterministic():
    text = _fixture("multi-file.diff")
    a = json.dumps(_pipeline(text), sort_keys=True)
    b = json.dumps(_pipeline(text), sort_keys=True)
    assert a == b


def _run_skill(module, business_input):
    """Run a Skill entry via subprocess through the common runtime; return output dict."""
    req = {"contract_version": "1", "request_id": "req-i", "trace_id": "tr-i",
           "input": business_input}
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-m", module], input=json.dumps(req),
                          capture_output=True, text=True, cwd=_REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr
    env_out = json.loads(proc.stdout)
    assert env_out["status"] == "OK", env_out
    assert env_out["contract_version"] == "1"
    return env_out["output"]


def test_09_cli_diffparse_then_riskclassify_end_to_end():
    dp_out = _run_skill("skills.diff_parse.run", {
        "repo": "o/r", "base_sha": SHA, "head_sha": HEAD,
        "diff_format": "unified",
        "diff_text": "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1,2 @@\n click\n+flask\n",
    })
    assert dp_out["schema_version"] == "1"
    rc_out = _run_skill("skills.risk_classify.run", {"change_context": dp_out})
    assert rc_out["risk_level"] == "L2"
    assert rc_out["advisory_only"] is True
    assert rc_out["approval_recommended"] is True


def test_10_common_cli_redacts_credential_in_malformed_envelope():
    # The common CLI's pre-validation error path must route through _finalize:
    # a malformed request envelope carrying a credential-shaped contract_version
    # must not leak to stdout.
    import subprocess
    import sys
    probe = "ghp_" + "a" * 36  # assembled -> source scanner-clean
    req = {"contract_version": probe, "request_id": "r", "trace_id": "t", "input": {}}
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "skills.common.runtime.cli",
         "--skill", "skills.diff_parse.run.handle"],
        input=json.dumps(req), capture_output=True, text=True, cwd=_REPO_ROOT, env=env,
    )
    env_out = json.loads(proc.stdout)
    assert probe not in proc.stdout
    assert env_out["status"] == "ERROR"
    assert env_out["error_code"] == "SCHEMA_VERSION_UNSUPPORTED"
    assert env_out["redactions"]  # the echoing message was redacted
