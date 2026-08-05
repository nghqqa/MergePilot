#!/usr/bin/env python3
"""M5-0B integration helper: mark the six expected Skills for a run as
SUCCEEDED + create matching schema-validated skill_invocations.

The six-Skill *execution* is M4-F's domain (the real SkillWorker). M5-0B's
surface is the DAG->review/fix/verify bridge, so this helper fabricates only
the terminal skill state the bridge gates on, while the handoffs themselves
flow through the real Matrix /sync against the mini homeserver.

Usage: inject_skill_completion.py <dsn> <run_id>

Idempotent: safe to call repeatedly (ON CONFLICT DO NOTHING on the
skill_invocations idempotency_key +PK).
"""
from __future__ import annotations

import sys

import psycopg2

EXPECTED = (
    "diff-parse", "risk-classify", "sast-scan",
    "test-runner", "case-retrieval", "pr-lifecycle",
)


def main(dsn: str, run_id: str) -> int:
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, skill_name, skill_version, trace_id, "
            "request_envelope_ref, attempt FROM skill_job_outbox WHERE run_id=%s",
            (run_id,))
        rows = cur.fetchall()
        if len(rows) != 6:
            print("inject: expected 6 skill_job_outbox rows for %s, got %d" % (run_id, len(rows)))
            return 1
        names = {r[1] for r in rows}
        if names != set(EXPECTED):
            print("inject: skill set mismatch for %s: %s" % (run_id, names))
            return 1
        # expected_output_schema_digest must match skill_version_registry (FK
        # skill_invocations_registry_fkey on skill_name,skill_version,output_schema_digest).
        # Read the registered digest per skill so the FK is satisfied.
        for job_id, skill_name, skill_version, trace_id, req_env, attempt in rows:
            cur.execute(
                "SELECT output_schema_digest FROM skill_version_registry "
                "WHERE skill_name=%s AND skill_version=%s",
                (skill_name, skill_version))
            reg = cur.fetchone()
            if not reg:
                print("inject: no registry entry for %s/%s" % (skill_name, skill_version))
                return 1
            schema_digest = reg[0]
            invocation_id = "m5inv-%s-%s" % (run_id, skill_name)
            cur.execute(
                """UPDATE skill_job_outbox
                   SET status='SUCCEEDED', completed_at=now(), result_invocation_id=%s,
                       error=NULL
                   WHERE job_id=%s AND status IN ('PENDING','LEASED','SUCCEEDED')""",
                (invocation_id, job_id))
            cur.execute(
                """INSERT INTO skill_invocations(
                       invocation_id, run_id, job_id, trace_id, skill_name, skill_version,
                       attempt, request_id, contract_version, status,
                       input_digest, expected_output_schema_digest, output_schema_validated,
                       duration_ms, started_at, finished_at, idempotency_key)
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s, '1', 'OK',
                          %s, %s, true, 0, now(), now(), %s)
                   ON CONFLICT (invocation_id) DO NOTHING""",
                (invocation_id, run_id, job_id, trace_id, skill_name, skill_version,
                 attempt, job_id, req_env, schema_digest,
                 "m5ikey-%s-%s" % (run_id, skill_name)))
        # sanity: exactly six SUCCEEDED + six validated invocations
        cur.execute(
            "SELECT count(*) FROM skill_job_outbox WHERE run_id=%s AND status='SUCCEEDED'",
            (run_id,))
        succ = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM skill_invocations WHERE run_id=%s AND output_schema_validated=true",
            (run_id,))
        val = cur.fetchone()[0]
    conn.commit()
    conn.close()
    print("inject: %s skill_job_outbox SUCCEEDED=%d skill_invocations validated=%d"
          % (run_id, succ, val))
    return 0 if (succ == 6 and val == 6) else 2


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: inject_skill_completion.py <dsn> <run_id>")
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
