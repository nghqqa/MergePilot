#!/usr/bin/env python3
"""Protocol-real Matrix ingress + Policy Gateway + six-Skill E2E driver."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any

import psycopg2


ROOT = pathlib.Path("/workspace")
TOOLS_DIR = ROOT / "tools"
CONTROLLER_DIR = TOOLS_DIR / "workflow-controller"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(CONTROLLER_DIR))

import controller  # noqa: E402
import m4f_demo  # noqa: E402
import m4f_skill_worker  # noqa: E402


RUN_ID = "run-agentteams-1"
TRACE_ID = "trace-agentteams-0001"
REPO = "example/project"
PR_NUMBER = 42
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _event_payload() -> dict[str, Any]:
    safe_source = (
        "def load_user(cur, user_id):\n"
        "    cur.execute('SELECT name FROM users WHERE id = %s', (user_id,))\n"
        "    return cur.fetchone()\n"
    )
    return {
        "contract_version": "1",
        "run_id": RUN_ID,
        "trace_id": TRACE_ID,
        "repo": REPO,
        "pr_number": PR_NUMBER,
        "risk_floor": "L1",
        "case_query": "parameterized SQL injection remediation",
        "test_runner": {
            "runner_key": "pytest",
            "test_paths": ["tests/m4f1/fixtures/demo_workspace/test_demo.py"],
            "timeout_ms": 30000,
            "expected_profiles_version": "1.0.0",
        },
        "pr_lifecycle": {
            "action": "ensure_fix_pr",
            "idempotency_key": "m4f.agentteams.fix.1",
            "changes": [{"path": "src/user_service.py", "content": safe_source}],
            "commit_message": "fix: parameterize user lookup",
            "pr_title": "fix: parameterize user lookup",
            "pr_body": "M4-F AgentTeams protocol E2E",
        },
    }


def _query_evidence(conn: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT j.skill_name,j.job_id,j.status,j.attempts,
                      i.invocation_id,i.status,i.error_code,i.verdict,
                      i.output_schema_validated,e.content_json->'output'
               FROM public.skill_job_outbox AS j
               LEFT JOIN public.skill_invocations AS i
                 ON i.invocation_id=j.result_invocation_id
               LEFT JOIN public.envelope_store AS e
                 ON e.content_digest=i.output_digest
               WHERE j.run_id=%s ORDER BY j.created_at,j.skill_name""",
            (RUN_ID,),
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT status,run_id,error FROM public.stage_events
               WHERE event_id='evt-agentteams-1'"""
        )
        event = cur.fetchone()
        cur.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE phase='RESULT' AND result_status='OK'),
                      count(*) FILTER (WHERE run_id=%s AND git_sha=%s)
               FROM public.mcp_calls""",
            (RUN_ID, BASE_SHA),
        )
        audit = cur.fetchone()
        cur.execute(
            """SELECT rb.base_sha,rb.head_sha,rs.snapshot_id,
                      (SELECT count(*) FROM public.snapshot_manifest_items smi
                       WHERE smi.snapshot_id=rs.snapshot_id)
               FROM public.revision_bindings rb
               JOIN public.run_snapshots rs ON rs.run_id=rb.run_id
               WHERE rb.run_id=%s""",
            (RUN_ID,),
        )
        revision = cur.fetchone()
    conn.commit()
    jobs = []
    for row in rows:
        output = row[9] if isinstance(row[9], dict) else {}
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
                "summary": {
                    key: output.get(key)
                    for key in ("risk_level", "verdict", "outcome", "complete")
                    if key in output
                },
            }
        )
    return jobs, {
        "stage_event": {
            "status": event[0] if event else None,
            "run_id": event[1] if event else None,
            "error": event[2] if event else None,
        },
        "gateway_audit": {
            "events": int(audit[0]),
            "successful_results": int(audit[1]),
            "bound_revision_results": int(audit[2]),
        },
        "revision": {
            "base_sha": revision[0] if revision else None,
            "head_sha": revision[1] if revision else None,
            "snapshot_id": revision[2] if revision else None,
            "manifest_items": int(revision[3]) if revision else 0,
        },
    }


