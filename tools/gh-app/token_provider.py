#!/usr/bin/env python3
"""token_provider.py — GitHub App JWT + scoped installation token provider.

M8-GH-4B2 (G3) frozen contract:

  * Config (all required, no defaults): ``GITHUB_APP_ID``,
    ``GITHUB_INSTALLATION_ID``, ``GITHUB_REPOSITORY_ID``,
    ``GITHUB_PRIVATE_KEY_PATH`` (frozen container path
    ``/run/secrets/github-app-private-key.pem``) and ``GITHUB_API_BASE``
    (production MUST be exactly ``https://api.github.com``).
  * App JWT: RS256, ``iss`` = App ID, ``iat`` = now-60, ``exp`` = now+480;
    freshly signed for EVERY exchange, never cached; base64url without
    padding; the PEM is read ONLY from the fixed read-only file path.
  * Exchange: ``POST {base}/app/installations/{id}/access_tokens`` with the
    R4-frozen scoped body
    ``{"permissions": {"checks": "write", "metadata": "read"},
    "repository_ids": [<fixture repository numeric ID>]}``.
  * The installation token + expires_at live ONLY in process memory;
    refresh at T-300s; ``threading.Lock`` single-flight so concurrent
    callers share one exchange; 401 lets the caller force ONE refresh per
    publish attempt (the second 401 is terminal).
  * Errors carry ONLY status codes / error kinds — never the token, JWT,
    PEM bytes, signatures or response bodies.

Secret-handling invariants: PEM/JWT/token never appear in logs, exception
messages, ``repr`` or ``argv``; the Authorization header is built by the
consumer at request time and never logged here.

A fake API is reachable ONLY via an explicitly injected ``transport`` /
``api_base`` in tests — production never falls back implicitly.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
from typing import Callable, Optional

PRODUCTION_API_BASE = "https://api.github.com"
FROZEN_PRIVATE_KEY_PATH = "/run/secrets/github-app-private-key.pem"

JWT_IAT_SKEW_SECONDS = 60
JWT_TTL_SECONDS = 480
REFRESH_MARGIN_SECONDS = 300
MAX_EXCHANGE_ATTEMPTS = 1   # one exchange per get_token call; no internal loops

PERMISSIONS_REQUEST = {"checks": "write", "metadata": "read"}


class TokenProviderError(Exception):
    """Base; ``detail`` never contains token/JWT/PEM material."""

    def __init__(self, code: str, detail: str,
                 retry_after: Optional[int] = None):
        self.code = code
        self.detail = detail
        self.retry_after = retry_after
        super().__init__("%s: %s" % (code, detail))


class TokenConfigError(TokenProviderError):
    """Fail-closed configuration error (static, no network)."""


class TokenExchangeTerminalError(TokenProviderError):
    """401 (second), 403, 422, malformed response, scope mismatch."""


class TokenExchangeRetryError(TokenProviderError):
    """429 (Retry-After honored), 5xx, network — bounded backoff advised."""


class TokenProviderConfig:
    """Strict validated config; values are non-secret except the key path
    itself (a path, not key material)."""

    def __init__(self, *, app_id: str, installation_id: str,
                 repository_id: str, private_key_path: str,
                 api_base: str):
        for name, value in (("app_id", app_id),
                            ("installation_id", installation_id),
                            ("repository_id", repository_id)):
            if not isinstance(value, str) or not value.isdigit() \
                    or int(value) <= 0:
                raise TokenConfigError(
                    "TOKEN_CONFIG_INVALID",
                    "%s must be a positive numeric string" % name)
        if private_key_path != FROZEN_PRIVATE_KEY_PATH:
            raise TokenConfigError(
                "TOKEN_CONFIG_INVALID",
                "private_key_path must be the frozen read-only container "
                "path %s" % FROZEN_PRIVATE_KEY_PATH)
        if api_base != PRODUCTION_API_BASE:
            raise TokenConfigError(
                "TOKEN_CONFIG_INVALID",
                "api_base must be exactly %s in production (fake APIs are "
                "explicitly injected for tests only)" % PRODUCTION_API_BASE)
        self.app_id = app_id
        self.installation_id = installation_id
        self.repository_id = repository_id
        self.private_key_path = private_key_path
        self.api_base = api_base

    @classmethod
    def from_env(cls, environ=None) -> "TokenProviderConfig":
        env = os.environ if environ is None else environ
        return cls(
            app_id=env.get("GITHUB_APP_ID", ""),
            installation_id=env.get("GITHUB_INSTALLATION_ID", ""),
            repository_id=env.get("GITHUB_REPOSITORY_ID", ""),
            private_key_path=env.get("GITHUB_PRIVATE_KEY_PATH", ""),
            api_base=env.get("GITHUB_API_BASE", ""))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_app_jwt(sign, *, app_id: str, iat: int) -> str:
    """RS256 JWT, freshly built; ``sign`` is an injectable
    ``bytes -> bytes`` RSASSA-PKCS1-v1_5(SHA-256) signer (tests inject a
    synthetic key; production uses the PEM below)."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": iat, "exp": iat + JWT_TTL_SECONDS, "iss": app_id}
    segments = ("%s.%s" % (
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(claims, separators=(",", ":")).encode()))).encode()
    return "%s.%s" % (segments.decode(), _b64url(sign(segments)))


