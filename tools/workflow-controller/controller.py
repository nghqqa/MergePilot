#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workflow_controller.py — M3-A 确定性 Controller(PG 权威 + Outbox + /sync)。

核心原则:
  - PG 是唯一事实来源;内存不做权威去重。
  - 状态转换 + outbox 写入同一个 PG 事务;Matrix 派发在事务后异步进行。
  - Matrix 事件通过 event_id 持久化到 stage_events,重启不丢。
  - /sync 游标持久化到 controller_offsets,不依赖 baseline 屏蔽。

在 hiclab-net 独立容器运行。环境变量:
  ADMIN_PW, PG_PASS, PG_HOST(默认 audit-pg), PG_PORT(5432),
  PG_DATABASE(mergepilot_audit), PG_USER(mergepilot), MATRIX_HS(hiclaw-controller:6167)
"""
import os, sys, json, time, re, hashlib, uuid, psycopg2, urllib.request, urllib.error

import m4f_ingress

# ── 配置 ──
ADMIN   = "admin"
SERVER  = os.environ.get("MATRIX_SERVER_NAME", "matrix-local.hiclaw.io:18080")
MATRIX_HS = os.environ.get("MATRIX_HS", "http://hiclaw-controller:6167")
PG_HOST = os.environ.get("PG_HOST", "audit-pg")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB   = os.environ.get("PG_DATABASE", "mergepilot_audit")
PG_USER = os.environ.get("PG_USER", "mergepilot")
PG_PASS = os.environ.get("PG_PASS", "")
ADMIN_PW = os.environ.get("ADMIN_PW", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "8"))
SYNC_TIMEOUT  = int(os.environ.get("SYNC_TIMEOUT", "30000"))

# ── B4c L2 审批流配置(复审:gating 默认关;Coordinator 持 token 调 Gateway,不持 PAT) ──
# L2_MERGE_ENABLED 语义(B4c-0.1 #3):**只决定新 run 的 approval_required 默认值**(TASK_SUBMITTED 时写)。
#   L2 维护循环(initiate/drain/reconcile)始终运行——函数内部按 approval_required=TRUE 过滤,
#   故已持久化 approval_required=TRUE 的 run 在开关关闭/重启后仍被维护,不卡死。
#   非法值(不在 {0,1,true,false,yes,no,on,off})→ startup_assert_l2 启动失败,不静默当 false。
_L2_RAW = os.environ.get("L2_MERGE_ENABLED", "0").strip().lower()
_L2_TRUE = {"1", "true", "yes", "on"}
_L2_FALSE = {"0", "false", "no", "off"}

_M4F_RAW = os.environ.get("M4F_ENABLED", "0").strip().lower()
M4F_ENABLED = _M4F_RAW in _L2_TRUE
M4F_ENABLED_INVALID = _M4F_RAW not in _L2_TRUE and _M4F_RAW not in _L2_FALSE
M4F_SNAPSHOT_DSN = os.environ.get("M4F_SNAPSHOT_DSN", "").strip()
M4F_EVENT_LEASE_SECONDS = int(os.environ.get("M4F_EVENT_LEASE_SECONDS", "120"))
M4F_EVENT_MAX_ATTEMPTS = int(os.environ.get("M4F_EVENT_MAX_ATTEMPTS", "5"))
L2_MERGE_ENABLED = _L2_RAW in _L2_TRUE
L2_MERGE_ENABLED_INVALID = _L2_RAW not in _L2_TRUE and _L2_RAW not in _L2_FALSE

# ── M5-0 Candidate Controller configuration (design freeze v2.3 §8) ──
# Backward-compatible defaults: M4F_ONLY_MODE=0 keeps existing behavior.
_M5_LIVE_RAW = os.environ.get("M4F_LIVE_MODE", "0").strip().lower()
M4F_LIVE_MODE = _M5_LIVE_RAW in _L2_TRUE
_M5_ONLY_RAW = os.environ.get("M4F_ONLY_MODE", "0").strip().lower()
M4F_ONLY_MODE = _M5_ONLY_RAW in _L2_TRUE
# MATRIX_USER: backward-compat default "admin"; Candidate must set non-admin.
MATRIX_USER = os.environ.get("MATRIX_USER", "admin")
# CONTROLLER_CONSUMER_NAME: backward-compat default "controller"; Candidate must set different.
CONTROLLER_CONSUMER_NAME = os.environ.get("CONTROLLER_CONSUMER_NAME", "controller")
# Room/sender allowlists: comma-separated; empty = reject all M4F_RUN from /sync.
M4F_ALLOWED_ROOMS = [r.strip() for r in os.environ.get("M4F_ALLOWED_ROOMS", "").split(",") if r.strip()]
M4F_ALLOWED_SENDERS = [s.strip() for s in os.environ.get("M4F_ALLOWED_SENDERS", "").split(",") if s.strip()]
# Run prefix for Candidate scoping (e.g. "m5live-"). Empty = no prefix check (legacy compat).
M4F_RUN_PREFIX = os.environ.get("M4F_RUN_PREFIX", "").strip()
# Reserved prefixes for Production drain_outbox exclusion (comma-separated).
RESERVED_RUN_PREFIXES = [p.strip() for p in os.environ.get("RESERVED_RUN_PREFIXES", "").split(",") if p.strip()]
# Advisory lock key for Candidate singleton.
_M5_LOCK_LABEL = "mergepilot:m5-0-candidate"
_m5_lock_conn = None  # session-level advisory lock connection (independent of _pg)
# Strict parser regexes (M5-0 §7.2)
import re as _m5_re
_M5_RUN_MARKER = "M4F_RUN:"
_M5_RE_HANDOFF = _m5_re.compile(r"^TASK_COMPLETED: ([A-Za-z0-9._:-]+)-(review|fix|verify)$")
_M5_RE_VERDICT_LINE = _m5_re.compile(r"(?mi)^\s*VERDICT\s*=\s*(PASS|FAIL|BLOCKED)\s*$")
_M5_RUN_ID_CHARSET = _m5_re.compile(r"^[A-Za-z0-9._:-]+$")
_M5_SQL_WILDCARD = _m5_re.compile(r"[%_]")
# v2.4 Fix 3: prefix must be plain [A-Za-z0-9.-]+ (no underscore=SQL wildcard,
# no %, no shell metachars, no path separators).
_M5_PREFIX_CHARSET = _m5_re.compile(r"^[A-Za-z0-9.-]+$")


def _m5_prefix_overlap(a, b):
    """True if a/b are in a parent-child relationship (one is a prefix of the other)."""
    return a != b and (b.startswith(a) or a.startswith(b))


def _validate_m5_candidate():
    """M5-0A startup assert for Candidate mode. Called only when M4F_ONLY_MODE=1.
    Validates all Candidate-required config. Fails before Matrix login."""
    if not M4F_ONLY_MODE:
        return  # not Candidate mode; skip
    errors = []
    if not M4F_ENABLED:
        errors.append("M4F_ENABLED must be 1 in Candidate mode")
    if not M4F_LIVE_MODE:
        errors.append("M4F_LIVE_MODE must be 1 in Candidate mode")
    if not MATRIX_USER or MATRIX_USER == "admin":
        errors.append("MATRIX_USER must be explicitly set and not 'admin' in Candidate mode")
    if not CONTROLLER_CONSUMER_NAME or CONTROLLER_CONSUMER_NAME == "controller":
        errors.append("CONTROLLER_CONSUMER_NAME must be explicitly set and not 'controller' in Candidate mode")
    if not M4F_ALLOWED_ROOMS:
        errors.append("M4F_ALLOWED_ROOMS must be non-empty in Candidate mode")
    if not M4F_ALLOWED_SENDERS:
        errors.append("M4F_ALLOWED_SENDERS must be non-empty in Candidate mode")
    if not M4F_RUN_PREFIX:
        errors.append("M4F_RUN_PREFIX must be non-empty in Candidate mode")
    if _M5_SQL_WILDCARD.search(M4F_RUN_PREFIX):
        errors.append("M4F_RUN_PREFIX must not contain SQL wildcards (% or _)")
    # v2.4 Fix 3: strict charset (reject shell metachars, path separators, etc.)
    if M4F_RUN_PREFIX and not _M5_PREFIX_CHARSET.match(M4F_RUN_PREFIX):
        errors.append("M4F_RUN_PREFIX must match [A-Za-z0-9.-]+ only")
    # Candidate must NOT have its own prefix in RESERVED (would self-exclude)
    if M4F_RUN_PREFIX in RESERVED_RUN_PREFIXES:
        errors.append("M4F_RUN_PREFIX must not appear in RESERVED_RUN_PREFIXES (self-exclusion)")
    for pfx in RESERVED_RUN_PREFIXES:
        if _M5_SQL_WILDCARD.search(pfx):
            errors.append(f"RESERVED_RUN_PREFIXES must not contain SQL wildcards: {pfx}")
        if not _M5_PREFIX_CHARSET.match(pfx):
            errors.append(f"RESERVED_RUN_PREFIXES must match [A-Za-z0-9.-]+ only: {pfx}")
    # v2.4 Fix 3: reject parent-child prefix overlap (M4F_RUN_PREFIX vs RESERVED)
    for rp in RESERVED_RUN_PREFIXES:
        if _m5_prefix_overlap(M4F_RUN_PREFIX, rp):
            errors.append(
                f"M4F_RUN_PREFIX '{M4F_RUN_PREFIX}' parent-child overlaps RESERVED '{rp}'")
    # v2.4 Fix 3: reject parent-child overlap WITHIN RESERVED_RUN_PREFIXES
    for i, a in enumerate(RESERVED_RUN_PREFIXES):
        for b in RESERVED_RUN_PREFIXES[i + 1:]:
            if _m5_prefix_overlap(a, b):
                errors.append(
                    f"RESERVED_RUN_PREFIXES parent-child overlap: '{a}' vs '{b}'")
    if errors:
        for e in errors:
            print(f"[ctrl][M5-0] FATAL: {e}")
        raise ValueError(f"M5-0 Candidate config invalid: {len(errors)} errors")
    # Cutover preflight: verify production Controller has matching RESERVED_RUN_PREFIXES
    # (read-only check; does NOT output full env or credentials)
    try:
        _pf_conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASS)
        with _pf_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_tables WHERE tablename='controller_offsets'")
            if cur.fetchone():
                # Check production controller exists and has a different consumer_name
                cur.execute(
                    "SELECT consumer_name FROM controller_offsets WHERE consumer_name='controller'")
                prod_exists = cur.fetchone()
                if prod_exists and M4F_RUN_PREFIX:
                    print(f"[ctrl][M5-0] cutover: production controller detected; "
                          f"Candidate prefix={M4F_RUN_PREFIX}")
                    # NOTE: production RESERVED_RUN_PREFIXES cannot be read from here;
                    # the start_candidate_controller.sh preflight script verifies it
                    # via docker inspect (read-only, env name only, no value output).
        _pf_conn.close()
    except Exception:
        pass  # PG probe is best-effort; advisory lock will catch concurrency issues


def acquire_m5_lock():
    """Acquire session-level advisory lock for Candidate singleton.
    Uses independent PG connection. Returns True on success, False if locked."""
    global _m5_lock_conn
    if _m5_lock_conn and not _m5_lock_conn.closed:
        return True  # already held
    _m5_lock_conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASS,
        application_name=f"{CONTROLLER_CONSUMER_NAME}-m5-lock")
    with _m5_lock_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (_M5_LOCK_LABEL,))
        acquired = cur.fetchone()[0]
    if not acquired:
        _m5_lock_conn.close()
        _m5_lock_conn = None
    return acquired


def release_m5_lock():
    """Release advisory lock and close the lock connection."""
    global _m5_lock_conn
    if _m5_lock_conn and not _m5_lock_conn.closed:
        try:
            with _m5_lock_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_M5_LOCK_LABEL,))
                cur.close()
            _m5_lock_conn.close()
        except Exception:
            pass  # PG session disconnect auto-releases
    _m5_lock_conn = None


def check_m5_lock_health():
    """Check advisory lock connection is still alive. Candidate exits if not."""
    global _m5_lock_conn
    if M4F_ONLY_MODE and (_m5_lock_conn is None or _m5_lock_conn.closed):
        return False
    if M4F_ONLY_MODE:
        try:
            with _m5_lock_conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except Exception:
            return False
    return True


_M5_RE_FULL_SENDER = re.compile(
    r"^@([A-Za-z0-9._=/+\-]+):(" + re.escape(SERVER) + r")$")


def verify_m5_sender(raw_sender, allowed_localparts):
    """Verify full Matrix user_id with server_name via strict fullmatch.
    Returns localpart or None.
    Rejects: missing @, multiple @, empty localpart, wrong homeserver, not in allowlist."""
    if not raw_sender or not isinstance(raw_sender, str):
        return None
    m = _M5_RE_FULL_SENDER.match(raw_sender)
    if not m:
        return None
    localpart = m.group(1)
    server = m.group(2)
    if not localpart or server != SERVER:
        return None
    if localpart not in allowed_localparts:
        return None
    return localpart


def m5_parse_m4f_run(body):
    """Strict M4F_RUN parser (§7.2). Returns parsed payload dict or None.
    Only accepts: M4F_RUN: {valid JSON} with no trailing prose.
    JSON must pass m4f_ingress.validate_event schema check."""
    if not body.startswith(_M5_RUN_MARKER):
        return None
    json_part = body[len(_M5_RUN_MARKER):].strip()
    if not json_part.startswith("{"):
        return None
    if _M5_RUN_MARKER in json_part:
        return None
    try:
        payload = json.loads(json_part)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("run_id", "")
    if not isinstance(run_id, str) or not run_id.startswith(M4F_RUN_PREFIX):
        return None
    if not _M5_RUN_ID_CHARSET.match(run_id[len(M4F_RUN_PREFIX):]):
        return None
    # Freeze contract: validate via m4f_ingress schema (not just json.loads)
    try:
        m4f_ingress.validate_event(payload)
    except m4f_ingress.M4FIngressError:
        return None
    return payload


def m5_parse_handoff(body, stage):
    """Strict TASK_COMPLETED handoff parser (§7.2).
    For review/fix: body must be exactly 'TASK_COMPLETED: <run_id>-<stage>'.
    Returns run_id or None."""
    if not body:
        return None
    m = _M5_RE_HANDOFF.match(body)
    if not m or m.group(2) != stage:
        return None
    run_id = m.group(1)
    if not run_id.startswith(M4F_RUN_PREFIX):
        return None
    return run_id


def m5_parse_verify(body):
    """Strict verify handoff parser (§7.2 freeze contract).
    Accepts:
      1 line:  TASK_COMPLETED: <run_id>-verify            → (run_id, None) PARTIAL
      2 lines: TASK_COMPLETED: <run_id>-verify\\nVERDICT=X  → (run_id, "X")
    Rejects: prose, code blocks, >2 lines, >1 VERDICT, missing TASK_COMPLETED.
    Returns (run_id, verdict|None) or ("REJECT", reason) or None."""
    if not body:
        return None
    lines = body.strip().splitlines()
    if len(lines) not in (1, 2):
        return None  # only 1 or 2 lines allowed
    # Line 1 must be TASK_COMPLETED: <run_id>-verify (full line match)
    m1 = _M5_RE_HANDOFF.match(lines[0].strip())
    if not m1 or m1.group(2) != "verify":
        return None
    run_id = m1.group(1)
    if not run_id.startswith(M4F_RUN_PREFIX):
        return None
    if len(lines) == 1:
        return (run_id, None)  # waiting for VERDICT (PARTIAL)
    # Line 2 must be exactly VERDICT=PASS|FAIL|BLOCKED
    vm = re.match(r"^VERDICT=(PASS|FAIL|BLOCKED)$", lines[1].strip())
    if not vm:
        return ("REJECT", f"invalid VERDICT line: {lines[1][:50]}")
    return (run_id, vm.group(1))


def _drain_outbox_sql_partition():
    """Build parameterized SQL WHERE clause for run_id prefix partitioning.
    Returns (sql_fragment, params_list)."""
    params = []
    clauses = ["status IN ('PENDING', 'RETRY')", "next_retry_at <= now()"]
    if M4F_ONLY_MODE and M4F_RUN_PREFIX:
        clauses.append("run_id LIKE %s")
        params.append(M4F_RUN_PREFIX + "%")
    for pfx in RESERVED_RUN_PREFIXES:
        clauses.append("run_id NOT LIKE %s")
        params.append(pfx + "%")
    return " AND ".join(clauses), params


def _drain_m4f_claim_sql_prefix():
    """Build additional WHERE clause for M4F claim prefix scoping.
    Returns (sql_fragment, params_list). ``process_event`` persists the parsed
    run ID before setting M4F_PENDING, so this column is the authoritative
    partition key for claims.
    """
    params = []
    clause = ""
    if M4F_RUN_PREFIX:
        clause = " AND run_id LIKE %s"
        params.append(M4F_RUN_PREFIX + "%")
    return clause, params


# ── M5-0B: DAG→review/fix/verify handoff closed loop (design freeze v2.5 §13/§18) ──
# Only meaningful when M4F_ONLY_MODE (Candidate). Production Controller (M4F_ONLY_MODE=0)
# never calls reconcile_* (guarded at top of each function + at the Candidate loop).
_M5_EXPECTED_SKILLS = (
    "diff-parse", "risk-classify", "sast-scan",
    "test-runner", "case-retrieval", "pr-lifecycle",
)
# Pre-bridge current_stage values (skills dispatched, awaiting completion).
_M5_PRE_BRIDGE_STAGES = ("m4f", "m4f_snapshot")
_M5_RECONCILE_LIMIT = int(os.environ.get("M5_RECONCILE_LIMIT", "50"))
# dispatch body templates — instruct the agent AND state the exact completion
# marker so the worker emits a strict-parseable handoff.
_M5_DISPATCH_TPL = {
    "review": "[M5-0B] run {run_id}: six Skills SUCCEEDED. 请审查变更, findings 写 shared/tasks/{run_id}-review/。完成时精确写一行(无代码块/无解释): TASK_COMPLETED: {run_id}-review",
    "fix": "[M5-0B] run {run_id}-review 完成。请据 findings 提修复。完成时精确写一行(无代码块/无解释): TASK_COMPLETED: {run_id}-fix",
    "verify": "[M5-0B] run {run_id}-fix 完成。请复核修复并出裁定。完成时精确写两行(无代码块/无解释):\nTASK_COMPLETED: {run_id}-verify\nVERDICT=PASS|FAIL|BLOCKED",
}
# Allowed sender localpart per handoff stage (§5 causal roles).
_M5_STAGE_SENDER = {"review": "reviewer", "fix": "fixer", "verify": "verifier"}
# task_runs.current_stage that an incoming handoff stage expects the run to be in.
_M5_AWAIT_FOR_STAGE = {"review": "m4f_await_review", "fix": "m4f_await_fix", "verify": "m4f_await_verify"}


class _M5PayloadConflict(Exception):
    """Raised when an idempotent upsert finds a conflicting payload (same key,
    different fields). Triggers a full transaction rollback so task_runs is NOT
    advanced and the existing row is NOT overwritten (P1-4)."""


def _m5_classify_handoff(body):
    """Strict-classify a TASK_COMPLETED handoff body via the M5-0A parsers (§7.2).
    Returns one of:
      (run_id, "review", None)
      (run_id, "fix", None)
      (run_id, "verify", None)          # 1-line verify, PARTIAL (no verdict yet)
      (run_id, "verify", "PASS"|"FAIL"|"BLOCKED")  # 2-line verify with verdict
      ("REJECT", "verify", reason)      # verify body with an invalid VERDICT line
      (None, None, None)                # not a strict handoff body at all
    """
    if not body:
        return (None, None, None)
    for stg in ("review", "fix"):
        rid = m5_parse_handoff(body, stg)
        if rid is not None:
            return (rid, stg, None)
    res = m5_parse_verify(body)
    if res is None:
        return (None, None, None)
    if isinstance(res, tuple) and len(res) == 2:
        if res[0] == "REJECT":
            return ("REJECT", "verify", res[1])
        return (res[0], "verify", res[1])
    return (None, None, None)


def _m5_mark_event(cur, event_id, status, error=None):
    """Mark a stage_events row status/error (used by reconcile on success/error)."""
    if error:
        cur.execute(
            "UPDATE stage_events SET status=%s, error=%s, processed_at=now() WHERE event_id=%s",
            (status, error[:500], event_id))
    else:
        cur.execute(
            "UPDATE stage_events SET status=%s, processed_at=now() WHERE event_id=%s",
            (status, event_id))


def _m5_record_handoff(event_id, room_id, raw_sender, body):
    """Record one TASK_COMPLETED Matrix event into stage_events using the STRICT
    parser (never legacy substring). RECEIVED + run_id when the strict parse
    succeeds and the run_id carries the Candidate prefix; ERROR (fail-closed)
    otherwise. Does NOT advance stages — reconcile_m5_handoffs is the sole
    advancement authority (§14)."""
    run_id, stage, verdict = _m5_classify_handoff(body)
    conn = ensure_pg()
    body_sha = hashlib.sha256(body.encode()).hexdigest()[:16]
    truncated = body[:2000]
    with conn.cursor() as ec:
        if run_id is None:
            ec.execute(
                """INSERT INTO stage_events(event_id, room_id, sender, event_type, raw_body, body_sha256, status, error)
                   VALUES(%s, %s, %s, 'TASK_COMPLETED', %s, %s, 'ERROR', 'M5-0B strict parse failed')
                   ON CONFLICT (event_id) DO NOTHING""",
                (event_id, room_id, raw_sender, truncated, body_sha))
            conn.commit()
            print(f"[ctrl][M5-0B] {event_id} strict handoff parse failed -> ERROR")
            return
        if run_id == "REJECT":
            ec.execute(
                """INSERT INTO stage_events(event_id, room_id, sender, event_type, stage, raw_body, body_sha256, status, error)
                   VALUES(%s, %s, %s, 'TASK_COMPLETED', 'verify', %s, %s, 'ERROR', %s)
                   ON CONFLICT (event_id) DO NOTHING""",
                (event_id, room_id, raw_sender, truncated, body_sha, str(verdict)[:200]))
            conn.commit()
            print(f"[ctrl][M5-0B] {event_id} verify VERDICT rejected -> ERROR")
            return
        if not run_id.startswith(M4F_RUN_PREFIX):
            ec.execute(
                """INSERT INTO stage_events(event_id, room_id, run_id, sender, event_type, stage, raw_body, body_sha256, status, error)
                   VALUES(%s, %s, %s, %s, 'TASK_COMPLETED', %s, %s, %s, 'ERROR', 'M5-0B run_id prefix mismatch')
                   ON CONFLICT (event_id) DO NOTHING""",
                (event_id, room_id, run_id, raw_sender, stage, truncated, body_sha))
            conn.commit()
            print(f"[ctrl][M5-0B] {event_id} run_id {run_id} not in prefix {M4F_RUN_PREFIX} -> ERROR")
            return
        ec.execute(
            """INSERT INTO stage_events(event_id, room_id, run_id, sender, event_type, stage, raw_body, body_sha256, status)
               VALUES(%s, %s, %s, %s, 'TASK_COMPLETED', %s, %s, %s, 'RECEIVED')
               ON CONFLICT (event_id) DO NOTHING""",
            (event_id, room_id, run_id, raw_sender, stage, truncated, body_sha))
    conn.commit()


def _m5_verify_six_skill_binding(cur, run_id):
    """Verify the EXACT six expected Skills for a run are all SUCCEEDED and each
    job is bound to a matching schema-validated skill_invocation (P1-2). No
    broad run-wide count(*) — the join binds each job to its own invocation.
    Returns (ok, reason). reason=='skill_failed' triggers HOLD; every other
    non-ok reason is a no-op wait (not ready)."""
    cur.execute(
        """SELECT j.skill_name, j.status, j.result_invocation_id, j.job_id, j.skill_version,
                  i.invocation_id, i.run_id AS i_run, i.job_id AS i_job,
                  i.skill_name AS i_skill, i.skill_version AS i_ver, i.output_schema_validated
           FROM skill_job_outbox j
           LEFT JOIN skill_invocations i ON i.invocation_id = j.result_invocation_id
           WHERE j.run_id = %s""",
        (run_id,))
    rows = cur.fetchall()
    if len(rows) != 6:
        return (False, "not_six_jobs")  # raw row count must be exactly 6 (missing/extra/duplicate)
    seen = set()
    for (skill_name, status, result_inv, job_id, skill_version,
         inv_id, i_run, i_job, i_skill, i_ver, i_validated) in rows:
        if skill_name in seen:
            return (False, "duplicate_skill")  # duplicate skill_name (extra version/attempt)
        seen.add(skill_name)
        if status == "FAILED":
            return (False, "skill_failed")
        if status != "SUCCEEDED":
            return (False, "not_succeeded")
        if not result_inv:
            return (False, "no_result_invocation")
        if inv_id is None:
            return (False, "invocation_missing")
        if inv_id != result_inv or i_run != run_id or i_job != job_id:
            return (False, "invocation_binding")
        if i_skill != skill_name or i_ver != skill_version:
            return (False, "invocation_skill_version")
        if not i_validated:
            return (False, "invocation_not_validated")
    if seen != set(_M5_EXPECTED_SKILLS):
        return (False, "skill_set_mismatch")
    return (True, "ok")


def _m5_insert_stage_run_checked(cur, run_id, stage, agent, attempt):
    """Idempotent stage_run upsert + payload reconciliation (P1-4). INSERT ON
    CONFLICT DO NOTHING, then re-read the authoritative row and compare
    run_id/stage/agent/attempt + a live (pre-terminal) status. Raises
    _M5PayloadConflict on any mismatch (caller rolls back)."""
    cur.execute(
        """INSERT INTO stage_runs(run_id, stage, agent, attempt, status, started_at)
           VALUES(%s, %s, %s, %s, 'PENDING_DISPATCH', now())
           ON CONFLICT (run_id, stage, attempt) DO NOTHING""",
        (run_id, stage, agent, attempt))
    cur.execute(
        "SELECT run_id, stage, agent, attempt, status FROM stage_runs "
        "WHERE run_id=%s AND stage=%s AND attempt=%s",
        (run_id, stage, attempt))
    row = cur.fetchone()
    if row is None:
        raise _M5PayloadConflict("stage_run %s/%s/%s vanished after upsert" % (run_id, stage, attempt))
    r_run, r_stage, r_agent, r_attempt, r_status = row
    if (r_run, r_stage, r_agent, r_attempt) != (run_id, stage, agent, attempt):
        raise _M5PayloadConflict(
            "stage_run %s/%s payload mismatch: got %r expected %r"
            % (run_id, stage, row, (run_id, stage, agent, attempt)))
    if r_status not in ("PENDING_DISPATCH", "DISPATCHED", "RUNNING"):
        raise _M5PayloadConflict("stage_run %s/%s status %r not live" % (run_id, stage, r_status))


def _m5_insert_dispatch_checked(cur, ikey, run_id, room_id, target_agent,
                                target_stage, attempt, body):
    """Idempotent dispatch_outbox upsert + payload reconciliation (P1-4). Re-reads
    the authoritative row by idempotency_key and compares ALL payload fields."""
    cur.execute(
        """INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
           VALUES(%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        (ikey, run_id, room_id, target_agent, target_stage, attempt, body))
    cur.execute(
        "SELECT idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body "
        "FROM dispatch_outbox WHERE idempotency_key=%s",
        (ikey,))
    row = cur.fetchone()
    if row is None:
        raise _M5PayloadConflict("dispatch %s vanished after upsert" % ikey)
    if tuple(row) != (ikey, run_id, room_id, target_agent, target_stage, attempt, body):
        raise _M5PayloadConflict(
            "dispatch %s payload mismatch: got %r expected %r"
            % (ikey, row, (ikey, run_id, room_id, target_agent, target_stage, attempt, body)))


