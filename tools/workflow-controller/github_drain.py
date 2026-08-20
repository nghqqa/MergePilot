#!/usr/bin/env python3
"""github_drain.py — Controller 侧 GitHub delivery drain + Checks desired reconcile。

冻结主链位置(设计 2026-08-18 收口):

    webhook → github_deliveries → [本模块 drain] → submit_task(唯一三表实现)
    → Reviewer/Fixer/Verifier → 真实 Manager(唯一 M4F_RUN producer)
    → Gateway 权威绑定 → [本模块 reconcile] github_check_outbox → reporter

合同:
  * claim: 合法 CTE + FOR UPDATE SKIP LOCKED;PENDING 与过期 RUNNING 皆可认领,
    每次轮换 claim_id;一切确认以 claim_id CAS —— 旧 worker 在 lease 被接管后
    rowcount=0,整个工作事务回滚(不为被窃取的 delivery 创建任何 run)。
  * room map: 版本化精确 repo→room_id 映射,与 policy.yaml repos.allowlist
    必须 1:1 对齐(启动验证);缺失/重复/歧义 → 永久 delivery ERROR。
  * run_id: gh-<sha256(installation\\0repo\\0pr\\0head)[:24]>;claim 后重算复检。
  * stage_events: event_id='gh:<delivery_id>' 命名空间;INSERT ON CONFLICT
    rowcount=1 才调用 submit_task/mark/update;rowcount=0 不触碰任何 helper,
    delivery 永久 ERROR(STAGE_EVENT_ID_COLLISION)。
  * Checks desired: (status,current_stage,rollback,stale) 有序 13+1 规则映射;
    desired 三元组未变不加版本;SHA 变更清空 check_run_id 并使旧 claim 失效。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from task_submit import (EventSource, SubmitTaskError, SubmitTaskTransient,
                         TaskSubmission, submit_task)

GH_RUN_ID_RE = re.compile(r"^gh-[0-9a-f]{24}$")
_DELIVERY_EVENT_ID_RE = re.compile(r"^gh:[A-Za-z0-9][A-Za-z0-9-]{7,63}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_PRODUCER_TIMEOUT_STAGE = "m4f_producer_timeout"
_PRODUCER_TIMEOUT_PREFIX = "PRODUCER_TIMEOUT:"

# HOLD 可判定 reason 枚举(代码实证写入 current_stage 的值;§3 设计收口)
_HOLD_NAMED_STAGES = frozenset({
    "m4f_skill_failed", "l2_binding_failed", "revision_superseded",
    "verify_max_hold", "reverify_failed", "m5_verify_passed",
    "m5_verify_failed",
})

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 5


class GithubDrainError(Exception):
    """永久 drain 配置/合同错误(调用方拒绝启动或 delivery 终局 ERROR)。"""


# ── run_id 派生与复检 ────────────────────────────────────────────────────────

def derive_github_run_id(installation_id: int, repo: str, pr_number: int,
                         observed_head_sha: str) -> str:
    material = "%s\x00%s\x00%s\x00%s" % (installation_id, repo, pr_number,
                                         observed_head_sha)
    return "gh-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def validate_derived_run_id(run_id: str, installation_id: int, repo: str,
                            pr_number: int, observed_head_sha: str) -> None:
    expected = derive_github_run_id(installation_id, repo, pr_number,
                                    observed_head_sha)
    if run_id != expected or not GH_RUN_ID_RE.fullmatch(run_id):
        raise GithubDrainError("RUN_ID_NAMESPACE_INVALID")


# ── room map / policy allowlist(受限格式严格解析,零第三方依赖) ─────────────

def _read_lines(path: Path) -> list:
    return [line.rstrip("\n").rstrip("\r") for line in
            path.read_text(encoding="utf-8").splitlines()]


def parse_room_map(path) -> dict:
    """严格解析受限格式 room map:

        repos:
          "owner/name":
            room_id: "!room:server"

    拒绝: 任何其他键/形状、重复 repo、缺失 room_id、非法 room_id。"""
    lines = _read_lines(Path(path))
    mapping: dict = {}
    idx, state = 0, "expect_repos"
    current_repo = None
    for lineno, raw in enumerate(lines, start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if state == "expect_repos":
            if line != "repos:":
                raise GithubDrainError(
                    "room map line %d: expected 'repos:' header" % lineno)
            state = "expect_entry"
        elif state == "expect_entry":
            m = re.fullmatch(r'  "([^"]+)":', line)
            if not m:
                raise GithubDrainError(
                    "room map line %d: expected two-space-indented "
                    '"owner/name": entry' % lineno)
            repo = m.group(1)
            if not _REPO_RE.fullmatch(repo):
                raise GithubDrainError(
                    "room map line %d: invalid repo %r" % (lineno, repo))
            if repo in mapping:
                raise GithubDrainError(
                    "room map line %d: duplicate repo %r" % (lineno, repo))
            current_repo = repo
            mapping[repo] = None
            state = "expect_room"
        elif state == "expect_room":
            m = re.fullmatch(r'    room_id: "(.+)"', line)
            if not m:
                raise GithubDrainError(
                    "room map line %d: expected four-space-indented "
                    'room_id: "value"' % lineno)
            mapping[current_repo] = m.group(1)
            state = "expect_entry"
    if state == "expect_repos":
        raise GithubDrainError("room map: missing 'repos:' header")
    missing = [repo for repo, room in mapping.items() if not room]
    if missing:
        raise GithubDrainError("room map: missing room_id for %s" % missing)
    return mapping


def parse_policy_repo_allowlist(path) -> set:
    """严格提取 policy.yaml 的 repos.allowlist 列表项(受限格式)。"""
    lines = _read_lines(Path(path))
    allowlist: set = set()
    in_repos = in_allowlist = False
    for lineno, raw in enumerate(lines, start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            in_repos = line == "repos:"
            in_allowlist = False
            continue
        if in_repos and line == "  allowlist:":
            in_allowlist = True
            continue
        if in_allowlist:
            m = re.fullmatch(r'    - "([^"]+)"', line)
            if m:
                repo = m.group(1)
                if repo in allowlist:
                    raise GithubDrainError(
                        "policy allowlist line %d: duplicate %r"
                        % (lineno, repo))
                allowlist.add(repo)
                continue
            in_allowlist = False
    if not allowlist:
        raise GithubDrainError("policy allowlist: repos.allowlist empty/absent")
    return allowlist


def load_github_ingress_config(room_map_path, policy_path) -> dict:
    """1:1 对齐验证: policy repos.allowlist == room map keys(缺一不可)。"""
    mapping = parse_room_map(room_map_path)
    allowlist = parse_policy_repo_allowlist(policy_path)
    only_policy = sorted(allowlist - set(mapping))
    only_map = sorted(set(mapping) - allowlist)
    if only_policy or only_map:
        raise GithubDrainError(
            "room map / policy allowlist not 1:1 aligned "
            "(policy_only=%s map_only=%s)" % (only_policy, only_map))
    return {"rooms": mapping, "allowlist": allowlist}


# ── Checks desired 映射(有序 13+1 规则;纯函数,仅 DB 字段可判定) ────────────

def desired_check_state(*, status: str, current_stage, last_error,
                        rollback_status, stale: bool) -> tuple:
    """返回 (desired_status, desired_conclusion, reason)。

    互斥性 = 有序求值;完备性 = 兜底 internal_state_unmapped。
    仅使用数据库可判定字段(current_stage 结构化枚举 + 实证 last_error 前缀)。
    """
    if stale:
        return ("completed", "neutral",
                "stale_delivery_superseded_by_authoritative_read")
    if status == "SUBMITTED":
        return ("queued", None, "registered")
    if status == "RUNNING":
        return ("in_progress", None, "stage:%s" % (current_stage or "unknown"))
    if status == "APPROVAL_PENDING":
        return ("completed", "action_required", "l2_approval_pending")
    if status == "PASS":
        return ("completed", "success", "verify_passed")
    if status == "MERGED":
        return ("completed", "success", "merged")
    if status == "FAIL":
        return ("completed", "failure", "terminal_fail")
    if status == "ROLLED_BACK":
        if rollback_status == "RECOVERED":
            return ("completed", "success", "revert_reverified_recovered")
        if rollback_status == "REVERIFYING":
            return ("in_progress", None, "revert_applied_reverify_pending")
        return ("completed", "failure", "merge_reverted_rollback_executed")
    if status == "HOLD":
        if current_stage == _PRODUCER_TIMEOUT_STAGE or (
                isinstance(last_error, str)
                and last_error.startswith(_PRODUCER_TIMEOUT_PREFIX)):
            return ("completed", "neutral", "infra_hold_not_measurable")
        if current_stage in _HOLD_NAMED_STAGES:
            return ("completed", "neutral",
                    "hold_reason_%s" % current_stage)
        return ("completed", "neutral", "internal_hold_unclassified")
    return ("completed", "neutral", "internal_state_unmapped")


# ── drain_github_deliveries ─────────────────────────────────────────────────

_CLAIM_SQL = """
WITH candidate AS (
    SELECT delivery_id
    FROM   public.github_deliveries
    WHERE  (status = 'PENDING'  AND next_retry_at <= now())
        OR (status = 'RUNNING'   AND lease_expires_at < now())
    ORDER BY received_at, delivery_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE public.github_deliveries d
SET    status           = 'RUNNING',
       claim_id         = gen_random_uuid()::text,
       claimed_at       = now(),
       lease_expires_at = now() + make_interval(secs => %s),
       attempt_count    = d.attempt_count + 1
FROM   candidate c
WHERE  d.delivery_id = c.delivery_id
RETURNING d.delivery_id, d.claim_id, d.canonical_payload, d.attempt_count
"""

_SUCCESS_CONFIRM_SQL = """
UPDATE public.github_deliveries
SET    status = 'PROCESSED', processed_at = now(), derived_run_id = %s,
       error = NULL
WHERE  delivery_id = %s AND claim_id = %s AND status = 'RUNNING'
"""

_FAILURE_CONFIRM_SQL = """
UPDATE public.github_deliveries
SET    status = CASE WHEN attempt_count >= %s THEN 'ERROR' ELSE 'PENDING' END,
       next_retry_at = now() + make_interval(secs => %s),
       error = %s, claim_id = NULL
WHERE  delivery_id = %s AND claim_id = %s AND status = 'RUNNING'
"""


def _backoff_seconds(attempt: int) -> int:
    return min(30 * (2 ** max(0, attempt - 1)), 900)


def _dispatch_body(repo: str, pr_number: int, branch: str, run_id: str) -> str:
    return ("请审查 %s PR#%s (分支 %s)。用 gh-mcp-read.sh + sast-scan,"
            "findings 写 shared/tasks/%s-review/findings.md。"
            "完成写 TASK_COMPLETED: %s-review。"
            % (repo, pr_number, branch or "", run_id, run_id))


def drain_github_deliveries(conn_factory: Callable[[], Any], *,
                            config: Mapping,
                            max_items: int = 1,
                            lease_seconds: int = DEFAULT_LEASE_SECONDS,
                            max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                            approval_required: bool = False,
                            observer: Callable[[Mapping], None] = lambda e: None,
                            submit=submit_task) -> int:
    """认领并落库 PENDING/过期 RUNNING 的 pull_request delivery。

    返回本循环处理条数。所有确认均 claim_id CAS;lease 被接管时工作事务
    整体回滚。submit 参数仅供测试注入(production 恒为 task_submit.submit_task)。
    """
    rooms = config["rooms"]
    handled = 0
    for _ in range(max(1, int(max_items))):
        conn = conn_factory()
        claimed = None
        try:
            with conn.cursor() as cur:
                cur.execute(_CLAIM_SQL, (int(lease_seconds),))
                claimed = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if not claimed:
            break
        delivery_id, claim_id, canonical_payload, attempt = claimed
        try:
            payload = json.loads(canonical_payload \
                                 if isinstance(canonical_payload, str)
                                 else json.dumps(canonical_payload))
            run_id = _process_one(conn, delivery_id, claim_id, payload, rooms,
                                  approval_required, submit)
            with conn.cursor() as cur:
                cur.execute(_SUCCESS_CONFIRM_SQL,
                            (run_id, delivery_id, claim_id))
                confirmed = cur.rowcount
            conn.commit()
            if confirmed == 0:
                # lease 被接管:本事务的 run/stage_events 一并回滚(下面 rollback)。
                conn.rollback()
                observer({"event": "github.delivery.stale_confirm",
                          "delivery_id": delivery_id})
            else:
                observer({"event": "github.delivery.processed",
                          "delivery_id": delivery_id, "run_id": run_id})
        except (SubmitTaskError, GithubDrainError, SubmitTaskTransient) as exc:
            _confirm_failure(conn, delivery_id, claim_id, attempt, exc,
                             max_attempts, permanent=True)
            observer({"event": "github.delivery.error",
                      "delivery_id": delivery_id,
                      "code": getattr(exc, "code", type(exc).__name__)})
        except Exception as exc:  # 瞬时(DB/网络)——退避重试
            _confirm_failure(conn, delivery_id, claim_id, attempt, exc,
                             max_attempts, permanent=False)
            observer({"event": "github.delivery.retry",
                      "delivery_id": delivery_id,
                      "code": type(exc).__name__})
        handled += 1
    return handled


def _process_one(conn, delivery_id, claim_id, payload, rooms,
                 approval_required, submit) -> str:
    """工作事务:通道门 → stage_events(rowcount 门)→ submit_task → marks。

    绝不 commit;成功确认由调用方在同一事务追加后统一 commit。
    """
    event_name = payload.get("event_name")
    if event_name != "pull_request":
        raise GithubDrainError("EVENT_NOT_DRAINABLE")
    repo = payload["repo"]
    room_id = rooms.get(repo)
    if not room_id:
        raise GithubDrainError("ROOM_MAPPING_MISSING")
    installation_id = int(payload["installation_id"])
    pr_number = int(payload["pr_number"])
    branch = str(payload.get("branch") or "")
    observed_head_sha = payload["observed_head_sha"]
    run_id = derive_github_run_id(installation_id, repo, pr_number,
                                  observed_head_sha)
    validate_derived_run_id(run_id, installation_id, repo, pr_number,
                            observed_head_sha)
    if not GH_RUN_ID_RE.fullmatch(run_id):
        raise GithubDrainError("RUN_ID_NAMESPACE_INVALID")

    event_id = "gh:%s" % delivery_id
    if not _DELIVERY_EVENT_ID_RE.fullmatch(event_id):
        raise GithubDrainError("EVENT_ID_INVALID")
    sender_identity = "github-app[%s]" % installation_id
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO stage_events(event_id, room_id, sender, event_type,
                                        raw_body, body_sha256, status)
               VALUES(%s, %s, %s, 'TASK_SUBMITTED', %s, %s, 'RECEIVED')
               ON CONFLICT (event_id) DO NOTHING""",
            (event_id, room_id, sender_identity, canonical[:2000],
             payload.get("body_sha256", "")[:16]))
        if cur.rowcount != 1:
            # 读取现有状态仅为充实错误;绝不调用 submit/mark/update。
            cur.execute(
                "SELECT status FROM stage_events WHERE event_id=%s",
                (event_id,))
            existing_status = cur.fetchone()
            raise GithubDrainError(
                "STAGE_EVENT_ID_COLLISION:%s"
                % (existing_status[0] if existing_status else "absent"))

    submission = TaskSubmission(
        run_id=run_id, room_id=room_id, repo=repo, pr_number=pr_number,
        branch=branch or None, approval_required=approval_required,
        dispatch_body=_dispatch_body(repo, pr_number, branch, run_id))
    result = submit(conn, submission, EventSource(
        channel="github", event_id=event_id,
        sender_identity=sender_identity))

    # marks 仅作用于本事务刚插入的 stage_events 行(event_id 精确匹配)。
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stage_events SET status='PROCESSED', processed_at=now() "
            "WHERE event_id=%s", (event_id,))
        cur.execute(
            "UPDATE stage_events SET run_id=%s, stage='review' "
            "WHERE event_id=%s", (run_id, event_id))
    return result.run_id


