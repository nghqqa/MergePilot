"""Demo-console container healthcheck (Phase 1-D retry v3, review-gap Fix 3).

REAL readiness for the demo-console container: an HTTP GET against the
live status endpoint on CONTAINER LOOPBACK ONLY.

Healthy (exit 0) requires ALL of:
  - HTTP 200 within the explicit timeout (3s),
  - a body that parses as JSON,
  - ``source_read_only`` is exactly ``true``,
  - ``source_kind`` is exactly ``POSTGRES_ISOLATED`` (never REPLAY /
    FILE_FIXTURE / PREGENERATED_BUNDLE),
  - a startup snapshot is available (``bundle_sha256`` non-null — the
    startup DB probe succeeded and the poller holds a valid bundle).

Anything else — HTTP error, timeout, bad JSON, wrong source kind, a
non-read-only source, or no snapshot — is unhealthy. The URL is fixed to
``http://127.0.0.1:8600/api/live/status`` (loopback; any non-loopback
target is rejected before a socket is opened). The preflight container's
own fail-closed HTTP gate is unchanged and remains the authoritative
in-network check; this probe only establishes demo-console readiness for
dependency ordering.

Read-only: one GET; no writes, no secrets, no shell.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = "http://127.0.0.1:8600/api/live/status"
TIMEOUT_SECONDS = 3.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_REQUIRED_SOURCE_KIND = "POSTGRES_ISOLATED"


def _assert_loopback(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError("healthcheck URL must be http")
    host = parsed.hostname or ""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            "healthcheck URL must target container loopback "
            "(127.0.0.1/localhost); refusing %r" % host)
    if parsed.username or parsed.password:
        raise ValueError("healthcheck URL must not carry credentials")


def check_status(url: str = URL, timeout: float = TIMEOUT_SECONDS) -> tuple:
    """Return (healthy: bool, reason: str). Never raises for probe failures."""
    _assert_loopback(url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        return False, "http_%s" % exc.code
    except Exception as exc:  # timeout, connection refused, DNS, ...
        return False, type(exc).__name__
    if status != 200:
        return False, "http_%s" % status
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return False, "bad_json"
    if not isinstance(payload, dict):
        return False, "bad_json"
    if payload.get("source_read_only") is not True:
        return False, "not_read_only"
    if payload.get("source_kind") != _REQUIRED_SOURCE_KIND:
        return False, "wrong_source_kind"
    if not payload.get("bundle_sha256"):
        return False, "no_snapshot"
    return True, "ok"


def main() -> int:
    try:
        healthy, reason = check_status()
    except ValueError as exc:
        print("console healthcheck: %s" % exc, file=sys.stderr)
        return 1
    if not healthy:
        print("console healthcheck: %s" % reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
