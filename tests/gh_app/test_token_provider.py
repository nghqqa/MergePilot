"""M8-GH-4B2 token provider tests — synthetic RSA key + fake API transport.

No real PEM, no real GitHub, no network. Covers the frozen §3 contract:
config strictness, RS256 JWT claims/padding/signature, scoped exchange
body, single-flight, T-300 refresh, 401/403/422/429/5xx/network
classification, malformed-response fail-closed, and secret non-leakage.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import token_provider as tp                              # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

APP_ID = "4648333"
INSTALLATION_ID = "154914965"
REPOSITORY_ID = "1314399289"
FAKE_TOKEN = "ghifake_" + "t" * 40


def _synthetic_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeTransport:
    """Scripted transport; records (method, url, headers, body)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers, body):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        kind, *rest = self.responses.pop(0)
        if kind == "net":
            raise tp._NetworkFailure("boom")
        status, body_dict = rest[0], rest[1]
        headers_out = rest[2] if len(rest) > 2 else {}
        return status, headers_out, body_dict


def _make_provider(transport, **config_over):
    key = _synthetic_key()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pem_dir = Path(os.environ.get("TEMP", "/tmp"))
    pem_path = pem_dir / "mp-b2-test-key.pem"
    pem_path.write_bytes(pem)
    config = tp.TokenProviderConfig(
        app_id=APP_ID, installation_id=INSTALLATION_ID,
        repository_id=REPOSITORY_ID,
        private_key_path=config_over.pop(
            "private_key_path", tp.FROZEN_PRIVATE_KEY_PATH),
        api_base=config_over.pop("api_base", tp.PRODUCTION_API_BASE))
    signer = tp._PemSigner.__new__(tp._PemSigner)
    signer._path = str(pem_path)
    signer._key = key
    signer._lock = threading.Lock()
    provider = tp.GitHubAppTokenProvider(config, transport=transport,
                                         now=lambda: 1000.0)
    provider._signer = signer
    return provider, key, pem_path


def _ok_response(token=FAKE_TOKEN, expires_in=3600, extra=None):
    import datetime
    dt = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(seconds=expires_in))
    body = {"token": token, "expires_at": dt.isoformat()}
    if extra:
        body.update(extra)
    return ("ok", 200, body, {})


class TestConfig(unittest.TestCase):

    def _cfg(self, **kw):
        params = dict(app_id=APP_ID, installation_id=INSTALLATION_ID,
                      repository_id=REPOSITORY_ID,
                      private_key_path=tp.FROZEN_PRIVATE_KEY_PATH,
                      api_base=tp.PRODUCTION_API_BASE)
        params.update(kw)
        return tp.TokenProviderConfig(**params)

    def test_valid(self):
        cfg = self._cfg()
        self.assertEqual(cfg.app_id, APP_ID)

    def test_numeric_and_frozen_paths(self):
        for bad in ({"app_id": "abc"}, {"installation_id": "-1"},
                    {"repository_id": "12.5"}):
            with self.assertRaises(tp.TokenConfigError):
                self._cfg(**bad)
        with self.assertRaises(tp.TokenConfigError):
            self._cfg(private_key_path="/etc/peek.pem")
        with self.assertRaises(tp.TokenConfigError):
            self._cfg(api_base="http://127.0.0.1:8091")  # fake forbidden

    def test_from_env(self):
        env = {"GITHUB_APP_ID": APP_ID,
               "GITHUB_INSTALLATION_ID": INSTALLATION_ID,
               "GITHUB_REPOSITORY_ID": REPOSITORY_ID,
               "GITHUB_PRIVATE_KEY_PATH": tp.FROZEN_PRIVATE_KEY_PATH,
               "GITHUB_API_BASE": tp.PRODUCTION_API_BASE}
        self.assertEqual(tp.TokenProviderConfig.from_env(env).app_id,
                         APP_ID)
        with self.assertRaises(tp.TokenConfigError):
            tp.TokenProviderConfig.from_env({})


