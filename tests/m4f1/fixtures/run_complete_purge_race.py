#!/usr/bin/env python3
"""Real-PG ordering checks for complete_skill_job racing advance_purge."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import threading
import time
from typing import Any

import psycopg2


ROOT = pathlib.Path(__file__).resolve().parents[3]
CONTROLLER_DIR = ROOT / "tools/workflow-controller"
sys.path.insert(0, str(CONTROLLER_DIR))

from m4f_controller import SKILLS, stage_six_skill_run  # noqa: E402


REPO = "example/project"
BASE_SHA = "7" * 40
HEAD_SHA = "8" * 40


def _source_digest(conn: Any, request_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT encode(public.digest(
                 public._canon_str(request_id) || public._canon_str(correlation_id) ||
                 public._canon_str(tool) || public._canon_str(target_repo) ||
                 public._canon_str(run_id) || public._canon_str(git_sha) ||
                 public._canon_str(result_status), 'sha256'), 'hex')
               FROM public.mcp_calls WHERE request_id=%s""",
            (request_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row or not row[0]:
        raise RuntimeError("source evidence digest missing")
    return str(row[0])


def _seed_run(admin: Any, run_id: str, trace_id: str, pr_number: int) -> tuple[str, str]:
    call_id = f"{run_id}-base-read"
    branch = f"fix/{run_id}-demo"
    with admin.cursor() as cur:
        cur.execute(
            """INSERT INTO public.task_runs(
                   run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id)
               VALUES(%s,%s,%s,%s,%s,'RUNNING','m4f_snapshot',%s)""",
            (run_id, f"!race:{run_id}", REPO, pr_number, branch, trace_id),
        )
        cur.execute(
            """INSERT INTO public.run_pr_bindings(
                   binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
               VALUES(%s,%s,%s,%s,%s,'main',%s)""",
            (f"bnd-{run_id}", run_id, REPO, pr_number, branch, HEAD_SHA),
        )
        cur.execute(
            """INSERT INTO public.mcp_calls(
                   request_id,correlation_id,phase,ts,caller_agent,tool,decision,
                   run_id,target_repo,git_sha,result_status)
               VALUES(%s,%s,'RESULT',now(),'coordinator','github.get_commit','ALLOW',
                      %s,%s,%s,'OK')""",
            (call_id, f"corr-{run_id}", run_id, REPO, BASE_SHA),
        )
    admin.commit()
    return call_id, _source_digest(admin, call_id)


def _response_bytes(name: str, trace_id: str, request_id: str) -> bytes:
    value = {
        "name": name,
        "version": "1.0.0",
        "contract_version": "1",
        "request_id": request_id,
        "trace_id": trace_id,
        "status": "ERROR",
        "error_code": "INTERNAL_ERROR",
        "warning_codes": [],
        "degradations": [],
        "message": "deterministic completion/purge race fixture",
        "output": {},
        "evidence": [],
        "artifacts": [],
        "started_at": "2026-08-02T00:00:00Z",
        "duration_ms": 1,
        "retryable": False,
        "side_effects": [],
        "redactions": [],
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _prepare_run(
    admin: Any,
    controller: Any,
    snapshot: Any,
    skill: Any,
    run_id: str,
    trace_id: str,
    pr_number: int,
) -> dict[str, Any]:
    call_id, evidence_digest = _seed_run(admin, run_id, trace_id, pr_number)
    staged = stage_six_skill_run(
        controller,
        snapshot,
        run_id=run_id,
        trace_id=trace_id,
        repo=REPO,
        pr_number=pr_number,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        source_call_id=call_id,
        source_evidence_digest=evidence_digest,
        skill_inputs={name: {"race_fixture": run_id} for name in SKILLS},
        snapshot_worker_id="race-snapshot-worker",
    )
    job_id = str(staged.skill_job_ids["diff-parse"])
    with skill.cursor() as cur:
        cur.execute(
            "SELECT public.claim_skill_job(%s,%s,%s)",
            (job_id, "race-skill-worker", 120),
        )
        row = cur.fetchone()
        claim_id = row[0] if row else None
        cur.execute(
            """SELECT e.content_json->>'request_id', r.output_schema_digest
                 FROM public.skill_job_outbox AS j
                 JOIN public.envelope_store AS e
                   ON e.content_digest=j.request_envelope_ref
                 JOIN public.skill_version_registry AS r
                   ON r.skill_name=j.skill_name AND r.skill_version=j.skill_version
                WHERE j.job_id=%s""",
            (job_id,),
        )
        metadata = cur.fetchone()
    skill.commit()
    if claim_id is None or not metadata:
        raise RuntimeError(f"failed to prepare claimed job for {run_id}")
    return {
        "run_id": run_id,
        "job_id": job_id,
        "claim_id": claim_id,
        "response": _response_bytes("diff-parse", trace_id, str(metadata[0])),
        "schema_digest": str(metadata[1]),
    }


def _configure(conn: Any, app_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('application_name',%s,false)", (app_name,))
        cur.execute("SET statement_timeout='8s'")
        cur.execute("SET lock_timeout='7s'")
    conn.commit()


def _wait_for_lock(admin: Any, app_name: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with admin.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(bool_or(wait_event_type='Lock'),false)
                     FROM pg_stat_activity WHERE application_name=%s""",
                (app_name,),
            )
            waiting = bool(cur.fetchone()[0])
        admin.commit()
        if waiting:
            return True
        time.sleep(0.05)
    return False


def _error_payload(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "sqlstate": str(getattr(exc, "pgcode", "") or ""),
        "message": str(exc).strip()[:300],
    }


def _complete_wins(admin: Any, prepared: dict[str, Any], skill_dsn: str, purge_dsn: str) -> dict[str, Any]:
    complete_returned = threading.Event()
    purge_started = threading.Event()
    release_complete = threading.Event()
    result: dict[str, Any] = {"errors": []}

    def complete_side() -> None:
        conn = psycopg2.connect(skill_dsn)
        try:
            _configure(conn, "m4f-complete-wins")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public.complete_skill_job(%s,%s,%s,%s,%s)",
                    (
                        prepared["job_id"],
                        prepared["claim_id"],
                        psycopg2.Binary(prepared["response"]),
                        prepared["schema_digest"],
                        False,
                    ),
                )
                result["complete"] = cur.fetchone()[0]
            complete_returned.set()
            if not release_complete.wait(7):
                raise TimeoutError("complete commit release not signalled")
            conn.commit()
        except BaseException as exc:  # fixture must report thread failures
            conn.rollback()
            result["errors"].append(_error_payload(exc))
            complete_returned.set()
        finally:
            conn.close()

    def purge_side() -> None:
        if not complete_returned.wait(7):
            result["errors"].append({"type": "TimeoutError", "sqlstate": "", "message": "complete did not return"})
            return
        conn = psycopg2.connect(purge_dsn)
        try:
            _configure(conn, "m4f-purge-after-complete")
            purge_started.set()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public.request_purge(%s,%s)",
                    (prepared["run_id"], "race-complete-wins"),
                )
                purge_id = cur.fetchone()[0]
                cur.execute("SELECT public.advance_purge(%s)", (purge_id,))
                result["purge"] = cur.fetchone()[0]
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            result["errors"].append(_error_payload(exc))
        finally:
            conn.close()

    t_complete = threading.Thread(target=complete_side, name="complete-wins-complete", daemon=True)
    t_purge = threading.Thread(target=purge_side, name="complete-wins-purge", daemon=True)
    t_complete.start()
    if not complete_returned.wait(7):
        result["errors"].append({"type": "TimeoutError", "sqlstate": "", "message": "complete function timeout"})
    t_purge.start()
    purge_started.wait(7)
    result["purge_observed_waiting"] = _wait_for_lock(admin, "m4f-purge-after-complete")
    release_complete.set()
    t_complete.join(10)
    t_purge.join(10)
    result["threads_stopped"] = not t_complete.is_alive() and not t_purge.is_alive()
    return result


def _purge_wins(admin: Any, prepared: dict[str, Any], skill_dsn: str, purge_dsn: str) -> dict[str, Any]:
    setup = psycopg2.connect(purge_dsn)
    try:
        with setup.cursor() as cur:
            cur.execute(
                "SELECT public.request_purge(%s,%s)",
                (prepared["run_id"], "race-purge-wins"),
            )
            purge_id = cur.fetchone()[0]
        setup.commit()
    finally:
        setup.close()

    purge_returned = threading.Event()
    complete_started = threading.Event()
    release_purge = threading.Event()
    result: dict[str, Any] = {"errors": []}

    def purge_side() -> None:
        conn = psycopg2.connect(purge_dsn)
        try:
            _configure(conn, "m4f-purge-wins")
            with conn.cursor() as cur:
                cur.execute("SELECT public.advance_purge(%s)", (purge_id,))
                result["purge"] = cur.fetchone()[0]
            purge_returned.set()
            if not release_purge.wait(7):
                raise TimeoutError("purge commit release not signalled")
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            result["errors"].append(_error_payload(exc))
            purge_returned.set()
        finally:
            conn.close()

    def complete_side() -> None:
        if not purge_returned.wait(7):
            result["errors"].append({"type": "TimeoutError", "sqlstate": "", "message": "purge did not return"})
            return
        conn = psycopg2.connect(skill_dsn)
        try:
            _configure(conn, "m4f-complete-after-purge")
            complete_started.set()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public.complete_skill_job(%s,%s,%s,%s,%s)",
                    (
                        prepared["job_id"],
                        prepared["claim_id"],
                        psycopg2.Binary(prepared["response"]),
                        prepared["schema_digest"],
                        False,
                    ),
                )
                result["complete"] = cur.fetchone()[0]
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            result["errors"].append(_error_payload(exc))
        finally:
            conn.close()

    t_purge = threading.Thread(target=purge_side, name="purge-wins-purge", daemon=True)
    t_complete = threading.Thread(target=complete_side, name="purge-wins-complete", daemon=True)
    t_purge.start()
    if not purge_returned.wait(7):
        result["errors"].append({"type": "TimeoutError", "sqlstate": "", "message": "purge function timeout"})
    t_complete.start()
    complete_started.wait(7)
    result["complete_observed_waiting"] = _wait_for_lock(admin, "m4f-complete-after-purge")
    release_purge.set()
    t_purge.join(10)
    t_complete.join(10)
    result["threads_stopped"] = not t_complete.is_alive() and not t_purge.is_alive()
    return result


def _database_state(admin: Any, run_id: str) -> dict[str, Any]:
    with admin.cursor() as cur:
        cur.execute("SELECT skill_data_state FROM public.task_runs WHERE run_id=%s", (run_id,))
        state_row = cur.fetchone()
        cur.execute("SELECT count(*) FROM public.skill_job_outbox WHERE run_id=%s", (run_id,))
        jobs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.skill_invocations WHERE run_id=%s", (run_id,))
        invocations = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.run_snapshots WHERE run_id=%s", (run_id,))
        snapshots = cur.fetchone()[0]
    admin.commit()
    return {
        "skill_data_state": state_row[0] if state_row else None,
        "jobs": jobs,
        "invocations": invocations,
        "snapshots": snapshots,
    }


def main() -> int:
    admin_dsn = os.environ["M4F_ADMIN_DSN"]
    controller_dsn = os.environ["M4F_CONTROLLER_DSN"]
    snapshot_dsn = os.environ["M4F_SNAPSHOT_DSN"]
    skill_dsn = os.environ["M4F_SKILL_DSN"]
    purge_dsn = os.environ["M4F_PURGE_DSN"]

    admin = psycopg2.connect(admin_dsn)
    controller = psycopg2.connect(controller_dsn)
    snapshot = psycopg2.connect(snapshot_dsn)
    skill = psycopg2.connect(skill_dsn)
    try:
        first = _prepare_run(
            admin,
            controller,
            snapshot,
            skill,
            "race-complete-wins",
            "trace-race-complete-wins",
            81,
        )
        first_result = _complete_wins(admin, first, skill_dsn, purge_dsn)
        first_state = _database_state(admin, first["run_id"])

        second = _prepare_run(
            admin,
            controller,
            snapshot,
            skill,
            "race-purge-wins",
            "trace-race-purge-wins",
            82,
        )
        second_result = _purge_wins(admin, second, skill_dsn, purge_dsn)
        second_state = _database_state(admin, second["run_id"])

        no_deadlock = all(
            error.get("sqlstate") != "40P01"
            for result in (first_result, second_result)
            for error in result["errors"]
        )
        checks = {
            "complete_wins_returned_invocation": bool(first_result.get("complete")),
            "complete_wins_purged": first_result.get("purge") == "PURGED",
            "complete_wins_lock_observed": first_result.get("purge_observed_waiting") is True,
            "complete_wins_no_partial_rows": first_state
            == {"skill_data_state": "PURGED", "jobs": 0, "invocations": 0, "snapshots": 0},
            "purge_wins_completed": second_result.get("purge") == "PURGED",
            "purge_wins_complete_null": second_result.get("complete") is None,
            "purge_wins_lock_observed": second_result.get("complete_observed_waiting") is True,
            "purge_wins_no_partial_rows": second_state
            == {"skill_data_state": "PURGED", "jobs": 0, "invocations": 0, "snapshots": 0},
            "threads_stopped": first_result.get("threads_stopped") is True
            and second_result.get("threads_stopped") is True,
            "no_errors": not first_result["errors"] and not second_result["errors"],
            "no_deadlock": no_deadlock,
        }
        report = {
            "schema": "m4f-complete-purge-race",
            "version": "1",
            "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "passed": all(checks.values()),
            "checks": checks,
            "complete_wins": {"result": first_result, "database": first_state},
            "purge_wins": {"result": second_result, "database": second_state},
        }
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] else 1
    finally:
        skill.close()
        snapshot.close()
        controller.close()
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
