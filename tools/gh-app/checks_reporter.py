#!/usr/bin/env python3
"""checks_reporter.py — github_check_outbox → GitHub Checks API 发布器。

冻结合同(M8-GH-1 起;M8-GH-4B2 扩展 G3):
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

M8-GH-4B2 (G3) 扩展:
  * ``token_provider``(GitHubAppTokenProvider)成为 E2E 生产认证来源:
    每次发布尝试取一次有效 token(lookup 与 publish 共享同一认证上下文);
    收到 401 → invalidate + 强制刷新一次并完整重试当前 HTTP 操作一次;
    第二次 401 按终局认证失败处理(无无限刷新)。Authorization 不进入
    observer、日志或异常。静态 GITHUB_CHECKS_TOKEN 仅保留给非生产 fake
    栈的显式注入;api.github.com 生产模式下存在静态 token 即 fail-closed。
  * max_attempts 原子终止(R4 §4 方案 A,零 schema 变更):
    - 每轮 claim 前原子收割(PENDING/过期 LEASED 且 attempt>=max →
      TERMINAL / claim_id=NULL / last_error='MAX_ATTEMPTS');
    - claim 仅选 attempt_count < max;
    - 失败确认时 attempt>=max → 以 outbox_id+claim_id CAS 直接 TERMINAL
      (last_error='MAX_ATTEMPTS:<分类>'),不回 PENDING;
    - 最后一次调用期间崩溃 → lease 过期后仅收割,零额外 HTTP;
    - CAS rowcount=0(丢 claim)→ 零写入,不动新 claim/新版本。
    desired_version 真实增加时的预算复位(attempt_count=0)由
    github_drain._UPSERT_CHECK_SQL 负责;同版本 upsert 不复活 TERMINAL。
  * SIGTERM:停止领取新任务,完成当前 publish_once 后退出(lease 过期
    兜底崩溃恢复)。
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 8

_CHECKS_MEDIA = "application/vnd.github+json"

_REAP_SQL = """
UPDATE public.github_check_outbox
SET    publish_state = 'TERMINAL',
       claim_id      = NULL,
       last_error    = 'MAX_ATTEMPTS'
WHERE  ((publish_state = 'PENDING' AND next_retry_at <= now())
        OR (publish_state = 'LEASED' AND lease_expires_at < now()))
  AND  attempt_count >= %s
"""

_CLAIM_SQL = """
UPDATE public.github_check_outbox o
SET    publish_state   = 'LEASED',
       claim_id        = gen_random_uuid()::text,
       claimed_at      = now(),
       lease_expires_at = now() + make_interval(secs => %s),
       attempt_count   = o.attempt_count + 1