class _PemSigner:
    """Lazily loads the PEM from the frozen read-only path and signs with
    RSASSA-PKCS1-v1_5 / SHA-256. Key material never leaves this object."""

    def __init__(self, path: str):
        self._path = path
        self._key = None
        self._lock = threading.Lock()

    def sign(self, data: bytes) -> bytes:
        with self._lock:
            if self._key is None:
                from cryptography.hazmat.primitives import serialization
                with open(self._path, "rb") as handle:
                    pem = handle.read()
                self._key = serialization.load_pem_private_key(
                    pem, password=None)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return self._key.sign(data, padding.PKCS1v15(), hashes.SHA256())


class _ForcedProxyHandler(urllib.request.ProxyHandler):
    """§3 R1-T2: ProxyHandler that NEVER checks proxy_bypass.

    The standard ProxyHandler calls proxy_bypass(req.host) at request
    time, which reads NO_PROXY/no_proxy from the environment even with
    an explicit proxy dict. This subclass overrides proxy_open() to
    SKIP that check entirely — the proxy is always used regardless of
    NO_PROXY, no_proxy, or any other environment state.

    This is instance-level and concurrent-safe: no os.environ reads or
    writes, no global state mutation, no locks needed."""

    def proxy_open(self, req, proxy, type):
        # Mirrors the stdlib ProxyHandler.proxy_open EXCEPT the
        # `proxy_bypass(req.host)` early-return is DELIBERATELY
        # SKIPPED — ambient NO_PROXY/no_proxy must never reroute
        # api.github.com into a direct (proxy-less) connection.
        from urllib.request import _parse_proxy, unquote
        orig_type = req.type
        proxy_type, user, password, hostport = _parse_proxy(proxy)
        if proxy_type is None:
            proxy_type = orig_type
        hostport = unquote(hostport)
        req.set_proxy(hostport, proxy_type)
        if orig_type == proxy_type or orig_type == 'https':
            # let the protocol handlers do the actual proxied dial
            # (absolute-form for http, CONNECT for https)
            return None
        return self.parent.open(req, timeout=req.timeout)


def build_proxy_opener(proxy_url: str):
    """§3 R1-T2: Build an EXPLICIT proxy-aware opener for api.github.com.

    Concurrent-safe, environment-immutable: uses _ForcedProxyHandler
    (subclass of ProxyHandler) that skips proxy_bypass() entirely.
    Does NOT modify os.environ, does NOT wrap open() with env clearing,
    does NOT read HTTPS_PROXY/NO_PROXY. Routes ALL traffic through the
    specified proxy. Returns an OpenerDirector; never logs credentials."""
    handler = _ForcedProxyHandler({
        "http": proxy_url,
        "https": proxy_url,
    })
    return urllib.request.build_opener(handler)


