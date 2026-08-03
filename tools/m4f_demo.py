#!/usr/bin/env python3
"""Disposable M4-F Controller -> snapshot -> six-Skill competition Demo."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
from importlib import metadata
from typing import Any

import psycopg2


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTROLLER_DIR = ROOT / "tools/workflow-controller"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_DIR))

from m4f_controller import SKILL_DAG, SKILLS, stage_six_skill_run  # noqa: E402


def _load_worker_module():
    path = ROOT / "tools/m4f_skill_worker.py"
    spec = importlib.util.spec_from_file_location("m4f_skill_worker_demo", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WORKER = _load_worker_module()
RUN_ID = "run-123"
TRACE_ID = "trace-m4f-demo-0001"
REPO = "example/project"
PR_NUMBER = 42
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
SOURCE_CALL_ID = "demo-base-read-result"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_case_fixture(admin_conn: Any) -> None:
    from skills.case_retrieval.embedding.fastembed_provider import DeterministicFakeProvider

    vector = DeterministicFakeProvider().embed("parameterized SQL injection remediation")
    vector_text = "[" + ",".join(repr(float(value)) for value in vector) + "]"
    with admin_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            """DO $role$
               BEGIN
                 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='case_retrieval_reader') THEN
                   CREATE ROLE case_retrieval_reader LOGIN;
                 END IF;
               END $role$"""
        )
        cur.execute(
            "ALTER ROLE case_retrieval_reader NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS"
        )
        cur.execute("ALTER ROLE case_retrieval_reader SET default_transaction_read_only=on")
        cur.execute("ALTER ROLE case_retrieval_reader SET statement_timeout='10s'")
        cur.execute("ALTER ROLE case_retrieval_reader SET lock_timeout='5s'")
        cur.execute("CREATE SCHEMA IF NOT EXISTS demo_cases")
        cur.execute(
            """CREATE TABLE IF NOT EXISTS demo_cases.knowledge(
                 id BIGSERIAL PRIMARY KEY,
                 task_id TEXT,finding_id TEXT,category TEXT,severity TEXT,
                 issue TEXT,fix TEXT,file TEXT,source TEXT,repo_scope TEXT,source_pr_url TEXT,
                 source_commit_sha VARCHAR(40),source_version TEXT,
                 embedding_version TEXT,embedding_model TEXT,content TEXT,
                 adopted BOOLEAN DEFAULT FALSE,created_at TIMESTAMPTZ DEFAULT now(),
                 embedding public.vector(384)
               )"""
        )
        cur.execute(
            """INSERT INTO demo_cases.knowledge(
                 task_id,finding_id,category,severity,issue,fix,file,source,repo_scope,
                 source_pr_url,source_commit_sha,source_version,
                 embedding_version,embedding_model,content,adopted,embedding)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s::public.vector
               WHERE NOT EXISTS (
                 SELECT 1 FROM demo_cases.knowledge WHERE finding_id=%s
               )""",
            (
                "demo-task",
                "DEMO-CASE-1",
                "security",
                "high",
                "SQL query concatenated untrusted input",
                "Use a parameterized query and bind variables",
                "src/user_service.py",
                "demo-fixture",
                REPO,
                "https://github.com/example/project/pull/7",
                "3" * 40,
                "v1",
                "1.0.0",
                "BAAI/bge-small-en-v1.5",
                "parameterized SQL injection remediation",
                vector_text,
                "DEMO-CASE-1",
            ),
        )
        cur.execute("REVOKE CREATE ON SCHEMA public FROM case_retrieval_reader")
        cur.execute("GRANT CONNECT ON DATABASE mergepilot_demo TO case_retrieval_reader")
        cur.execute("GRANT USAGE ON SCHEMA public TO case_retrieval_reader")
        cur.execute("GRANT USAGE ON SCHEMA demo_cases TO case_retrieval_reader")
        cur.execute("GRANT SELECT ON demo_cases.knowledge TO case_retrieval_reader")
    admin_conn.commit()


def _source_evidence_digest(controller_conn: Any) -> str:
    with controller_conn.cursor() as cur:
        cur.execute(
            """SELECT encode(public.digest(
                 public._canon_str(request_id) || public._canon_str(correlation_id) ||
                 public._canon_str(tool) || public._canon_str(target_repo) ||
                 public._canon_str(run_id) || public._canon_str(git_sha) ||
                 public._canon_str(result_status), 'sha256'), 'hex')
               FROM public.mcp_calls WHERE request_id=%s""",
            (SOURCE_CALL_ID,),
        )
        row = cur.fetchone()
    controller_conn.commit()
    if not row or not row[0]:
        raise RuntimeError("source evidence row missing")
    return str(row[0])


def _risk_context() -> dict[str, Any]:
    file_ = {
        "path": "src/user_service.py",
        "old_path": None,
        "change_type": "M",
        "additions": 2,
        "deletions": 1,
        "binary": False,
        "mode_changed": False,
        "categories": ["source", "security_sensitive"],
        "hunks": [],
    }
    return {
        "schema_version": "1",
        "source": {"repo": REPO},
        "input_sha256": hashlib.sha256(b"demo-diff").hexdigest(),
        "complete": True,
        "files": [file_],
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


def _skill_inputs() -> dict[str, Any]:
    safe_source = (
        "def load_user(cur, user_id):\n"
        "    cur.execute('SELECT name FROM users WHERE id = %s', (user_id,))\n"
        "    return cur.fetchone()\n"
    )
    return {
        "diff-parse": {
            "repo": REPO,
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "diff_format": "unified",
            "pr_number": PR_NUMBER,
            "diff_text": (
                "diff --git a/src/user_service.py b/src/user_service.py\n"
                "--- a/src/user_service.py\n+++ b/src/user_service.py\n"
                "@@ -1 +1,2 @@\n"
                "-cur.execute('SELECT * FROM users WHERE id=' + user_id)\n"
                "+cur.execute('SELECT name FROM users WHERE id = %s', (user_id,))\n"
                "+return cur.fetchone()\n"
            ),
        },
        "risk-classify": {"change_context": _risk_context(), "risk_floor": "L1"},
        "sast-scan": {
            "mode": "inline",
            "files": [{"path": "src/user_service.py", "content": safe_source}],
        },
        "test-runner": {
            "runner_key": "pytest",
            "test_paths": ["tests/m4f1/fixtures/demo_workspace/test_demo.py"],
            "timeout_ms": 30000,
            "expected_profiles_version": "1.0.0",
        },
        "case-retrieval": {
            "query": "parameterized SQL injection remediation",
            "top_k": 3,
            "filters": {"category": "security"},
            "expected_embedding_version": "1.0.0",
        },
        "pr-lifecycle": {
            "action": "ensure_fix_pr",
            "idempotency_key": "m4f.demo.fix.1",
            "changes": [{"path": "src/user_service.py", "content": safe_source}],
            "commit_message": "fix: parameterize user lookup",
            "pr_title": "fix: parameterize user lookup",
            "pr_body": "M4-F deterministic competition fixture",
        },
    }


def _collect_result(skill_conn: Any, run_id: str) -> tuple[list[dict[str, Any]], int]:
    with skill_conn.cursor() as cur:
        cur.execute(
            """SELECT j.skill_name,j.job_id,j.status,j.attempts,
                      i.invocation_id,i.status,i.error_code,i.verdict,
                      i.output_schema_validated,e.content_json->>'message',
                      e.content_json->'output'
               FROM public.skill_job_outbox j
               LEFT JOIN public.skill_invocations i
                 ON i.invocation_id=j.result_invocation_id
               LEFT JOIN public.envelope_store e ON e.content_digest=i.output_digest
               WHERE j.run_id=%s ORDER BY j.created_at,j.skill_name""",
            (run_id,),
        )
        rows = cur.fetchall()
    skill_conn.commit()
    jobs = []
    for row in rows:
        output = row[10] if isinstance(row[10], dict) else {}
        jobs.append(
            {
                "skill": row[0],
                "job_id": row[1],
                "job_status": row[2],
                "attempts": row[3],
                "invocation_id": row[4],
                "response_status": row[5],
                "error_code": row[6],
                "verdict": row[7],
                "output_schema_validated": row[8],
                "message": row[9],
                "summary": {
                    key: output.get(key)
                    for key in ("risk_level", "verdict", "outcome", "complete")
                    if key in output
                },
            }
        )
    return jobs, len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--admin-dsn", default=os.environ.get("M4F_ADMIN_DSN"))
    parser.add_argument("--controller-dsn", default=os.environ.get("M4F_CONTROLLER_DSN"))
    parser.add_argument("--snapshot-dsn", default=os.environ.get("M4F_SNAPSHOT_DSN"))
    parser.add_argument("--skill-dsn", default=os.environ.get("M4F_SKILL_DSN"))
    args = parser.parse_args(argv)
    if not all((args.admin_dsn, args.controller_dsn, args.snapshot_dsn, args.skill_dsn)):
        raise SystemExit("four role DSNs are required")

    observations: list[dict[str, Any]] = []

    def observe(event: dict[str, Any]) -> None:
        observations.append(event)
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)

    admin_conn = psycopg2.connect(args.admin_dsn)
    controller_conn = psycopg2.connect(args.controller_dsn)
    snapshot_conn = psycopg2.connect(args.snapshot_dsn)
    skill_conn = psycopg2.connect(args.skill_dsn)
    try:
        _seed_case_fixture(admin_conn)
        # The fixture admin represents the Gateway/audit producer here.
        # Controller itself intentionally has no direct mcp_calls table read.
        evidence_digest = _source_evidence_digest(admin_conn)
        staged = stage_six_skill_run(
            controller_conn,
            snapshot_conn,
            run_id=RUN_ID,
            trace_id=TRACE_ID,
            repo=REPO,
            pr_number=PR_NUMBER,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            source_call_id=SOURCE_CALL_ID,
            source_evidence_digest=evidence_digest,
            skill_inputs=_skill_inputs(),
            observer=observe,
        )

        artifact_root = pathlib.Path("/tmp/m4f-demo-artifacts")
        artifact_root.mkdir(parents=True, exist_ok=True)
        db_host = os.environ.get("M4F_DB_HOST", "m4f-pg")
        db_name = os.environ.get("M4F_DB_NAME", "mergepilot_demo")
        trusted_env = {
            "test-runner": {
                "MERGEPILOT_TR_WORKSPACE": str(ROOT),
                "MERGEPILOT_TR_EXECUTOR": "process",
                "MERGEPILOT_TR_TRUSTED_DEV": "true",
                "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
                "MERGEPILOT_TR_ARTIFACT_ROOT": str(artifact_root),
            },
            "case-retrieval": {
                "MERGEPILOT_CR_PG_DSN": (
                    f"host={db_host} dbname={db_name} user=case_retrieval_reader"
                ),
                "MERGEPILOT_CR_REPO_SCOPE": REPO,
                "MERGEPILOT_CR_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
                "MERGEPILOT_CR_EMBEDDING_VERSION": "1.0.0",
                "MERGEPILOT_CR_DB_SCHEMA": "demo_cases",
                "MERGEPILOT_CR_DB_TABLE": "knowledge",
            },
            "pr-lifecycle": {
                "MERGEPILOT_PRL_GATEWAY_URL": "http://policy-fixture.invalid",
                "MERGEPILOT_PRL_ROLE": "fixer",
                "MERGEPILOT_PRL_TOKEN": "fixture-" + "a" * 40,
                "MERGEPILOT_PRL_REPO": REPO,
                "MERGEPILOT_PRL_BASE_BRANCH": "main",
                "MERGEPILOT_PRL_RUN_ID": RUN_ID,
                "MERGEPILOT_PRL_RISK_LEVEL": "L1",
                "MERGEPILOT_PRL_HMAC_KEY": "fixture-binding-" + "k" * 32,
                "MERGEPILOT_PRL_EXPECTED_BASE_SHA": BASE_SHA,
            },
        }
        worker = WORKER.SkillWorker(
            skill_conn,
            repo_root=ROOT,
            worker_id="demo-skill-worker",
            trusted_skill_env=trusted_env,
            skill_modules={
                "case-retrieval": "tests.m4f1.fixtures.case_retrieval_entry",
                "pr-lifecycle": "tests.m4f1.fixtures.pr_lifecycle_entry",
            },
            observer=observe,
        )
        handled = worker.drain(max_jobs=12)
        jobs, job_count = _collect_result(skill_conn, RUN_ID)
        all_jobs_succeeded = (
            job_count == len(SKILLS)
            and handled == len(SKILLS)
            and all(item["job_status"] == "SUCCEEDED" for item in jobs)
            and all(item["response_status"] == "OK" for item in jobs)
            and all(item["output_schema_validated"] is True for item in jobs)
        )
        test_passed = any(
            item["skill"] == "test-runner" and item["verdict"] == "PASS"
            for item in jobs
        )
        lifecycle_created = any(
            item["skill"] == "pr-lifecycle"
            and item["summary"].get("outcome") == "CREATED"
            for item in jobs
        )
        all_passed = all_jobs_succeeded and test_passed and lifecycle_created

        evidence = {
            "schema": "m4f-full-chain-e2e",
            "version": "1",
            "generated_at": _now(),
            "all_passed": all_passed,
            "fixture": {
                "ephemeral": True,
                "policy_gateway": "deterministic in-memory adapter",
                "case_retrieval": "real pgvector adapter + deterministic embedding",
                "external_credentials": False,
            },
            "runtime_versions": {
                "python": sys.version.split()[0],
                "psycopg2": _version("psycopg2-binary"),
                "jsonschema": _version("jsonschema"),
                "pytest": _version("pytest"),
            },
            "source_sha256": {
                "controller": _file_digest(ROOT / "tools/workflow-controller/controller.py"),
                "orchestration": _file_digest(
                    ROOT / "tools/workflow-controller/m4f_controller.py"
                ),
                "worker": _file_digest(ROOT / "tools/m4f_skill_worker.py"),
                "migration": _file_digest(ROOT / "tools/audit-db/m4f1_state.sql"),
            },
            "run": {
                "run_id": RUN_ID,
                "trace_id": TRACE_ID,
                "revision_binding_id": staged.revision_binding_id,
                "snapshot_id": staged.snapshot_id,
                "snapshot_job_id": staged.snapshot_job_id,
                "skill_job_ids": dict(staged.skill_job_ids),
            },
            "dag": {key: list(value) for key, value in SKILL_DAG.items()},
            "checks": {
                "handled_jobs": handled,
                "job_count": job_count,
                "all_jobs_succeeded": all_jobs_succeeded,
                "test_runner_passed": test_passed,
                "pr_lifecycle_created": lifecycle_created,
                "control_credentials_in_skill_env": False,
            },
            "jobs": jobs,
            "observations": observations,
        }
        output = pathlib.Path(args.evidence)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "event": "demo.completed",
                    "all_passed": all_passed,
                    "run_id": RUN_ID,
                    "snapshot_id": staged.snapshot_id,
                    "jobs": job_count,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if all_passed else 1
    finally:
        for conn in (skill_conn, snapshot_conn, controller_conn, admin_conn):
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