FROM   (SELECT outbox_id FROM public.github_check_outbox
        WHERE  ((publish_state = 'PENDING' AND next_retry_at <= now())
                OR (publish_state = 'LEASED' AND lease_expires_at < now()))
          AND  attempt_count < %s
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


def _error_class(status: Optional[int], phase: str,
                 error: Optional[BaseException]) -> str:
    """安全分类(仅类别,无正文):用于 MAX_ATTEMPTS:<分类>。"""
    if error is not None:
        name = type(error).__name__
        if name == "TransportError":
            return "TRANSPORT"
        return "TOKEN"
    if status is None:
        return "UNKNOWN"
    if status == 429:
        return "HTTP_429"
    if status >= 500:
        return "HTTP_5XX"
    return "HTTP_%d" % status


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


class _AuthProvider:
    """每次发布尝试的认证上下文:provider 模式下 lookup 与 publish 共享
    同一次 get_token;401 → 单次 invalidate+强刷+重试当前操作;第二次
    401 落入终局分类。Authorization 头绝不进入 observer/日志/异常。"""

    def __init__(self, *, token_provider=None, static_token: str = ""):
        self._provider = token_provider
        self._static = static_token
        self.auth_retried = False

    def headers(self) -> dict:
        base = {"Accept": _CHECKS_MEDIA, "User-Agent": "mergepilot-gh-app"}
        if self._provider is not None:
            base["Authorization"] = "Bearer %s" \
                % self._provider.get_token()
        elif self._static:
            base["Authorization"] = "Bearer %s" % self._static
        return base

    def on_401(self):
        """返回 True 表示已强制刷新、应重试当前 HTTP 操作一次;
        返回 False 表示重试预算已耗尽(第二次 401 走终局)。"""
        if self._provider is None or self.auth_retried:
            return False
        self.auth_retried = True
        self._provider.invalidate()
        self._provider.get_token(force_refresh=True)
        return True


class _ProviderFailure(Exception):
    """Classified token-provider failure. Carries ONLY the safe code,
    terminal flag and optional retry_after — never the exception text,
    token, JWT or response body."""

    def __init__(self, *, terminal: bool, code: str,
                 retry_after=None, error=None):
        self.terminal = terminal
        self.code = code
        self.retry_after = retry_after
        self.error = error
        super().__init__("%s:%s" % ("TOKEN_TERMINAL" if terminal
                                    else "TOKEN_RETRY", code))


def _classify_provider_error(exc) -> tuple:
    """(terminal, code, retry_after) for a provider exception.
    TokenExchangeTerminalError (403/422/malformed/scope-mismatch) is
    IMMEDIATELY terminal; TokenExchangeRetryError (429/5xx/network/
    exchange-401) is retry-class with its Retry-After; anything unknown
    degrades to the safe retry class 'TOKEN'."""
    try:
        import token_provider as _tp
    except ImportError:  # pragma: no cover - image always ships it
        return (False, "TOKEN", None)
    if isinstance(exc, _tp.TokenExchangeTerminalError):
        return (True, exc.code, None)
    if isinstance(exc, _tp.TokenExchangeRetryError):
        return (False, exc.code, exc.retry_after)
    return (False, "TOKEN", None)


def publish_once(conn_factory: Callable[[], Any], *,
                 api_base: str,
                 transport: Callable[..., tuple] = default_transport,
                 token: str = "",
                 token_provider=None,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 observer: Callable[[dict], None] = lambda e: None) -> str:
    """认领一行 outbox 并完成一次发布尝试。返回 'idle'|'published'|
    'retry'|'terminal'。max_attempts 真实进入收割/claim/失败确认 SQL。"""
    max_attempts = max(1, int(max_attempts))
    conn = conn_factory()
    try:
        # 原子收割(最后一次调用期间崩溃 → lease 过期后在此终结,零 HTTP)
        with conn.cursor() as cur:
            cur.execute(_REAP_SQL, (max_attempts,))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(_CLAIM_SQL, (int(lease_seconds), max_attempts))
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

    def _fail(status, phase, response_headers=None, error=None):
        """失败确认:达 max → TERMINAL(MAX_ATTEMPTS:<分类>);否则 RETRY。"""
        if attempt >= max_attempts:
            _confirm(conn, _TERMINAL_CONFIRM_SQL,
                     ("MAX_ATTEMPTS:%s" % _error_class(status, phase, error),
                      outbox_id, claim_id))
            observer({"event": "github.check.terminal_max", "run_id": run_id,
                      "attempt": attempt})
            return "terminal"
        delay = _retry_after_seconds(response_headers or {}, attempt) \
            if error is None else _backoff_seconds(attempt)
        label = ("%s http %d" % (phase, status) if error is None
                 else "%s %s" % (phase, type(error).__name__))
        _confirm(conn, _RETRY_CONFIRM_SQL,
                 (delay, label, outbox_id, claim_id))
        observer({"event": "github.check.retry", "run_id": run_id,
                  "http": status})
        return "retry"

    def _fail_token(failure: _ProviderFailure) -> str:
        """Provider 失败确认:terminal 类不经 max 直接 CAS 置 TERMINAL
        (last_error=TOKEN_TERMINAL:<code>);retry 类在达 max 时置
        MAX_ATTEMPTS:TOKEN,否则回 PENDING(retry_after 优先)。"""
        if failure.terminal:
            _confirm(conn, _TERMINAL_CONFIRM_SQL,
                     ("TOKEN_TERMINAL:%s" % failure.code,
                      outbox_id, claim_id))
            observer({"event": "github.check.token_terminal",
                      "run_id": run_id, "code": failure.code})
            return "terminal"
        if attempt >= max_attempts:
            _confirm(conn, _TERMINAL_CONFIRM_SQL,
                     ("MAX_ATTEMPTS:TOKEN", outbox_id, claim_id))
            observer({"event": "github.check.terminal_max", "run_id": run_id,
                      "attempt": attempt})
            return "terminal"
        delay = failure.retry_after if failure.retry_after is not None \
            else _backoff_seconds(attempt)
        _confirm(conn, _RETRY_CONFIRM_SQL,
                 (delay, "token %s" % failure.code, outbox_id, claim_id))
        observer({"event": "github.check.token_retry", "run_id": run_id,
                  "code": failure.code})
        return "retry"

    auth = _AuthProvider(token_provider=token_provider, static_token=token)

    def _call(method, url, body):
        """一个 HTTP 操作:取认证头(初始 token 获取)、执行、401 时
        invalidate+强刷一次并完整重试同一操作。三个点上的 provider
        异常都分类为 _ProviderFailure,绝不落入无分类的通用 except。"""
        try:
            headers = auth.headers()
        except Exception as exc:
            terminal, code, retry_after = _classify_provider_error(exc)
            raise _ProviderFailure(terminal=terminal, code=code,
                                   retry_after=retry_after, error=exc) from None
        status, response_headers, parsed = transport(
            method, url, headers=headers, body=body)
        if status == 401:
            try:
                retry = auth.on_401()
            except Exception as exc:
                terminal, code, retry_after = _classify_provider_error(exc)
                raise _ProviderFailure(terminal=terminal, code=code,
                                       retry_after=retry_after,
                                       error=exc) from None
            if retry:
                try:
                    headers = auth.headers()
                except Exception as exc:
                    terminal, code, retry_after = \
                        _classify_provider_error(exc)
                    raise _ProviderFailure(terminal=terminal, code=code,
                                           retry_after=retry_after,
                                           error=exc) from None
                status, response_headers, parsed = transport(
                    method, url, headers=headers, body=body)
        return status, response_headers, parsed

    try:
        owner, name = repo.split("/", 1)
        base = api_base.rstrip("/")
        resolved_id = check_run_id

        if resolved_id is None:
            lookup_url = ("%s/repos/%s/commits/%s/check-runs?per_page=100"
                          % (base, repo, observed_head_sha))
            status, response_headers, body = _call(
                "GET", lookup_url, None)
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
                return _fail(status, "lookup", response_headers)
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
        status, response_headers, body = _call(method, url, payload)
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
        return _fail(status, "publish", response_headers)
    except _ProviderFailure as failure:
        return _fail_token(failure)
    except TransportError as exc:
        return _fail(None, "transport", error=exc)
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


class _StopFlag:
    def __init__(self):
        self.flag = False


def run_loop(conn_factory: Callable[[], Any], *, api_base: str,
             transport: Callable[..., tuple] = default_transport,
             token: str = "", token_provider=None, poll_seconds: float = 5.0,
             once: bool = False, lease_seconds: int = DEFAULT_LEASE_SECONDS,
             max_attempts: int = DEFAULT_MAX_ATTEMPTS,
             observer: Callable[[dict], None] = lambda e: None) -> None:
    """reporter 主循环(容器/独立进程入口;测试用 once=True)。

    SIGTERM:停止领取新任务,完成当前 publish_once 后退出(连接由
    publish_once 自行关闭);崩溃恢复依赖 lease 过期收割。"""
    stop = _StopFlag()

    def _on_term(_signum, _frame):
        stop.flag = True

    previous = None
    if not once:
        try:
            if threading.current_thread() is threading.main_thread():
                previous = signal.signal(signal.SIGTERM, _on_term)
        except (ValueError, OSError):
            previous = None
    try:
        while True:
            if stop.flag:
                observer({"event": "github.check.stopping"})
                break
            try:
                publish_once(conn_factory, api_base=api_base,
                             transport=transport, token=token,
                             token_provider=token_provider,
                             lease_seconds=lease_seconds,
                             max_attempts=max_attempts, observer=observer)
            except Exception as exc:  # 循环永不因发布失败退出
                observer({"event": "github.check.loop_error",
                          "error": type(exc).__name__})
            if once:
                return
            # 可中断的轮询间隔(SIGTERM 后最长 1s 内退出)
            remaining = poll_seconds
            while remaining > 0 and not stop.flag:
                step = min(1.0, remaining)
                time.sleep(step)
                remaining -= step
            if stop.flag:
                observer({"event": "github.check.stopping"})
                break
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGTERM, previous)
            except (ValueError, OSError):
                pass


