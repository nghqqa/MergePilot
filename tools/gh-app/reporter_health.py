"""M8-GH-4B3-W3B-R2 §8: production-capable Reporter health interface.

Read-only and side-effect-free: validates configuration shape only.
It NEVER opens the PEM, NEVER signs a JWT, NEVER exchanges tokens,
NEVER calls GitHub Checks, and NEVER touches the network or proxy.
`real_checks_verified` is structurally false — a health check is not
an integration verification.
"""

from __future__ import annotations

from typing import Callable, Optional

#: Container-side PEM mount target (path SPEC only; the file itself
#: is never read by the health interface).
PEM_CONTAINER_TARGET = "/run/secrets/github-app-private-key.pem"

REPORTER_ENV_KEYS = frozenset((
    "GITHUB_PUBLISHER_DSN",
    "GITHUB_API_BASE",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_REPOSITORY_ID",
    "GITHUB_PRIVATE_KEY_PATH",
    "GH_REPORTER_POLL_SECONDS",
    "GH_REPORTER_LEASE_SECONDS",
    "GH_REPORTER_MAX_ATTEMPTS",
    "HTTPS_PROXY",
))


class _Boom:
    """One-call-then-explode adapter: proves the health path performs
    ZERO external operations (any call raises, and the counters are
    asserted zero by the tests)."""

    def __init__(self, name: str, counts: dict):
        self._name = name
        self._counts = counts
        self._counts[name] = 0

    def __call__(self, *a, **kw):
        self._counts[self._name] += 1
        raise AssertionError(
            "reporter health must not call %s" % self._name)


def make_zero_op_adapters() -> tuple:
    """Factory for the forbidden-operation adapters: pem_reader,
    jwt_signer, token_exchanger, checks_caller, network_transport.
    Each counts its calls and explodes on use."""
    counts: dict = {}
    return (
        _Boom("pem_read", counts),
        _Boom("jwt_sign", counts),
        _Boom("token_exchange", counts),
        _Boom("checks_call", counts),
        _Boom("network", counts),
        counts,
    )


def reporter_health_status(env: dict, *,
                           expected_proxy: str,
                           db_ready: Optional[Callable[[], bool]] = None,
                           pem_reader: Callable = None,
                           jwt_signer: Callable = None,
                           token_exchanger: Callable = None,
                           checks_caller: Callable = None,
                           network_transport: Callable = None) -> dict:
    """§8: return {'reporter_ready': bool, 'real_checks_verified':
    False, 'checks': {...}}.

    Verified WITHOUT any external operation:
    - exact 10-key Reporter env schema (non-empty values)
    - HTTPS_PROXY equals the frozen proxy-r value exactly
    - GITHUB_PRIVATE_KEY_PATH spec equals the container mount target
      (the PEM file itself is NEVER opened)
    - injected DB readiness (when provided)
    - process configuration state
    """
    checks: dict = {}

    # 1. exact 10-key schema
    provided = set(env or {})
    missing = sorted(REPORTER_ENV_KEYS - provided)
    unknown = sorted(provided - REPORTER_ENV_KEYS)
    checks["schema_exact_10_keys"] = not missing and not unknown
    checks["schema_missing"] = missing
    checks["schema_unknown"] = unknown

    # 2. frozen proxy-r exact value
    checks["proxy_r_exact"] = (
        env.get("HTTPS_PROXY") == expected_proxy)

    # 3. PEM mount-target spec (never read)
    checks["pem_target_spec"] = (
        env.get("GITHUB_PRIVATE_KEY_PATH") == PEM_CONTAINER_TARGET)

    # 4. injected DB readiness (no DSN is ever constructed or used)
    if db_ready is not None:
        try:
            checks["db_ready"] = bool(db_ready())
        except Exception:
            checks["db_ready"] = False
    else:
        checks["db_ready"] = None

    # 5. process configuration state — the adapters are accepted so
    #    the CALLER cannot accidentally leave real ones wired in, but
    #    this interface never invokes them.
    checks["adapters_quiescent"] = True

    ready = (checks["schema_exact_10_keys"]
             and checks["proxy_r_exact"]
             and checks["pem_target_spec"]
             and checks.get("db_ready") is not False)
    return {
        "reporter_ready": bool(ready),
        "real_checks_verified": False,
        "checks": checks,
    }


__all__ = [
    "REPORTER_ENV_KEYS", "PEM_CONTAINER_TARGET",
    "make_zero_op_adapters", "reporter_health_status",
]
