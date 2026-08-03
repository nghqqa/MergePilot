#!/usr/bin/env python3
"""Unit + negative counterexample tests for the M4-F release evidence package.

Covers:
* delivery_digest determinism, scope inclusion (deployment entry), and the
  evidence / log / cache exclusions.
* write_verification final-rc logic: all-pass, gate failure, delivery digest
  mismatch, missing evidence (fail-closed), and the M4F_VFY_FORCE_FAIL
  fault-injection hook.

Stdlib only; runs inside the host runtime fixture under pytest.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TF1 = ROOT / "tests/m4f1"
sys.path.insert(0, str(TF1))

import delivery_digest  # noqa: E402
import write_verification  # noqa: E402


def _good_evidence(digest: str) -> dict:
    return {
        "all_passed": True,
        "secret_leaks": 0,
        "residue": {"containers": 0, "networks": 0, "temp_dirs": 0},
        "runner": {"run_rc": 0, "migration_round_1_rc": 0, "migration_round_2_rc": 0},
        "delivery": {"digest": digest, "files": 1, "scope": "test"},
        "fixture": {"external_credentials": False},
        "jobs": [],
    }


def _write(p: pathlib.Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def test_delivery_digest_deterministic():
    a, n = delivery_digest.compute_digest(ROOT)
    b, m = delivery_digest.compute_digest(ROOT)
    assert a == b
    assert n == m and n > 0


def test_delivery_digest_includes_deployment_entry():
    files = delivery_digest.delivery_files(ROOT)
    assert "tools/start-controller-container.sh" in files
    assert "tools/policy-gateway/gateway.py" in files
    assert "tools/m4f-runtime/Dockerfile" in files


def test_delivery_digest_excludes_generated_evidence_and_cache():
    files = set(delivery_digest.delivery_files(ROOT))
    assert not any("__pycache__" in f for f in files)
    assert not any(f.endswith(".pyc") for f in files)
    assert "tests/m4f1/evidence.json" not in files
    assert "evidence/m4/m4f/verification.txt" not in files


def test_write_verification_all_pass(tmp_path):
    digest, _ = delivery_digest.compute_digest(ROOT)
    evid = tmp_path / "agentteams-e2e.json"
    _write(evid, _good_evidence(digest))
    gates = tmp_path / "gates.tsv"
    gates.write_text("0\talpha\n0\tbeta\n", encoding="utf-8")
    out = tmp_path / "verification.txt"
    rc = write_verification.main_with_args([str(gates), str(evid), str(out), str(ROOT)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "ALL GATES PASSED" in text
    assert "gates_passed: 2" in text
    assert "delivery_digest_check: OK" in text


def test_write_verification_gate_failure(tmp_path):
    digest, _ = delivery_digest.compute_digest(ROOT)
    evid = tmp_path / "agentteams-e2e.json"
    _write(evid, _good_evidence(digest))
    gates = tmp_path / "gates.tsv"
    gates.write_text("0\talpha\n2\tbeta\n", encoding="utf-8")
    out = tmp_path / "verification.txt"
    rc = write_verification.main_with_args([str(gates), str(evid), str(out), str(ROOT)])
    assert rc != 0
    text = out.read_text(encoding="utf-8")
    assert "ALL GATES PASSED" not in text
    assert "gates_failed: 1" in text


def test_write_verification_digest_mismatch(tmp_path):
    evid = tmp_path / "agentteams-e2e.json"
    _write(evid, _good_evidence("deadbeef" * 8))
    gates = tmp_path / "gates.tsv"
    gates.write_text("0\talpha\n", encoding="utf-8")
    out = tmp_path / "verification.txt"
    rc = write_verification.main_with_args([str(gates), str(evid), str(out), str(ROOT)])
    assert rc != 0
    text = out.read_text(encoding="utf-8")
    assert "MISMATCH" in text


def test_write_verification_missing_evidence_is_fail_closed(tmp_path):
    gates = tmp_path / "gates.tsv"
    gates.write_text("0\talpha\n", encoding="utf-8")
    out = tmp_path / "verification.txt"
    rc = write_verification.main_with_args(
        [str(gates), str(tmp_path / "does-not-exist.json"), str(out), str(ROOT)]
    )
    assert rc != 0
    text = out.read_text(encoding="utf-8")
    assert "evidence not generated" in text


def test_write_verification_force_fail_hook(monkeypatch):
    monkeypatch.setenv("M4F_VFY_FORCE_FAIL", "1")
    assert write_verification.main() != 0


def test_negatives_gate_bash_wiring():
    """Drive the full bash gate (release_finish sourced + write_verification)
    and assert exit-status propagation + the three counterexample outcomes.
    This covers the Bash wiring that the per-writer unit tests cannot."""
    import subprocess
    script = TF1 / "run_release_evidence_negatives.sh"
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=180,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, "negatives gate rc=%d\n%s" % (proc.returncode, combined)
    assert "RELEASE EVIDENCE NEGATIVES ALL PASSED" in proc.stdout
    assert "SCENARIO-PASS positive control" in proc.stdout
    assert "SCENARIO-PASS case1 writer failure" in proc.stdout
    assert "SCENARIO-PASS case2 digest mismatch" in proc.stdout
    assert "Traceback" not in combined, "traceback leaked:\n%s" % combined