def reconcile_m5_skill_to_review(run_prefix=None, limit=None):
    """M5-0B §13: for each scoped Candidate run whose six expected Skills are all
    SUCCEEDED, atomically + idempotently create the review stage + reviewer
    dispatch and advance current_stage to m4f_await_review. Any terminal Skill
    failure -> HOLD (m4f_skill_failed), no dispatch. Bounded + stable-sorted;
    no full-table scan. Production (M4F_ONLY_MODE=0) is a no-op."""
    if not M4F_ONLY_MODE:
        return 0
    pfx = run_prefix if run_prefix is not None else M4F_RUN_PREFIX
    if not pfx:
        return 0
    lim = limit if limit is not None else _M5_RECONCILE_LIMIT
    conn = ensure_pg()
    # Candidate read in its own short transaction, closed immediately so the
    # connection is never left idle-in-transaction between reconcile calls (P1-1).
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT run_id FROM task_runs
                   WHERE run_id LIKE %s AND status='RUNNING'
                     AND current_stage IN ('m4f','m4f_snapshot')
                   ORDER BY run_id LIMIT %s""",
                (pfx + "%", lim))
            candidates = [r[0] for r in cur.fetchall()]
    finally:
        conn.rollback()
    bridged = 0
    for run_id in candidates:
        try:
            if _m5_skill_to_review_one(conn, run_id):
                bridged += 1
        except _M5PayloadConflict as e:
            print(f"[ctrl][M5-0B] skill_to_review {run_id} payload conflict: {e}")
        except Exception as e:
            print(f"[ctrl][M5-0B] skill_to_review {run_id}: {type(e).__name__}: {e}")
    return bridged


def _m5_skill_to_review_one(conn, run_id):
    """Per-run skill->review bridge. Locks task_runs first (§13 lock order),
    checks the exact-six-job->invocation binding (P1-2), then payload-reconciled
    upserts (P1-4) for review stage + reviewer dispatch + advances stage. Every
    exit path ends the transaction (P1-1): commit on any write, rollback for a
    pure read no-op (releases the FOR UPDATE lock)."""
    wrote = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_stage, status, room_id FROM task_runs WHERE run_id=%s FOR UPDATE",
                (run_id,))
            row = cur.fetchone()
            if not row:
                return False
            current_stage, status, room_id = row
            if status != "RUNNING" or current_stage not in _M5_PRE_BRIDGE_STAGES:
                return False  # already bridged / held / not pre-bridge
            # Idempotency: a review stage_run already exists -> already bridged
            cur.execute(
                "SELECT 1 FROM stage_runs WHERE run_id=%s AND stage='review' LIMIT 1",
                (run_id,))
            if cur.fetchone():
                return False
            # Exact-six-job -> invocation binding (P1-2)
            ok, reason = _m5_verify_six_skill_binding(cur, run_id)
            if not ok:
                if reason == "skill_failed":
                    cur.execute(
                        "UPDATE task_runs SET status='HOLD', current_stage='m4f_skill_failed', updated_at=now() "
                        "WHERE run_id=%s AND status='RUNNING'",
                        (run_id,))
                    wrote = True
                    print(f"[ctrl][M5-0B] {run_id} terminal Skill FAILED -> HOLD (m4f_skill_failed)")
                # every other reason = not ready (pending/missing/extra/binding) -> no-op wait
                return False
            # ---- bridge (payload-reconciled upserts, P1-4) ----
            _m5_insert_stage_run_checked(cur, run_id, "review", "reviewer", 1)
            _m5_insert_dispatch_checked(
                cur, "m5-%s-review-dispatch" % run_id, run_id, room_id,
                "reviewer", "review", 1, _M5_DISPATCH_TPL["review"].format(run_id=run_id))
            cur.execute(
                "UPDATE task_runs SET current_stage='m4f_await_review', updated_at=now() WHERE run_id=%s",
                (run_id,))
            wrote = True
            print(f"[ctrl][M5-0B] {run_id} 6/6 Skills SUCCEEDED -> review PENDING_DISPATCH (m4f_await_review)")
            return True
    finally:
        if wrote:
            conn.commit()
        else:
            conn.rollback()


def reconcile_m5_handoffs(run_prefix=None, limit=None):
    """M5-0B: consume RECEIVED TASK_COMPLETED handoffs (scoped by run prefix) and
    advance review->fix->verify using the STRICT parser. Sole advancement
    authority for M5 handoffs (§14). Bounded + stable-sorted. Production no-op."""
    if not M4F_ONLY_MODE:
        return 0
    pfx = run_prefix if run_prefix is not None else M4F_RUN_PREFIX
    if not pfx:
        return 0
    lim = limit if limit is not None else _M5_RECONCILE_LIMIT
    conn = ensure_pg()
    # Candidate read in its own short transaction, closed immediately (P1-1).
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT event_id, room_id, sender, raw_body FROM stage_events
                   WHERE event_type='TASK_COMPLETED' AND status='RECEIVED'
                     AND run_id LIKE %s
                   ORDER BY received_at, event_id LIMIT %s""",
                (pfx + "%", lim))
            events = cur.fetchall()
    finally:
        conn.rollback()
    processed = 0
    for event_id, room_id, raw_sender, body in events:
        try:
            if _m5_handoff_one(conn, event_id, room_id, raw_sender, body):
                processed += 1
        except _M5PayloadConflict as e:
            print(f"[ctrl][M5-0B] handoff {event_id} payload conflict: {e}")
        except Exception as e:
            print(f"[ctrl][M5-0B] handoff {event_id}: {type(e).__name__}: {e}")
    return processed


def _m5_handoff_one(conn, event_id, room_id, raw_sender, body):
    """Process one RECEIVED handoff (P1-1 lock order: task_runs -> stage_events;
    P1-3 room/status authoritative; P1-4 payload-reconciled advance). Every exit
    path ends the transaction (commit on write, rollback for pure read no-op)."""
    # Unlocked metadata lookup to learn run_id (P1-1: no lock taken yet). The
    # formal transaction below re-reads + revalidates under the correct lock order.
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id FROM stage_events WHERE event_id=%s", (event_id,))
            meta = cur.fetchone()
    finally:
        conn.rollback()  # close the read-only snapshot immediately
    if not meta or meta[0] is None:
        return False
    run_id = meta[0]

    wrote = False
    try:
        with conn.cursor() as cur:
            # Lock task_runs FIRST (§13 global lock order: task_runs -> children)
            cur.execute(
                "SELECT current_stage, status, room_id FROM task_runs WHERE run_id=%s FOR UPDATE",
                (run_id,))
            trow = cur.fetchone()
            if not trow:
                cur.execute(
                    "SELECT run_id FROM stage_events WHERE event_id=%s FOR UPDATE", (event_id,))
                if cur.fetchone():
                    _m5_mark_event(cur, event_id, "ERROR", "M5-0B unknown run_id")
                    wrote = True
                return False
            t_stage, t_status, t_room = trow
            # NOW lock the event row (task_runs already locked -> correct order)
            cur.execute(
                "SELECT run_id, stage, sender, status FROM stage_events WHERE event_id=%s FOR UPDATE",
                (event_id,))
            ev = cur.fetchone()
            if not ev:
                return False
            rec_run_id, rec_stage, rec_sender, rec_status = ev
            if rec_status != "RECEIVED":
                return False  # another worker already finalized this event
            # Strict re-parse + bind to the locked event row
            cls_run, stage, verdict = _m5_classify_handoff(body)
            if cls_run is None or cls_run == "REJECT":
                _m5_mark_event(cur, event_id, "ERROR", "M5-0B strict re-parse failed")
                wrote = True
                return False
            if cls_run != rec_run_id or cls_run != run_id or stage != rec_stage:
                _m5_mark_event(cur, event_id, "ERROR", "M5-0B run_id/stage drift")
                wrote = True
                return False
            localpart = verify_m5_sender(rec_sender, set(M4F_ALLOWED_SENDERS))
            if localpart is None or localpart != _M5_STAGE_SENDER.get(stage):
                _m5_mark_event(cur, event_id, "ERROR",
                               "M5-0B sender mismatch (%s)" % rec_sender)
                wrote = True
                return False
            # P1-3: room authoritative — event room must equal the task_run room
            if room_id != t_room:
                _m5_mark_event(cur, event_id, "ERROR",
                               "M5-0B room mismatch event=%s task=%s" % (room_id, t_room))
                wrote = True
                return False
            if M4F_ALLOWED_ROOMS and room_id not in M4F_ALLOWED_ROOMS:
                _m5_mark_event(cur, event_id, "ERROR", "M5-0B room not allowlisted")
                wrote = True
                return False
            # P1-3: advance requires the matching await stage; HOLD/past runs are
            # idempotent (no resume, no new dispatch) — frozen design §18.
            expected_await = _M5_AWAIT_FOR_STAGE.get(stage)
            if t_stage != expected_await:
                # already past this handoff (replay / out-of-order / finalized run)
                _m5_mark_event(cur, event_id, "PROCESSED",
                               "run at %s/%s (no dispatch)" % (t_status, t_stage))
                wrote = True
                return True
            if t_status != "RUNNING":
                # in the await stage but not RUNNING (e.g. HOLD) -> fail-closed
                _m5_mark_event(cur, event_id, "ERROR",
                               "M5-0B run not RUNNING (status=%s)" % t_status)
                wrote = True
                return False
            # ---- advance (payload-reconciled, P1-4; unified helpers) ----
            if stage == "review":
                _m5_advance_to_next(cur, event_id, run_id, t_room,
                                    from_stage="review", next_stage="fix", next_agent="fixer",
                                    next_dispatch="fix", next_await="m4f_await_fix")
            elif stage == "fix":
                _m5_advance_to_next(cur, event_id, run_id, t_room,
                                    from_stage="fix", next_stage="verify", next_agent="verifier",
                                    next_dispatch="verify", next_await="m4f_await_verify")
            else:
                _m5_advance_verify(cur, event_id, run_id, verdict)
            wrote = True
            return True
    finally:
        if wrote:
            conn.commit()
        else:
            conn.rollback()


def _m5_advance_to_next(cur, event_id, run_id, room_id,
                        from_stage, next_stage, next_agent, next_dispatch, next_await):
    """Complete the from_stage (review/fix) and create the next stage_run +
    dispatch via the unified payload-reconciled helpers (P1-4). Caller has
    already verified t_stage == await_for(from_stage) and t_status == RUNNING.
    No commit — the caller's transaction boundary owns that."""
    cur.execute(
        """SELECT id FROM stage_runs
           WHERE run_id=%s AND stage=%s AND status IN ('PENDING_DISPATCH','DISPATCHED','RUNNING')
           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""",
        (run_id, from_stage))
    current = cur.fetchone()
    if not current:
        _m5_mark_event(cur, event_id, "ERROR", "M5-0B no active %s stage_run" % from_stage)
        return
    cur.execute(
        "UPDATE stage_runs SET status='COMPLETED', completed_at=now() WHERE id=%s",
        (current[0],))
    _m5_insert_stage_run_checked(cur, run_id, next_stage, next_agent, 1)
    _m5_insert_dispatch_checked(
        cur, "m5-%s-%s-dispatch" % (run_id, next_dispatch), run_id, room_id,
        next_agent, next_stage, 1, _M5_DISPATCH_TPL[next_dispatch].format(run_id=run_id))
    cur.execute(
        "UPDATE task_runs SET current_stage=%s, updated_at=now() WHERE run_id=%s",
        (next_await, run_id))
    _m5_mark_event(cur, event_id, "PROCESSED", None)
    print(f"[ctrl][M5-0B] {run_id}-{from_stage} COMPLETED -> {next_stage} PENDING_DISPATCH ({next_await})")


def _m5_advance_verify(cur, event_id, run_id, verdict):
    """Advance verify: PARTIAL (no verdict) waits; PASS -> HOLD/m5_verify_passed;
    FAIL/BLOCKED -> HOLD/m5_verify_failed (no further dispatch). Caller owns the
    transaction boundary."""
    if verdict is None:
        # 1-line verify: partial snapshot, waiting for explicit VERDICT (§7.2)
        _m5_mark_event(cur, event_id, "PARTIAL", "waiting for explicit VERDICT")
        print(f"[ctrl][M5-0B] {run_id}-verify partial snapshot; waiting for VERDICT")
        return
    cur.execute(
        """SELECT id FROM stage_runs
           WHERE run_id=%s AND stage='verify' AND status IN ('PENDING_DISPATCH','DISPATCHED','RUNNING')
           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""",
        (run_id,))
    current = cur.fetchone()
    if not current:
        _m5_mark_event(cur, event_id, "ERROR", "M5-0B no active verify stage_run")
        return
    cur.execute(
        "UPDATE stage_runs SET status='COMPLETED', completed_at=now(), verdict=%s WHERE id=%s",
        (verdict, current[0]))
    if verdict == "PASS":
        cur.execute(
            "UPDATE task_runs SET status='HOLD', current_stage='m5_verify_passed', verdict='PASS', updated_at=now() WHERE run_id=%s",
            (run_id,))
        print(f"[ctrl][M5-0B] {run_id}-verify VERDICT=PASS -> HOLD (m5_verify_passed)")
    else:
        cur.execute(
            "UPDATE task_runs SET status='HOLD', current_stage='m5_verify_failed', verdict=%s, updated_at=now() WHERE run_id=%s",
            (verdict, run_id))
        print(f"[ctrl][M5-0B] {run_id}-verify VERDICT={verdict} -> HOLD (m5_verify_failed)")
    _m5_mark_event(cur, event_id, "PROCESSED", None)


GATEWAY_URL      = os.environ.get("GATEWAY_URL", "http://policy-gw:8083")
COORDINATOR_TOKEN = os.environ.get("COORDINATOR_TOKEN", "")
# v2.4 勘误:Candidate(M4F_ONLY_MODE=1)使用独立最小权限 GATEWAY_TOKEN(m5coordinator 身份),
# 不读取生产 COORDINATOR_TOKEN。gateway_client 同样回退 GATEWAY_TOKEN → COORDINATOR_TOKEN。
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "").strip() or COORDINATOR_TOKEN
# B4c.1: 对账阈值固定为代码常量(与 l2_reconcile_executing SQL `interval '120 seconds'` 一致;安全下限,非部署参数)
L2_RECONCILE_MIN_AGE_SECONDS = 120
L2_LEASE_SECONDS  = int(os.environ.get("L2_LEASE_SECONDS", "90"))   # outbox DISPATCHED lease(须 ≥ L2_GW_TIMEOUT+5,启动断言)
L2_GW_TIMEOUT     = int(os.environ.get("L2_GW_TIMEOUT", "60"))      # 单次 Gateway MCP 调用总超时(含 SSE+initialize)
# B4c.1 瞬时退避(指数,上限封顶;attempts 仅在真实 Gateway 调用时 +1,等待期不长)
L2_RETRY_BASE_SECONDS = int(os.environ.get("L2_RETRY_BASE_SECONDS", "5"))
L2_RETRY_MAX_SECONDS  = int(os.environ.get("L2_RETRY_MAX_SECONDS", "300"))
# B4c.1 单循环工作预算 + 发现期限(替代旧 L2_DISCOVERY_MAX 计数 HOLD)
L2_MAINTENANCE_MAX_ITEMS      = int(os.environ.get("L2_MAINTENANCE_MAX_ITEMS", "3"))
L2_MAINTENANCE_BUDGET_SECONDS = int(os.environ.get("L2_MAINTENANCE_BUDGET_SECONDS", "60"))
L2_DISCOVERY_TIMEOUT_SECONDS  = int(os.environ.get("L2_DISCOVERY_TIMEOUT_SECONDS", "300"))
L2_EXPIRY_BATCH              = int(os.environ.get("L2_EXPIRY_BATCH", "50"))   # B4c.1.4:expiry/stranded 独立 DB 批量上限(不消耗 gateway item budget,受 deadline 门)

# ── M3-C 状态感知失败处理配置 ──
# 决策 3:MAX_VERIFY_ATTEMPTS = 总验证次数上限(默认 3)。verify FAIL:已验证次数 < MAX → 回退 Fixer 重试;
#   达到 MAX → HOLD(不自动 CLOSE;CLOSE 是独立人工 L2 流程)。
MAX_VERIFY_ATTEMPTS = int(os.environ.get("MAX_VERIFY_ATTEMPTS", "3"))


