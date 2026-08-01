#!/usr/bin/env python3
"""Versioned Docker orchestrator for the M4-E pgvector fixture E2E.

Spins up an isolated, LABELED ``pgvector/pgvector:pg16`` container with a
dedicated database, calls the GUARDED seeder, runs the core E2E, then ALWAYS
tears down every container and network carrying this run's label.  Emits
structured, digest-bound lifecycle evidence to
``evidence/m4/m4e/docker-fixture-e2e.json``.

Hardening:
  * Every container (postgres, seeder, core) and the network gets a UNIQUE name
    and the SAME ``--label m4e-fixture=<run_id>``.
  * Cleanup enumerates ``docker container ls -aq`` / ``docker network ls -q`` by
    THIS label and ``rm -f``s each -- never prune / never global.  Residual is
    re-queried including STOPPED containers; a failed query is fail-closed
    (non-zero), never 0.
  * Secrets (admin password, reader DSN) travel via a 0600 ``--env-file`` deleted
    afterward; a delete failure sets cleanup_ok/all_passed false.  No secret in
    any argv.
  * In-progress evidence (all_passed=false) is written atomically at start; ANY
    exception (incl. TimeoutExpired) completes cleanup and overwrites with a
    failure record -- a stale PASS is never retained.
  * Evidence records the live ``delivery_digest`` and the pgvector-e2e.json
    SHA-256; ``run_all.sh`` recomputes and verifies both.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
EVIDENCE = ROOT / "evidence" / "m4" / "m4e" / "docker-fixture-e2e.json"
PG_EVIDENCE = ROOT / "evidence" / "m4" / "m4e" / "pgvector-e2e.json"
FIXTURE_DB = "mergepilot_m4e_fixture"
CONFIRM_VALUE = "i-understand-this-drops-the-m4e-fixture"
LABEL = "m4e-fixture"
SCHEMA_VERSION = "2"

_DSN_REDACT_RE = re.compile(r":[^/@]*@", re.IGNORECASE)
_SECRET_ARGV = re.compile(r"(PASSWORD|DSN|TOKEN|SECRET|CREDENTIAL)", re.IGNORECASE)


def _run_docker(argv, timeout=None):
    """Real docker invocation. Tests monkeypatch this. Returns (rc, out, err)."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _label(run_id: str) -> str:
    return "%s=%s" % (LABEL, run_id)


def _redact(text: str) -> str:
    return _DSN_REDACT_RE.sub(":***@", text or "")


def _assert_no_secret_in_argv(argv):
    """Ensures no element exposes a password/DSN/token literally."""
    for token in argv:
        if _SECRET_ARGV.search(token) and "=" in token and not token.startswith("--label"):
            # an `-e VAR=secret` form is forbidden; env-file / label are allowed
            raise AssertionError("secret-shaped token in argv: %r" % (token,))


def _enum_ids(kind: str, run_id: str):
    """`docker container ls -aq` (incl. stopped) / `docker network ls -q`,
    filtered by this run's label. Raises on failure."""
    flag = "-aq" if kind == "container" else "-q"  # networks have no -a
    rc, out, err = _run_docker(["docker", kind, "ls", flag, "--filter", "label=" + _label(run_id)])
    if rc != 0:
        raise RuntimeError("%s enumeration failed: %s" % (kind, _redact(err)))
    return [ln for ln in out.splitlines() if ln.strip()]


def _cleanup_labeled(run_id: str):
    """Remove every container and network with this run's label. Containers use
    `docker rm -f`, networks use `docker network rm`. Returns (n, ok)."""
    ok = True
    removed = 0
    for kind in ("container", "network"):
        try:
            ids = _enum_ids(kind, run_id)
        except RuntimeError:
            ids = []
            ok = False
        rm_prefix = ["docker", "rm", "-f"] if kind == "container" else ["docker", "network", "rm"]
        for identifier in ids:
            rc, _out, _err = _run_docker(rm_prefix + [identifier])
            if rc == 0:
                removed += 1
            else:
                ok = False
    return removed, ok


def _residual(run_id: str) -> int:
    """Containers (incl. stopped) + networks with this label. Fail-closed:
    any enumeration error returns a large non-zero number, never 0."""
    try:
        return len(_enum_ids("container", run_id)) + len(_enum_ids("network", run_id))
    except Exception:
        return 999999


def _secure_unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def _write_env_file(entries: dict):
    fd, path = tempfile.mkstemp(prefix="m4e_env_", suffix=".env")
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w") as handle:
        for key, value in entries.items():
            handle.write("%s=%s\n" % (key, value))
    return path


def _write_evidence(evidence: dict) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(EVIDENCE) + ".tmp"
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    os.replace(tmp, EVIDENCE)  # atomic