__all__ = [
    "DEFAULT_LEASE_SECONDS", "DEFAULT_MAX_ATTEMPTS", "TransportError",
    "default_transport", "main", "publish_once", "run_loop",
]

import sys  # noqa: E402  (kept last: only the standalone entry needs it)


def main() -> int:
    """Standalone reporter entry: poll the outbox forever from env config.

    Env (M8-GH-4B2): GITHUB_PUBLISHER_DSN (required);
    GITHUB_API_BASE — 'https://api.github.com' selects PRODUCTION mode,
    which REQUIRES the GitHubAppTokenProvider env contract and FORBIDS a
    static GITHUB_CHECKS_TOKEN (fail-closed). Any other base keeps the
    legacy fake-stack behavior (optional static token) for tests/stubs.
    """
    dsn = os.environ.get("GITHUB_PUBLISHER_DSN", "")
    if not dsn:
        sys.stderr.write("[gh-reporter] missing GITHUB_PUBLISHER_DSN\n")
        return 3
    api_base = os.environ.get("GITHUB_API_BASE",
                              "http://127.0.0.1:8091")
    token_provider = None
    static_token = ""
    if api_base == "https://api.github.com":
        static_token = os.environ.get("GITHUB_CHECKS_TOKEN", "")
        if static_token:
            sys.stderr.write("[gh-reporter] static GITHUB_CHECKS_TOKEN is "
                             "forbidden in production api.github.com mode\n")
            return 3
        from token_provider import (GitHubAppTokenProvider,
                                    TokenConfigError, TokenProviderConfig)
        try:
            token_provider = GitHubAppTokenProvider(
                TokenProviderConfig.from_env())
        except TokenConfigError as exc:
            sys.stderr.write("[gh-reporter] %s: %s\n"
                             % (exc.code, exc.detail))
            return 3
    else:
        static_token = os.environ.get("GITHUB_CHECKS_TOKEN", "")

    def _cf():
        import psycopg2
        from dsn_guard import ensure_connect_timeout
        return psycopg2.connect(ensure_connect_timeout(dsn))

    run_loop(_cf,
             api_base=api_base,
             token=static_token,
             token_provider=token_provider,
             poll_seconds=float(os.environ.get(
                 "GH_REPORTER_POLL_SECONDS",
                 os.environ.get("GH_REPORTER_POLL", "5"))),
             lease_seconds=int(os.environ.get("GH_REPORTER_LEASE_SECONDS",
                                              str(DEFAULT_LEASE_SECONDS))),
             max_attempts=int(os.environ.get("GH_REPORTER_MAX_ATTEMPTS",
                                             str(DEFAULT_MAX_ATTEMPTS))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