def _validate_l2_config():
    """B4c.1.2:数值配置校验(正数/上下限/关系)。非法 raise(startup_assert 转 FATAL,防静默停摆/热循环)。"""
    if L2_MAINTENANCE_MAX_ITEMS < 1: raise ValueError("L2_MAINTENANCE_MAX_ITEMS 须 ≥1")
    if L2_MAINTENANCE_BUDGET_SECONDS < 1: raise ValueError("L2_MAINTENANCE_BUDGET_SECONDS 须 ≥1")
    if L2_RETRY_BASE_SECONDS < 1 or L2_RETRY_MAX_SECONDS < 1: raise ValueError("L2_RETRY_* 须 ≥1")
    if L2_RETRY_BASE_SECONDS > L2_RETRY_MAX_SECONDS: raise ValueError("L2_RETRY_BASE 须 ≤ L2_RETRY_MAX")
    if L2_DISCOVERY_TIMEOUT_SECONDS < 1: raise ValueError("L2_DISCOVERY_TIMEOUT_SECONDS 须 ≥1")
    if L2_LEASE_SECONDS < 1 or L2_GW_TIMEOUT < 1: raise ValueError("L2_LEASE_SECONDS/L2_GW_TIMEOUT 须 ≥1")
    if L2_EXPIRY_BATCH < 1 or L2_EXPIRY_BATCH > 500: raise ValueError("L2_EXPIRY_BATCH 须 1..500")
    if MAX_VERIFY_ATTEMPTS < 1: raise ValueError("MAX_VERIFY_ATTEMPTS 须 ≥1")
    if M4F_ENABLED_INVALID:
        raise ValueError("M4F_ENABLED 须为显式布尔值")
    if M4F_EVENT_LEASE_SECONDS < 1 or M4F_EVENT_LEASE_SECONDS > 3600:
        raise ValueError("M4F_EVENT_LEASE_SECONDS 须 1..3600")
    if M4F_EVENT_MAX_ATTEMPTS < 1 or M4F_EVENT_MAX_ATTEMPTS > 100:
        raise ValueError("M4F_EVENT_MAX_ATTEMPTS 须 1..100")
    if M4F_ENABLED and not M4F_SNAPSHOT_DSN:
        raise ValueError("M4F_ENABLED=1 时必须配置 M4F_SNAPSHOT_DSN")


class GatewayOutcome:
    """B4c.1 结构化 drain 结果。kind:
       SUCCESS(Gateway OK,approval 应已迁移)/ TRANSIENT(网络/超时/L2_DB_UNAVAILABLE → 退避,不终结)/
       TICKET_DENY(claim 前确定性拒绝 → l2_reject_approved → FAILED/HOLD)/
       GLOBAL_DEGRADED(全局配置故障 → 退回 outbox + 本 tick 停消费)。"""
    __slots__ = ("kind", "reason_code", "detail")
    def __init__(self, kind, reason_code="", detail=""):
        self.kind = kind; self.reason_code = reason_code; self.detail = detail


def _l2_backoff_seconds(retry_count):
    """min(MAX, BASE * 2 ** min(retry_count, 8))。retry_count 用 outbox.attempts。"""
    return min(L2_RETRY_MAX_SECONDS, L2_RETRY_BASE_SECONDS * (2 ** min(max(int(retry_count or 0), 0), 8)))


def _l2_requeue(run_id, reason, stage):
    """B4c.1.1 #1:RETRY 重新排队(retry_count++ / next_attempt_at=now+backoff),CAS current_stage=stage
    防覆盖(任务已不在该阶段则不排)。供 discover/build RETRY 路径防饿死。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT l2_retry_count FROM task_runs WHERE run_id=%s AND current_stage=%s", (run_id, stage))
            r = cur.fetchone()
            if not r:
                conn.commit(); return
            cur.execute("UPDATE task_runs SET l2_retry_count=l2_retry_count+1, l2_retry_reason=%s, l2_next_attempt_at=now()+make_interval(secs=>%s), updated_at=now() WHERE run_id=%s AND current_stage=%s",
                        ((reason or "")[:60], _l2_backoff_seconds(r[0] or 0), run_id, stage))
        conn.commit()
    except Exception:
        conn.rollback()


# B4c.1 Gateway circuit breaker(GLOBAL_DEGRADED 或 网络故障触发):本 tick 不再让其他任务连环撞 Gateway;
#   degraded_until 过后自动恢复(无需重启 Controller)。memory:failure_count/retry_at/last_error。
_L2_GW = {"degraded_until": 0.0, "failure_count": 0, "last_error": ""}


def _l2_gw_degraded():
    """Gateway 是否处于降级窗口。True ⇒ drain 本 tick 不消费 outbox(纯 DB 收敛仍继续)。"""
    return time.monotonic() < _L2_GW["degraded_until"]


def _l2_gw_mark_degraded(reason, seconds=None):
    """打开 breaker:记录 failure,degraded_until = now + 退避(默认按 failure_count 指数)。"""
    if seconds is None:
        seconds = _l2_backoff_seconds(_L2_GW["failure_count"])
    _L2_GW["failure_count"] += 1
    _L2_GW["last_error"] = (reason or "")[:80]
    _L2_GW["degraded_until"] = time.monotonic() + seconds


def _l2_gw_ok():
    """Gateway 调用成功 → 关 breaker(清 failure_count)。"""
    if _L2_GW["failure_count"] or _L2_GW["degraded_until"]:
        _L2_GW["failure_count"] = 0
        _L2_GW["degraded_until"] = 0.0
        _L2_GW["last_error"] = ""

NEXT_STAGE = {"review": "fix", "fix": "verify"}
NEXT_AGENT = {"review": "fixer", "fix": "verifier"}
STAGE_TPL = {
    "review": "{p}-review 完成,findings 见 shared/tasks/{p}-review/findings.md。请用 gh-mcp-fix.sh 提修复 PR(L2 密钥/依赖/删除类只出方案)。完成写 TASK_COMPLETED: {p}-fix。",
    "fix":    "{p}-fix 完成,修复 PR 见 shared/tasks/{p}-fix/。请用 gh-mcp-read.sh 读修复分支逐项复核。完成写 TASK_COMPLETED: {p}-verify。",
}
PAT_SUBMIT  = re.compile(r"TASK_SUBMITTED:\s*(\{.*\})", re.I | re.S)
PAT_COMPLETE = {s: re.compile(rf"TASK_COMPLETED[:\s]*([A-Za-z0-9\-]+)-{s}", re.I) for s in ("review", "fix", "verify", "revert", "reverify")}
# M3-C:结构化 POST_MERGE_VERIFY_FAILED(仅 verifier)—— JSON payload 紧跟冒号后(真实入口,需求 3)
PAT_PMF = re.compile(r"POST_MERGE_VERIFY_FAILED\s*:\s*(\{.*?\})\s*\Z", re.I | re.S)
PAT_M4F = re.compile(r"M4F_RUN\s*:", re.I)

class MatrixUnavailable(Exception): pass
class MatrixRejected(Exception): pass

# ── Matrix API ──
_token = None
def matrix_request(method, path, body=None, timeout=30):
    """带错误分类的 Matrix API 调用。不返回空 dict 伪装成功。"""
    global _token
    url = MATRIX_HS + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if _token:
        req.add_header("Authorization", "Bearer " + _token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        code = e.code
        body_text = e.read().decode()[:200]
        if code == 401:
            _token = None  # 触发重登录
            raise MatrixUnavailable(f"401 Unauthorized — 需要重登录")
        elif code == 429:
            retry_after = e.headers.get("Retry-After", "5")
            raise MatrixUnavailable(f"429 Rate limited — Retry-After={retry_after}")
        elif code in (500, 502, 503, 504):
            raise MatrixUnavailable(f"{code} Server error — {body_text}")
        else:
            raise MatrixRejected(f"{code} {body_text}")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
        raise MatrixUnavailable(f"连接失败: {e}")

def ensure_matrix_login():
    global _token
    if _token:
        return _token
    resp = matrix_request("POST", "/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": MATRIX_USER},
        "password": ADMIN_PW,
    })
    _token = resp.get("access_token")
    if not _token:
        raise MatrixUnavailable("login 返回无 token")
    print("[ctrl] Matrix login OK")
    return _token

def matrix_sync(since=None, timeout=30000):
    """使用 /sync 增量拉取事件。"""
    params = f"timeout={timeout}"
    if since:
        params += f"&since={since}"
    return matrix_request("GET", f"/_matrix/client/v3/sync?{params}", timeout=timeout // 1000 + 10)

def send_mention(room_id, user, text):
    """发真 @mention 消息,返回 event_id。"""
    uid = f"@{user}:{SERVER}"
    txn = "c_" + hashlib.sha256(f"{room_id}:{user}:{text}".encode()).hexdigest()[:16]
    resp = matrix_request("PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}", {
        "msgtype": "m.room.text",
        "body": f"@{user} {text}",
        "format": "org.matrix.custom.html",
        "formatted_body": f'<a href="https://matrix.to/#/{uid}">{user}</a> {text}',
        "m.mentions": {"user": [uid]},
    })
    return resp.get("event_id")

# ── PG 连接(带重连) ──
_pg = None
_m4f_snapshot_pg = None
def ensure_pg():
    global _pg
    if _pg is not None and not _pg.closed:
        try:
            _pg.cursor().execute("SELECT 1")
            return _pg
        except Exception:
            _pg.close()
            _pg = None
    _pg = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASS)
    _pg.autocommit = False
    print("[ctrl] PG connected")
    return _pg

def reset_pg():
    global _pg
    if _pg:
        try: _pg.close()
        except: pass
    _pg = None


def ensure_m4f_snapshot_pg():
    global _m4f_snapshot_pg
    if not M4F_ENABLED:
        raise RuntimeError("M4-F ingress disabled")
    if _m4f_snapshot_pg is not None and not _m4f_snapshot_pg.closed:
        try:
            with _m4f_snapshot_pg.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return _m4f_snapshot_pg
        except Exception:
            try:
                _m4f_snapshot_pg.close()
            except Exception:
                pass
            _m4f_snapshot_pg = None
    _m4f_snapshot_pg = psycopg2.connect(M4F_SNAPSHOT_DSN)
    _m4f_snapshot_pg.autocommit = False
    print("[ctrl][M4F] snapshot-worker PG connected")
    return _m4f_snapshot_pg


def reset_m4f_snapshot_pg():
    global _m4f_snapshot_pg
    if _m4f_snapshot_pg:
        try:
            _m4f_snapshot_pg.close()
        except Exception:
            pass
    _m4f_snapshot_pg = None

# ── 事件处理(原子事务) ──
def process_event(event_id, room_id, raw_sender, sender, body, ts):
    """处理单个 Matrix 事件,在一个 PG 事务内完成状态转换 + outbox。

    v2.4 Fix 1: raw_sender = 完整 @localpart:server(持久化到 stage_events.sender,
    保留 provenance);sender = 解析出的 localpart(用于 role 校验,同旧语义)。
    """
    conn = ensure_pg()
    cur = conn.cursor()

    # 记录到 stage_events(幂等:event_id PK)。sender 存完整 @localpart:server。
    inserted = False
    try:
        _etype = (
            "M4F_RUN"
            if PAT_M4F.search(body)
            else (
                "POST_MERGE_VERIFY_FAILED"
                if PAT_PMF.search(body)
                else ("TASK_SUBMITTED" if PAT_SUBMIT.search(body) else "TASK_COMPLETED")
            )
        )
        cur.execute("""INSERT INTO stage_events(event_id, room_id, sender, event_type, raw_body, body_sha256, status)
                       VALUES(%s, %s, %s, %s, %s, %s, 'RECEIVED')
                       ON CONFLICT (event_id) DO NOTHING
                       RETURNING event_id""",
                    (event_id, room_id, raw_sender, _etype,
                     body[:2000], hashlib.sha256(body.encode()).hexdigest()[:16]))
        row = cur.fetchone()
        inserted = row is not None
    except Exception as e:
        conn.rollback()
        print(f"[ctrl] stage_events insert err: {e}")
        return
    if not inserted:
        return  # event_id 已处理(幂等)

    # 0.5 M4-F AgentTeams ingress.  Network/Gateway work is deliberately
    # deferred to drain_m4f_events; this transaction only durably records the
    # validated Matrix event.
    if PAT_M4F.search(body):
        # M5-0 Candidate mode: accept M4F_RUN from allowlisted Manager only
        # (verified sender already checked in consume_events; here we check role)
        if M4F_ONLY_MODE:
            # In Candidate mode, sender has already been verified by verify_m5_sender
            # in consume_events. Only accept M4F_RUN from @manager.
            if sender != "manager":
                mark_error(cur, event_id, f"M4F_RUN sender must be manager in Candidate mode (got {sender})")
                conn.commit()
                return
        elif sender != ADMIN:
            # Legacy mode: only admin sends M4F_RUN (M3/M4-F fixture behavior)
            mark_error(cur, event_id, "M4F_RUN sender must be admin")
            conn.commit()
            return
        if not M4F_ENABLED:
            mark_error(cur, event_id, "M4F ingress disabled")
            conn.commit()
            return
        try:
            payload = m4f_ingress.parse_event(body)
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cur.execute(
                """UPDATE stage_events
                   SET run_id=%s,stage='m4f',event_type='M4F_RUN',
                       raw_body=%s,status='M4F_PENDING',error=NULL,
                       processed_at=NULL
                   WHERE event_id=%s""",
                (payload["run_id"], canonical, event_id),
            )
            conn.commit()
            print(f"[ctrl][M4F] {payload['run_id']} ingress queued")
        except m4f_ingress.M4FIngressError as exc:
            conn.rollback()
            mark_error(cur, event_id, str(exc)[:500])
            conn.commit()
        return

    # 1. TASK_SUBMITTED
    m = PAT_SUBMIT.search(body)
    if m and sender == ADMIN:
        try:
            payload = json.loads(m.group(1))
            run_id = payload.get("run_id", "")
            if not run_id:
                mark_error(cur, event_id, "no run_id"); conn.commit(); return
            cur.execute("""INSERT INTO task_runs(run_id, room_id, repo, pr_number, branch, status, current_stage, approval_required)
                           VALUES(%s, %s, %s, %s, %s, 'RUNNING', 'review', %s)
                           ON CONFLICT(run_id) DO NOTHING""", (
                run_id, room_id, payload.get("repo"), payload.get("pr_number"), payload.get("branch"), L2_MERGE_ENABLED))
            cur.execute("""INSERT INTO stage_runs(run_id, stage, agent, attempt, status, started_at)
                           VALUES(%s, 'review', 'reviewer', 1, 'PENDING_DISPATCH', now())
                           ON CONFLICT(run_id, stage, attempt) DO NOTHING""", (run_id,))
            cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                           VALUES(%s, %s, %s, 'reviewer', 'review', 1, %s)
                           ON CONFLICT(idempotency_key) DO NOTHING""", (
                f"{run_id}:review:1", run_id, room_id,
                f"请审查 {payload.get('repo','')} PR#{payload.get('pr_number','')} (分支 {payload.get('branch','')})。用 gh-mcp-read.sh + sast-scan,findings 写 shared/tasks/{run_id}-review/findings.md。完成写 TASK_COMPLETED: {run_id}-review。"))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, "review")
            conn.commit()
            print(f"[ctrl] TASK_SUBMITTED {run_id} → task_run + review PENDING_DISPATCH")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 1.5 POST_MERGE_VERIFY_FAILED(M3-C 真实入口,需求 3:仅 verifier;校验 room/run/repo/pr/result_sha)
    #    纯 DB(不在事件事务调 Gateway,与 process_event 其余分支一致)→ 建 rollback_runs(PENDING,parent_run_id)
    #    + child task_run(revert_run_id,独占 revert 链)。E2E 经 process_event 驱动,不直接 INSERT stage_events。
    if PAT_PMF.search(body):
        try:
            if sender != "verifier":
                mark_error(cur, event_id, f"POST_MERGE_VERIFY_FAILED from non-verifier sender={sender}"); conn.commit(); return
            mpmf = PAT_PMF.search(body)
            try:
                payload = json.loads(mpmf.group(1))
            except Exception:
                mark_error(cur, event_id, "POST_MERGE_VERIFY_FAILED payload 非 JSON"); conn.commit(); return
            if not isinstance(payload, dict):
                mark_error(cur, event_id, "POST_MERGE_VERIFY_FAILED payload 非 object"); conn.commit(); return
            p_run = payload.get("run_id"); p_repo = payload.get("repo")
            p_pr = payload.get("pr_number"); p_sha = payload.get("result_sha"); p_room = payload.get("room")
            if not (p_run and p_repo and p_pr and p_sha and room_id):
                mark_error(cur, event_id, "POST_MERGE_VERIFY_FAILED 缺字段 run/repo/pr/sha/room"); conn.commit(); return
            if p_room and p_room != room_id:
                mark_error(cur, event_id, f"room mismatch payload={p_room} event={room_id}"); conn.commit(); return
            cur.execute("SELECT status, repo, pr_number, room_id FROM task_runs WHERE run_id=%s FOR UPDATE", (p_run,))
            _t = cur.fetchone()
            if not _t:
                mark_error(cur, event_id, f"unknown run_id={p_run}"); conn.commit(); return
            t_status, t_repo, t_pr, t_room = _t
            if t_status != "MERGED":
                mark_error(cur, event_id, f"task status={t_status} != MERGED"); conn.commit(); return
            if t_repo != p_repo or int(t_pr or 0) != int(p_pr):
                mark_error(cur, event_id, f"repo/pr mismatch task={t_repo}#{t_pr} event={p_repo}#{p_pr}"); conn.commit(); return
            if t_room and t_room != room_id:
                mark_error(cur, event_id, f"task room mismatch task={t_room} event={room_id}"); conn.commit(); return
            cur.execute("SELECT result_sha FROM approvals WHERE run_id=%s AND action='merge' AND status='USED' ORDER BY used_at DESC LIMIT 1", (p_run,))
            _a = cur.fetchone()
            recorded = _a[0] if _a else None
            if not recorded or recorded != p_sha:
                mark_error(cur, event_id, f"result_sha mismatch event={p_sha} recorded={recorded} (forged/spurious, no rollback)")
                print(f"[ctrl][M3C] {p_run} POST_MERGE_VERIFY_FAILED result_sha mismatch → 拒(不回滚)")
                conn.commit(); return
            # 幂等:同 (parent_run,bad_sha) 已有 rollback → DUPLICATE
            cur.execute("SELECT 1 FROM rollback_runs WHERE parent_run_id=%s AND reverted_merge_sha=%s", (p_run, recorded))
            if cur.fetchone():
                mark_duplicate(cur, event_id); update_event_meta(cur, event_id, p_run, "post_merge_fail"); conn.commit(); return
            child_run = f"{p_run}-revert-{recorded[:8]}"
            rb_id = "rb-" + str(uuid.uuid4())
            # 先建 child task_run(parent_run_id 已存在;task_runs.rollback_id 软指向无 FK),
            # 再建 rollback_runs(revert_run_id FK → task_runs 要求 child 先存在)
            cur.execute("""INSERT INTO task_runs(run_id, parent_run_id, room_id, repo, pr_number, status, current_stage, approval_required, rollback_id)
                           VALUES(%s, %s, %s, %s, %s, 'RUNNING', 'rollback_revert', TRUE, %s)
                           ON CONFLICT (run_id) DO NOTHING""",
                        (child_run, p_run, room_id, p_repo, int(p_pr), rb_id))
            cur.execute("""INSERT INTO rollback_runs(rollback_id, parent_run_id, revert_run_id, reverted_merge_sha, repo, pr_number, trigger_event_id, status)
                           VALUES(%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                           ON CONFLICT (parent_run_id, reverted_merge_sha) DO NOTHING""",
                        (rb_id, p_run, child_run, recorded, p_repo, int(p_pr), event_id))
            cur.execute("UPDATE task_runs SET status='FAIL', current_stage='rollback_pending', rollback_id=%s, last_error='POST_MERGE_VERIFY_FAILED', updated_at=now() WHERE run_id=%s AND status='MERGED'",
                        (rb_id, p_run))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, p_run, "post_merge_fail")
            conn.commit()
            print(f"[ctrl][M3C] {p_run} POST_MERGE_VERIFY_FAILED (verifier,校验通过) → rollback PENDING (bad merge {recorded[:12]}) child={child_run}")
        except Exception as e:
            conn.rollback()
            print(f"[ctrl][M3C] POST_MERGE_VERIFY_FAILED ingest err: {e}")
            mark_error(cur, event_id, str(e)[:300]); conn.commit()
        return

    # 2. TASK_COMPLETED(review/fix)
    for stage in ("review", "fix"):
        mt = PAT_COMPLETE[stage].search(body)
        if not mt or sender not in ("reviewer", "fixer"):
            continue
        run_id = mt.group(1)
        try:
            # 锁住 task
            cur.execute("SELECT status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            task = cur.fetchone()
            if not task:
                mark_error(cur, event_id, f"unknown run_id={run_id}"); conn.commit(); return

            # 找当前 RUNNING/PENDING_DISPATCH attempt
            cur.execute("""SELECT id, attempt FROM stage_runs
                           WHERE run_id=%s AND stage=%s AND status IN ('RUNNING','PENDING_DISPATCH','DISPATCHED')
                           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""", (run_id, stage))
            current = cur.fetchone()
            if not current:
                mark_duplicate(cur, event_id); update_event_meta(cur, event_id, run_id, stage); conn.commit(); return

            # 完成当前阶段
            cur.execute("UPDATE stage_runs SET status='COMPLETED', completed_at=now() WHERE id=%s", (current[0],))

            # 创建下一阶段
            ns = NEXT_STAGE.get(stage)
            if ns:
                na = NEXT_AGENT[stage]
                _next_attempt = current[1]  # M3-C: attempt 传播(支持 verify FAIL 回退重试;正常流程 current[1]=1 不变)
                cur.execute("""INSERT INTO stage_runs(run_id, stage, agent, attempt, status)
                               VALUES(%s, %s, %s, %s, 'PENDING_DISPATCH')
                               ON CONFLICT(run_id, stage, attempt) DO NOTHING""", (run_id, ns, na, _next_attempt))
                cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                               VALUES(%s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT(idempotency_key) DO NOTHING""", (
                    f"{run_id}:{ns}:{_next_attempt}", run_id, room_id, na, ns, _next_attempt,
                    STAGE_TPL[stage].format(p=run_id)))
                cur.execute("UPDATE task_runs SET current_stage=%s, status='RUNNING', updated_at=now() WHERE run_id=%s", (ns, run_id))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, stage)
            conn.commit()
            print(f"[ctrl] {sender} TASK_COMPLETED {run_id}-{stage} → {ns or 'done'} PENDING_DISPATCH | PG committed")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 3. TASK_COMPLETED(verify) — 只认独立行 VERDICT=,流式快照标 PARTIAL 不终结
    mt = PAT_COMPLETE["verify"].search(body)
    if mt and sender == "verifier":
        run_id = mt.group(1)
        # 必须是独立行 VERDICT=PASS|FAIL|BLOCKED(?mi 多行+忽略大小写)
        vd = re.search(r"(?mi)^\s*VERDICT\s*=\s*(PASS|FAIL|BLOCKED)\s*$", body)
        if not vd:
            # 流式中间快照(还没到 VERDICT=)→ 标 PARTIAL,不动 stage/task
            update_event_meta(cur, event_id, run_id, "verify")
            cur.execute("UPDATE stage_events SET status='PARTIAL', processed_at=now(), error='waiting for explicit VERDICT' WHERE event_id=%s", (event_id,))
            conn.commit()
            print(f"[ctrl] {run_id}-verify partial snapshot; waiting for VERDICT")
            return
        verdict = vd.group(1).upper()
        if verdict == "BLOCKED": verdict = "blocked-needs-approval"
        try:
            cur.execute("SELECT status FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            task = cur.fetchone()
            if not task:
                mark_error(cur, event_id, f"unknown run_id={run_id}"); conn.commit(); return

            # 幂等:找当前 RUNNING/PENDING_DISPATCH/DISPATCHED 的 verify stage
            cur.execute("""SELECT id FROM stage_runs
                           WHERE run_id=%s AND stage='verify'
                             AND status IN ('RUNNING','PENDING_DISPATCH','DISPATCHED')
                           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""", (run_id,))
            current = cur.fetchone()
            if not current:
                mark_duplicate(cur, event_id)
                update_event_meta(cur, event_id, run_id, "verify")
                conn.commit()
                print(f"[ctrl] {run_id}-verify 重复事件(已 COMPLETED)→ DUPLICATE")
                return

            cur.execute("UPDATE stage_runs SET status='COMPLETED', completed_at=now(), verdict=%s WHERE id=%s",
                        (verdict, current[0]))
            # B4c(复审 #1/#2):verify PASS 且 run 级 approval_required=TRUE → 写持久化待办
            #   task=APPROVAL_PENDING + current_stage='l2_binding'。**不在事件事务内调 Gateway**
            #   (/sync 游标照推进,Gateway 临时不可达不会让事件报错卡死任务)。绑定发现/建票/drain
            #   由主循环 initiate_l2_pending 异步完成(独立故障域,复审 #3)。
            # approval_required=FALSE → 旧行为:verify PASS → task PASS(L2 关闭,不退化)。
            if verdict == "PASS":
                cur.execute("SELECT approval_required FROM task_runs WHERE run_id=%s", (run_id,))
                _ar = cur.fetchone()
                if _ar and _ar[0]:
                    cur.execute("UPDATE task_runs SET status='APPROVAL_PENDING', verdict=%s, current_stage='l2_binding', updated_at=now() WHERE run_id=%s",
                                (verdict, run_id))
                    _task_status = "APPROVAL_PENDING"
                else:
                    cur.execute("UPDATE task_runs SET status='PASS', verdict=%s, updated_at=now() WHERE run_id=%s",
                                (verdict, run_id))
                    _task_status = "PASS"
            else:
                # M3-C(决策 3+7):verify FAIL 原子分支 —— 同事务递增 verify_attempt + 决定 retry/HOLD。
                #   并发幂等:task 已 FOR UPDATE 锁(本函数上方)+ stage_runs(run_id,'fix',attempt) UNIQUE CAS。
                #   仅处理"未合并"verify FAIL(回退/HOLD);**已合并 FAIL 由结构化 POST_MERGE_VERIFY_FAILED
                #   事件触发回滚**(见 process_post_merge_failures),普通 Matrix 文本 FAIL 不触发回滚。
                cur.execute("SELECT verify_attempt FROM task_runs WHERE run_id=%s", (run_id,))
                _va_row = cur.fetchone()
                _va = _va_row[0] if _va_row else 0
                _new_va = _va + 1
                cur.execute("SELECT attempt FROM stage_runs WHERE id=%s", (current[0],))
                _vrow = cur.fetchone()
                _ver_attempt = _vrow[0] if _vrow else 1
                if _new_va < MAX_VERIFY_ATTEMPTS:
                    # 回退 Fixer:建 fix attempt = verify_attempt+1 stage_run(UNIQUE CAS)+ dispatch(幂等 key)
                    _next_fix = _ver_attempt + 1
                    cur.execute("""INSERT INTO stage_runs(run_id, stage, agent, attempt, status)
                                   VALUES(%s, 'fix', 'fixer', %s, 'PENDING_DISPATCH')
                                   ON CONFLICT (run_id, stage, attempt) DO NOTHING""", (run_id, _next_fix))
                    cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                                   VALUES(%s, %s, %s, 'fixer', 'fix', %s, %s)
                                   ON CONFLICT (idempotency_key) DO NOTHING""",
                                (f"{run_id}:fix:{_next_fix}", run_id, room_id, _next_fix,
                                 f"verify FAIL(第 {_new_va}/{MAX_VERIFY_ATTEMPTS} 次),回退修复。完成写 TASK_COMPLETED: {run_id}-fix。"))
                    cur.execute("UPDATE task_runs SET verify_attempt=%s, status='RUNNING', current_stage='fix', verdict=%s, last_error=%s, updated_at=now() WHERE run_id=%s",
                                (_new_va, verdict, f"verify FAIL; retry fix attempt={_next_fix} ({_new_va}/{MAX_VERIFY_ATTEMPTS})", run_id))
                    _task_status = f"RUNNING(retry fix attempt={_next_fix},{_new_va}/{MAX_VERIFY_ATTEMPTS})"
                else:
                    # 达上限 → HOLD(决策 3:不自动 CLOSE;CLOSE 是独立人工 L2 流程)
                    cur.execute("UPDATE task_runs SET verify_attempt=%s, status='HOLD', current_stage='verify_max_hold', verdict=%s, last_error=%s, updated_at=now() WHERE run_id=%s",
                                (_new_va, verdict, f"verify FAIL reached MAX_VERIFY_ATTEMPTS={MAX_VERIFY_ATTEMPTS}", run_id))
                    _task_status = f"HOLD(verify max={MAX_VERIFY_ATTEMPTS})"
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, "verify")
            conn.commit()
            print(f"[ctrl] {run_id}-verify VERDICT={verdict} → task {_task_status} | PG committed")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 4. TASK_COMPLETED(reverify) — M3-C:reverify PASS→RECOVERED(parent 复活)/FAIL→HELD(不二回滚,决策 9)
    #    reverify 派发在 **parent run**(rollback.revert_run_id 是 child;reverify 验 main 恢复 → 归属原 run)。
    mtr = PAT_COMPLETE["reverify"].search(body)
    if mtr and sender == "verifier":
        rv_run = mtr.group(1)   # parent run
        vd = re.search(r"(?mi)^\s*VERDICT\s*=\s*(PASS|FAIL|BLOCKED)\s*$", body)
        verdict = vd.group(1).upper() if vd else "FAIL"
        try:
            cur.execute("SELECT rollback_id, revert_run_id FROM rollback_runs WHERE parent_run_id=%s AND status='REVERIFYING' ORDER BY created_at DESC LIMIT 1 FOR UPDATE", (rv_run,))
            rb = cur.fetchone()
            if not rb:
                mark_duplicate(cur, event_id); update_event_meta(cur, event_id, rv_run, "reverify"); conn.commit(); return
            rb_id, child_run = rb[0], rb[1]
            if verdict == "PASS":
                cur.execute("UPDATE rollback_runs SET status='RECOVERED', reverify_verdict='PASS', reverify_event_id=%s, updated_at=now() WHERE rollback_id=%s", (event_id, rb_id))
                cur.execute("UPDATE task_runs SET status='PASS', current_stage='reverified', last_error='rollback recovered (reverify PASS)', updated_at=now() WHERE run_id=%s", (rv_run,))
                cur.execute("UPDATE task_runs SET current_stage='reverified', last_error='rollback recovered (reverify PASS)', updated_at=now() WHERE run_id=%s", (child_run,))
                _rv = "RECOVERED"
            else:
                # 决策 9:reverify FAIL → HOLD/人工升级,不生成第二回滚
                cur.execute("UPDATE rollback_runs SET status='HELD', reverify_verdict='FAIL', reverify_event_id=%s, updated_at=now() WHERE rollback_id=%s", (event_id, rb_id))
                cur.execute("UPDATE task_runs SET status='HOLD', current_stage='reverify_failed', last_error='reverify FAIL after rollback (human escalation, no 2nd rollback)', updated_at=now() WHERE run_id=%s", (rv_run,))
                _rv = "HELD(reverify FAIL)"
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, rv_run, "reverify")
            conn.commit()
            print(f"[ctrl][M3C] {rv_run}-reverify VERDICT={verdict} → rollback {_rv}")
        except Exception as e:
            conn.rollback()
            mark_error(cur, event_id, str(e)); conn.commit()
        return

    # 5. TASK_COMPLETED(revert) — child fixer 完成 revert PR 创建(阶段完成只记账;后续由 process_rollback_advance 发现 PR 并建票)
    mrv = PAT_COMPLETE["revert"].search(body)
    if mrv and sender == "fixer":
        run_id = mrv.group(1)
        try:
            cur.execute("SELECT status FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            task = cur.fetchone()
            if not task:
                mark_error(cur, event_id, f"unknown run_id={run_id}"); conn.commit(); return
            cur.execute("""SELECT id FROM stage_runs
                           WHERE run_id=%s AND stage='revert'
                             AND status IN ('RUNNING','PENDING_DISPATCH','DISPATCHED')
                           ORDER BY attempt DESC LIMIT 1 FOR UPDATE""", (run_id,))
            current = cur.fetchone()
            if not current:
                mark_duplicate(cur, event_id)
                update_event_meta(cur, event_id, run_id, "revert")
                conn.commit()
                print(f"[ctrl] {run_id}-revert 重复事件(已 COMPLETED)→ DUPLICATE")
                return
            cur.execute("UPDATE stage_runs SET status='COMPLETED', completed_at=now() WHERE id=%s", (current[0],))
            mark_processed(cur, event_id)
            update_event_meta(cur, event_id, run_id, "revert")
            conn.commit()
            print(f"[ctrl][M3C] {run_id}-revert → stage COMPLETED")
        except Exception as e:
            conn.rollback()
            print(f"[ctrl][M3C] revert completion err: {e}")
            mark_error(cur, event_id, str(e)[:300]); conn.commit()
        return

    # 非匹配事件
    mark_processed(cur, event_id)
    conn.commit()

def mark_processed(cur, event_id):
    cur.execute("UPDATE stage_events SET status='PROCESSED', processed_at=now() WHERE event_id=%s", (event_id,))

def mark_error(cur, event_id, err):
    cur.execute("UPDATE stage_events SET status='ERROR', error=%s WHERE event_id=%s", (err[:500], event_id))

def mark_duplicate(cur, event_id):
    cur.execute("UPDATE stage_events SET status='DUPLICATE', processed_at=now() WHERE event_id=%s", (event_id,))

def update_event_meta(cur, event_id, run_id, stage):
    """处理完成后回填 stage_events 的 run_id/stage,形成审计链。"""
    cur.execute("UPDATE stage_events SET run_id=%s, stage=%s WHERE event_id=%s", (run_id, stage, event_id))

# ── Outbox 派发 ──
def _m4f_attempt(error):
    match = re.match(r"attempt=(\d+)\b", error or "")
    return int(match.group(1)) if match else 0


def drain_m4f_events(max_items=1):
    """Claim and stage durable M4F_RUN Matrix events.

    ``stage_six_skill_run`` is idempotent at every database boundary, so a
    process crash after a partial stage is safely reclaimed after the lease.
    """
    if not M4F_ENABLED:
        return 0
    import gateway_client

    handled = 0
    for _ in range(max(1, int(max_items))):
        conn = ensure_pg()
        try:
            with conn.cursor() as cur:
                _pf_clause, _pf_params = _drain_m4f_claim_sql_prefix()
                cur.execute(
                    f"""WITH candidate AS (
                           SELECT event_id
                           FROM public.stage_events
                           WHERE event_type='M4F_RUN'
                             AND (
                               status='M4F_PENDING'
                               OR (
                                 status='M4F_RUNNING'
                                 AND processed_at < now()-make_interval(secs=>%s)
                               )
                             ){_pf_clause}
                           ORDER BY received_at,event_id
                           FOR UPDATE SKIP LOCKED
                           LIMIT 1
                       )
                       UPDATE public.stage_events AS e
                       SET status='M4F_RUNNING',processed_at=now()
                       FROM candidate AS c
                       WHERE e.event_id=c.event_id
                       RETURNING e.event_id,e.raw_body,e.error""",
                    [M4F_EVENT_LEASE_SECONDS] + _pf_params,
                )
                claimed = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if not claimed:
            break

        event_id, raw_body, prior_error = claimed
        attempt = _m4f_attempt(prior_error) + 1
        try:
            payload = m4f_ingress.validate_event(json.loads(raw_body))
            staged = m4f_ingress.stage_agentteams_event(
                conn,
                ensure_m4f_snapshot_pg(),
                payload,
                gateway=gateway_client,
                observer=lambda event: print(
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    flush=True,
                ),
            )
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE public.stage_events
                       SET run_id=%s,status='PROCESSED',stage='m4f',
                           error=NULL,processed_at=now()
                       WHERE event_id=%s AND status='M4F_RUNNING'""",
                    (staged.run_id, event_id),
                )
            conn.commit()
            print(f"[ctrl][M4F] {staged.run_id} staged six-Skill DAG")
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                reset_pg()
                conn = ensure_pg()
            try:
                snapshot_conn = _m4f_snapshot_pg
                if snapshot_conn is not None:
                    snapshot_conn.rollback()
            except Exception:
                reset_m4f_snapshot_pg()
            permanent = isinstance(exc, m4f_ingress.M4FIngressError)
            terminal = permanent or attempt >= M4F_EVENT_MAX_ATTEMPTS
            state = "ERROR" if terminal else "M4F_PENDING"
            safe_error = " ".join(str(exc).split())[:420]
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE public.stage_events
                       SET status=%s,error=%s,processed_at=now()
                       WHERE event_id=%s""",
                    (state, f"attempt={attempt} {type(exc).__name__}: {safe_error}", event_id),
                )
            conn.commit()
            print(
                f"[ctrl][M4F] event={event_id} attempt={attempt} "
                f"state={state} error={type(exc).__name__}: {safe_error}"
            )
        handled += 1
    return handled