def default_transport(method: str, url: str, *, headers: dict,
                      body: Optional[dict]) -> tuple:
    """Proxy-aware urllib transport for the token exchange (injectable
    in tests). Uses an EXPLICIT ProxyHandler when HTTPS_PROXY is set;
    falls back to plain urlopen only when no proxy is configured (fake
    stacks). Returns (status, headers, parsed_body)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    proxy_url = os.environ.get("HTTPS_PROXY", "")
    if proxy_url:
        opener = build_proxy_opener(proxy_url)
        open_func = opener.open
    else:
        open_func = urllib.request.urlopen
    try:
        with open_func(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw.strip() else {}
            return response.status, dict(response.headers), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        parsed = json.loads(raw) if raw.strip() else {}
        return exc.code, dict(exc.headers or {}), parsed
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _NetworkFailure(type(exc).__name__) from exc


class _NetworkFailure(Exception):
    pass


class GitHubAppTokenProvider:
    """Single-flight installation-token provider (all state in memory)."""

    def __init__(self, config: TokenProviderConfig, *,
                 transport: Optional[Callable[..., tuple]] = None,
                 now: Callable[[], float] = time.time):
        self._config = config
        self._transport = transport or default_transport
        self._now = now
        self._signer = _PemSigner(config.private_key_path)
        self._lock = threading.Lock()
        self._token: str = ""
        self._expires_at: float = 0.0
        self._generation = 0
        self.exchange_count = 0

    # ── public API ─────────────────────────────────────────────────────

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid token; refreshes at T-300s or on force_refresh.

        Single-flight via lock + generation: the caller snapshots the
        generation BEFORE acquiring the lock; inside, a force_refresh
        caller whose snapshot is stale (a newer refresh completed while it
        waited) shares that newer result instead of exchanging again. A
        FAILED refresh never leaves a seemingly-valid token behind (the
        cache is cleared before the error propagates); later calls retry
        per the caller's classification."""
        observed = self._generation
        with self._lock:
            if not force_refresh and self._token \
                    and self._now() < self._expires_at - REFRESH_MARGIN_SECONDS:
                return self._token
            if force_refresh and self._generation != observed \
                    and self._token \
                    and self._now() < self._expires_at - REFRESH_MARGIN_SECONDS:
                return self._token
            try:
                self._exchange_locked()
            except BaseException:
                self._token = ""
                self._expires_at = 0.0
                raise
            self._generation += 1
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token (called after a 401). The NEXT
        get_token(force_refresh=True) performs one fresh exchange."""
        with self._lock:
            self._token = ""
            self._expires_at = 0.0
            self._generation += 1

    def retry_after(self, error: TokenProviderError) -> int:
        if error.retry_after is not None:
            return max(1, int(error.retry_after))
        return 30

    # ── internals ──────────────────────────────────────────────────────

    def _exchange_locked(self) -> None:
        iat = int(self._now()) - JWT_IAT_SKEW_SECONDS
        jwt_value = build_app_jwt(
            self._signer.sign, app_id=self._config.app_id, iat=iat)
        url = ("%s/app/installations/%s/access_tokens"
               % (self._config.api_base.rstrip("/"),
                  self._config.installation_id))
        body = {
            "permissions": dict(PERMISSIONS_REQUEST),
            "repository_ids": [int(self._config.repository_id)],
        }
        headers = {
            "Authorization": "Bearer %s" % jwt_value,
            "Accept": "application/vnd.github+json",
            "User-Agent": "mergepilot-gh-app",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.exchange_count += 1
        try:
            status, response_headers, parsed = self._transport(
                "POST", url, headers=headers, body=body)
        except _NetworkFailure as exc:
            raise TokenExchangeRetryError(
                "TOKEN_EXCHANGE_NETWORK", "network %s" % exc) from None
        if status == 200:
            self._parse_and_store(parsed)
            return
        if status == 401:
            # An exchange-level 401 means the App JWT itself was rejected
            # — one retry happens at the REPORTER layer (401 policy);
            # a second exchange 401 is terminal there via the same rule.
            raise TokenExchangeRetryError(
                "TOKEN_EXCHANGE_UNAUTHORIZED", "http 401")
        if status in (403, 422):
            raise TokenExchangeTerminalError(
                "TOKEN_EXCHANGE_HTTP_%d" % status, "http %d" % status)
        if status == 429:
            raw = (response_headers or {}).get("Retry-After") \
                or (response_headers or {}).get("retry-after")
            try:
                wait = max(1, int(str(raw).strip())) if raw else None
            except ValueError:
                wait = None
            raise TokenExchangeRetryError(
                "TOKEN_EXCHANGE_RATE_LIMITED", "http 429", retry_after=wait)
        if status >= 500:
            raise TokenExchangeRetryError(
                "TOKEN_EXCHANGE_HTTP_5XX", "http %d" % status)
        raise TokenExchangeTerminalError(
            "TOKEN_EXCHANGE_HTTP_%d" % status, "http %d" % status)

    def _parse_and_store(self, parsed) -> None:
        if not isinstance(parsed, dict):
            raise TokenExchangeTerminalError(
                "TOKEN_EXCHANGE_MALFORMED", "response not an object")
        token = parsed.get("token")
        if not isinstance(token, str) or not token.strip():
            raise TokenExchangeTerminalError(
                "TOKEN_EXCHANGE_MALFORMED", "missing token field")
        expires_raw = parsed.get("expires_at")
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(
                str(expires_raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            expires = dt.timestamp()
        except (TypeError, ValueError):
            raise TokenExchangeTerminalError(
                "TOKEN_EXCHANGE_MALFORMED", "invalid expires_at") from None
        # scope echo validation: absent echo fields are accepted (GitHub
        # contract); PRESENT fields must be exactly the requested scope —
        # missing keys, extra keys, duplicates and malformed entries are
        # all rejected (fail-closed).
        if "permissions" in parsed:
            permissions = parsed["permissions"]
            if not isinstance(permissions, dict)                     or permissions != PERMISSIONS_REQUEST:
                raise TokenExchangeTerminalError(
                    "TOKEN_SCOPE_MISMATCH",
                    "permissions echo must exactly equal the request")
        if "repositories" in parsed:
            repositories = parsed["repositories"]
            if not isinstance(repositories, list):
                raise TokenExchangeTerminalError(
                    "TOKEN_EXCHANGE_MALFORMED",
                    "repositories echo must be a list")
            ids = []
            for entry in repositories:
                if not isinstance(entry, dict):
                    raise TokenExchangeTerminalError(
                        "TOKEN_EXCHANGE_MALFORMED",
                        "repositories entry must be an object")
                rid = entry.get("id")
                if not isinstance(rid, int) or isinstance(rid, bool):
                    raise TokenExchangeTerminalError(
                        "TOKEN_EXCHANGE_MALFORMED",
                        "repositories entry id must be an integer")
                ids.append(rid)
            if len(set(ids)) != len(ids):
                raise TokenExchangeTerminalError(
                    "TOKEN_EXCHANGE_MALFORMED",
                    "repositories echo contains duplicates")
            if set(ids) != {int(self._config.repository_id)}:
                raise TokenExchangeTerminalError(
                    "TOKEN_SCOPE_MISMATCH",
                    "repositories echo must be exactly the fixture id")
        if expires <= self._now():
            raise TokenExchangeTerminalError(
                "TOKEN_EXCHANGE_MALFORMED", "expires_at in the past")
        self._token = token
        self._expires_at = expires


__all__ = [
    "PRODUCTION_API_BASE", "FROZEN_PRIVATE_KEY_PATH", "PERMISSIONS_REQUEST",
    "TokenProviderError", "TokenConfigError", "TokenExchangeTerminalError",
    "TokenExchangeRetryError", "TokenProviderConfig", "GitHubAppTokenProvider",
    "build_app_jwt", "default_transport",
]
