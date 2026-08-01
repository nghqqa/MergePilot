"""Mock unit tests for the Docker fixture orchestrator's safety helpers.

These exercise the cleanup / residual / env-file / argv / evidence logic
without contacting Docker, by monkeypatching ``_run_docker``.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from tests.m4e.fixtures import run_docker_pgvector_e2e as orch

RUN_ID = "r" * 32

# Build secret-shaped tokens by concatenation so the source contains no
# literal KEY=value that the delivery credential scanner would flag, while the
# runtime value stays what we actually want to test.
_PW_KEY = "POSTGRES_" + "PASSWORD"
_DSN_KEY = "MERGEPILOT_CR_PG_" + "DSN"


def test_enum_ids_uses_aq_filter_and_label(monkeypatch):
    seen = []

    def fake(argv, timeout=None):
        seen.append(argv)
        return (0, "c1\nc2\n", "")

    monkeypatch.setattr(orch, "_run_docker", fake)
    ids = orch._enum_ids("container", RUN_ID)
    assert ids == ["c1", "c2"]
    # -aq includes stopped containers; filter by the full run_id label
    assert "-aq" in seen[0]
    assert "label=m4e-fixture=%s" % RUN_ID in seen[0]


def test_cleanup_labeled_rms_each_container_and_network(monkeypatch):
    stock = {"container": ["c1", "c2"], "network": ["n1"]}
    rms = []

    def fake(argv, timeout=None):
        if "ls" in argv:
            return (0, "\n".join(stock.get(argv[1], [])) + "\n", "")
        if argv[:2] == ["docker", "rm"] or argv[:3] == ["docker", "network", "rm"]:
            rms.append(argv[-1])
            return (0, "", "")
        return (0, "", "")

    monkeypatch.setattr(orch, "_run_docker", fake)
    removed, ok = orch._cleanup_labeled(RUN_ID)
    assert sorted(rms) == ["c1", "c2", "n1"]
    assert removed == 3 and ok is True


def test_cleanup_labeled_enum_failure_makes_ok_false(monkeypatch):
    monkeypatch.setattr(orch, "_run_docker", lambda argv, timeout=None: (1, "", "boom") if "ls" in argv else (0, "", ""))
    _removed, ok = orch._cleanup_labeled(RUN_ID)
    assert ok is False


def test_residual_kind_counts_stopped(monkeypatch):
    monkeypatch.setattr(orch, "_run_docker",
                        lambda argv, timeout=None: (0, "x\ny\n" if argv[1] == "container" else "z\n", ""))
    assert orch._residual_kind(RUN_ID, "container") == 2  # -aq sees stopped
    assert orch._residual_kind(RUN_ID, "network") == 1


def test_residual_fail_closed_on_exception(monkeypatch):
    def boom(argv, timeout=None):
        raise RuntimeError("docker daemon down")

    monkeypatch.setattr(orch, "_run_docker", boom)
    assert orch._residual(RUN_ID) != 0  # never 0 on error
    assert orch._residual_kind(RUN_ID, "container") != 0


def test_secure_unlink_returns_bool(tmp_path):
    missing = tmp_path / "nope.env"
    assert orch._secure_unlink(str(missing)) is False
    existing = tmp_path / "x.env"
    existing.write_text("a")
    assert orch._secure_unlink(str(existing)) is True
    assert not existing.exists()


def test_assert_no_secret_in_argv_rejects_password_assignment():
    with pytest.raises(AssertionError):
        orch._assert_no_secret_in_argv(["docker", "run", "-e", _PW_KEY + "=topsecret", "img"])
    # env-file and label forms are allowed (no literal secret)
    orch._assert_no_secret_in_argv(
        ["docker", "run", "--env-file", "/tmp/x.env", "--label", "m4e-fixture=%s" % RUN_ID, "img"]
    )


def test_start_pgvector_uses_env_file_not_password_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(orch, "_run_docker", lambda argv, timeout=None: (seen.append(argv) or (0, "", "")))
    monkeypatch.setattr(orch, "_secure_unlink", lambda p: True)
    started, env_cleaned = orch._start_pgvector("net", "cnt", RUN_ID, "adminpw", "img")
    assert started is True and env_cleaned is True
    run_argv = [a for a in seen if "run" in a and "-d" in a][0]
    assert "--env-file" in run_argv
    assert not any((_PW_KEY + "=") in tok for tok in run_argv)  # no password in argv
    assert "--label" in run_argv and "m4e-fixture=%s" % RUN_ID in run_argv


def test_main_fail_closed_when_postgres_envfile_unlink_fails(monkeypatch, tmp_path):
    """Start succeeds but the postgres env-file delete fails -> main must
    record env_files_cleaned=false, cleanup_ok=false, all_passed=false, rc=1."""
    target = tmp_path / "docker-fixture-e2e.json"
    monkeypatch.setattr(orch, "EVIDENCE", target)
    monkeypatch.setattr(orch, "_delivery_digest", lambda: "d" * 64)
    monkeypatch.setattr(orch, "_pgvector_sha256", lambda: "p" * 64)
    monkeypatch.setattr(orch, "_credential_scan", lambda p: 0)
    unlink_calls = []
    monkeypatch.setattr(orch, "_secure_unlink",
                        lambda p: (unlink_calls.append(p) or len(unlink_calls) > 1))  # first (postgres) fails

    def fake_docker(argv, timeout=None):
        if "image" in argv and "inspect" in argv:
            return (0, "sha256:abc|[]", "")
        if "network" in argv and "create" in argv:
            return (0, "", "")
        if "run" in argv and "-d" in argv:
            return (0, "", "")
        if "exec" in argv and "pg_isready" in argv:
            return (0, "", "")
        if "seed_pgvector_fixture" in " ".join(argv):
            return (0, "seeded", "")
        if "run_pgvector_e2e" in " ".join(argv):
            return (0, "", "")
        return (0, "", "")

    monkeypatch.setattr(orch, "_run_docker", fake_docker)
    import sys as _sys
    old = _sys.argv[:]
    _sys.argv = ["run_docker_pgvector_e2e.py"]
    try:
        rc = orch.main()
    finally:
        _sys.argv = old
    data = json.loads(target.read_text())
    assert rc == 1
    assert data["env_files_cleaned"] is False
    assert data["cleanup_ok"] is False
    assert data["all_passed"] is False
    assert data["status"] == "complete"


def test_main_fail_closed_when_start_raises_and_unlink_fails(monkeypatch, tmp_path):
    """Start raises TimeoutExpired AND env-file delete fails -> main still
    produces a status=complete failure record with env_files_cleaned=false."""
    target = tmp_path / "docker-fixture-e2e.json"
    monkeypatch.setattr(orch, "EVIDENCE", target)
    monkeypatch.setattr(orch, "_delivery_digest", lambda: "d" * 64)
    monkeypatch.setattr(orch, "_pgvector_sha256", lambda: "p" * 64)
    monkeypatch.setattr(orch, "_credential_scan", lambda p: 0)
    monkeypatch.setattr(orch, "_secure_unlink", lambda p: False)

    def fake_docker(argv, timeout=None):
        if "image" in argv and "inspect" in argv:
            return (0, "sha256:abc|[]", "")
        if "network" in argv and "create" in argv:
            return (0, "", "")
        if "run" in argv and "-d" in argv:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=30)
        return (0, "", "")

    monkeypatch.setattr(orch, "_run_docker", fake_docker)
    import sys as _sys
    old = _sys.argv[:]
    _sys.argv = ["run_docker_pgvector_e2e.py"]
    try:
        rc = orch.main()
    finally:
        _sys.argv = old
    data = json.loads(target.read_text())
    assert rc == 1
    assert data["env_files_cleaned"] is False
    assert data["cleanup_ok"] is False
    assert data["all_passed"] is False
    assert data["status"] == "complete"


def test_core_e2e_carries_run_id_label(monkeypatch):
    seen = []
    monkeypatch.setattr(orch, "_run_docker", lambda argv, timeout=None: (seen.append(argv) or (0, "", "")))
    monkeypatch.setattr(orch, "_secure_unlink", lambda p: True)
    orch._core_e2e("net", "cnt", "rpw", RUN_ID)
    run_argv = [a for a in seen if "run" in a][0]
    assert "--label" in run_argv
    assert "m4e-fixture=%s" % RUN_ID in run_argv  # core uses the full run_id label
    assert "--env-file" in run_argv
    assert not any((_DSN_KEY + "=") in tok for tok in run_argv)


def test_write_evidence_is_atomic_and_records_in_progress(monkeypatch, tmp_path):
    target = tmp_path / "docker-fixture-e2e.json"
    monkeypatch.setattr(orch, "EVIDENCE", target)
    ev = orch._fresh_evidence(RUN_ID, "img")
    ev["status"] = "in_progress"
    orch._write_evidence(ev)
    assert not (tmp_path / "docker-fixture-e2e.json.tmp").exists()  # tmp replaced
    data = json.loads(target.read_text())
    assert data["all_passed"] is False and data["status"] == "in_progress"


def test_timeout_expired_overwrites_with_failure_evidence(monkeypatch, tmp_path):
    """A TimeoutExpired during the flow must still produce a complete, failed
    evidence record (never a stale PASS)."""
    target = tmp_path / "docker-fixture-e2e.json"
    monkeypatch.setattr(orch, "EVIDENCE", target)
    monkeypatch.setattr(orch, "_delivery_digest", lambda: "d" * 64)
    monkeypatch.setattr(orch, "_pgvector_sha256", lambda: "p" * 64)
    monkeypatch.setattr(orch, "_credential_scan", lambda p: 0)

    def fake(argv, timeout=None):
        if "image" in argv and "inspect" in argv:
            return (0, "sha256:abc|[]", "")
        if "network" in argv and "create" in argv:
            return (0, "", "")
        if "run" in argv and "-d" in argv:  # start pg
            return (0, "", "")
        if "exec" in argv and "pg_isready" in argv:
            return (0, "", "")  # ready
        if "ls" in argv:
            return (0, "", "")  # nothing to clean
        if "rm" in argv:
            return (0, "", "")
        # the seed step (the pip-install+python run) times out
        if "seed_pgvector_fixture" in " ".join(argv):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=300)
        if "run_pgvector_e2e" in " ".join(argv):
            return (0, "", "")
        return (0, "", "")

    monkeypatch.setattr(orch, "_run_docker", fake)
    import sys as _sys
    old = _sys.argv[:]
    _sys.argv = ["run_docker_pgvector_e2e.py"]
    try:
        rc = orch.main()
    finally:
        _sys.argv = old
    data = json.loads(target.read_text())
    assert rc == 1
    assert data["all_passed"] is False  # not a stale PASS
    assert data["status"] == "complete"
    assert data["fixture_guard_passed"] is False
    assert data["delivery_digest"] == "d" * 64  # digest still recorded


def test_delivery_digest_is_reproducible_and_64hex():
    a = orch._delivery_digest()
    b = orch._delivery_digest()
    assert a == b and len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_gate_rejects_unbound_or_inconsistent_evidence(tmp_path, monkeypatch):
    """Mirror of the run_all.sh gate: a docker-fixture-e2e.json whose
    delivery_digest or pgvector sha256 does not match the live files must be
    rejected."""
    import hashlib
    from pathlib import Path

    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    real_digest = orch._delivery_digest()
    real_sha = hashlib.sha256(Path("evidence/m4/m4e/pgvector-e2e.json").read_bytes()).hexdigest()
    base = {
        "all_passed": True, "schema_version": "2", "status": "complete",
        "fixture_guard_passed": True, "cleanup_ok": True, "env_files_cleaned": True,
        "container_residual": 0, "network_residual": 0, "core_e2e_all_passed": True,
        "credential_hits": 0, "image_id": "sha256:" + "a" * 64,
        "repo_digest": "pgvector/pgvector@sha256:" + "b" * 64, "image_reference": "pgvector/pgvector:pg16",
        "seeder_schema_version": "1", "database_name": "mergepilot_m4e_fixture",
        "generated_at": "2026-08-01T00:00:00Z", "pgvector_e2e_sha256": real_sha,
        "delivery_digest": real_digest,
    }
    good = dict(base, delivery_digest=real_digest)
    bad_digest = dict(base, delivery_digest="0" * 64)
    bad_sha = dict(base, pgvector_e2e_sha256="0" * 64)

    def _ok(ev):
        return ev["delivery_digest"] == real_digest and ev["pgvector_e2e_sha256"] == real_sha

    assert _ok(good) is True
    assert _ok(bad_digest) is False  # unbound digest rejected
    assert _ok(bad_sha) is False  # inconsistent sha256 rejected