class TestJwt(unittest.TestCase):

    def test_claims_padding_and_signature(self):
        key = _synthetic_key()
        pub = key.public_key()
        jwt_value = tp.build_app_jwt(
            lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
            app_id=APP_ID, iat=1000)
        parts = jwt_value.split(".")
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertNotIn("=", part)          # no padding anywhere

        def _dec(seg):
            return json.loads(base64.urlsafe_b64decode(
                seg + "=" * (-len(seg) % 4)))
        header, claims = _dec(parts[0]), _dec(parts[1])
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(claims["iss"], APP_ID)
        self.assertEqual(claims["iat"], 1000)
        self.assertEqual(claims["exp"], 1000 + 480)   # now+480
        message = ("%s.%s" % (parts[0], parts[1])).encode()
        pub.verify(base64.urlsafe_b64decode(
            parts[2] + "=" * (-len(parts[2]) % 4)), message,
            padding.PKCS1v15(), hashes.SHA256())      # signature valid

    def test_fresh_jwt_per_exchange(self):
        # RSASSA-PKCS1-v1_5 is deterministic: an identical iat yields an
        # identical JWT. "Freshly signed" is observable via an ADVANCING
        # clock (different iat -> different JWT) plus exchange_count=2.
        clock = {"t": 1000.0}

        def advancing():
            clock["t"] += 100.0
            return clock["t"]

        provider, _key, _pem = _make_provider(
            _FakeTransport([_ok_response(), _ok_response(token="second")]))
        provider._now = advancing
        provider.get_token()
        first_jwt = provider._transport.calls[0]["headers"]["Authorization"]
        provider.get_token(force_refresh=True)
        second_jwt = provider._transport.calls[1]["headers"]["Authorization"]
        self.assertNotEqual(first_jwt, second_jwt)   # freshly signed
        self.assertEqual(provider.exchange_count, 2)


class TestExchange(unittest.TestCase):

    def test_url_headers_and_scoped_body_exact(self):
        provider, _key, _pem = _make_provider(
            _FakeTransport([_ok_response()]))
        provider.get_token()
        call = provider._transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            "https://api.github.com/app/installations/%s/access_tokens"
            % INSTALLATION_ID)
        self.assertTrue(call["headers"]["Authorization"]
                        .startswith("Bearer ey"))
        self.assertEqual(call["headers"]["Accept"],
                         "application/vnd.github+json")
        self.assertEqual(call["body"], {
            "permissions": {"checks": "write", "metadata": "read"},
            "repository_ids": [int(REPOSITORY_ID)],   # fixture numeric only
        })

    def test_single_flight_concurrent(self):
        gate = threading.Event()

        def slow_transport(method, url, *, headers, body):
            gate.wait(5)
            _kind, status, body_dict, headers_out = _ok_response()
            return status, headers_out, body_dict
        provider, _key, _pem = _make_provider(slow_transport)
        results = []

        def worker():
            results.append(provider.get_token())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r == FAKE_TOKEN for r in results))
        self.assertEqual(provider.exchange_count, 1)   # ONE exchange

    def test_t300_refresh_window(self):
        provider, _key, _pem = _make_provider(
            _FakeTransport([_ok_response(expires_in=3600),
                            _ok_response(token="second")]))
        provider.get_token()
        self.assertEqual(provider.exchange_count, 1)
        provider.get_token()                     # 3600-300 > 0 → cached
        self.assertEqual(provider.exchange_count, 1)
        self.assertEqual(provider.get_token(force_refresh=True),
                         "second")               # forced → fresh exchange
        self.assertEqual(provider.exchange_count, 2)

    def test_error_classification(self):
        # 403/422 terminal; 429 Retry-After; 5xx/network retry; 401 retryable
        for status, exc in ((403, tp.TokenExchangeTerminalError),
                            (422, tp.TokenExchangeTerminalError),
                            (429, tp.TokenExchangeRetryError),
                            (500, tp.TokenExchangeRetryError),
                            (401, tp.TokenExchangeRetryError)):
            provider, _k, _p = _make_provider(
                _FakeTransport([("ok", status, {}, {})]))
            with self.assertRaises(exc):
                provider.get_token()
        provider, _k, _p = _make_provider(_FakeTransport([("net",)]))
        with self.assertRaises(tp.TokenExchangeRetryError):
            provider.get_token()

    def test_429_retry_after_honored(self):
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 429, {}, {"Retry-After": "77"})]))
        with self.assertRaises(tp.TokenExchangeRetryError) as ctx:
            provider.get_token()
        self.assertEqual(ctx.exception.retry_after, 77)

    def test_malformed_fail_closed(self):
        for body in ({}, {"token": ""}, {"token": FAKE_TOKEN},
                     {"token": FAKE_TOKEN, "expires_at": "not-a-date"},
                     {"token": FAKE_TOKEN, "expires_at": "1970-01-01T00:00:00Z"}):
            provider, _k, _p = _make_provider(
                _FakeTransport([("ok", 200, body, {})]))
            with self.assertRaises(tp.TokenExchangeTerminalError):
                provider.get_token()

    def test_scope_echo_mismatch_fail_closed(self):
        bad_perms = {"token": FAKE_TOKEN,
                     "expires_at": _ok_response()[2]["expires_at"],
                     "permissions": {"checks": "read", "metadata": "read"}}
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, bad_perms, {})]))
        with self.assertRaises(tp.TokenExchangeTerminalError) as ctx:
            provider.get_token()
        self.assertEqual(ctx.exception.code, "TOKEN_SCOPE_MISMATCH")
        bad_repos = {"token": FAKE_TOKEN,
                     "expires_at": _ok_response()[2]["expires_at"],
                     "repositories": [{"id": 1}]}
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, bad_repos, {})]))
        with self.assertRaises(tp.TokenExchangeTerminalError):
            provider.get_token()

    def test_no_secret_leakage_in_errors(self):
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200,
                              {"token": FAKE_TOKEN,
                               "expires_at": "garbage"}, {})]))
        try:
            provider.get_token()
            self.fail("expected terminal")
        except tp.TokenExchangeTerminalError as exc:
            text = "%s %s %r" % (exc.code, exc.detail, exc)
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertNotIn("BEGIN", text)