def _confirm_failure(conn, delivery_id, claim_id, attempt, exc,
                     max_attempts, permanent: bool) -> None:
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        terminal = permanent or attempt >= max_attempts
        error_text = "%s: %s" % (getattr(exc, "code", type(exc).__name__),
                                 " ".join(str(exc).split())[:360])
        with conn.cursor() as cur:
            cur.execute(_FAILURE_CONFIRM_SQL,
                        (int(max_attempts),
                         _backoff_seconds(attempt),
                         ("PERMANENT " if permanent else "") + error_text,
                         delivery_id, claim_id))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


# ── reconcile_github_checks(Controller 只 upsert desired,不发 HTTP) ────────

_UPSERT_CHECK_SQL = """
INSERT INTO public.github_check_outbox
       (outbox_id, run_id, repo, pr_number, observed_head_sha, external_id,
        desired_status, desired_conclusion, desired_version, publish_state)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 'PENDING')
ON CONFLICT (external_id) DO UPDATE
SET desired_status     = EXCLUDED.desired_status,
    desired_conclusion = EXCLUDED.desired_conclusion,
    observed_head_sha  = EXCLUDED.observed_head_sha,
    check_run_id       = CASE WHEN public.github_check_outbox.observed_head_sha
                                 IS DISTINCT FROM EXCLUDED.observed_head_sha
                              THEN NULL
                              ELSE public.github_check_outbox.check_run_id END,
    claim_id           = NULL,
    publish_state      = CASE WHEN public.github_check_outbox.publish_state
                                 IN ('LEASED','PUBLISHED','TERMINAL')
                              THEN 'PENDING'
                              ELSE public.github_check_outbox.publish_state END,
    desired_version    = public.github_check_outbox.desired_version + 1,
    -- M8-GH-4B2 (R4 section-4 plan A): a REAL version increase grants a
    -- fresh attempt budget; a same-version upsert never touches the row
    -- (the WHERE gate below), so TERMINAL is never revived by echoes.
    attempt_count      = 0,
    last_error         = NULL,
    updated_at         = now()
WHERE  (public.github_check_outbox.desired_status,
        public.github_check_outbox.desired_conclusion,
        public.github_check_outbox.observed_head_sha)
       IS DISTINCT FROM
       (EXCLUDED.desired_status,
        EXCLUDED.desired_conclusion,
        EXCLUDED.observed_head_sha)
"""