def drain_outbox():
    """处理 PENDING/RETRY 的 outbox 条目。"""
    conn = ensure_pg()
    cur = conn.cursor()
    _where_clause, _where_params = _drain_outbox_sql_partition()
    cur.execute(
        f"""SELECT id, idempotency_key, room_id, target_agent, body, retry_count
           FROM dispatch_outbox
           WHERE {_where_clause}
           ORDER BY id LIMIT 20""",
        _where_params)
    items = cur.fetchall()
    for oid, ikey, room_id, agent, body, rc in items:
        try:
            # 检查 stage 状态(如果已 DISPATCHED/RUNNING,跳过)
            stage = body.split("TASK_COMPLETED:")[1].split("-")[0] if "TASK_COMPLETED:" in body else "review"
            # 发送
            eid = send_mention(room_id, agent, body)
            if eid:
                cur.execute("""UPDATE dispatch_outbox SET status='DISPATCHED', matrix_event_id=%s, dispatched_at=now()
                               WHERE id=%s""", (eid, oid))
                # 更新 stage 状态
                cur.execute("""SELECT run_id, target_stage, attempt FROM dispatch_outbox WHERE id=%s""", (oid,))
                r = cur.fetchone()
                if r:
                    cur.execute("""UPDATE stage_runs SET status='RUNNING', started_at=COALESCE(started_at, now())
                                   WHERE run_id=%s AND stage=%s AND attempt=%s AND status='PENDING_DISPATCH'""",
                                (r[0], r[1], r[2]))
                conn.commit()
                print(f"[ctrl] outbox #{oid} → {agent} @ {room_id[:14]} (eid={eid[:16]})")
            else:
                raise Exception("send_mention returned no event_id")
        except MatrixUnavailable as e:
            conn.rollback()
            delay = min(5 * (2 ** rc), 60)
            cur.execute("""UPDATE dispatch_outbox SET status='RETRY', retry_count=retry_count+1,
                           next_retry_at=now()+interval '%s seconds', last_error=%s WHERE id=%s""", (delay, str(e)[:300], oid))
            conn.commit()
            print(f"[ctrl] outbox #{oid} Matrix 不可达 → RETRY({delay}s): {e}")
            raise  # 让外层 backoff
        except Exception as e:
            conn.rollback()
            if rc >= 10:
                cur.execute("""UPDATE dispatch_outbox SET status='FAILED', last_error=%s WHERE id=%s""", (str(e)[:300], oid))
                conn.commit()
                print(f"[ctrl] outbox #{oid} FAILED (>10 retries)")
            else:
                delay = min(5 * (2 ** rc), 60)
                cur.execute("""UPDATE dispatch_outbox SET status='RETRY', retry_count=retry_count+1,
                               next_retry_at=now()+interval '%s seconds', last_error=%s WHERE id=%s""", (delay, str(e)[:300], oid))
                conn.commit()
                print(f"[ctrl] outbox #{oid} err → RETRY({delay}s): {e}")

# ── /sync 消费 ──
def consume_events():
    """使用 /sync 拉取新事件,逐个处理。"""
    conn = ensure_pg()
    cur = conn.cursor()
    cur.execute("SELECT sync_token FROM controller_offsets WHERE consumer_name=%s", (CONTROLLER_CONSUMER_NAME,))
    row = cur.fetchone()
    since = row[0] if row else None

    data = matrix_sync(since=since, timeout=SYNC_TIMEOUT)
    next_batch = data.get("next_batch")
    joined = data.get("rooms", {}).get("join", {})
    event_count = 0

    for room_id, room_data in joined.items():
        # M4F_ONLY_MODE: skip rooms not in allowlist
        if M4F_ONLY_MODE and M4F_ALLOWED_ROOMS and room_id not in M4F_ALLOWED_ROOMS:
            continue
        for evt in room_data.get("timeline", {}).get("events", []):
            if evt.get("type") != "m.room.message":
                continue
            eid = evt.get("event_id")
            raw_sender = evt.get("sender", "")
            body = evt.get("content", {}).get("body", "") or ""
            ts = evt.get("origin_server_ts", 0)

            # M5-0 Candidate mode: use strict verify_m5_sender + room/sender allowlist
            if M4F_ONLY_MODE:
                sender = verify_m5_sender(raw_sender, set(M4F_ALLOWED_SENDERS))
                if sender is None:
                    continue  # sender not verified or not allowlisted
                # M5-0A: M4F_RUN ingress (strict parser only)
                if M4F_LIVE_MODE and body.startswith(_M5_RUN_MARKER):
                    # Use strict parser instead of loose substring match
                    payload = m5_parse_m4f_run(body)
                    if payload is not None:
                        process_event(eid, room_id, raw_sender, sender, body, ts)
                        event_count += 1
                    else:
                        # Strict parse failed: record as ERROR, don't enter M4F_PENDING
                        conn_err = ensure_pg()
                        with conn_err.cursor() as ec:
                            ec.execute(
                                """INSERT INTO stage_events(event_id, room_id, sender, event_type, raw_body, body_sha256, status)
                                   VALUES(%s, %s, %s, 'M4F_RUN', %s, %s, 'ERROR')
                                   ON CONFLICT (event_id) DO NOTHING""",
                                (eid, room_id, raw_sender, body[:2000],
                                 hashlib.sha256(body.encode()).hexdigest()[:16]))
                            ec.execute(
                                "UPDATE stage_events SET error='M5-0 strict parse failed' WHERE event_id=%s",
                                (eid,))
                        conn_err.commit()
                        print(f"[ctrl][M5-0] {eid} strict M4F_RUN parse failed → ERROR")
                        event_count += 1
                    continue
                # M5-0B: TASK_COMPLETED handoff (review/fix/verify). Record via the
                # STRICT parser (never legacy substring); reconcile_m5_handoffs is
                # the sole advancement authority. Non-strict bodies -> ERROR.
                if M4F_LIVE_MODE and body.lstrip().startswith("TASK_COMPLETED:"):
                    _m5_record_handoff(eid, room_id, raw_sender, body)
                    event_count += 1
                continue  # Candidate: skip all other event types

            # Legacy mode: original loose substring matching + localpart truncation
            sender = raw_sender.split(":")[0].lstrip("@")

            # M4F_LIVE_MODE but not ONLY_MODE: also match M4F_RUN from /sync
            should_process = ("TASK_SUBMITTED" in body or "TASK_COMPLETED" in body
                              or "POST_MERGE_VERIFY_FAILED" in body)
            if M4F_LIVE_MODE and not M4F_ONLY_MODE:
                should_process = should_process or (_M5_RUN_MARKER in body)

            if should_process:
                process_event(eid, room_id, raw_sender, sender, body, ts)
                event_count += 1

    # 保存游标
    if next_batch:
        cur.execute("""INSERT INTO controller_offsets(consumer_name, sync_token, updated_at)
                       VALUES(%s, %s, now())
                       ON CONFLICT(consumer_name) DO UPDATE SET sync_token=EXCLUDED.sync_token, updated_at=now()""",
                    (CONTROLLER_CONSUMER_NAME, next_batch))
        conn.commit()

    if event_count:
        print(f"[ctrl] /sync: 处理了 {event_count} 个事件, next_batch={next_batch[:16] if next_batch else 'none'}")