def _delivery_digest() -> str:
    digest = hashlib.sha256()
    for base in (ROOT / "skills/case_retrieval", ROOT / "tests/m4e"):
        for p in sorted(base.rglob("*"), key=lambda v: v.relative_to(ROOT).as_posix()):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                digest.update(p.relative_to(ROOT).as_posix().encode())
                digest.update(b"\0")
                digest.update(p.read_bytes())
                digest.update(b"\0")
    return digest.hexdigest()


def _pgvector_sha256() -> str:
    return hashlib.sha256(PG_EVIDENCE.read_bytes()).hexdigest()


def _wait_ready(container: str) -> bool:
    for _ in range(60):
        rc, _o, _e = _run_docker(["docker", "exec", container, "pg_isready", "-U", "postgres"])
        if rc == 0:
            return True
        time.sleep(1)
    return False


def _start_pgvector(network: str, container: str, run_id: str, admin_pw: str, image: str):
    """Start postgres. Returns ``(started, env_cleaned)``.

    Catches TimeoutExpired/OSError so the env-file delete result is never lost
    (a start failure + delete failure still reports env_cleaned=False, and main
    aggregates it into cleanup_ok/all_passed). The admin password never enters
    argv (only the 0600 --env-file path is referenced).
    """
    env_file = _write_env_file({"POSTGRES_PASSWORD": admin_pw, "POSTGRES_DB": FIXTURE_DB})
    started = False
    env_cleaned = False
    try:
        argv = ["docker", "run", "-d", "--name", container, "--network", network,
                "--label", _label(run_id), "--env-file", env_file, image]
        _assert_no_secret_in_argv(argv)
        rc, _o, _e = _run_docker(argv)
        started = rc == 0
    except Exception:  # noqa: BLE001 -- TimeoutExpired/OSError -> not started
        started = False
    finally:
        env_cleaned = _secure_unlink(env_file)
    return started, env_cleaned


def _seed(network: str, container: str, admin_pw: str, reader_pw: str, run_id: str):
    admin_dsn = "postgres://postgres:%s@%s:5432/%s" % (admin_pw, container, FIXTURE_DB)
    env_file = _write_env_file({
        "PGADMIN_DSN": admin_dsn,
        "READER_PASSWORD": reader_pw,
        "M4E_EPHEMERAL_CONFIRM": CONFIRM_VALUE,
        "M4E_FIXTURE_RUN_ID": run_id,
    })
    ok_unlink = True
    rc = -1
    out = ""
    try:
        argv = ["docker", "run", "--rm", "--network", network,
                "-v", "%s:/work" % ROOT, "-w", "/work",
                "--name", "m4e-seed-%s" % run_id[:8],
                "--label", _label(run_id), "--env-file", env_file,
                "python:3.10-slim", "sh", "-lc",
                "pip install -q psycopg2-binary==2.9.12 && PYTHONPATH=/work "
                "python tests/m4e/fixtures/seed_pgvector_fixture.py"]
        _assert_no_secret_in_argv(argv)
        rc, out, err = _run_docker(argv, timeout=300)
        if rc != 0:
            sys.stderr.write("seed failed: %s\n" % _redact(err[-400:]))
    finally:
        ok_unlink = _secure_unlink(env_file)
    return ("seeded" in out and rc == 0), ok_unlink


def _core_e2e(network: str, container: str, reader_pw: str, run_id: str):
    reader_dsn = "postgres://case_retrieval_reader:%s@%s:5432/%s" % (reader_pw, container, FIXTURE_DB)
    env_file = _write_env_file({"MERGEPILOT_CR_PG_DSN": reader_dsn})
    ok_unlink = True
    rc = -1
    try:
        argv = ["docker", "run", "--rm", "--network", network,
                "-v", "%s:/work" % ROOT, "-w", "/work",
                "--name", "m4e-core-%s" % run_id[:8],
                "--label", _label(run_id), "--env-file", env_file,
                "python:3.10-slim", "sh", "-lc",
                'pip install -q psycopg2-binary==2.9.12 fastembed==0.7.4 "jsonschema>=4" '
                "&& PYTHONPATH=/work python tests/m4e/fixtures/run_pgvector_e2e.py "
                "--output /work/evidence/m4/m4e/pgvector-e2e.json"]
        _assert_no_secret_in_argv(argv)
        rc, out, err = _run_docker(argv, timeout=600)
        if rc != 0:
            sys.stderr.write("core E2E failed: %s\n" % _redact(err[-400:]))
    finally:
        ok_unlink = _secure_unlink(env_file)
    return rc == 0, ok_unlink


def _fresh_evidence(run_id: str, image: str, *, all_passed=False) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_id_summary": "%s..len%d" % (run_id[:6], len(run_id)),
        "image_reference": image,
        "image_id": None,
        "repo_digest": None,
        "database_name": FIXTURE_DB,
        "fixture_guard_passed": False,
        "seeder_schema_version": None,
        "core_e2e_all_passed": False,
        "container_residual": None,
        "network_residual": None,
        "cleanup_ok": False,
        "env_files_cleaned": False,
        "delivery_digest": _delivery_digest(),
        "pgvector_e2e_sha256": None,
        "credential_hits": None,
        "all_passed": all_passed,
    }


