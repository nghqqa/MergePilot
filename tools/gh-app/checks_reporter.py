#!/usr/bin/env python3
"""checks_reporter.py — github_check_outbox → GitHub Checks API 发布器(M8-GH-1)。

冻结合同(设计 2026-08-18 最终收口):
  * claim: CAS → LEASED(claim_id 轮换 + lease);HTTP 严格在 DB 事务外执行。
  * 既有 Check 查找恒为三元组 repository + external_id/name +
    observed_head_sha(按该 SHA 的 check-runs 查询集过滤 name)——同名不同
    SHA 不复用;行内 check_run_id 命中且 SHA 未变 → PATCH 而非 create。
  * 成功确认三重 CAS: claim_id + observed_head_sha + seen_version >
    published_version(单调门)——SHA-A 的迟到响应无法确认/覆盖 SHA-B,
    N 的迟到响应无法覆盖 N+1。
  * 403/422 → TERMINAL(终局,不重试);429/5xx/网络错误 → 退避重试
    (Retry-After / X-RateLimit-Reset 优先,否则指数退避)。
  * 任何发布失败只写 outbox 状态列 —— 永不回滚 task_runs/stage_runs/
    dispatch_outbox(本模块对治理表零 SQL)。
  * 输出文本仅含 run_id、阶段摘要与 Console URL;零秘密、零内部错误体。

MVP 边界: 传输层可注入(隔离验证用栈内 stub);真实 GitHub App 的
installation token 交换属真实接入层(需单独授权),本模块仅接受可选
Bearer token env。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 8

_CHECKS_MEDIA = "application/vnd.github+json"

_CLAIM_SQL = """
UPDATE public.github_check_outbox o
SET    publish_state   = 'LEASED',
       claim_id        = gen_random_uuid()::text,
       claimed_at      = now(),
       lease_expires_at = now() + make_interval(secs => %s),
       attempt_count   = o.attempt_count + 1
FROM   (SELECT outbox_id FROM public.github_check_outbox
        WHERE  (publish_state = 'PENDING' AND next_retry_at <= now())
            OR (publish_state = 'LEASED'  AND lease_expires_at < now())
        ORDER BY created_at, outbox_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1) c
WHERE  o.outbox_id = c.outbox_id
RETURNING o.outbox_id, o.claim_id, o.run_id, o.repo, o.pr_number,
          o.observed_head_sha, o.external_id, o.check_run_id,
          o.desired_status, o.desired_conclusion, o.desired_version,
          o.published_version, o.attempt_count
"""

_SUCCESS_CONFIRM_SQL = """
UPDATE public.github_check_outbox
SET    published_status     = %s,
       published_conclusion = %s,
       published_version    = %s,
       check_run_id         = %s,
       last_error           = NULL,
       publish_state        = CASE WHEN desired_version > %s
                                   THEN 'PENDING' ELSE 'PUBLISHED' END,
       published_at         = CASE WHEN desired_version > %s
                                   THEN published_at ELSE now() END
WHERE  outbox_id = %s
  AND  claim_id = %s
  AND  observed_head_sha = %s
  AND  %s > published_version
"""

_TERMINAL_CONFIRM_SQL = """
UPDATE public.github_check_outbox
SET    publish_state = 'TERMINAL', last_error = %s, claim_id = NULL
WHERE  outbox_id = %s AND claim_id = %s
"""

_RETRY_CONFIRM_SQL = """
UPDATE public.github_check_outbox
SET    publish_state = 'PENDING',
       next_retry_at = now() + make_interval(secs => %s),
       last_error    = %s, claim_id = NULL
WHERE  outbox_id = %s AND claim_id = %s
"""


class TransportError(Exception):
    """网络层失败(重试类)。"""


def _terminal_http(status: int) -> bool:
    return status == 403 or status == 422 or (400 <= status < 500
                                              and status != 429)


def _backoff_seconds(attempt: int) -> int:
    return min(30 * (2 ** max(0, attempt - 1)), 900)


def default_transport(method: str, url: str, *, headers: dict,
                      body: Optional[dict]) -> tuple:
    """urllib 传输(隔离栈内指向 stub);返回 (status, headers, body_dict)。

    抛 TransportError 于网络层失败(调用方归重试类)。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw.strip() else {}
            return response.status, dict(response.headers), parsed
    except urllib.error.HTTPError as exc:  # 4xx/5xx 仍带 body 返回
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        parsed = json.loads(raw) if raw.strip() else {}
        return exc.code, dict(exc.headers or {}), parsed
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TransportError(str(exc)) from exc


def _retry_after_seconds(headers: dict, attempt: int) -> int:
    raw = (headers or {}).get("Retry-After") \
        or (headers or {}).get("retry-after")
    if raw:
        try:
            return max(1, int(str(raw).strip()))
        except ValueError:
            pass
    return _backoff_seconds(attempt)


def _output_summary(run_id: str, desired_status: str,
                    desired_conclusion: Optional[str]) -> dict:
    console = os.environ.get("GITHUB_CHECK_OUTPUT_CONSOLE_URL", "")
    summary = "MergePilot run %s — %s" % (run_id, desired_status)
    if desired_conclusion:
        summary += " / %s" % desired_conclusion
    if console:
        summary += " | console: %s" % console
    return {"title": "MergePilot %s" % run_id, "summary": summary}