# ════════════════════════════════════════════════════════════════════
# B4c · L2 审批流(Controller 侧)
# ════════════════════════════════════════════════════════════════════
# 设计(M3-B4 v2 §11 + 复审 9 条):
#   verify PASS + approval_required → task APPROVAL_PENDING + current_stage='l2_binding'
#     → initiate_l2_pending():discover_binding(B4c-1)→ l2_ensure_ticket(B4c-2)
#   approve CLI → APPROVED
#     → drain_l2_outbox():DISPATCHED+lease → Gateway merge → 推进 outbox/task(B4c-3)
#   reconcile_l2():UNKNOWN/超时EXECUTING/滞留DISPATCHED/过期(B4c-4)
# 绝不自动重试 UNKNOWN L2 动作;绑定来源不信任 LLM(GitHub 读回)。

def _gateway_reachable(timeout=5):
    """TCP 连通性探测 policy-gw host:port(不验鉴权——鉴权在首次真实 MCP 调用时验)。
    返回 True/False。用于 startup_assert:容器不可达即 fail-closed。"""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(GATEWAY_URL)
        host = u.hostname; port = u.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_pg(max_attempts=30, delay=2):
    """B4c-2.2:等 PG ready(SELECT 1 成功)。返回 (conn, ready)。ready=True 时 conn 可用。
    提取为独立函数便于单元测试(monkeypatch ensure_pg/time.sleep)。
    ready 标志(仅 SELECT 1 成功后 True;异常 conn=None)——不用 conn is not None。"""
    conn = None; ready = False
    for _ in range(max_attempts):
        try:
            conn = ensure_pg()
            with conn.cursor() as _c:
                _c.execute("SELECT 1")
            ready = True
            break
        except psycopg2.OperationalError as e:
            conn = None
            reset_pg()
            print(f"[ctrl] startup: PG 未就绪({str(e)[:80]}),{delay}s 后重试...")
            time.sleep(delay)
    return conn, ready


def _assert_m4f_snapshot_identity(snapshot_conn) -> None:
    """M8-A1 runtime identity gate for the snapshot-worker DSN.

    Read-only catalog probe requiring BOTH:
      * ``current_user == 'snapshot_worker'`` — cluster superusers (e.g. the
        initdb ``mergepilot`` role) pass ``has_function_privilege`` for ANY
        function even without a GRANT, so privilege alone is NOT an
        identity proof;
      * ``has_function_privilege(..., 'public.claim_snapshot_job(...)',
        'EXECUTE') is True``.
    Any other combination fails closed via ``sys.exit`` BEFORE
    ``mark_ready``/``run_forever``/any M4F event consumption. The probe is
    a SELECT followed by ROLLBACK (zero database side effects). Errors are
    stable, secret-free text: exception paths print only the exception
    TYPE (never ``str(exc)``, which for connection failures can embed
    host/user context); no DSN, host or password is ever emitted.
    """
    try:
        with snapshot_conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, has_function_privilege("
                "current_user,"
                "'public.claim_snapshot_job(text,text,integer)',"
                "'EXECUTE')"
            )
            row = cur.fetchone()
        snapshot_conn.rollback()
    except Exception as exc:
        sys.exit(
            f"[ctrl] FATAL: M4-F snapshot-worker DSN 不可用: "
            f"{type(exc).__name__}"
        )
    if not row or row[0] != "snapshot_worker" or row[1] is not True:
        sys.exit(
            "[ctrl] FATAL: M4-F snapshot 身份门失败: DSN 必须以 "
            "snapshot_worker 连接且持有 claim_snapshot_job EXECUTE")


def startup_assert_l2():
    """fail-closed 启动断言(B4c-0.1 #3):
    - 非法 L2_MERGE_ENABLED 值 → 拒启动(不静默当 false)。
    - 若存在非终态 approval_required=TRUE 的 run,但缺 COORDINATOR_TOKEN/GATEWAY_URL/Gateway 不可达 →
      拒启动(否则这些 run 永久卡死,无人维护)。
    - L2_MERGE_ENABLED=1(新 run 默认进审批流)→ 额外要求 token/url/Gateway 连通/l2 函数可 EXECUTE。
    L2 维护循环本身始终运行(函数按 approval_required 过滤),开关只管新 run 默认值。
    B4c.1.2:数值配置校验 + migration 检查**始终**(不 gated by need_gateway,防 L2 关闭时放过缺调度列)。"""
    try:
        _validate_l2_config()
    except (ValueError, TypeError) as e:
        sys.exit(f"[ctrl] FATAL: L2 配置非法: {e}")
    if L2_MERGE_ENABLED_INVALID:
        sys.exit(f"[ctrl] FATAL: L2_MERGE_ENABLED='{_L2_RAW}' 非法(允许:0/1/true/false/yes/no/on/off)")

    # 是否存在"需要维护但尚未终结"的审批 run
    # B4c-2.2:等 PG ready(_wait_for_pg,提取为独立函数;ready 标志仅 SELECT 1 成功后 True)
    conn, ready = _wait_for_pg()
    if not ready:
        sys.exit("[ctrl] FATAL: PG 60s 内未就绪(startup_assert)—— 先起 audit-pg")
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM task_runs
                       WHERE approval_required AND status NOT IN ('PASS','FAIL','HOLD','MERGED','ROLLED_BACK');""")
        pending_l2 = cur.fetchone()[0]
    conn.commit()  # 释放只读事务(防 idle-in-transaction)

    # B4c.1.2 #7:migration/lease 校验**始终**(不 gated by need_gateway;防 L2 关闭时放过缺调度列)
    with conn.cursor() as cur:
        # 函数缺失时 has_function_privilege(text::regprocedure) 会 ERROR → 用 EXISTS+CASE 短路到 FALSE(干净 FATAL)
        cur.execute("SELECT "
                    "(CASE WHEN EXISTS(SELECT 1 FROM pg_proc WHERE proname='l2_ensure_ticket') THEN has_function_privilege('mergepilot','l2_ensure_ticket(text,text,jsonb,text,integer,integer)','EXECUTE') ELSE FALSE END),"
                    "(CASE WHEN EXISTS(SELECT 1 FROM pg_proc WHERE proname='l2_expire_approved') THEN has_function_privilege('mergepilot','l2_expire_approved(text)','EXECUTE') ELSE FALSE END),"
                    "(CASE WHEN EXISTS(SELECT 1 FROM pg_proc WHERE proname='l2_reject_approved') THEN has_function_privilege('mergepilot','l2_reject_approved(text,text)','EXECUTE') ELSE FALSE END),"
                    "(SELECT count(*)::int FROM information_schema.columns WHERE table_schema='public' AND table_name='task_runs'"
                    " AND column_name IN ('l2_next_attempt_at','l2_retry_count','l2_discovery_deadline_at'));")
        ok = cur.fetchone()
    conn.commit()
    _ensure, _expire, _reject, _sched = (ok + (None, None, None, None))[:4] if ok else (None, None, None, None)
    if not (_ensure and _expire and _reject and _sched == 3):
        sys.exit(f"[ctrl] FATAL: B4c/B4c.1 migration 未应用完整(ensure={_ensure} expire={_expire} reject={_reject} sched_cols={_sched}/3);依次应用 m3b_b4.sql + m3b_b4c.sql + m3b_b4c1.sql + m3b_b4c1_1.sql")
    if L2_LEASE_SECONDS < L2_GW_TIMEOUT + 5:
        sys.exit(f"[ctrl] FATAL: L2_LEASE_SECONDS({L2_LEASE_SECONDS}) < L2_GW_TIMEOUT+5({L2_GW_TIMEOUT+5});lease 须 ≥ Gateway 超时 + 安全余量(防双发)")

    if M4F_ENABLED:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
                     to_regclass('public.revision_bindings') IS NOT NULL,
                     to_regclass('public.snapshot_job_outbox') IS NOT NULL,
                     to_regclass('public.skill_job_outbox') IS NOT NULL,
                     to_regprocedure('public.bind_revision(text,text,integer,text,text,text,text)') IS NOT NULL,
                     to_regprocedure('public.enqueue_snapshot_job(text,text)') IS NOT NULL,
                     to_regprocedure('public.enqueue_skill_job(text,text,text,text,text,integer,text,text[])') IS NOT NULL"""
            )
            m4f_ready = cur.fetchone()
        conn.commit()
        if not m4f_ready or not all(m4f_ready):
            sys.exit("[ctrl] FATAL: M4F_ENABLED=1 但 M4-F1 migration/API 未就绪")
        _assert_m4f_snapshot_identity(ensure_m4f_snapshot_pg())

    need_gateway = bool(pending_l2) or L2_MERGE_ENABLED or M4F_ENABLED
    if need_gateway:
        # v2.4 勘误:Candidate(M4F_ONLY_MODE)用独立 GATEWAY_TOKEN(m5coordinator),
        # 不持生产 COORDINATOR_TOKEN。生产 Controller 走 COORDINATOR_TOKEN(向后兼容)。
        _gw_tok = GATEWAY_TOKEN if M4F_ONLY_MODE else COORDINATOR_TOKEN
        _gw_name = "GATEWAY_TOKEN" if M4F_ONLY_MODE else "COORDINATOR_TOKEN"
        if not _gw_tok:
            sys.exit(f"[ctrl] FATAL: 有 {pending_l2} 个未终结审批 run(L2_MERGE_ENABLED={'on' if L2_MERGE_ENABLED else 'off'}),但缺 {_gw_name} → 这些 run 会卡死")
        if not GATEWAY_URL:
            sys.exit("[ctrl] FATAL: 审批流需要 GATEWAY_URL")
        # B4c.1: Gateway TCP 不可达 → DEGRADED_NETWORK(**不 fatal**)。纯 DB 收敛继续;恢复后 circuit breaker 自动放行(无需重启)。
        if not _gateway_reachable():
            _l2_gw_mark_degraded("STARTUP:Gateway TCP 不可达", seconds=L2_RETRY_BASE_SECONDS)
            print(f"[ctrl] DEGRADED_NETWORK: Gateway {GATEWAY_URL} TCP 不可达 → L2 外部调用降级(纯 DB 收敛继续;恢复后自动续)")

    mode = "on" if L2_MERGE_ENABLED else "off"
    dg = " DEGRADED_NETWORK" if _l2_gw_degraded() else ""
    print(f"[ctrl] L2_MERGE_ENABLED={mode};未终结审批 run={pending_l2};Gateway={GATEWAY_URL if need_gateway else '(未启用)'};reconcile_min_age={L2_RECONCILE_MIN_AGE_SECONDS}s;budget={L2_MAINTENANCE_BUDGET_SECONDS}s×{L2_MAINTENANCE_MAX_ITEMS}{dg}")


# ── B4c L2 主循环函数(B4c-1.1:权威身份 + 原子 CAS + branch 双源)──
# 事务边界(B4c-0.1 #4 / B4c-1.1 P1-3):Gateway 调用不持 PG 事务;
#   binding 写入 + 阶段推进 + attempts++ 同一短事务(SELECT task FOR UPDATE + CAS current_stage=l2_binding)。
def _revision_cut_run_id(run_id, repo, pr_number, head_sha):
    """Deterministic child run id for an externally changed bound revision."""
    material = f"{run_id}\x00{repo}\x00{pr_number}\x00{head_sha}".encode("utf-8")
    return f"{run_id[:72]}-rev-{hashlib.sha256(material).hexdigest()[:16]}"


def _atomic_advance(run_id, status, info, candidate=None):
    """B4c-1.1 P1-3:原子推进 task 状态(单短事务,advisory_xact_lock per run + SELECT FOR UPDATE + CAS)。
    status 分支:FOUND/UPDATED(写 binding + l2_awaiting_ticket)/ NOT_FOUND(attempts++达阈值→HOLD)/
      AMBIGUOUS|HOLD_*(→HOLD)/ RETRY|RETRY_HEAD_UNSETTLED|CONCURRENT(不动,下轮重试)。
    candidate:FOUND 时的权威绑定数据{pr_num,head_sha,head_ref,base_ref,repo}。
    返回 (status, info)(写库后)。CONCURRENT=CAS 失败(task 已被并发推进)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("disc:" + run_id,))
            cur.fetchone()
            cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            t = cur.fetchone()
            if not t:
                conn.commit(); return ("CONCURRENT", {"reason": "task 消失"})
            ar, status_db, cstage = t
            cas_ok = bool(ar) and status_db == 'APPROVAL_PENDING' and cstage == 'l2_binding'
            if status in ("RETRY", "RETRY_HEAD_UNSETTLED", "RETRY_INCONSISTENT"):
                # B4c.1.1 #1:gateway 瞬时 → 重新排队(retry_count++/next_attempt_at=now+backoff),防老任务占满 LIMIT 饿死后续
                if cas_ok:
                    cur.execute("SELECT l2_retry_count FROM task_runs WHERE run_id=%s", (run_id,))
                    _rc = cur.fetchone(); _rc = _rc[0] if _rc else 0
                    cur.execute("UPDATE task_runs SET l2_retry_count=l2_retry_count+1, l2_retry_reason=%s, l2_next_attempt_at=now()+make_interval(secs=>%s), updated_at=now() WHERE run_id=%s",
                                (status, _l2_backoff_seconds(_rc), run_id))
                conn.commit()
                return (status, info)
            if status == "CONCURRENT" or not cas_ok:
                conn.commit()
                return ("CONCURRENT", {"reason": f"task 已变 status={status_db} stage={cstage}", **(info or {})})
            if status in ("FOUND", "UPDATED") and candidate:
                pr_num = candidate["pr_num"]; head_sha = candidate["head_sha"]
                head_ref = candidate["head_ref"]; base_ref = candidate["base_ref"]; repo = candidate["repo"]
                cur.execute("SELECT binding_id, pr_number, fix_branch, base_branch, repo, head_sha FROM run_pr_bindings WHERE run_id=%s", (run_id,))
                ex = cur.fetchone()
                if ex:
                    ebid, epr, ebr, eba, erepo, esha = ex
                    if epr == pr_num and ebr == head_ref and eba == base_ref and erepo == repo:
                        if esha != head_sha:
                            # M4-F revision cut: a run with immutable revision evidence
                            # must never have its PR identity/head rewritten in place.
                            cur.execute("SELECT to_regclass('public.revision_bindings') IS NOT NULL")
                            has_revision_table = bool(cur.fetchone()[0])
                            bound = False
                            if has_revision_table:
                                cur.execute(
                                    "SELECT EXISTS(SELECT 1 FROM revision_bindings WHERE run_id=%s)",
                                    (run_id,),
                                )
                                bound = bool(cur.fetchone()[0])
                            if bound:
                                child_run = _revision_cut_run_id(run_id, repo, pr_num, head_sha)
                                child_binding = "bnd-" + str(uuid.uuid4())
                                cur.execute(
                                    """INSERT INTO task_runs(
                                           run_id,room_id,repo,pr_number,branch,status,
                                           current_stage,attempt,approval_required,verdict,last_error)
                                       SELECT %s,room_id,%s,%s,branch,'APPROVAL_PENDING',
                                              'l2_awaiting_ticket',attempt,approval_required,
                                              NULL,NULL
                                       FROM task_runs WHERE run_id=%s
                                       ON CONFLICT(run_id) DO NOTHING""",
                                    (child_run, repo, pr_num, run_id),
                                )
                                cur.execute(
                                    """INSERT INTO run_pr_bindings(
                                           binding_id,run_id,repo,pr_number,fix_branch,
                                           base_branch,head_sha)
                                       VALUES(%s,%s,%s,%s,%s,%s,%s)
                                       ON CONFLICT(run_id) DO NOTHING""",
                                    (child_binding, child_run, repo, pr_num, head_ref, base_ref, head_sha),
                                )
                                cur.execute(
                                    """UPDATE task_runs
                                       SET status='HOLD',current_stage='revision_superseded',
                                           last_error=%s,updated_at=now()
                                       WHERE run_id=%s""",
                                    (f"external head drift cut to {child_run}", run_id),
                                )
                                out = (
                                    "REVISION_CUT",
                                    {
                                        "prior_run_id": run_id,
                                        "run_id": child_run,
                                        "head_sha": head_sha,
                                        "pr_number": pr_num,
                                    },
                                )
                            else:
                                cur.execute("UPDATE run_pr_bindings SET head_sha=%s, repo=%s, recorded_at=now() WHERE binding_id=%s", (head_sha, repo, ebid))
                                out = ("UPDATED", {"binding_id": ebid, "head_sha": head_sha, "pr_number": pr_num})
                        else:
                            out = ("FOUND", {"binding_id": ebid, "head_sha": esha, "pr_number": pr_num})
                        if out[0] != "REVISION_CUT":
                            cur.execute("UPDATE task_runs SET current_stage='l2_awaiting_ticket', l2_discovery_attempts=0, l2_retry_count=0, l2_retry_reason=NULL, l2_discovery_deadline_at=NULL, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (run_id,))
                    else:
                        # B4c-1.2 P1-1:身份冲突 → 同事务置 HOLD(否则每 tick 重复冲突)
                        cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                                    (f"HOLD_BINDING_CONFLICT existing_pr={epr} existing_branch={ebr} new_pr={pr_num} new_branch={head_ref}", run_id))
                        out = ("HOLD_BINDING_CONFLICT", {"existing": ebid, "existing_pr": epr, "new_pr": pr_num,
                                                          "reason": "PR 身份(pr/branch/base/repo)变更,不静默覆盖"})
                else:
                    bid = "bnd-" + str(uuid.uuid4())
                    cur.execute("""INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s)""", (bid, run_id, repo, pr_num, head_ref, base_ref, head_sha))
                    out = ("FOUND", {"binding_id": bid, "head_sha": head_sha, "pr_number": pr_num})
                    cur.execute("UPDATE task_runs SET current_stage='l2_awaiting_ticket', l2_discovery_attempts=0, l2_retry_count=0, l2_retry_reason=NULL, l2_discovery_deadline_at=NULL, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (run_id,))
            elif status == "NOT_FOUND":
                # B4c.1: 发现期限(l2_discovery_deadline_at,惰性初始化 now+timeout)代替旧 L2_DISCOVERY_MAX 计数 HOLD;
                #   l2_discovery_attempts 仅审计;公平退避 l2_next_attempt_at=now+backoff(retry_count++)。
                cur.execute("SELECT l2_retry_count FROM task_runs WHERE run_id=%s", (run_id,))
                rc = cur.fetchone()[0] or 0
                cur.execute("""UPDATE task_runs
                               SET l2_discovery_attempts = l2_discovery_attempts + 1,
                                   l2_retry_count = l2_retry_count + 1, l2_retry_reason = 'NOT_FOUND',
                                   l2_discovery_deadline_at = COALESCE(l2_discovery_deadline_at, now() + make_interval(secs => %s)),
                                   l2_next_attempt_at = now() + make_interval(secs => %s), updated_at = now()
                               WHERE run_id=%s""", (L2_DISCOVERY_TIMEOUT_SECONDS, _l2_backoff_seconds(rc), run_id))
                cur.execute("SELECT (l2_discovery_deadline_at < now()), l2_discovery_attempts FROM task_runs WHERE run_id=%s", (run_id,))
                past, attempts = cur.fetchone()
                if past:
                    cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                                (f"无 fix PR(达发现期限 {L2_DISCOVERY_TIMEOUT_SECONDS}s,attempts={attempts})", run_id))
                    out = ("HOLD", {"reason": f"discovery deadline reached (attempts={attempts})", "attempts": attempts})
                else:
                    out = ("NOT_FOUND", {"attempts": attempts, "deadline_s": L2_DISCOVERY_TIMEOUT_SECONDS})
            else:   # AMBIGUOUS | HOLD_*
                reason = status + (" " + json.dumps(info, default=str) if info else "")
                cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s", (reason[:300], run_id))
                out = (status, info)
        conn.commit()
        return out
    except Exception as e:
        conn.rollback()
        return ("RETRY", {"reason": f"_atomic_advance 异常: {e}"})


def discover_binding_for_run(run_id, deadline=None):
    """B4c-1.2:GitHub 权威绑定发现 + 原子推进。返回 (status, info)。
    B4c.1.1 #3:包一层 catcher —— GatewayGlobalDegraded(坏 token/角色路径)→ 打开 circuit breaker
      + 返回 ("DEGRADED", ...),由 initiate 停止本 tick discover(防连环撞 Gateway)。"""
    import gateway_client
    try:
        result = _discover_binding_for_run_inner(run_id, deadline)
        _l2_gw_ok()   # B4c.1.3 P2:discover 成功 → 清 breaker failure_count(恢复后重新计数)
        return result
    except gateway_client.GatewayGlobalDegraded as e:
        _l2_gw_mark_degraded(e.reason_code)
        print(f"[ctrl][L2] {run_id} discover: Gateway GLOBAL_DEGRADED({e.reason_code}) → circuit breaker")
        return ("DEGRADED", {"reason": e.reason_code})
    except gateway_client.GatewayUnavailable as e:
        # B4c.1.2 #2:网络/401 瞬时 → 重排该 task + 打开 breaker(本 tick 停 discover,防连环撞)
        _l2_requeue(run_id, "gw unavailable", "l2_binding")
        _l2_gw_mark_degraded("UNAVAILABLE", seconds=L2_RETRY_BASE_SECONDS)
        print(f"[ctrl][L2] {run_id} discover: Gateway unavailable({str(e)[:50]}) → 重排 + circuit breaker")
        return ("DEGRADED", {"reason": "gateway unavailable"})
    except gateway_client.GatewayDenied as e:
        # B4c.1.2 #2:discovery 级确定性拒绝(REPO_NOT_ALLOWED 等)→ HOLD(收敛,不无限重试)
        _c = ensure_pg()
        with _c.cursor() as _cur:
            _cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_binding_failed', last_error=%s, updated_at=now() WHERE run_id=%s AND status='APPROVAL_PENDING' AND current_stage='l2_binding'", (f"gateway denied:{e.reason_code}", run_id))
        _c.commit()
        print(f"[ctrl][L2] {run_id} discover: Gateway denied({e.reason_code}) → HOLD(收敛,不无限重试)")
        return ("HOLD", {"reason": f"gateway denied:{e.reason_code}"})


def _gw_timeout_for(deadline):
    """B4c.1.2 #3:剩余预算内的单次 Gateway 超时 = min(L2_GW_TIMEOUT, 剩余),**无 5s 下限**(下限 0.1s 防 0)。
    调用方 loop 已用 _budget_exhausted 确保不在 <1s 时启动新项;故运行中剩余≥0.1s,单调用 ≤ 剩余。"""
    return min(L2_GW_TIMEOUT, max(deadline - time.monotonic(), 0)) if deadline else L2_GW_TIMEOUT


def _budget_exhausted(deadline):
    """B4c.1.2 #3:剩余预算不足以启动新项(<1s)或已过。loop 顶用,防启动后单调用超整轮预算。"""
    return deadline is not None and (deadline - time.monotonic()) < 1.0