def main() -> int:
    evidence_path = pathlib.Path(os.environ["M4F_EVIDENCE_PATH"])
    controller_conn = psycopg2.connect(os.environ["M4F_CONTROLLER_DSN"])
    admin_conn = psycopg2.connect(os.environ["M4F_ADMIN_DSN"])
    skill_conn = psycopg2.connect(os.environ["M4F_SKILL_DSN"])
    observations: list[dict[str, Any]] = []
    try:
        m4f_demo._seed_case_fixture(admin_conn)
        body = "M4F_RUN: " + json.dumps(
            _event_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        controller.process_event(
            "evt-agentteams-1", "!agentteams:fixture",
            "@admin:" + controller.SERVER, "admin",
            body, int(time.time() * 1000)
        )
        staged = controller.drain_m4f_events(max_items=1)

        artifact_root = pathlib.Path("/tmp/m4f-agentteams-artifacts")
        artifact_root.mkdir(parents=True, exist_ok=True)
        trusted_env = {
            "test-runner": {
                "MERGEPILOT_TR_WORKSPACE": str(ROOT),
                "MERGEPILOT_TR_EXECUTOR": "process",
                "MERGEPILOT_TR_TRUSTED_DEV": "true",
                "MERGEPILOT_TR_NETWORK_POLICY": "allowed",
                "MERGEPILOT_TR_ARTIFACT_ROOT": str(artifact_root),
            },
            "case-retrieval": {
                "MERGEPILOT_CR_PG_DSN": os.environ["M4F_CASE_DSN"],
                "MERGEPILOT_CR_REPO_SCOPE": REPO,
                "MERGEPILOT_CR_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
                "MERGEPILOT_CR_EMBEDDING_VERSION": "1.0.0",
                "MERGEPILOT_CR_DB_SCHEMA": "demo_cases",
                "MERGEPILOT_CR_DB_TABLE": "knowledge",
            },
            "pr-lifecycle": {
                "MERGEPILOT_PRL_GATEWAY_URL": os.environ["M4F_GATEWAY_URL"],
                "MERGEPILOT_PRL_ROLE": "fixer",
                "MERGEPILOT_PRL_TOKEN": os.environ["M4F_FIXER_TOKEN"],
                "MERGEPILOT_PRL_REPO": REPO,
                "MERGEPILOT_PRL_BASE_BRANCH": "main",
                "MERGEPILOT_PRL_RUN_ID": RUN_ID,
                "MERGEPILOT_PRL_RISK_LEVEL": "L1",
                "MERGEPILOT_PRL_HMAC_KEY": os.environ["M4F_PRL_HMAC_KEY"],
                "MERGEPILOT_PRL_EXPECTED_BASE_SHA": BASE_SHA,
            },
        }
        worker = m4f_skill_worker.SkillWorker(
            skill_conn,
            repo_root=ROOT,
            worker_id="agentteams-skill-worker",
            trusted_skill_env=trusted_env,
            skill_modules={
                "case-retrieval": "tests.m4f1.fixtures.case_retrieval_entry"
            },
            observer=observations.append,
        )
        handled = worker.drain(max_jobs=12)
        jobs, details = _query_evidence(controller_conn)
        all_jobs = len(jobs) == 6 and all(
            item["job_status"] == "SUCCEEDED" for item in jobs
        )
        test_pass = any(
            item["skill"] == "test-runner" and item["verdict"] == "PASS"
            for item in jobs
        )
        pr_created = any(
            item["skill"] == "pr-lifecycle"
            and item["summary"].get("outcome") in {"CREATED", "EXISTING"}
            for item in jobs
        )
        checks = {
            "matrix_event_queued_and_processed": (
                staged == 1
                and details["stage_event"]["status"] == "PROCESSED"
                and details["stage_event"]["run_id"] == RUN_ID
            ),
            "policy_gateway_revision_provenance": (
                details["gateway_audit"]["bound_revision_results"] >= 1
                and details["revision"]["base_sha"] == BASE_SHA
                and details["revision"]["head_sha"] == HEAD_SHA
            ),
            "snapshot_manifest_complete": details["revision"]["manifest_items"] == 6,
            "six_jobs_handled": handled == 6 and all_jobs,
            "test_runner_passed": test_pass,
            "pr_lifecycle_via_gateway": pr_created,
        }
        evidence = {
            "schema": "m4f-agentteams-gateway-e2e",
            "version": "1",
            "all_passed": all(checks.values()),
            "fixture": {
                "matrix_entry": "controller.process_event M4F_RUN",
                "policy_gateway": "real MCP SSE gateway",
                "github_upstream": "stateful protocol fixture",
                "case_retrieval": "real pgvector adapter + deterministic embedding",
                "external_credentials": False,
            },
            "checks": checks,
            "details": details,
            "jobs": jobs,
            "observations": observations,
            "source_sha256": {
                "controller": _digest(ROOT / "tools/workflow-controller/controller.py"),
                "ingress": _digest(ROOT / "tools/workflow-controller/m4f_ingress.py"),
                "orchestration": _digest(ROOT / "tools/workflow-controller/m4f_controller.py"),
                "gateway": _digest(ROOT / "tools/policy-gateway/gateway.py"),
                "worker": _digest(ROOT / "tools/m4f_skill_worker.py"),
                "migration": _digest(ROOT / "tools/audit-db/m4f1_state.sql"),
            },
            "residue": {"containers": None, "networks": None, "temp_dirs": None},
        }
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        return 0 if evidence["all_passed"] else 1
    finally:
        controller.reset_pg()
        controller.reset_m4f_snapshot_pg()
        for conn in (controller_conn, admin_conn, skill_conn):
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