def _image_identity(image: str):
    rc, out, _e = _run_docker(["docker", "image", "inspect", image,
                               "--format", "{{.Id}}|{{json .RepoDigests}}"])
    if rc != 0 or not out.strip():
        return None, []
    ident, _, rest = out.strip().partition("|")
    try:
        digests = json.loads(rest) if rest else []
    except ValueError:
        digests = []
    return ident.strip(), digests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="pgvector/pgvector:pg16")
    args = parser.parse_args()

    run_id = secrets.token_hex(16)
    container = "m4e-pg-%s" % run_id[:8]
    network = "m4e-net-%s" % run_id[:8]
    admin_pw = secrets.token_hex(16)
    reader_pw = secrets.token_hex(16)

    evidence = _fresh_evidence(run_id, args.image)
    evidence["status"] = "in_progress"
    _write_evidence(evidence)  # atomic: no stale PASS retained on crash

    image_id, repo_digests = _image_identity(args.image)
    evidence["image_id"] = image_id
    evidence["repo_digest"] = repo_digests[0] if repo_digests else None

    guard_ok = core_ok = False
    env_files_ok = True
    try:
        nrc, _o, _e = _run_docker(["docker", "network", "create", "--label", _label(run_id), network])
        if nrc != 0:
            raise RuntimeError("network create failed")
        started, postgres_env_cleaned = _start_pgvector(network, container, run_id, admin_pw, args.image)
        env_files_ok = env_files_ok and postgres_env_cleaned
        if not started:
            raise RuntimeError("postgres container did not start")
        if not _wait_ready(container):
            raise RuntimeError("postgres did not become ready")
        guard_ok, env_ok1 = _seed(network, container, admin_pw, reader_pw, run_id)
        env_files_ok = env_files_ok and env_ok1
        evidence["fixture_guard_passed"] = guard_ok
        evidence["seeder_schema_version"] = "1" if guard_ok else None
        if not guard_ok:
            raise RuntimeError("seeder guard/seed failed")
        core_ok, env_ok2 = _core_e2e(network, container, reader_pw, run_id)
        env_files_ok = env_files_ok and env_ok2
        evidence["core_e2e_all_passed"] = core_ok
        if not core_ok:
            raise RuntimeError("core E2E failed")
    except Exception as exc:  # noqa: BLE001 -- TimeoutExpired, OSError, RuntimeError
        sys.stderr.write("orchestrator error (%s): %s\n" % (type(exc).__name__, exc))
    finally:
        _removed, cleanup_ok = _cleanup_labeled(run_id)
        evidence["cleanup_ok"] = bool(cleanup_ok and env_files_ok)
        evidence["env_files_cleaned"] = env_files_ok
        evidence["container_residual"], evidence["network_residual"] = (
            _residual_kind(run_id, "container"), _residual_kind(run_id, "network")
        )

    evidence["pgvector_e2e_sha256"] = _pgvector_sha256() if PG_EVIDENCE.exists() else None
    evidence["credential_hits"] = _credential_scan(PG_EVIDENCE)
    evidence["all_passed"] = bool(
        guard_ok and core_ok and evidence["cleanup_ok"]
        and evidence["container_residual"] == 0
        and evidence["network_residual"] == 0
        and evidence["env_files_cleaned"]
        and evidence["credential_hits"] == 0
    )
    evidence["status"] = "complete"
    evidence["generated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    _write_evidence(evidence)
    sys.stdout.write("run_id_summary=%s all_passed=%s guard=%s core=%s cleanup=%s "
                     "container_residual=%s network_residual=%s env_files_cleaned=%s\n" % (
                         evidence["run_id_summary"], evidence["all_passed"],
                         evidence["fixture_guard_passed"], evidence["core_e2e_all_passed"],
                         evidence["cleanup_ok"], evidence["container_residual"],
                         evidence["network_residual"], evidence["env_files_cleaned"]))
    return 0 if evidence["all_passed"] else 1


def _residual_kind(run_id: str, kind: str) -> int:
    """Fail-closed per-kind residual (exception -> large non-zero)."""
    try:
        return len(_enum_ids(kind, run_id))
    except Exception:
        return 999999


def _credential_scan(path: Path) -> int:
    scanner = ROOT / "tests" / "skills" / "scan_delivery.py"
    if not scanner.exists() or not path.exists():
        return 0
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, str(scanner), str(path)], capture_output=True, text=True, env=env)
    m = re.search(r"total_hits=(\d+)", r.stdout or "")
    return int(m.group(1)) if m else (0 if r.returncode == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