def _tick_ok(budget):
    """B4c.1.4:检查 budget 是否允许新项(**不扣减**)。空队列/锁竞争前用,防空阶段消耗预算饿死后续。"""
    return budget is None or budget[0] > 0


def _tick_use(budget):
    """B4c.1.4:确认取得真实工作(advisory lock / outbox row / fetched item)后扣减一个额度。"""
    if budget is not None and budget[0] > 0:
        budget[0] -= 1


def _discover_binding_for_run_inner(run_id, deadline=None):
    """B4c-1.2:GitHub 权威绑定发现 + 原子推进。返回 (status, info)。
    P1-2 全字段严格(gateway_read_pr/branch 已强制 40hex + 字段必存在);
    P1-3 binding 写入+阶段推进同一短事务(_atomic_advance CAS),冲突也同事务置 HOLD;
    P1-4 PR head.sha == branch ref sha 双源校验;
    B4c-1.2 P1-3 已有 binding:**直验**其 PR(identity+state+branch 双源 SHA),list 仅用于检测**额外**匹配 PR;
      已绑定 open PR + list 返回 0 匹配(缓存/瞬态不一致)→ RETRY_INCONSISTENT(不累计 NOT_FOUND);
      list 出现额外匹配 PR → AMBIGUOUS。"""
    import gateway_client

    def validate_pr(pr_num):
        """权威读回 + 严格身份 + branch 双源。返回 (status, info, candidate)。status=OK 时 candidate 含绑定数据。"""
        rstatus, prd = gateway_client.gateway_read_pr(owner, repo_name, pr_num, timeout=_gw_timeout_for(deadline))
        if rstatus == "RETRY":
            return ("RETRY", {"reason": "pull_request_read 失败"}, None)
        head_sha = prd["head_sha"]; base_ref = prd["base"]; state = prd["state"]
        head_ref = prd["head_ref"]; head_repo = prd["head_repo_full_name"]
        if state != "open":
            return ("HOLD_PR_NOT_OPEN", {"pr_number": pr_num, "state": state}, None)
        if base_ref != "main":
            return ("HOLD_IDENTITY", {"reason": f"base_ref={base_ref} 非 main", "pr_number": pr_num}, None)
        if not head_ref.startswith(f"fix/{run_id}-"):
            return ("HOLD_IDENTITY", {"reason": f"head_ref={head_ref} 不匹配 fix/{run_id}-", "pr_number": pr_num}, None)
        if prd["pr_number"] != pr_num:
            return ("RETRY", {"reason": "PR 返回 number 与请求不一致"}, None)
        if head_repo != repo:
            return ("HOLD_IDENTITY", {"reason": f"head.repo={head_repo} != 目标 {repo}(防 fork PR)", "pr_number": pr_num}, None)
        bstatus, branch_sha = gateway_client.gateway_read_branch(owner, repo_name, head_ref, timeout=_gw_timeout_for(deadline), deadline=deadline)
        if bstatus == "RETRY":
            return ("RETRY", {"reason": "list_branches 失败/未找到 branch"}, None)
        if branch_sha != head_sha:
            return ("RETRY_HEAD_UNSETTLED", {"pr_head": head_sha, "branch_sha": branch_sha, "reason": "PR head.sha != branch ref sha"}, None)
        return ("OK", None, {"pr_num": pr_num, "head_sha": head_sha, "head_ref": head_ref, "base_ref": base_ref, "repo": repo})

    # Phase 1: repo + existing binding(短事务)
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("SELECT repo FROM task_runs WHERE run_id=%s", (run_id,))
        row = cur.fetchone()
        cur.execute("SELECT binding_id, pr_number FROM run_pr_bindings WHERE run_id=%s", (run_id,))
        existing = cur.fetchone()
    conn.commit()
    if not row or not row[0]:
        return _atomic_advance(run_id, "HOLD_NO_REPO", {"reason": "task 无 repo"})
    repo = row[0]
    owner, _, repo_name = repo.partition("/")
    if not owner or not repo_name:
        return _atomic_advance(run_id, "HOLD_NO_REPO", {"reason": f"repo 非法: {repo}"})

    if existing:
        # 已有 binding:直验其 PR(identity+state+branch 双源),不靠 list 判存在
        ebid, epr = existing
        st, info, cand = validate_pr(epr)
        if st != "OK":
            info = dict(info or {}); info.setdefault("existing", ebid)
            return _atomic_advance(run_id, st, info)
        # list 仅检测**额外**匹配 PR(防 AMBIGUOUS 漏检)
        lstatus, prs = gateway_client.gateway_list_prs(owner, repo_name, run_id, timeout=_gw_timeout_for(deadline), deadline=deadline)
        if lstatus == "RETRY":
            return _atomic_advance(run_id, "RETRY", {"reason": "list_pull_requests 失败(检测额外 PR)"})
        extra = [p["number"] for p in prs if p.get("number") != epr]
        if extra:
            return _atomic_advance(run_id, "AMBIGUOUS", {"bound_pr": epr, "extra_prs": extra, "count": len(prs)})
        # 已绑定 open PR 但 list 返回 0 匹配 → 缓存/瞬态不一致 → RETRY_INCONSISTENT(不累计 NOT_FOUND)
        if len(prs) == 0:
            return _atomic_advance(run_id, "RETRY_INCONSISTENT", {"reason": f"bound PR {epr} open 但 list 返回 0 匹配", "bound_pr": epr})
        return _atomic_advance(run_id, "FOUND", {"pr_number": epr}, candidate=cand)

    # 无 existing binding → list 主导
    lstatus, prs = gateway_client.gateway_list_prs(owner, repo_name, run_id, timeout=_gw_timeout_for(deadline), deadline=deadline)
    if lstatus == "RETRY":
        return _atomic_advance(run_id, "RETRY", {"reason": "list_pull_requests 失败"})
    if lstatus == "AMBIGUOUS":
        return _atomic_advance(run_id, "AMBIGUOUS", {"count": len(prs), "prs": [p.get("number") for p in prs]})
    if lstatus == "NOT_FOUND":
        return _atomic_advance(run_id, "NOT_FOUND", {})
    pr = prs[0]; pr_num = pr["number"]
    st, info, cand = validate_pr(pr_num)
    if st != "OK":
        return _atomic_advance(run_id, st, info)
    return _atomic_advance(run_id, "FOUND", {"pr_number": pr_num}, candidate=cand)


def _hold_ticket_atomic(run_id, status, reason):
    """B4c-2.2:建票异常路径(22023 第二事务)原子置 task HOLD。**完整 CAS**:
    approval_required=TRUE AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_ticket'
    (不只检查 stage,防覆盖已非 APPROVAL_PENDING 的任务)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ticket:" + run_id,))
            cur.fetchone()
            cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            t = cur.fetchone()
            if not t:
                conn.commit(); return (status, {"reason": reason, "note": "task 消失"})
            ar, status_db, cstage = t
            if not (ar and status_db == "APPROVAL_PENDING" and cstage == "l2_awaiting_ticket"):
                conn.commit(); return ("CONCURRENT", {"reason": f"task 已变 status={status_db} stage={cstage}", "orig": status})
            cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_ticket_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                        (f"{status}: {reason}"[:300], run_id))
        conn.commit()
        return (status, {"reason": reason})
    except Exception as e:
        conn.rollback()
        return ("RETRY", {"reason": f"_hold_ticket_atomic 异常: {e}"})


def create_ticket_for_run(run_id):
    """B4c-2(+2.2):固化双源 SHA(承 discover)→ canonical_payload + args_hash → l2_ensure_ticket 幂等建票
    (活动票返回旧的;同事务 outbox)→ 推进 l2_awaiting_approval。
    **B4c-2.2 P1:全部在同一锁事务内**(ticket:<run_id> xact lock + SELECT task FOR UPDATE + 完整 CAS +
    binding 查询 + HOLD/l2_ensure_ticket/推进),无跨事务窗口:
      无 binding → 同事务 HOLD_NO_BINDING;
      l2_ensure_ticket 抛 22023 → (rollback 后)_hold_ticket_atomic 第二事务 HOLD_TICKET_CONFLICT;
      瞬时 DB 错 → RETRY。
    返回 (status, info)。"""
    import gateway_client
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ticket:" + run_id,))
            cur.fetchone()
            # 完整 CAS(同事务)
            cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            t = cur.fetchone()
            if not t:
                conn.commit(); return ("CONCURRENT", {"reason": "task 消失"})
            ar, status, cstage = t
            if not (ar and status == "APPROVAL_PENDING" and cstage == "l2_awaiting_ticket"):
                conn.commit(); return ("CONCURRENT", {"reason": f"task 已变 status={status} stage={cstage}"})
            # binding 查询 **在同一事务内**(B4c-2.2 P1)
            cur.execute("SELECT binding_id, repo, pr_number, base_branch, head_sha FROM run_pr_bindings WHERE run_id=%s", (run_id,))
            b = cur.fetchone()
            if not b:
                cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_ticket_failed', last_error=%s, updated_at=now() WHERE run_id=%s",
                            ("HOLD_NO_BINDING: 无 binding(应先经 l2_binding 发现)"[:300], run_id))
                conn.commit()
                return ("HOLD_NO_BINDING", {"reason": "无 binding(应先经 l2_binding 发现)"})
            binding_id, repo, pr_num, base_ref, head_sha = b
            owner, _, repo_name = repo.partition("/")
            payload = {"owner": owner, "repo": repo_name, "pullNumber": int(pr_num),
                       "commit_title": f"Merge fix {run_id}", "merge_method": "squash"}
            args_hash = gateway_client.canonical_args_hash(payload)
            cur.execute("SELECT l2_ensure_ticket(%s, 'merge', %s::jsonb, %s, 24, 1)",
                        (binding_id, json.dumps(payload), args_hash))
            ticket_id = cur.fetchone()[0]
            cur.execute("UPDATE task_runs SET current_stage='l2_awaiting_approval', l2_retry_count=0, l2_retry_reason=NULL, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (run_id,))
        conn.commit()
        return ("CREATED", {"ticket_id": ticket_id, "binding_id": binding_id,
                            "args_hash": args_hash[:12], "pr_number": pr_num, "payload": payload})
    except psycopg2.Error as e:
        # B4c-2.1 P1-2:按 pgcode 分类。22023(payload/hash/TTL 确定性冲突)→ HOLD_TICKET_CONFLICT(第二事务,完整 CAS)
        conn.rollback()
        if getattr(e, "pgcode", None) == "22023":
            return _hold_ticket_atomic(run_id, "HOLD_TICKET_CONFLICT", f"l2_ensure_ticket 22023: {str(e)[:200]}")
        _l2_requeue(run_id, f"建票DB:{e.pgcode}", "l2_awaiting_ticket")   # B4c.1.1 #1:重新排队防饿死
        return ("RETRY", {"reason": f"建票 DB 错误(pgcode={e.pgcode}): {str(e)[:150]}"})
    except Exception as e:
        conn.rollback()
        _l2_requeue(run_id, "建票异常", "l2_awaiting_ticket")   # B4c.1.1 #1
        return ("RETRY", {"reason": f"建票失败: {e}"})


def initiate_l2_pending(deadline=None, budget=None):
    """扫 approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_binding':
    B4c-1.1 绑定发现。**per-run session advisory lock**(pg_advisory_lock)序列化多 Controller 发现,
    防一次真实 NOT_FOUND 被并发累计多次。discover 内部 _atomic_advance 做 CAS 原子写推进。"""
    if _l2_gw_degraded():
        return   # B4c.1.1 #3:circuit breaker 打开 → 跳过本 tick discover(防连环撞 Gateway)
    conn = ensure_pg()
    with conn.cursor() as cur:
        # B4c.1 公平调度 + B4c.1.1 #4 单循环预算:仅到期候选,按到期/更新序,LIMIT MAX_ITEMS
        cur.execute("""SELECT run_id FROM task_runs
                       WHERE approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_binding'
                         AND l2_next_attempt_at <= now()
                       ORDER BY l2_next_attempt_at, updated_at, run_id LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
        runs = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT run_id FROM task_runs
                       WHERE approval_required AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_ticket'
                         AND l2_next_attempt_at <= now()
                       ORDER BY l2_next_attempt_at, updated_at, run_id LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
        ticket_runs = [r[0] for r in cur.fetchall()]
    conn.commit()
    for run_id in runs:
        if _budget_exhausted(deadline) or not _tick_ok(budget):
            break   # B4c.1 工作预算到期,剩余候选下 tick 处理(已持久化 next_attempt_at)
        if _l2_gw_degraded():
            break   # B4c.1.1 #3:discover 途中 breaker 打开 → 停
        # per-run session advisory lock(跨 Controller 序列化整个 discover;crash 时连接断开自动释放)
        lc = ensure_pg()
        try:
            with lc.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", ("disc:" + run_id,))
                got = cur.fetchone()[0]
            lc.commit()
            if not got:
                continue   # B4c.1:另一 Controller 持锁 → 跳过(不阻塞;持锁者处理)
        except Exception as e:
            print(f"[ctrl][L2] {run_id} advisory_lock 异常: {e}"); continue
        _tick_use(budget)   # B4c.1.4:advisory lock 取得 = 确认工作 → 扣额度
        try:
            st, info = discover_binding_for_run(run_id, deadline)
            if st in ("FOUND", "UPDATED"):
                print(f"[ctrl][L2] {run_id} 绑定 {st}: {str(info.get('binding_id',''))[:16]} pr={info.get('pr_number')} head={str(info.get('head_sha',''))[:12]} → l2_awaiting_ticket")
            elif st == "NOT_FOUND":
                print(f"[ctrl][L2] {run_id} 绑定 0 PR(attempts={info.get('attempts')},deadline={info.get('deadline_s')}s),退避重试")
            elif st == "HOLD":
                print(f"[ctrl][L2] {run_id} HOLD: {info}")
            elif st in ("RETRY", "RETRY_HEAD_UNSETTLED", "RETRY_INCONSISTENT"):
                print(f"[ctrl][L2] {run_id} {st}({info.get('reason','')}),不累加,下轮重试")
            elif st == "CONCURRENT":
                print(f"[ctrl][L2] {run_id} CONCURRENT({info.get('reason','')}),跳过")
            elif st == "DEGRADED":
                print(f"[ctrl][L2] {run_id} discover DEGRADED({info.get('reason','')}) → 本 tick 停 discover")
                break   # B4c.1.1 #3:breaker 已开,finally 解锁后退出 disc 循环
            else:   # AMBIGUOUS / HOLD_PR_NOT_OPEN / HOLD_IDENTITY / HOLD_NO_REPO / HOLD_BINDING_CONFLICT
                print(f"[ctrl][L2] {run_id} HOLD: {st} {info}")
        finally:
            try:
                lc2 = ensure_pg()
                with lc2.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("disc:" + run_id,))
                    cur.fetchone()
                lc2.commit()
            except Exception:
                pass

    # B4c-2:l2_awaiting_ticket → 幂等建票(per-run session advisory lock)
    for run_id in ticket_runs:
        if _budget_exhausted(deadline) or not _tick_ok(budget):
            break   # B4c.1 工作预算到期
        if _l2_gw_degraded():
            break   # B4c.1.1 #3:circuit breaker 打开 → 停建票
        lc = ensure_pg()
        try:
            with lc.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", ("ticket:" + run_id,))
                got = cur.fetchone()[0]
            lc.commit()
            if not got:
                continue   # B4c.1:另一 Controller 持锁 → 跳过(不阻塞)
        except Exception as e:
            print(f"[ctrl][L2] {run_id} ticket advisory_lock 异常: {e}"); continue
        _tick_use(budget)   # B4c.1.4:advisory lock 取得 = 确认工作 → 扣额度
        try:
            st, info = create_ticket_for_run(run_id)
            if st == "CREATED":
                print(f"[ctrl][L2] {run_id} 建票 {st}: ticket={str(info.get('ticket_id',''))[:20]} pr={info.get('pr_number')} hash={info.get('args_hash')} → l2_awaiting_approval")
            elif st == "CONCURRENT":
                print(f"[ctrl][L2] {run_id} 建票 CONCURRENT({info.get('reason','')}),跳过")
            elif st in ("HOLD_NO_BINDING", "HOLD_TICKET_CONFLICT"):
                print(f"[ctrl][L2] {run_id} 建票 HOLD: {st} {info}")
            else:   # RETRY
                print(f"[ctrl][L2] {run_id} 建票 {st}({info.get('reason','')}),下轮重试")
        finally:
            try:
                lc2 = ensure_pg()
                with lc2.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("ticket:" + run_id,))
                    cur.fetchone()
                lc2.commit()
            except Exception:
                pass


L2_ACTION_TOOL = {"merge": "merge_pull_request", "close": "update_pull_request"}


def _advance_outbox_by_approval(oid, ticket_id, action, outcome):
    """B4c-3 边界② + B4c.1 结构化 outcome:Gateway 返回/超时后读 **approvals 权威态**(l2_* 已写 status)。
    **关键顺序**:先读 approval 状态;只有**仍 APPROVED**(Gateway 未 claim)时才按 outcome 分类;
    approval 已迁移(USED/FAILED/UNKNOWN/EXECUTING)时**绝不**被 outcome 覆盖(复审 #4)。
    返回 "DEGRADED"(drain 应本 tick 停消费)或 None。
    action-aware:merge USED→task MERGED/l2_done;close USED→task HOLD/verified-closed。
    task 终态用完整 CAS(approval_required+APPROVAL_PENDING+l2_awaiting_approval);CAS 失败→CONCURRENT_STATE_CHANGE。
    边界③:UNKNOWN/EXECUTING/APPROVED 绝不自动重 merge。"""
    if outcome is None:
        outcome = GatewayOutcome("SUCCESS")   # 兼容对账收敛调用(无 outcome)
    conn = ensure_pg()
    signal = None
    with conn.cursor() as cur:
        cur.execute("SELECT status, result_sha, run_id FROM approvals WHERE ticket_id=%s", (ticket_id,))
        r = cur.fetchone()
        if not r:
            conn.commit(); return None
        astatus, result_sha, run_id = r
        cas_clause = "approval_required=TRUE AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_approval'"
        if astatus == "APPROVED":
            # Gateway 未 claim(approval 仍 APPROVED)→ 按 outcome 分类(绝不覆盖已迁移态)
            cur.execute("SELECT attempts FROM policy_action_outbox WHERE id=%s", (oid,))
            _ar = cur.fetchone(); delay = _l2_backoff_seconds(_ar[0] if _ar else 0)
            if outcome.kind == "TICKET_DENY":
                # B4c.1.3:完整三字段 CAS —— 锁 task FOR UPDATE + 校验 approval_required+status+current_stage;
                #   再 l2_reject_approved(approval CAS);task UPDATE 断言 rowcount==1;**任一不命中 → 回滚,outbox 不动**。
                cur.execute("SELECT approval_required, status, current_stage FROM task_runs WHERE run_id=%s FOR UPDATE", (run_id,))
                _tr = cur.fetchone()
                if not _tr or not (bool(_tr[0]) and _tr[1] == 'APPROVAL_PENDING' and _tr[2] == 'l2_awaiting_approval'):
                    conn.rollback()   # task 已并发迁移或缺 approval_required → 不终结
                    return None
                cur.execute("SELECT l2_reject_approved(%s,%s)", (ticket_id, outcome.reason_code))
                if not cur.fetchone()[0]:
                    conn.rollback()   # approval 已并发迁移(claim/恰好过期)→ 不终结(task/outbox 不动)
                    return None
                cur.execute(f"UPDATE task_runs SET status='HOLD', current_stage='l2_drain_denied', last_error=%s, updated_at=now() WHERE run_id=%s AND {cas_clause}", (f"preclaim denied:{outcome.reason_code}", run_id))
                if cur.rowcount != 1:
                    conn.rollback()   # task CAS 未命中(并发)→ 不终结 outbox
                    return None
                cur.execute("UPDATE policy_action_outbox SET status='FAILED', completed_at=now(), last_error_code=%s, error=%s WHERE id=%s", (outcome.reason_code, f"preclaim denied:{outcome.reason_code}", oid))
            elif outcome.kind == "GLOBAL_DEGRADED":
                # 全局配置故障:退回 PENDING_DISPATCH + 退避;signal 让 drain 本 tick 停消费
                cur.execute("UPDATE policy_action_outbox SET status='PENDING_DISPATCH', last_error_code=%s, error=%s, next_retry_at=now()+make_interval(secs=>%s) WHERE id=%s", (outcome.reason_code, f"global degraded:{outcome.reason_code}", delay, oid))
                signal = "DEGRADED"
            else:  # TRANSIENT(网络/超时/L2_DB_UNAVAILABLE)或 SUCCESS-race(未 claim)→ 退避重试
                # approval 留 APPROVED,outbox 留 DISPATCHED,设 next_retry_at(attempts 已在领取 +1,等待期不再 +)
                cur.execute("UPDATE policy_action_outbox SET last_error_code=%s, error=%s, next_retry_at=now()+make_interval(secs=>%s) WHERE id=%s", (outcome.kind, outcome.detail[:200], delay, oid))
        elif astatus == "USED":
            cur.execute("UPDATE policy_action_outbox SET status='SUCCEEDED', result_sha=%s, completed_at=now() WHERE id=%s", (result_sha, oid))
            if action == "close":
                cur.execute(f"UPDATE task_runs SET status='HOLD', current_stage='verified-closed', updated_at=now() WHERE run_id=%s AND {cas_clause}", (run_id,))
            else:  # merge
                cur.execute(f"UPDATE task_runs SET status='MERGED', current_stage='l2_done', updated_at=now() WHERE run_id=%s AND {cas_clause}", (run_id,))
            if cur.rowcount == 0:
                cur.execute("UPDATE policy_action_outbox SET error=%s WHERE id=%s", ("CONCURRENT_STATE_CHANGE: task 已脱离 APPROVAL_PENDING/l2_awaiting_approval,未覆盖"[:200], oid))
        elif astatus == "FAILED":
            # Gateway claim 后失败(approval 已 FAILED):task HOLD(l2_drain_failed)+ outbox FAILED
            cur.execute(f"UPDATE task_runs SET status='HOLD', current_stage='l2_drain_failed', last_error=%s, updated_at=now() WHERE run_id=%s AND {cas_clause}", ((f"{action} FAILED: {outcome.detail or ''}")[:300], run_id))
            err_msg = (outcome.detail or f"{action} FAILED")
            if cur.rowcount == 0:
                err_msg = f"{err_msg} | CONCURRENT_STATE_CHANGE: task 未同步(已脱离 APPROVAL_PENDING/l2_awaiting_approval)"
            cur.execute("UPDATE policy_action_outbox SET status='FAILED', completed_at=now(), last_error_code=%s, error=%s WHERE id=%s", ("CLAIM_FAILED", err_msg[:300], oid))
        elif astatus == "UNKNOWN":
            cur.execute("UPDATE policy_action_outbox SET status='UNKNOWN', error=%s WHERE id=%s", ((outcome.detail or "marked unknown")[:200], oid))
            # task 留 APPROVAL_PENDING,交对账(绝不重 merge)
        # EXECUTING → outbox 留 DISPATCHED(不动),lease/对账兜底
    conn.commit()
    print(f"[ctrl][L2] drain ticket={ticket_id[:16]} action={action} → approval={astatus}" + (f" outcome={outcome.kind}/{outcome.reason_code}" if outcome and outcome.kind != "SUCCESS" else ""))
    return signal