class TestInvalidate(unittest.TestCase):

    def test_invalidate_forces_next_exchange(self):
        provider, _k, _p = _make_provider(
            _FakeTransport([_ok_response(), _ok_response(token="second")]))
        self.assertEqual(provider.get_token(), FAKE_TOKEN)
        provider.invalidate()
        self.assertEqual(provider.get_token(), "second")
        self.assertEqual(provider.exchange_count, 2)




class TestForceRefreshSingleFlight(unittest.TestCase):
    """§4: barrier-synchronized concurrency — no accidental scheduling."""

    @staticmethod
    def _provider_with_gate(gate, responses):
        import threading

        def transport(method, url, *, headers, body):
            gate.wait(10)
            _kind, status, body_dict, headers_out = responses.pop(0)
            return status, headers_out, body_dict
        return _make_provider(transport)

    def test_concurrent_force_refresh_single_exchange(self):
        import threading
        # Deterministic simultaneity: main HOLDS the provider lock while
        # every worker (released together by the barrier) takes its
        # generation snapshot and queues on the lock; releasing the lock
        # then lets exactly ONE worker exchange — the rest share it via
        # the stale-snapshot rule.
        barrier = threading.Barrier(8)

        def transport(method, url, *, headers, body):
            _kind, status, body_dict, headers_out = _ok_response()
            return status, headers_out, body_dict

        provider, _k, _p = _make_provider(transport)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(10)
                results.append(
                    provider.get_token(force_refresh=True))
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        with provider._lock:                  # snapshots queue up under it
            threads = [threading.Thread(target=worker)
                       for _ in range(8)]
            for t in threads:
                t.start()
            time.sleep(0.5)                   # let every worker snapshot
        for t in threads:
            t.join(15)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(provider.exchange_count, 1)

    def test_independent_force_after_first_round(self):
        provider, _k, _p = _make_provider(
            _FakeTransport([_ok_response(), _ok_response(token="second")]))
        provider.get_token()
        provider.get_token(force_refresh=True)
        self.assertEqual(provider.exchange_count, 2)   # a NEW exchange

    def test_t300_window_concurrent_refresh_single_exchange(self):
        import threading
        # Wall-relative mutable clock: prime with a 3600s token, then
        # advance the clock past expires-300 (3600-300=3300) so the
        # T-300 refresh window is active while the token is still
        # nominally valid. 8 simultaneous NON-forced callers -> exactly
        # ONE exchange, everyone shares the fresh result.
        wall = {"t": time.time()}

        def clock():
            return wall["t"]

        provider, _k, _p = _make_provider(
            _FakeTransport([_ok_response(expires_in=3600),
                            _ok_response(token="second",
                                         expires_in=7200)]))
        provider._now = clock
        provider.get_token()
        self.assertEqual(provider.exchange_count, 1)
        wall["t"] += 3400                      # cross the T-300 window
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait(10)
            results.append(provider.get_token())

        with provider._lock:
            threads = [threading.Thread(target=worker)
                       for _ in range(8)]
            for t in threads:
                t.start()
            time.sleep(0.5)
        for t in threads:
            t.join(15)
        self.assertEqual(len(results), 8)
        self.assertEqual(provider.exchange_count, 2)

    def test_invalidate_then_refresh_no_deadlock(self):
        import threading
        provider, _k, _p = _make_provider(
            _FakeTransport([_ok_response(), _ok_response(token="second")]))
        provider.get_token()
        done = threading.Event()

        def worker():
            provider.invalidate()
            provider.get_token(force_refresh=True)
            done.set()

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(done.wait(10))
        t.join(15)
        self.assertEqual(provider.exchange_count, 2)

    def test_failed_refresh_clears_cache(self):
        provider, _k, _p = _make_provider(
            _FakeTransport([_ok_response(), ("ok", 503, {}, {})]))
        self.assertEqual(provider.get_token(), FAKE_TOKEN)
        with self.assertRaises(tp.TokenExchangeRetryError):
            provider.get_token(force_refresh=True)
        # no seemingly-valid token remains after the failed refresh
        self.assertEqual(provider._token, "")
        self.assertEqual(provider._expires_at, 0.0)
        # a later call retries per contract (fresh exchange, success ok)
        provider._transport.responses.append(_ok_response(token="third"))
        self.assertEqual(provider.get_token(force_refresh=True), "third")