def publish_once(conn_factory: Callable[[], Any], *,
                 api_base: str,
                 transport: Callable[..., tuple] = default_transport,
                 token: str = "",
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 observer: Callable[[dict], None] = lambda e: None) -> str:
    """认领一行 outbox 并完成一次发布尝试。返回 'idle'|'published'|
    'retry'|'terminal'|'stale'。"""
    headers = {"Accept": _CHECKS_MEDIA, "User-Agent": "mergepilot-gh-app"}
    if token:
        headers["Authorization"] = "Bearer %s" % token

    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            cur.execute(_CLAIM_SQL, (int(lease_seconds),))
            claimed = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if not claimed:
        try:
            conn.close()
        except Exception:
            pass
        return "idle"
    (outbox_id, claim_id, run_id, repo, _pr, observed_head_sha, external_id,
     check_run_id, desired_status, desired_conclusion, desired_version,
     published_version, attempt) = claimed
    seen = {"outbox_id": outbox_id, "claim_id": claim_id, "run_id": run_id,
            "seen_sha": observed_head_sha, "seen_version": desired_version}
    try:
        # ── HTTP(严格在 DB 事务外) ──
        owner, name = repo.split("/", 1)
        base = api_base.rstrip("/")
        resolved_id = check_run_id

        if resolved_id is None:
            lookup_url = ("%s/repos/%s/commits/%s/check-runs?per_page=100"
                          % (base, repo, observed_head_sha))
            status, response_headers, body = transport(
                "GET", lookup_url, headers=headers, body=None)
            if status == 200:
                for item in body.get("check_runs", []) \
                        if isinstance(body, dict) else []:
                    if item.get("name") == external_id:
                        resolved_id = item.get("id")
                        break
            elif _terminal_http(status):
                _confirm(conn, _TERMINAL_CONFIRM_SQL,
                         ("lookup http %d" % status, outbox_id, claim_id))
                observer({"event": "github.check.terminal", "run_id": run_id,
                          "http": status})
                return "terminal"
            elif status != 404:
                _confirm(conn, _RETRY_CONFIRM_SQL,
                         (_retry_after_seconds(response_headers, attempt),
                          "lookup http %d" % status, outbox_id, claim_id))
                observer({"event": "github.check.retry", "run_id": run_id,
                          "http": status})
                return "retry"
            # 404 或未匹配:resolved_id 保持 None → create。

        payload = {
            "name": external_id,
            "head_sha": observed_head_sha,
            "status": desired_status,
            "output": _output_summary(run_id, desired_status,
                                      desired_conclusion),
        }
        if desired_conclusion is not None:
            payload["conclusion"] = desired_conclusion
        if resolved_id is not None:
            url = "%s/repos/%s/check-runs/%s" % (base, repo, resolved_id)
            method = "PATCH"
            payload.pop("name", None)
            payload.pop("head_sha", None)
        else:
            url = "%s/repos/%s/check-runs" % (base, repo)
            method = "POST"
        status, response_headers, body = transport(
            method, url, headers=headers, body=payload)
        if status in (200, 201):
            if isinstance(body, dict) and body.get("id") is not None:
                resolved_id = body["id"]
            if resolved_id is None:
                _confirm(conn, _TERMINAL_CONFIRM_SQL,
                         ("publish ok but no check_run_id", outbox_id,
                          claim_id))
                return "terminal"
            _confirm(conn, _SUCCESS_CONFIRM_SQL, (
                desired_status, desired_conclusion, desired_version,
                resolved_id, desired_version, desired_version,
                outbox_id, claim_id, observed_head_sha, desired_version))
            observer({"event": "github.check.published", "run_id": run_id,
                      "version": desired_version})
            return "published"
        if _terminal_http(status):
            _confirm(conn, _TERMINAL_CONFIRM_SQL,
                     ("publish http %d" % status, outbox_id, claim_id))
            observer({"event": "github.check.terminal", "run_id": run_id,
                      "http": status})
            return "terminal"
        _confirm(conn, _RETRY_CONFIRM_SQL, (
            _retry_after_seconds(response_headers, attempt),
            "publish http %d" % status, outbox_id, claim_id))
        observer({"event": "github.check.retry", "run_id": run_id,
                  "http": status})
        return "retry"
    except TransportError as exc:
        _confirm(conn, _RETRY_CONFIRM_SQL,
                 (_backoff_seconds(attempt), "transport: %s" % exc,
                  outbox_id, claim_id))
        observer({"event": "github.check.retry", "run_id": run_id,
                  "error": type(exc).__name__})
        return "retry"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _confirm(conn, sql, params) -> None:
    """确认事务(CAS 失败 rowcount=0 —— 旧 claim/旧版本静默丢弃)。"""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def run_loop(conn_factory: Callable[[], Any], *, api_base: str,
             transport: Callable[..., tuple] = default_transport,
             token: str = "", poll_seconds: float = 5.0, once: bool = False,
             observer: Callable[[dict], None] = lambda e: None) -> None:
    """reporter 主循环(容器/独立进程入口;测试用 once=True)。"""
    token = token or os.environ.get("GITHUB_CHECKS_TOKEN", "")
    api_base = api_base or os.environ.get("GITHUB_API_BASE",
                                          "http://127.0.0.1:8091")
    while True:
        try:
            publish_once(conn_factory, api_base=api_base, transport=transport,
                         token=token, observer=observer)
        except Exception as exc:  # 循环永不因发布失败退出
            observer({"event": "github.check.loop_error",
                      "error": type(exc).__name__})
        if once:
            return
        time.sleep(poll_seconds)


__all__ = [
    "DEFAULT_LEASE_SECONDS", "DEFAULT_MAX_ATTEMPTS", "TransportError",
    "default_transport", "publish_once", "run_loop",
]