def drain_l2_outbox(deadline=None, budget=None):
    """B4c-3 lease drain + B4c.1 typed outcome / 退避 / 确定性拒绝 / circuit breaker / 工作预算。
    派发条件:APPROVED 且 expires_at>now 且 (next_retry_at IS NULL OR next_retry_at<=now)。
    边界:① 领取事务调 Gateway 前提交;② 读 approvals.status 权威态;③ UNKNOWN/EXECUTING 不重 merge;
          ④ 仅 approval 仍 APPROVED 时按 outcome 分类。attempts 每次真实派发 +1;等待期不 +1。
    circuit breaker:降级期(_l2_gw_degraded)整体跳过;TRANSIENT/GLOBAL_DEGRADED → 打开 breaker + 本 tick 停;
      SUCCESS → 关 breaker。工作预算:deadline(run_forever 传)到期不领下一条;单次 GW 超时 ≤ 剩余预算。"""
    import gateway_client
    if _l2_gw_degraded():
        print(f"[ctrl][L2] drain 跳过:Gateway degraded({_L2_GW['last_error'][:50]}),待恢复")
        return
    for _ in range(L2_MAINTENANCE_MAX_ITEMS):
        if _budget_exhausted(deadline) or not _tick_ok(budget):
            break
        conn = ensure_pg()
        with conn.cursor() as cur:
            cur.execute("""SELECT o.id, o.ticket_id, a.canonical_payload, a.action
                           FROM policy_action_outbox o JOIN approvals a ON o.ticket_id=a.ticket_id
                           WHERE a.status='APPROVED' AND a.expires_at > now()
                             AND (o.next_retry_at IS NULL OR o.next_retry_at <= now())
                             AND ( (o.status='PENDING_DISPATCH')
                                OR (o.status='DISPATCHED' AND o.lease_expires_at IS NOT NULL AND o.lease_expires_at < now()) )
                           ORDER BY o.id FOR UPDATE SKIP LOCKED LIMIT 1""")
            item = cur.fetchone()
            if not item:
                conn.commit(); break
            oid, ticket_id, payload, action = item
            _tick_use(budget)   # B4c.1.4:确认取得 outbox 行 → 扣额度(空队列不扣)
            # 每次真实派发 attempts +1(首派 + lease 重派;SKIP LOCKED 未取得的不在 items,不加);重置 next_retry_at=now(立即再合格;outbox.next_retry_at NOT NULL)
            cur.execute("""UPDATE policy_action_outbox SET status='DISPATCHED',
                             lease_expires_at = now() + make_interval(secs => %s),
                             dispatched_at = now(), attempts = attempts + 1, next_retry_at = now()
                           WHERE id=%s""", (L2_LEASE_SECONDS, oid))
        conn.commit()   # 边界①:提交领取(逐条),释放 FOR UPDATE,再调 Gateway
        gw_to = _gw_timeout_for(deadline)   # B4c.1.3:统一用 _gw_timeout_for(≤剩余,无 5s 下限)
        tool = L2_ACTION_TOOL.get(action)
        if not tool:
            _advance_outbox_unknown(oid, ticket_id, f"未知 action {action}"); continue
        call_args = dict(payload) if isinstance(payload, dict) else {}
        call_args["approval_ticket"] = ticket_id
        outcome = GatewayOutcome("SUCCESS")
        try:
            gateway_client.gateway_call(tool, call_args, timeout=gw_to)
        except gateway_client.GatewayDenied as e:           # 票据级确定性拒绝
            outcome = GatewayOutcome("TICKET_DENY", e.reason_code, e.detail)
        except gateway_client.GatewayGlobalDegraded as e:   # 全局配置故障
            outcome = GatewayOutcome("GLOBAL_DEGRADED", e.reason_code, e.detail)
        except gateway_client.GatewayUnavailable as e:      # 瞬时(网络/超时/L2_DB_UNAVAILABLE)
            outcome = GatewayOutcome("TRANSIENT", "", str(e)[:160])
        except Exception as e:                              # 未分类异常 → 保守瞬时
            outcome = GatewayOutcome("TRANSIENT", "", f"{type(e).__name__}: {str(e)[:120]}")
        # 边界②:读 approvals 权威态推进(边界③:UNKNOWN/EXECUTING 不重 merge;④ 仅 APPROVED 按 outcome 分类)
        sig = _advance_outbox_by_approval(oid, ticket_id, action, outcome)
        # circuit breaker:成功关;瞬时/全局降级 → 打开 breaker + 本 tick 停消费(防连环撞 Gateway)
        if outcome.kind == "SUCCESS":
            _l2_gw_ok()
        elif outcome.kind in ("TRANSIENT", "GLOBAL_DEGRADED"):
            _l2_gw_mark_degraded(outcome.reason_code or outcome.kind)
            print(f"[ctrl][L2] Gateway {outcome.kind}({outcome.reason_code or outcome.detail[:40]}) → circuit breaker 打开,本 tick 停止消费 outbox")
            break
        if sig == "DEGRADED":
            break


def _advance_outbox_unknown(oid, ticket_id, reason):
    """未知 action → outbox UNKNOWN(不调 Gateway)。"""
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("UPDATE policy_action_outbox SET status='UNKNOWN', error=%s WHERE id=%s", (reason[:200], oid))
    conn.commit()


def _l2_outbox_backoff(oid, reason):
    """B4c.1.1 #2:outbox 退避(next_retry_at=now+backoff),供 reconcile 读失败持久化退避(防反复阻塞后续对账项)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT attempts FROM policy_action_outbox WHERE id=%s", (oid,))
            r = cur.fetchone()
            cur.execute("UPDATE policy_action_outbox SET next_retry_at=now()+make_interval(secs=>%s), last_error_code=%s WHERE id=%s",
                        (_l2_backoff_seconds(r[0] if r else 0), (reason or "")[:40], oid))
        conn.commit()
    except Exception:
        conn.rollback()


def _reconcile_ticket(ticket_id, run_id, action, astatus, pr_num, repo, oid, deadline=None):
    """B4c-4:对账单张 UNKNOWN/超时EXECUTING 票。读 GitHub 权威态 → l2_reconcile_*。
    effect_applied:merge→prd.merged;close→state==closed AND NOT merged(收紧 #7)。
    B4c.1.1 #2:gateway_read_pr RETRY → 持久化退避(outbox next_retry_at)防反复阻塞后续对账项;
    B4c.1.1 #3:GatewayGlobalDegraded → 打开 circuit breaker + 返回 False。
    返回 bool(l2_reconcile_* 返回值):True=已迁移(USED/FAILED),False=未迁移/读失败(下轮重试)。"""
    import gateway_client
    owner, _, repo_name = repo.partition("/")
    try:
        rstatus, prd = gateway_client.gateway_read_pr(owner, repo_name, pr_num, timeout=_gw_timeout_for(deadline))
        _l2_gw_ok()   # B4c.1.3 P2:reconcile 读成功 → 清 breaker failure_count
    except gateway_client.GatewayGlobalDegraded as e:
        _l2_gw_mark_degraded(e.reason_code)
        print(f"[ctrl][L2] reconcile {ticket_id[:16]}: Gateway GLOBAL_DEGRADED({e.reason_code}) → breaker")
        return False
    except gateway_client.GatewayUnavailable as e:
        # B4c.1.2 #2:网络/401 瞬时 → outbox 退避 + 打开 breaker
        _l2_outbox_backoff(oid, "reconcile unavailable"); _l2_gw_mark_degraded("UNAVAILABLE", seconds=L2_RETRY_BASE_SECONDS)
        print(f"[ctrl][L2] reconcile {ticket_id[:16]}: Gateway unavailable → 退避 + breaker"); return False
    except gateway_client.GatewayDenied as e:
        # B4c.1.2 #2:确定性拒绝(REPO_NOT_ALLOWED 等)→ outbox 退避(不无限撞;UNKNOWN/EXECUTING 票留待人工)
        _l2_outbox_backoff(oid, f"reconcile denied:{e.reason_code}")
        print(f"[ctrl][L2] reconcile {ticket_id[:16]}: Gateway denied({e.reason_code}) → 退避"); return False
    if rstatus == "RETRY":
        _l2_outbox_backoff(oid, "reconcile read RETRY")   # B4c.1.1 #2
        return False   # 读失败,退避后下轮重试
    if action == "close":
        effect_applied = (prd["state"] == "closed" and not prd["merged"])
    else:  # merge
        effect_applied = prd["merged"]
    actual_sha = prd.get("merge_commit_sha") or prd.get("head_sha") or ""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            if astatus == "UNKNOWN":
                cur.execute("SELECT l2_reconcile_unknown(%s,%s,%s)", (ticket_id, effect_applied, actual_sha))
            else:  # EXECUTING
                cur.execute("SELECT l2_reconcile_executing(%s,%s,%s)", (ticket_id, effect_applied, actual_sha))
            ok_bool = cur.fetchone()[0]
        conn.commit()
        new_st = "USED" if effect_applied else "FAILED"
        print(f"[ctrl][L2] reconcile {ticket_id[:16]} {astatus}→{new_st if ok_bool else '(未迁移)'} (effect={effect_applied})")
        return bool(ok_bool)
    except Exception as e:
        conn.rollback()
        print(f"[ctrl][L2] reconcile {ticket_id[:16]} 异常: {e}")
        return False


def reconcile_l2(deadline=None, budget=None):
    """B4c-4(+4.1):延迟对账 + 过期收敛 + 全状态收敛(读 GitHub 实际态,**绝不自动重 merge**)。
    ① UNKNOWN/EXECUTING 且 executing_at<now()-120s(延迟防竞态;B4c-4.1 P1-1:UNKNOWN 也需延迟)
       → gateway_read_pr → l2_reconcile_* → **复用 _advance_outbox_by_approval 收敛 outbox+task**(P1-2);
    ③ PENDING 过期 → l2_expire_pending → EXPIRED + outbox FAILED + task HOLD;
    ④ APPROVED 过期 → l2_expire_approved → EXPIRED + outbox FAILED + task HOLD;
    ⑤ 滞留 outbox(DISPATCHED 或 UNKNOWN)+ approval 已终结(USED/FAILED)
       → **复用 _advance_outbox_by_approval**(P1-3:读 action + action-aware task 推进 + 完整 CAS)。"""
    # ① + ②:UNKNOWN/超时EXECUTING 延迟对账(B4c-4.1:两者都需 executing_at<now()-120s)
    conn = ensure_pg()
    with conn.cursor() as cur:
        cur.execute("""SELECT a.ticket_id, a.run_id, a.action, a.status, a.pr_number, a.repo, o.id
                       FROM approvals a JOIN policy_action_outbox o ON a.ticket_id=o.ticket_id
                       WHERE a.executing_at IS NOT NULL AND a.executing_at < now() - interval '120 seconds'
                         AND a.status IN ('UNKNOWN','EXECUTING')
                         AND a.pr_number IS NOT NULL AND a.repo IS NOT NULL
                         AND o.next_retry_at <= now()
                       ORDER BY o.next_retry_at, o.id LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
        reconcile_items = cur.fetchall()
    conn.commit()
    for ticket_id, run_id, action, astatus, pr_num, repo, oid in reconcile_items:
        if _budget_exhausted(deadline) or not _tick_ok(budget):
            break   # B4c.1 工作预算到期,剩余对账项下 tick
        if _l2_gw_degraded():
            break   # B4c.1.1 #3:circuit breaker 打开 → 停对账
        _tick_use(budget)   # B4c.1.4:确认对账项(fetched)→ 扣额度
        changed = _reconcile_ticket(ticket_id, run_id, action, astatus, pr_num, repo, oid, deadline)
        if changed:
            # B4c-4.1 P1-2:对账后复用 _advance_outbox_by_approval 收敛 outbox+task(action-aware + 完整 CAS)
            _advance_outbox_by_approval(oid, ticket_id, action, None)

    # B4c.1.5 #1:deadline 到期 → 跳过 expiry/stranded(纯 DB 快操作,延后一 tick 无害;时间硬边界)
    if _budget_exhausted(deadline):
        return

    # ③ + ④ + ⑤:过期 PENDING/APPROVED → EXPIRED;EXPIRED 收敛;滞留 outbox 收敛
    #   B4c.1.6:每个 DB 批次前 + 逐项前检查 deadline → 到期 commit 已完成 + 跳过(时间硬边界)
    conn = ensure_pg()
    with conn.cursor() as cur:
        if not _budget_exhausted(deadline):
            cur.execute("""SELECT ticket_id FROM approvals WHERE status='PENDING' AND approval_expires_at < now() LIMIT %s""", (L2_EXPIRY_BATCH,))
            for (tid,) in cur.fetchall():
                if _budget_exhausted(deadline): break
                cur.execute("SELECT l2_expire_pending(%s)", (tid,))
        if not _budget_exhausted(deadline):
            cur.execute("""SELECT ticket_id FROM approvals WHERE status='APPROVED' AND expires_at IS NOT NULL AND expires_at < now() LIMIT %s""", (L2_EXPIRY_BATCH,))
            for (tid,) in cur.fetchall():
                if _budget_exhausted(deadline): break
                cur.execute("SELECT l2_expire_approved(%s)", (tid,))
        if not _budget_exhausted(deadline):
            cur.execute("""SELECT a.ticket_id, a.run_id FROM approvals a
                           WHERE a.status='EXPIRED' AND EXISTS (SELECT 1 FROM policy_action_outbox o WHERE o.ticket_id=a.ticket_id AND o.status != 'FAILED') LIMIT %s""", (L2_EXPIRY_BATCH,))
            for tid, run_id in cur.fetchall():
                if _budget_exhausted(deadline): break
                cur.execute("UPDATE task_runs SET status='HOLD', current_stage='l2_expired', last_error='ticket EXPIRED', updated_at=now() WHERE run_id=%s AND approval_required=TRUE AND status='APPROVAL_PENDING' AND current_stage='l2_awaiting_approval'", (run_id,))
                exp_err = "ticket EXPIRED" + ("" if cur.rowcount else " | CONCURRENT_STATE_CHANGE: task 已脱离 APPROVAL_PENDING/l2_awaiting_approval,未覆盖")
                cur.execute("UPDATE policy_action_outbox SET status='FAILED', completed_at=now(), error=%s WHERE ticket_id=%s AND status != 'FAILED'", (exp_err[:300], tid))
    conn.commit()

    # ⑤ 滞留 outbox(DISPATCHED 或 UNKNOWN)+ approval 已终结(USED/FAILED)
    if not _budget_exhausted(deadline):
        conn = ensure_pg()
        with conn.cursor() as cur:
            cur.execute("""SELECT o.id, o.ticket_id, a.action FROM policy_action_outbox o JOIN approvals a ON o.ticket_id=a.ticket_id
                           WHERE o.status IN ('DISPATCHED','UNKNOWN') AND a.status IN ('USED','FAILED') LIMIT %s""", (L2_EXPIRY_BATCH,))
            stranded = cur.fetchall()
        conn.commit()
        for oid, ticket_id, action in stranded:
            if _budget_exhausted(deadline): break
            _advance_outbox_by_approval(oid, ticket_id, action, GatewayOutcome("SUCCESS", "", "reconcile stranded convergence"))


# ════════════════════════════════════════════════════════════════════
# M3-C · 状态感知失败处理 + 回滚(POST_MERGE_VERIFY_FAILED → revert child run → ROLLED_BACK → reverify)
# 架构(决策 2):revert 走 **child run** 模型 —— 原 run(parent)保留原 binding;rollback 建 deterministic
#   child task_run(parent_run_id 回链),独占 revert binding/ticket/L2 执行链(走正常 drain → MERGED)。
#   run_pr_bindings UNIQUE(run_id) 保留(child run_id 独立)。
# 决策:2(仅结构化事件触发)/1(逆向 + 冲突检测)/6(UNIQUE(parent_run_id,reverted_merge_sha))/
#      4(merge_method=merge)/8(L2 gate,真实 merge 后才 REVERTED)/9(reverify FAIL→HOLD,不二回滚)。
# 需求 4:changed-files / merge-parent / 恢复内容一律 GitHub 权威(get_commit/get_file_contents/list_commits),
#        **绝不信任 event.files 或 fixer 自报内容**;EmbeddedResource 真实内容由 gateway_client 解析。
# 需求 5:L2 前重新验证 main==bad merge、revert PR head 内容==parent;冲突/读失败/不支持 diff → HOLD(fail-closed)。
# 不改 B4/B5 边界:复用 l2_ensure_ticket / drain_l2_outbox / policy_action_outbox / approvals。
# ════════════════════════════════════════════════════════════════════
def _m3c_set_hold_cur(cur, rollback_id, run_id, status, reason):
    """fail-closed(同事务):rollback → CONFLICT/UNSUPPORTED;**parent task** → HOLD(决策 1/5)。"""
    cs = "rollback_conflict_hold" if status == "CONFLICT" else "rollback_unsupported_hold"
    cur.execute("UPDATE rollback_runs SET status=%s, fail_reason=%s, updated_at=now() WHERE rollback_id=%s",
                (status, reason[:200], rollback_id))
    cur.execute("UPDATE task_runs SET status='HOLD', current_stage=%s, last_error=%s, updated_at=now() WHERE run_id=%s",
                (cs, f"rollback {status}: {reason}"[:300], run_id))

def _m3c_set_hold(rollback_id, run_id, status, reason):
    """fail-closed(独立事务,供 Gateway 调用后)。"""
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            _m3c_set_hold_cur(cur, rollback_id, run_id, status, reason)
        conn.commit()
        print(f"[ctrl][M3C] {run_id} rollback {status} → HOLD({reason[:80]})")
    except Exception:
        conn.rollback()

def _m3c_diff_verdicts(owner, repo_name, files, parent_sha, deadline=None):
    """需求 1+5:逐 changed-file 判定 REVERT/CONFLICT/UNSUPPORTED(GitHub 权威内容,parent_sha 处可读=可还原)。
    - modified/removed:parent 有内容且可读 → REVERT;读失败 → UNSUPPORTED(fail-closed)
    - added:parent 应 MISSING(revert 需删除)→ REVERT;parent 存在 → UNSUPPORTED
    - 其他(renamed/copied/...)→ UNSUPPORTED
    返回 [(path, verdict)]。"""
    import gateway_client
    out = []
    for path, status in files:
        if not path:
            continue
        if status in ("modified", "removed"):
            pst, _ptxt, _ = gateway_client.gateway_get_file_text(owner, repo_name, path, sha=parent_sha, timeout=L2_GW_TIMEOUT)
            out.append((path, "REVERT") if pst == "OK" else (path, "UNSUPPORTED"))
        elif status == "added":
            pst, _ptxt, _ = gateway_client.gateway_get_file_text(owner, repo_name, path, sha=parent_sha, timeout=L2_GW_TIMEOUT)
            out.append((path, "REVERT") if pst == "MISSING" else (path, "UNSUPPORTED"))
        else:
            out.append((path, "UNSUPPORTED"))
    return out

def _m3c_verify_revert_contents(owner, repo_name, files, parent_sha, revert_branch):
    """需求 5(L2 前重验):revert PR head 内容 == parent 内容(逐 changed file)。
    返回 None(全一致/可放行)或首个不一致 path(冲突/读失败 → 调用方 HOLD)。"""
    import gateway_client
    for path, status in files:
        if not path:
            continue
        pst, ptxt, _ = gateway_client.gateway_get_file_text(owner, repo_name, path, sha=parent_sha, timeout=L2_GW_TIMEOUT)
        rst, rtxt, _ = gateway_client.gateway_get_file_text(owner, repo_name, path, ref=f"refs/heads/{revert_branch}", timeout=L2_GW_TIMEOUT)
        if status == "added":
            # added 文件 revert 应删除:parent 与 revert-head 都须 MISSING
            if pst != "MISSING" or rst != "MISSING":
                return path
            continue
        # modified/removed:内容须一致(读失败亦视为不一致 → HOLD)
        if pst != "OK" or rst != "OK" or (ptxt or "") != (rtxt or ""):
            return path
    return None

def process_rollback(deadline=None, budget=None):
    """PENDING:GitHub 权威派生 changed-files + merge-parent(需求 4);校验 main==bad merge(需求 5);
    冲突/不支持/读失败 → HOLD(fail-closed);否则派 fixer(**child run**)建 revert PR → REVERT_PR_OPEN。"""
    import gateway_client
    if _l2_gw_degraded():
        return
    conn = ensure_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT rb.rollback_id, rb.parent_run_id, rb.revert_run_id, rb.reverted_merge_sha, rb.repo, t.room_id
                           FROM rollback_runs rb
                           JOIN task_runs t ON t.run_id=rb.parent_run_id
                           WHERE rb.status='PENDING' ORDER BY rb.created_at LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
            pendings = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback(); return
    for rb_id, parent_run, child_run, bad_sha, repo, room_id in pendings:
        if _budget_exhausted(deadline): break
        owner, _, repo_name = repo.partition("/")
        if not room_id:
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", "missing parent room_id")
            continue
        # 需求 4:changed files 来自 get_commit(GitHub 权威,不信 event.files)
        cst, cdict = gateway_client.gateway_get_commit(owner, repo_name, bad_sha, timeout=L2_GW_TIMEOUT)
        if cst != "OK":
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"get_commit({bad_sha[:12]}) failed: {cst}"); continue
        files = [(f.get("filename"), f.get("status")) for f in cdict.get("files", []) if isinstance(f, dict)]
        # 需求 5:main==bad merge(main tip 必须就是 bad_sha;否则 main 已动 → HOLD)
        mst, mc = gateway_client.gateway_list_commits(owner, repo_name, sha="main", per_page=1, timeout=L2_GW_TIMEOUT)
        _tip = (mc[0].get("sha", "")[:12] if (mst == "OK" and mc) else "none")
        if mst != "OK" or not mc or mc[0].get("sha") != bad_sha:
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"main tip != bad_sha (list_commits(main)={mst} tip={_tip})"); continue
        # 需求 4:merge parent(get_commit 不返 parents → list_commits(bad_sha) 第 2 条)
        pst, pc = gateway_client.gateway_list_commits(owner, repo_name, sha=bad_sha, per_page=2, timeout=L2_GW_TIMEOUT, deadline=deadline)
        if pst != "OK" or len(pc) < 2 or pc[0].get("sha") != bad_sha or not pc[1].get("sha"):
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"cannot derive merge parent (list_commits={pst} n={len(pc) if pst == 'OK' else '?'})"); continue
        parent_sha = pc[1]["sha"]
        verdicts = _m3c_diff_verdicts(owner, repo_name, files, parent_sha, deadline)
        blocked = [(p, v) for (p, v) in verdicts if v != "REVERT"]
        revertible = [p for (p, v) in verdicts if v == "REVERT"]
        conn = ensure_pg()
        try:
            with conn.cursor() as cur:
                if blocked:
                    bv = blocked[0][1]
                    _m3c_set_hold_cur(cur, rb_id, parent_run, "CONFLICT" if bv == "CONFLICT" else "UNSUPPORTED",
                                      f"{bv} on {blocked[0][0]}: {','.join(p for p, _ in blocked)[:120]}")
                    conn.commit()
                    print(f"[ctrl][M3C] {parent_run} rollback {bv} on {blocked[0][0]} → HOLD(不回滚)")
                    continue
                # 派 fixer(child run)建 revert PR;revert_branch=fix/<child_run>-x(命中 gateway_list_prs(child) 前缀)
                rb_branch = f"fix/{child_run}-x"
                cur.execute("""INSERT INTO stage_runs(run_id, stage, agent, attempt, status)
                               VALUES(%s, 'revert', 'fixer', 1, 'PENDING_DISPATCH')
                               ON CONFLICT(run_id, stage, attempt) DO NOTHING""", (child_run,))
                cur.execute("""UPDATE rollback_runs SET status='REVERT_PR_OPEN', revert_branch=%s, merge_parent_sha=%s, diff_summary=%s, updated_at=now() WHERE rollback_id=%s""",
                            (rb_branch, parent_sha,
                             json.dumps({"bad": bad_sha[:12], "parent": parent_sha[:12],
                                         "files": [{"path": p, "status": s} for p, s in files]}, ensure_ascii=False)[:500], rb_id))
                cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                               VALUES(%s, %s, %s, 'fixer', 'revert', 1, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
                            (f"{child_run}:revert:{bad_sha[:8]}", child_run, room_id,
                             f"POST_MERGE_VERIFY_FAILED: 建 revert PR。分支={rb_branch},base=main,merge_method=merge。"
                             f"坏 merge={bad_sha[:12]},其 parent={parent_sha[:12]}。将以下文件还原为 parent 内容:{','.join(revertible)}。"
                             f"完成写 TASK_COMPLETED: {child_run}-revert。"))
                cur.execute("UPDATE task_runs SET current_stage='rollback_revert_dispatched', updated_at=now() WHERE run_id=%s", (child_run,))
            conn.commit()
            print(f"[ctrl][M3C] {parent_run} rollback PENDING → 派 fixer({child_run}) 建 revert PR({rb_branch},parent={parent_sha[:12]})")
        except Exception as e:
            conn.rollback(); print(f"[ctrl][M3C] rollback PENDING err {parent_run}: {e}")