class TestExactScopeEcho(unittest.TestCase):
    """§5: echo fields absent -> accepted; present -> EXACT match only."""

    @staticmethod
    def _echo_body(expires_in=3600, **extra):
        import datetime
        dt = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=expires_in))
        body = {"token": FAKE_TOKEN, "expires_at": dt.isoformat()}
        body.update(extra)
        return body

    def test_echo_absent_accepted(self):
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, self._echo_body(), {})]))
        self.assertEqual(provider.get_token(), FAKE_TOKEN)

    def test_exact_echo_accepted(self):
        body = self._echo_body(
            permissions={"checks": "write", "metadata": "read"},
            repositories=[{"id": int(REPOSITORY_ID)}])
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, body, {})]))
        self.assertEqual(provider.get_token(), FAKE_TOKEN)

    def test_permission_missing_rejected(self):
        body = self._echo_body(permissions={"checks": "write"})
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, body, {})]))
        with self.assertRaises(tp.TokenExchangeTerminalError):
            provider.get_token()

    def test_permission_extra_rejected(self):
        body = self._echo_body(permissions={"checks": "write",
                                            "metadata": "read",
                                            "contents": "read"})
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, body, {})]))
        with self.assertRaises(tp.TokenExchangeTerminalError):
            provider.get_token()

    def test_repository_missing_fixture_rejected(self):
        body = self._echo_body(repositories=[{"id": 1}])
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, body, {})]))
        with self.assertRaises(tp.TokenExchangeTerminalError):
            provider.get_token()

    def test_repository_extra_rejected(self):
        body = self._echo_body(repositories=[
            {"id": int(REPOSITORY_ID)}, {"id": 1}])
        provider, _k, _p = _make_provider(
            _FakeTransport([("ok", 200, body, {})]))
        with self.assertRaises(tp.TokenExchangeTerminalError):
            provider.get_token()

    def test_repository_malformed_rejected(self):
        for bad in ([{"id": "not-int"}], [{"no_id": 1}], ["flat"], [True],
                    [{"id": int(REPOSITORY_ID)}, {"id": int(REPOSITORY_ID)}]):
            body = self._echo_body(repositories=bad)
            provider, _k, _p = _make_provider(
                _FakeTransport([("ok", 200, body, {})]))
            with self.assertRaises(tp.TokenExchangeTerminalError):
                provider.get_token()


if __name__ == "__main__":
    unittest.main()