def reconcile_github_checks(conn_factory: Callable[[], Any], *,
                            observer: Callable[[Mapping], None] = lambda e: None,
                            mapper=desired_check_state) -> int:
    """扫描 gh- run,按有序规则 upsert desired(三元组未变不加版本)。"""
    conn = conn_factory()
    updated = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.run_id, r.status, r.current_stage, r.last_error,
                          r.repo, r.pr_number
                   FROM public.task_runs r
                   WHERE r.run_id LIKE 'gh-%'""")
            runs = cur.fetchall()
        conn.commit()
        for (run_id, status, current_stage, last_error, repo,
             pr_number) in runs:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT head_sha FROM public.run_pr_bindings "
                    "WHERE run_id=%s", (run_id,))
                binding = cur.fetchone()
                cur.execute(
                    "SELECT observed_head_sha FROM public.github_deliveries "
                    "WHERE derived_run_id=%s ORDER BY received_at DESC LIMIT 1",
                    (run_id,))
                observed = cur.fetchone()
                cur.execute(
                    "SELECT status FROM public.rollback_runs "
                    "WHERE parent_run_id=%s ORDER BY created_at DESC LIMIT 1",
                    (run_id,))
                rollback = cur.fetchone()
            conn.commit()
            observed_sha = observed[0] if observed else None
            if not observed_sha:
                continue  # 无 delivery 观察值(理论不可达)——跳过,不猜
            stale = bool(binding and binding[0]
                         and binding[0] != observed_sha)
            desired_status, desired_conclusion, reason = mapper(
                status=status, current_stage=current_stage,
                last_error=last_error,
                rollback_status=rollback[0] if rollback else None,
                stale=stale)
            outbox_id = "chk-" + hashlib.sha256(
                run_id.encode("utf-8")).hexdigest()[:24]
            external_id = "mergepilot/%s" % run_id
            with conn.cursor() as cur:
                cur.execute(_UPSERT_CHECK_SQL,
                            (outbox_id, run_id, repo, pr_number, observed_sha,
                             external_id, desired_status, desired_conclusion))
                updated += cur.rowcount
            conn.commit()
            observer({"event": "github.check.desired", "run_id": run_id,
                      "status": desired_status,
                      "conclusion": desired_conclusion, "reason": reason})
        return updated
    except Exception:
        conn.rollback()
        raise


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "GH_RUN_ID_RE",
    "GithubDrainError",
    "desired_check_state",
    "derive_github_run_id",
    "drain_github_deliveries",
    "load_github_ingress_config",
    "parse_policy_repo_allowlist",
    "parse_room_map",
    "reconcile_github_checks",
    "validate_derived_run_id",
]
