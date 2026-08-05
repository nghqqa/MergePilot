#!/usr/bin/env python3
"""M5-0B dual-connection concurrency + negative-scenario runner (P1-1/2/3/4).

Spawns its own temp PG16 (via the harness), applies the migrations, then uses
TWO real psycopg2 connections to drive controller._m5_skill_to_review_one /
_m5_handoff_one directly. Verifies: no idle-in-transaction leaks, no deadlock,
exactly-one stage/dispatch under concurrency, the exact-six-job->invocation
binding negatives, room/status authoritative checks, and ON CONFLICT payload
conflict rollback.

Run inside the mergepilot-m4f-runtime container by _concurrency_inner.sh.
Env: M5B_PG_DSN (admin), M5B_CAND_DSN (mergepilot), M5B_PREFIX, M5B_ROOM.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import psycopg2

CTRL_DIR = "/workspace/tools/workflow-controller"
sys.path.insert(0, CTRL_DIR)

# controller reads env at import; set Candidate mode BEFORE import.
os.environ.setdefault("M4F_ONLY_MODE", "1")
os.environ.setdefault("M4F_LIVE_MODE", "1")
os.environ.setdefault("M4F_ENABLED", "1")
os.environ.setdefault("MATRIX_USER", "m5ctrl")
os.environ.setdefault("CONTROLLER_CONSUMER_NAME", "m5-conc-test")
os.environ.setdefault("M4F_ALLOWED_SENDERS", "manager,reviewer,fixer,verifier")
os.environ.setdefault("MATRIX_SERVER_NAME", "conc-hs")
os.environ.setdefault("SERVER", "conc-hs")
os.environ.setdefault("PG_HOST", os.environ.get("M5B_PG_HOST", "m5b-conc-pg"))
os.environ.setdefault("ADMIN_PW", "x")

import controller  # noqa: E402

PFX = os.environ.get("M5B_PREFIX", "m5con-")
ROOM = os.environ.get("M5B_ROOM", "!room:conc-hs")
ADMIN_DSN = os.environ["M5B_ADMIN_DSN"]
CAND_DSN = os.environ["M5B_CAND_DSN"]

PASS = 0
FAIL = 0


def gate(name, ok):
    global PASS, FAIL
    if ok:
        print("GATE PASS: %s" % name); PASS += 1
    else:
        print("GATE FAIL: %s" % name); FAIL += 1


def admin_exec(sql, args=None, fetch=False):
    c = psycopg2.connect(ADMIN_DSN)
    try:
        with c.cursor() as cur:
            cur.execute(sql, args or ())
            c.commit()
            if fetch:
                return cur.fetchall()
    except Exception as e:
        c.rollback()
        print("ADMIN_EXEC ERROR: %s | SQL head: %s | args: %s" % (e, sql[:120], str(args)[:200]))
        raise
    finally:
        c.close()
    return None


def _envelope(digest_prefix, payload_text="m5-envelope"):
    """Create a minimal envelope_store row (FK target) for a digest."""
    import hashlib
    body = (digest_prefix + ":" + payload_text).encode()
    digest = hashlib.sha256(body).hexdigest()
    admin_exec(
        "INSERT INTO envelope_store(content_digest,content_bytes,content_type,size_bytes) "
        "VALUES(%s,%s,'application/vnd.mergepilot.skill-request.v1+json',%s) "
        "ON CONFLICT (content_digest) DO NOTHING",
        (digest, body, len(body)))
    return digest


def seed_run(run_id, stage="m4f_snapshot", status="RUNNING", room=ROOM):
    admin_exec(
        "INSERT INTO task_runs(run_id,room_id,repo,pr_number,branch,status,current_stage,trace_id,skill_data_state) "
        "VALUES(%s,%s,'example/project',42,'fix/m','" + status + "','" + stage + "','t','ACTIVE') "
        "ON CONFLICT (run_id) DO NOTHING",
        (run_id, room))


def seed_six_skills(run_id, fail_skill=None, dup_skill=False,
                    no_result_inv=False, inv_not_validated=False):
    """Create the six skill_job_outbox rows + matching skill_invocations for a run.
    Flags inject the P1-2 negative scenarios. (extra_skill / cross_job /
    version_mismatch are DB-prevented by the skill_version_registry CHECK + FKs
    + the not_six_jobs guard, so they are not seeded here.)"""
    skills = list(controller._M5_EXPECTED_SKILLS)
    if dup_skill:
        skills.append("diff-parse")  # 7 rows, duplicate skill_name
    reg = {}
    for s in set(skills):
        row = admin_exec(
            "SELECT output_schema_digest FROM skill_version_registry WHERE skill_name=%s AND skill_version='1.0.0'",
            (s,), fetch=True)
        reg[s] = row[0][0] if row else ("0" * 64)
    for i, s in enumerate(skills):
        job_id = "job-%s-%d" % (run_id, i)
        inv_id = "inv-%s-%d" % (run_id, i)
        status = "SUCCEEDED"
        if fail_skill and s == fail_skill:
            status = "FAILED"
        req_env = _envelope("req-" + job_id)
        # 3-step to satisfy both circular FKs (INITIALLY IMMEDIATE):
        # 1) job with NULL result_invocation_id
        admin_exec(
            "INSERT INTO skill_job_outbox(job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,"
            "attempt,request_envelope_ref,idempotency_key,status,result_invocation_id) "
            "VALUES(%s,%s,NULL,'t',%s,'1.0.0',1,%s,%s,%s,NULL) ON CONFLICT (job_id) DO NOTHING",
            (job_id, run_id, s, req_env, "ik-" + job_id, status))
        # 2) invocation (now the job row exists for skill_invocations_run_job_fkey)
        if status == "SUCCEEDED" and not (no_result_inv and s == "diff-parse"):
            not_validated = (inv_not_validated and s == "diff-parse")
            validated = not not_validated
            inv_status = "ERROR" if not_validated else "OK"  # DB CHECK sinv_status_validated
            input_digest = _envelope("in-" + inv_id)
            admin_exec(
                "INSERT INTO skill_invocations(invocation_id,run_id,job_id,trace_id,skill_name,skill_version,"
                "attempt,request_id,contract_version,status,error_code,input_digest,expected_output_schema_digest,"
                "output_schema_validated,duration_ms,started_at,finished_at,idempotency_key) "
                "VALUES(%s,%s,%s,'t',%s,'1.0.0',1,%s,'1',%s,%s,%s,%s,%s,0,now(),now(),%s) "
                "ON CONFLICT (invocation_id) DO NOTHING",
                (inv_id, run_id, job_id, s, "req-" + job_id,
                 inv_status, "INTERNAL_ERROR" if not_validated else None,
                 input_digest, reg.get(s, "0" * 64), validated, "ik-" + inv_id))
            # 3) link job -> invocation
            admin_exec(
                "UPDATE skill_job_outbox SET result_invocation_id=%s WHERE job_id=%s",
                (inv_id, job_id))


def count_stage(run_id, stage="review"):
    r = admin_exec("SELECT count(*) FROM stage_runs WHERE run_id=%s AND stage=%s", (run_id, stage), fetch=True)
    return r[0][0] if r else 0


def count_dispatch(ikey):
    r = admin_exec("SELECT count(*) FROM dispatch_outbox WHERE idempotency_key=%s", (ikey,), fetch=True)
    return r[0][0] if r else 0


def run_stage(run_id):
    r = admin_exec("SELECT status,current_stage FROM task_runs WHERE run_id=%s", (run_id,), fetch=True)
    return r[0] if r else None


# ── P1-1: dual-connection concurrency on skill->review ──
def test_concurrency_skill_review():
    run_id = PFX + "conc1"
    seed_run(run_id)
    seed_six_skills(run_id)
    conn_a = psycopg2.connect(CAND_DSN)
    conn_b = psycopg2.connect(CAND_DSN)
    # run reconcile on both connections in parallel
    results = []
    def worker(conn, tag):
        try:
            r = controller._m5_skill_to_review_one(conn, run_id)
            results.append((tag, r, "ok"))
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            results.append((tag, False, type(e).__name__ + ":" + str(e)[:80]))
    t1 = threading.Thread(target=worker, args=(conn_a, "A"))
    t2 = threading.Thread(target=worker, args=(conn_b, "B"))
    t1.start(); t2.start(); t1.join(); t2.join()
    conn_a.close(); conn_b.close()
    bridges = sum(1 for _, r, _ in results if r)
    n_stage = count_stage(run_id, "review")
    n_disp = count_dispatch("m5-%s-review-dispatch" % run_id)
    stg = run_stage(run_id)
    # exactly one bridge, one stage, one dispatch, run advanced, no deadlock
    ok = (bridges == 1 and n_stage == 1 and n_disp == 1
          and stg is not None and stg[1] == "m4f_await_review"
          and all(s != "DeadlockDetected" for _, _, s in results))
    gate("P1-1 concurrent skill->review: one bridge/stage/dispatch, no deadlock", ok)
    # P1-1: after no-op, second connection can lock the same row (no idle-in-transaction leak)
    conn_c = psycopg2.connect(CAND_DSN)
    locked = False
    try:
        with conn_c.cursor() as cur:
            cur.execute("SET lock_timeout='3s'")
            cur.execute("SELECT 1 FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            locked = cur.fetchone() is not None
        conn_c.commit()
    finally:
        conn_c.close()
    gate("P1-1 no lock leak: second conn can lock task_runs after concurrent reconcile", locked)


# ── P1-1: no idle-in-transaction leak after a not-ready no-op ──
def test_no_idle_after_noop():
    run_id = PFX + "noop1"
    seed_run(run_id)
    # only 3 skills (not ready)
    for i, s in enumerate(controller._M5_EXPECTED_SKILLS[:3]):
        req_env = _envelope("nj-req-" + run_id + str(i))
        admin_exec(
            "INSERT INTO skill_job_outbox(job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,"
            "attempt,request_envelope_ref,idempotency_key,status) "
            "VALUES(%s,%s,NULL,'t',%s,'1.0.0',1,%s,%s,'PENDING') ON CONFLICT (job_id) DO NOTHING",
            ("nj-%s-%d" % (run_id, i), run_id, s, req_env, "ik-" + str(i)))
    conn = psycopg2.connect(CAND_DSN)
    try:
        controller._m5_skill_to_review_one(conn, run_id)  # no-op, not ready
    finally:
        conn.close()
    # check pg_stat_activity: no M5 connection idle in transaction
    time.sleep(0.5)
    rows = admin_exec(
        "SELECT count(*) FROM pg_stat_activity WHERE state='idle in transaction'",
        fetch=True)
    # (other test connections may briefly appear; close() ends the txn, so the
    #  candidate-role connection we just closed must NOT remain.)
    gate("P1-1 no idle-in-transaction leak after not-ready no-op", rows[0][0] == 0)


# ── P1-2: exact-six binding negatives ──
def test_binding_negative(name, **kwargs):
    run_id = PFX + "bneg_" + name
    seed_run(run_id)
    seed_six_skills(run_id, **kwargs)
    conn = psycopg2.connect(CAND_DSN)
    try:
        controller._m5_skill_to_review_one(conn, run_id)
    finally:
        conn.close()
    n_stage = count_stage(run_id, "review")
    n_disp = count_dispatch("m5-%s-review-dispatch" % run_id)
    stg = run_stage(run_id)
    advanced = stg is not None and stg[1] == "m4f_await_review"
    gate("P1-2 binding negative (%s): zero stage/dispatch, no advance" % name,
         n_stage == 0 and n_disp == 0 and not advanced)


# ── P1-3: room/status authoritative ──
def test_room_mismatch():
    run_id = PFX + "room1"
    seed_run(run_id, stage="m4f_await_review", room=ROOM)
    other_room = "!other:conc-hs"
    # seed a review stage + record a handoff event from a DIFFERENT room
    admin_exec("INSERT INTO stage_runs(run_id,stage,agent,attempt,status) "
               "VALUES(%s,'review','reviewer',1,'PENDING_DISPATCH') ON CONFLICT DO NOTHING", (run_id,))
    eid = "evt-room-" + run_id
    raw_body = "TASK_COMPLETED: %s-review" % run_id
    admin_exec(
        "INSERT INTO stage_events(event_id,room_id,run_id,sender,event_type,stage,raw_body,body_sha256,status) "
        "VALUES(%s,%s,%s,%s,'TASK_COMPLETED','review',%s,'h','RECEIVED') "
        "ON CONFLICT (event_id) DO NOTHING",
        (eid, other_room, run_id, "@reviewer:conc-hs", raw_body))
    conn = psycopg2.connect(CAND_DSN)
    try:
        controller._m5_handoff_one(conn, eid, other_room, "@reviewer:conc-hs", raw_body)
    finally:
        conn.close()
    # event must be ERROR; no fix stage / dispatch created
    r = admin_exec("SELECT status,error FROM stage_events WHERE event_id=%s", (eid,), fetch=True)
    n_fix = count_stage(run_id, "fix")
    n_disp = count_dispatch("m5-%s-fix-dispatch" % run_id)
    gate("P1-3 room mismatch -> ERROR, no advance",
         r and r[0][0] == "ERROR" and "room" in (r[0][1] or "").lower() and n_fix == 0 and n_disp == 0)


def test_hold_run_no_resume():
    run_id = PFX + "hold1"
    seed_run(run_id, stage="m5_verify_passed", status="HOLD")
    eid = "evt-hold-" + run_id
    raw_body = "TASK_COMPLETED: %s-review" % run_id
    admin_exec(
        "INSERT INTO stage_events(event_id,room_id,run_id,sender,event_type,stage,raw_body,body_sha256,status) "
        "VALUES(%s,%s,%s,%s,'TASK_COMPLETED','review',%s,'h','RECEIVED') "
        "ON CONFLICT (event_id) DO NOTHING",
        (eid, ROOM, run_id, "@reviewer:conc-hs", raw_body))
    conn = psycopg2.connect(CAND_DSN)
    try:
        controller._m5_handoff_one(conn, eid, ROOM, "@reviewer:conc-hs", raw_body)
    finally:
        conn.close()
    # HOLD run must NOT resume: status stays HOLD, no new fix stage/dispatch
    stg = run_stage(run_id)
    n_fix = count_stage(run_id, "fix")
    n_disp = count_dispatch("m5-%s-fix-dispatch" % run_id)
    gate("P1-3 HOLD run not resumed, no new dispatch",
         stg is not None and stg[0] == "HOLD" and stg[1] == "m5_verify_passed"
         and n_fix == 0 and n_disp == 0)


# ── P1-4: ON CONFLICT payload conflict ──
def test_dispatch_conflict():
    run_id = PFX + "conf1"
    seed_run(run_id)
    seed_six_skills(run_id)
    # pre-create the review dispatch_outbox row with a DIFFERENT room (conflict)
    admin_exec(
        "INSERT INTO dispatch_outbox(idempotency_key,run_id,room_id,target_agent,target_stage,attempt,body,status) "
        "VALUES(%s,%s,'!different:hs','reviewer','review',1,'OTHER','PENDING') ON CONFLICT DO NOTHING",
        ("m5-%s-review-dispatch" % run_id, run_id))
    conn = psycopg2.connect(CAND_DSN)
    conflict_raised = False
    try:
        controller._m5_skill_to_review_one(conn, run_id)
    except controller._M5PayloadConflict:
        conflict_raised = True
    finally:
        try: conn.rollback()
        except Exception: pass
        conn.close()
    stg = run_stage(run_id)
    # task_runs must NOT advance; existing dispatch row untouched (still '!different:hs')
    r = admin_exec("SELECT room_id FROM dispatch_outbox WHERE idempotency_key=%s",
                   ("m5-%s-review-dispatch" % run_id,), fetch=True)
    gate("P1-4 dispatch payload conflict -> rollback, no advance, existing row untouched",
         conflict_raised and stg is not None and stg[1] != "m4f_await_review"
         and r and r[0][0] == "!different:hs")


def test_stage_run_conflict():
    # stage_run conflict is reachable on the ADVANCE path (the review->fix
    # bridge idempotency check fires before _insert_stage_run_checked for the
    # review stage, so we test the fix stage via a review handoff advance).
    run_id = PFX + "conf2"
    seed_run(run_id, stage="m4f_await_review")
    admin_exec(
        "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) "
        "VALUES(%s,'review','reviewer',1,'PENDING_DISPATCH') ON CONFLICT DO NOTHING", (run_id,))
    # pre-create the FIX stage_run with a DIFFERENT agent (conflict target)
    admin_exec(
        "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) "
        "VALUES(%s,'fix','wrongagent',1,'PENDING_DISPATCH') ON CONFLICT DO NOTHING", (run_id,))
    eid = "evt-srconf-" + run_id
    raw_body = "TASK_COMPLETED: %s-review" % run_id
    admin_exec(
        "INSERT INTO stage_events(event_id,room_id,run_id,sender,event_type,stage,raw_body,body_sha256,status) "
        "VALUES(%s,%s,%s,%s,'TASK_COMPLETED','review',%s,'h','RECEIVED') ON CONFLICT DO NOTHING",
        (eid, ROOM, run_id, "@reviewer:conc-hs", raw_body))
    conn = psycopg2.connect(CAND_DSN)
    conflict_raised = False
    try:
        controller._m5_handoff_one(conn, eid, ROOM, "@reviewer:conc-hs", raw_body)
    except controller._M5PayloadConflict:
        conflict_raised = True
    finally:
        try: conn.rollback()
        except Exception: pass
        conn.close()
    stg = run_stage(run_id)
    r = admin_exec("SELECT agent FROM stage_runs WHERE run_id=%s AND stage='fix'", (run_id,), fetch=True)
    gate("P1-4 stage_run payload conflict -> rollback, no advance, existing agent untouched",
         conflict_raised and stg is not None and stg[1] == "m4f_await_review"
         and r and r[0][0] == "wrongagent")


def main():
    # set controller globals for Candidate mode (env import already did most)
    controller.M4F_ONLY_MODE = True
    controller.M4F_RUN_PREFIX = PFX
    controller.M4F_ALLOWED_ROOMS = [ROOM]
    # clean any prior test rows
    admin_exec("DELETE FROM stage_events WHERE run_id LIKE %s", (PFX + "%",))
    admin_exec("DELETE FROM dispatch_outbox WHERE run_id LIKE %s", (Pfx := PFX + "%",))
    admin_exec("DELETE FROM stage_runs WHERE run_id LIKE %s", (PFX + "%",))
    admin_exec("DELETE FROM skill_invocations WHERE run_id LIKE %s", (PFX + "%",))
    admin_exec("DELETE FROM skill_job_outbox WHERE run_id LIKE %s", (PFX + "%",))
    admin_exec("DELETE FROM task_runs WHERE run_id LIKE %s", (PFX + "%",))

    test_concurrency_skill_review()
    test_no_idle_after_noop()
    test_binding_negative("seven_rows_dup", dup_skill=True)
    test_binding_negative("no_result_inv", no_result_inv=True)
    test_binding_negative("inv_not_validated", inv_not_validated=True)
    test_binding_negative("skill_failed", fail_skill="diff-parse")  # -> HOLD, not advance
    test_room_mismatch()
    test_hold_run_no_resume()
    test_dispatch_conflict()
    test_stage_run_conflict()

    # final: no M5 candidate connections idle in transaction
    time.sleep(0.5)
    rows = admin_exec(
        "SELECT count(*) FROM pg_stat_activity WHERE state=%s "
        "AND application_name LIKE %s",
        ("idle in transaction", "%m5%"), fetch=True)
    gate("P1-1 final: no M5 idle-in-transaction connections", rows[0][0] == 0)

    print("=== SUMMARY: PASS=%d FAIL=%d ===" % (PASS, FAIL))
    # hold_run_no_resume: the skill_failed binding case should HOLD the run
    sf = run_stage(PFX + "bneg_skill_failed")
    gate("P1-2 skill_failed -> HOLD/m4f_skill_failed",
         sf is not None and sf[0] == "HOLD" and sf[1] == "m4f_skill_failed")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
