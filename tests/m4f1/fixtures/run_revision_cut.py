#!/usr/bin/env python3
"""Real-PG Controller revision-cut integration check."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys

import psycopg2


ROOT = pathlib.Path(__file__).resolve().parents[3]
CONTROLLER_DIR = ROOT / "tools/workflow-controller"
sys.path.insert(0, str(CONTROLLER_DIR))
import controller  # noqa: E402


RUN_ID = "revision-cut-source"
REPO = "example/project"
PR = 77
BASE = "5" * 40
OLD_HEAD = "4" * 40
NEW_HEAD = "6" * 40
CALL_ID = "revision-cut-base-read"


def _source_digest(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT encode(public.digest(
                 public._canon_str(request_id) || public._canon_str(correlation_id) ||
                 public._canon_str(tool) || public._canon_str(target_repo) ||
                 public._canon_str(run_id) || public._canon_str(git_sha) ||
                 public._canon_str(result_status), 'sha256'), 'hex')
               FROM public.mcp_calls WHERE request_id=%s""",
            (CALL_ID,),
        )
        return cur.fetchone()[0]


def main() -> int:
    admin = psycopg2.connect(os.environ["M4F_ADMIN_DSN"])
    runtime = psycopg2.connect(os.environ["M4F_CONTROLLER_DSN"])
    try:
        with admin.cursor() as cur:
            cur.execute(
                """INSERT INTO public.task_runs(
                     run_id,room_id,repo,pr_number,branch,status,current_stage,
                     approval_required,trace_id)
                   VALUES(%s,'!revision:fixture',%s,%s,'fix/revision-cut-source-demo',
                          'APPROVAL_PENDING','l2_binding',true,'trace-revision-cut')""",
                (RUN_ID, REPO, PR),
            )
            cur.execute(
                """INSERT INTO public.run_pr_bindings(
                     binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
                   VALUES('bnd-revision-cut',%s,%s,%s,
                          'fix/revision-cut-source-demo','main',%s)""",
                (RUN_ID, REPO, PR, OLD_HEAD),
            )
            cur.execute(
                """INSERT INTO public.mcp_calls(
                     request_id,correlation_id,phase,ts,caller_agent,tool,decision,
                     run_id,target_repo,git_sha,result_status)
                   VALUES(%s,'revision-cut-correlation','RESULT',now(),'coordinator',
                          'github.get_commit','ALLOW',%s,%s,%s,'OK')""",
                (CALL_ID, RUN_ID, REPO, BASE),
            )
        admin.commit()
        digest = _source_digest(admin)
        with runtime.cursor() as cur:
            cur.execute(
                "SELECT public.bind_revision(%s,%s,%s,%s,%s,%s,%s)",
                (RUN_ID, REPO, PR, OLD_HEAD, BASE, CALL_ID, digest),
            )
            binding_id = cur.fetchone()[0]
        runtime.commit()

        controller.ensure_pg = lambda: runtime
        result = controller._atomic_advance(
            RUN_ID,
            "FOUND",
            {},
            {
                "pr_num": PR,
                "head_sha": NEW_HEAD,
                "head_ref": "fix/revision-cut-source-demo",
                "base_ref": "main",
                "repo": REPO,
            },
        )
        child = controller._revision_cut_run_id(RUN_ID, REPO, PR, NEW_HEAD)

        with admin.cursor() as cur:
            cur.execute(
                "SELECT status,current_stage FROM public.task_runs WHERE run_id=%s",
                (RUN_ID,),
            )
            old_state = cur.fetchone()
            cur.execute(
                "SELECT status,current_stage FROM public.task_runs WHERE run_id=%s",
                (child,),
            )
            child_state = cur.fetchone()
            cur.execute(
                "SELECT head_sha FROM public.run_pr_bindings WHERE run_id=%s", (RUN_ID,)
            )
            old_head = cur.fetchone()[0]
            cur.execute(
                "SELECT head_sha FROM public.run_pr_bindings WHERE run_id=%s", (child,)
            )
            child_head_row = cur.fetchone()
            child_head = child_head_row[0] if child_head_row else None
            cur.execute(
                "SELECT count(*) FROM public.revision_bindings WHERE run_id IN (%s,%s)",
                (RUN_ID, child),
            )
            revision_rows = cur.fetchone()[0]
        admin.commit()

        checks = {
            "result": result[0] == "REVISION_CUT",
            "child_id": result[1].get("run_id") == child,
            "old_run_held": old_state == ("HOLD", "revision_superseded"),
            "child_ready": child_state == ("APPROVAL_PENDING", "l2_awaiting_ticket"),
            "old_head_immutable": old_head == OLD_HEAD,
            "new_head_on_child": child_head == NEW_HEAD,
            "only_old_revision_bound": revision_rows == 1,
        }
        passed = all(checks.values())
        print(
            json.dumps(
                {
                    "passed": passed,
                "source_run": RUN_ID,
                "child_run": child,
                "binding_id": binding_id,
                "advance_result": result,
                "checks": checks,
                },
                sort_keys=True,
            )
        )
        return 0 if passed else 1
    finally:
        runtime.close()
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