def process_rollback_advance(deadline=None):
    """REVERT_PR_OPEN → (需求 5 L2 前重验 main==bad merge + revert head==parent)→ 发现 revert PR +
    child binding + L2 merge 票 → child run APPROVAL_PENDING/l2_awaiting_approval → AWAITING_APPROVAL。
    AWAITING_APPROVAL + child run MERGED(真实 merge,决策 8)→ REVERTED + 派 reverify(parent run)→ REVERIFYING。"""
    import gateway_client
    if _l2_gw_degraded():
        return
    conn = ensure_pg()
    # ── REVERT_PR_OPEN ──
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT rb.rollback_id, rb.parent_run_id, rb.revert_run_id, rb.reverted_merge_sha, rb.repo,
                                  rb.revert_branch, rb.merge_parent_sha, t.room_id
                           FROM rollback_runs rb
                           JOIN task_runs t ON t.run_id=rb.revert_run_id
                           WHERE rb.status='REVERT_PR_OPEN'
                           ORDER BY rb.created_at
                           LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
            opens = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback(); opens = []
    for rb_id, parent_run, child_run, bad_sha, repo, rb_branch, parent_sha, room_id in opens:
        if _budget_exhausted(deadline): break
        owner, _, repo_name = repo.partition("/")
        if not room_id:
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", "missing child room_id")
            continue
        # 需求 5:L2 前重新验证 main==bad merge(main tip == bad_sha)
        mst, mc = gateway_client.gateway_list_commits(owner, repo_name, sha="main", per_page=1, timeout=L2_GW_TIMEOUT)
        if mst != "OK" or not mc or mc[0].get("sha") != bad_sha:
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"L2-pre main tip != bad_sha ({mst})"); continue
        # 发现 child 的 revert PR(gateway_list_prs 按 fix/<child_run>- 前缀)
        lstatus, prs = gateway_client.gateway_list_prs(owner, repo_name, child_run, timeout=L2_GW_TIMEOUT, deadline=deadline)
        if lstatus == "AMBIGUOUS":
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"ambiguous revert PRs for {child_run}")
            continue
        if lstatus != "FOUND" or not prs:
            continue   # 尚无 PR / RETRY → 下轮
        revert_prs = [p for p in prs if rb_branch and str(p.get("head", {}).get("ref", "")) == rb_branch] or list(prs)
        if len(revert_prs) != 1:
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"unexpected revert PR fanout for {child_run}")
            continue
        pr_num = revert_prs[0]["number"]
        rstatus, prd = gateway_client.gateway_read_pr(owner, repo_name, pr_num, timeout=L2_GW_TIMEOUT)
        head_sha = prd.get("head_sha") if isinstance(prd, dict) else None
        if rstatus != "OK" or not isinstance(prd, dict) or prd.get("state") != "open" or not isinstance(head_sha, str) or not head_sha:
            continue
        # 需求 5:revert PR head 内容 == parent 内容(逐 changed file;读失败/不一致 → HOLD)
        cst, cdict = gateway_client.gateway_get_commit(owner, repo_name, bad_sha, timeout=L2_GW_TIMEOUT)
        cfiles = [(f.get("filename"), f.get("status")) for f in (cdict.get("files", []) if cst == "OK" and isinstance(cdict, dict) else []) if isinstance(f, dict)]
        if parent_sha and cfiles:
            mismatch = _m3c_verify_revert_contents(owner, repo_name, cfiles, parent_sha, rb_branch)
            if mismatch:
                _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"revert head != parent on {mismatch}"); continue
        # child binding + L2 merge 票(merge_method=merge,决策 4)
        conn = ensure_pg()
        try:
            with conn.cursor() as cur:
                bid = "bnd-" + child_run
                cur.execute("""INSERT INTO run_pr_bindings(binding_id, run_id, repo, pr_number, fix_branch, base_branch, head_sha)
                               VALUES(%s, %s, %s, %s, %s, 'main', %s)
                               ON CONFLICT (binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha, pr_number=EXCLUDED.pr_number""",
                            (bid, child_run, repo, pr_num, rb_branch, head_sha))
                payload = {"owner": owner, "repo": repo_name, "pullNumber": int(pr_num), "commit_title": f"revert {bad_sha[:12]}", "merge_method": "merge"}
                ahash = gateway_client.canonical_args_hash(payload)
                cur.execute("SELECT l2_ensure_ticket(%s, 'merge', %s::jsonb, %s, 24, 1)", (bid, json.dumps(payload), ahash))
                tkt = cur.fetchone()[0]
                cur.execute("UPDATE rollback_runs SET status='AWAITING_APPROVAL', revert_pr_number=%s, revert_ticket_id=%s, updated_at=now() WHERE rollback_id=%s", (pr_num, tkt, rb_id))
                # child run 进 L2 审批链(drain MERGED CAS 命中:approval_required+APPROVAL_PENDING+l2_awaiting_approval)
                cur.execute("UPDATE task_runs SET status='APPROVAL_PENDING', current_stage='l2_awaiting_approval', approval_required=TRUE, l2_next_attempt_at=now(), updated_at=now() WHERE run_id=%s", (child_run,))
            conn.commit()
            print(f"[ctrl][M3C] {parent_run} revert PR#{pr_num}(child={child_run}) → L2 ticket {tkt[:16]} → AWAITING_APPROVAL")
        except psycopg2.Error as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "22023":
                _m3c_set_hold(rb_id, parent_run, "CONFLICT", f"L2 ensure_ticket 22023: {str(e)[:160]}")
            else:
                print(f"[ctrl][M3C] rollback REVERT_PR_OPEN db err {parent_run}: {e}")
        except Exception as e:
            conn.rollback(); print(f"[ctrl][M3C] rollback REVERT_PR_OPEN err {parent_run}: {e}")

    # ── AWAITING_APPROVAL + child run MERGED(真实 merge 后,决策 8)→ REVERTED + 派 reverify(parent)──
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT rb.rollback_id, rb.parent_run_id, rb.revert_run_id, rb.revert_ticket_id, t.room_id
                           FROM rollback_runs rb
                           JOIN task_runs t ON t.run_id=rb.revert_run_id
                           WHERE rb.status='AWAITING_APPROVAL' AND t.status='MERGED' LIMIT %s""", (L2_MAINTENANCE_MAX_ITEMS,))
            used = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback(); used = []
    for rb_id, parent_run, child_run, tkt, room_id in used:
        if _budget_exhausted(deadline): break
        if not room_id:
            _m3c_set_hold(rb_id, parent_run, "CONFLICT", "missing reverify room_id")
            continue
        conn = ensure_pg()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT result_sha FROM approvals WHERE ticket_id=%s", (tkt,))
                _r = cur.fetchone()
                rsha = _r[0] if _r else None
                cur.execute("UPDATE rollback_runs SET status='REVERTED', revert_result_sha=%s, updated_at=now() WHERE rollback_id=%s AND status='AWAITING_APPROVAL'", (rsha, rb_id))
                if cur.rowcount == 1 and rsha:
                    # parent run:FAIL→ROLLED_BACK(坏 merge 已撤销,等 reverify);child run:MERGED→reverify stage
                    cur.execute("UPDATE task_runs SET status='ROLLED_BACK', current_stage='reverify', last_error='revert merged; reverify pending', updated_at=now() WHERE run_id=%s", (parent_run,))
                    cur.execute("UPDATE task_runs SET current_stage='reverify', last_error='revert merged; reverify pending', updated_at=now() WHERE run_id=%s", (child_run,))
                    cur.execute("""INSERT INTO dispatch_outbox(idempotency_key, run_id, room_id, target_agent, target_stage, attempt, body)
                                   VALUES(%s, %s, %s, 'verifier', 'reverify', 1, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
                                (f"{parent_run}:reverify:{rsha[:8]}", parent_run, room_id,
                                 f"revert 已 merge({rsha[:12]}),重新验证 main 是否恢复。完成写 TASK_COMPLETED: {parent_run}-reverify + VERDICT=PASS/FAIL。"))
                    cur.execute("UPDATE rollback_runs SET status='REVERIFYING' WHERE rollback_id=%s", (rb_id,))
            conn.commit()
            print(f"[ctrl][M3C] {parent_run} revert merged({(rsha or '')[:12]}) → REVERTED + 派 reverify")
        except Exception as e:
            conn.rollback(); print(f"[ctrl][M3C] rollback ROLLED_BACK err {parent_run}: {e}")



#   域 A(PG-驱动 L2):绑定/建票/drain/对账 —— 始终运行(按 approval_required 自过滤),Matrix 挂也照跑。
#   域 B(Matrix):login/consume/dispatch —— 独立;Matrix 不可用不阻断 L2 恢复。
def run_forever():
    backoff = 1
    if M4F_ONLY_MODE:
        print(f"[ctrl][M5-0] Candidate 启动;PG={PG_HOST}:{PG_PORT};Matrix={MATRIX_HS} user={MATRIX_USER};"
              f"consumer={CONTROLLER_CONSUMER_NAME};prefix={M4F_RUN_PREFIX};POLL={POLL_INTERVAL}s")
    else:
        print(f"[ctrl] 启动;PG={PG_HOST}:{PG_PORT};Matrix={MATRIX_HS};POLL={POLL_INTERVAL}s;L2_MERGE_ENABLED={'on' if L2_MERGE_ENABLED else 'off'}")
    while True:
        if M4F_ONLY_MODE:
            # ── M5-0 Candidate: scoped M4-F only (design freeze v2.3 §11) ──
            # Advisory lock health check
            if not check_m5_lock_health():
                print("[ctrl][M5-0] advisory lock connection lost — exiting")
                sys.exit(1)
            try:
                ensure_pg()
                drain_m4f_events(max_items=1)
                # M5-0B §11 Domain A order: skill→review bridge, then handoff advancement.
                reconcile_m5_skill_to_review()
                reconcile_m5_handoffs()
            except psycopg2.OperationalError as e:
                print(f"[ctrl][M5-0] PG degraded: {e}; reconnecting in {backoff}s")
                reset_pg()
                time.sleep(backoff); backoff = min(backoff * 2, 30)
                continue
            except Exception as e:
                print(f"[ctrl][M5-0] Domain A unexpected: {type(e).__name__}: {e}; retry in 5s")
                time.sleep(5)

            # Domain B: scoped Matrix
            try:
                ensure_matrix_login()
                consume_events()
                drain_outbox()
                backoff = 1
            except MatrixUnavailable as e:
                print(f"[ctrl][M5-0] Matrix degraded: {e}; backoff={backoff}s")
                time.sleep(backoff); backoff = min(backoff * 2, 30)
                continue
            except psycopg2.OperationalError as e:
                print(f"[ctrl][M5-0] Matrix 域 PG degraded: {e}; reconnecting in {backoff}s")
                reset_pg()
                time.sleep(backoff); backoff = min(backoff * 2, 30)
                continue
            except Exception as e:
                print(f"[ctrl][M5-0] Matrix 域 unexpected: {type(e).__name__}: {e}; retry in 5s")
                time.sleep(5)

            time.sleep(POLL_INTERVAL)
            continue  # skip legacy Domain A/B below

        # ── Legacy mode: full controller (Domain A + B) ──
        # 故障域 A:PG-驱动 L2(始终运行,不 gated by 开关;函数内部按 approval_required 过滤)
        try:
            ensure_pg()
            drain_m4f_events(max_items=1)
            l2_deadline = time.monotonic() + L2_MAINTENANCE_BUDGET_SECONDS   # B4c.1 单循环工作预算(共享)
            l2_budget = [L2_MAINTENANCE_MAX_ITEMS]   # B4c.1.3:每 tick 共享 item 预算(整轮硬边界,跨阶段)
            initiate_l2_pending(l2_deadline, l2_budget)
            drain_l2_outbox(l2_deadline, l2_budget)
            reconcile_l2(l2_deadline, l2_budget)
            process_rollback(l2_deadline, l2_budget)          # M3-C: PENDING→冲突检测/派 fixer 建 revert PR
            process_rollback_advance(l2_deadline)  # M3-C: revert PR→L2 票;child MERGED→REVERTED+reverify
        except psycopg2.OperationalError as e:
            print(f"[ctrl] L2 域 PG degraded: {e}; reconnecting in {backoff}s")
            reset_pg()
            time.sleep(backoff); backoff = min(backoff * 2, 30)
            continue
        except Exception as e:
            print(f"[ctrl] L2 域 unexpected: {type(e).__name__}: {e}; retry in 5s")
            time.sleep(5)

        # 故障域 B:Matrix(login/consume/dispatch)
        try:
            ensure_matrix_login()
            consume_events()
            drain_outbox()
            backoff = 1
        except MatrixUnavailable as e:
            print(f"[ctrl] Matrix degraded: {e}; backoff={backoff}s (L2 域继续运行)")
            time.sleep(backoff); backoff = min(backoff * 2, 30)
            continue
        except MatrixRejected as e:
            print(f"[ctrl] Matrix rejected: {e}; skip")
        except psycopg2.OperationalError as e:
            print(f"[ctrl] Matrix 域 PG degraded: {e}; reconnecting in {backoff}s")
            reset_pg()
            time.sleep(backoff); backoff = min(backoff * 2, 30)
            continue
        except Exception as e:
            print(f"[ctrl] Matrix 域 unexpected: {type(e).__name__}: {e}; retry in 5s")
            time.sleep(5)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    if not ADMIN_PW or not PG_PASS:
        print("ERROR: 需 ADMIN_PW + PG_PASS 环境变量"); sys.exit(1)
    # ISOLATED_LIVE readiness contract (Phase 1-D retry v3 review-gap Fix 2):
    # clear any stale sentinel FIRST (a restarted container never inherits
    # the previous boot's readiness), then create it atomically ONLY after
    # every startup assertion passed and the run loop is about to start.
    # Disabled unless CONTROLLER_READY_SENTINEL is set (stack deployments).
    import readiness as _readiness
    _ready_path = ""
    try:
        _ready_path = _readiness.readiness_path()
    except ValueError as _exc:
        print("ERROR: %s" % _exc); sys.exit(1)
    if _ready_path:
        _readiness.clear_stale_sentinel(_ready_path)
    startup_assert_l2()
    # M5-0A: validate Candidate configuration before any Matrix interaction
    _validate_m5_candidate()
    # B4c-0.2:部署预检模式——仅跑 startup_assert 后 exit 0/1,供 start 脚本替换前
    #   用同一镜像+env 预检(startup_assert 失败时上面已 sys.exit 非零)。通过后才替换旧容器。
    if os.environ.get("STARTUP_CHECK_ONLY", "0").strip().lower() in ("1", "true", "yes"):
        # M5-0A: verify advisory lock is obtainable, then release
        if M4F_ONLY_MODE:
            if acquire_m5_lock():
                release_m5_lock()
                print("[ctrl] STARTUP_CHECK_ONLY: startup_assert + advisory lock OK → exit 0")
            else:
                print("[ctrl] STARTUP_CHECK_ONLY: advisory lock DENIED → exit 1")
                sys.exit(1)
        else:
            print("[ctrl] STARTUP_CHECK_ONLY: startup_assert passed → exit 0")
        sys.exit(0)
    # M5-0A: Candidate must acquire advisory lock before Matrix login
    if M4F_ONLY_MODE:
        if not acquire_m5_lock():
            print("[ctrl][M5-0] Candidate advisory lock DENIED — another candidate is running")
            sys.exit(1)
        print("[ctrl][M5-0] Candidate advisory lock acquired")
        import atexit, signal as _sig
        atexit.register(release_m5_lock)
        _sig.signal(_sig.SIGTERM, lambda *_: (release_m5_lock(), sys.exit(0)))
    if _ready_path:
        # All startup assertions passed; the controller is entering the run
        # loop — THIS is the earliest point readiness may be signalled.
        _readiness.mark_ready(_ready_path)
    run_forever()
