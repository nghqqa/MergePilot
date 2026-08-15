"""Policy-gateway container entrypoint (Phase 1-D retry v3 Fix 3).

Validates the gateway's runtime contract — variable names extracted from the
ACTUAL tools/policy-gateway/gateway.py (never guessed) — then execs it.

Required/validated environment:
  UPSTREAM_URL     REQUIRED. An MCP SSE endpoint the gateway lifespan can
                   reach. Must be an http(s) URL without userinfo (a DSN or
                   password must never ride the URL) and without a fragment.
  ROLE_TOKENS      optional; if set, must be valid JSON mapping role names
                   to non-empty string tokens (gateway.py parses it with
                   json.loads at import time).
  LISTEN_HOST      optional; must be a container-appropriate bind
                   (0.0.0.0/127.0.0.1/localhost) — LAN addresses rejected.
  LISTEN_PORT      optional; integer port (default 8083).

In-container upstream stub: when UPSTREAM_URL points at the isolated-stack
stub URL (http://127.0.0.1:8084/sse — see tools/policy-gateway/
upstream_stub.py), the entrypoint starts the stub first (loopback-only,
zero tools). Any OTHER upstream is passed through untouched.

Exit codes: 0 = launched (exec); 1 = CONFIG_INVALID.
Secrets never appear in argv, logs or exceptions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from one_click_startup import redact  # noqa: E402

STUB_URL = "http://127.0.0.1:8084/sse"
STUB_SCRIPT = "/app/upstream_stub.py"
GATEWAY_SCRIPT = "/app/gateway.py"

_VALID_LISTEN_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost"})


class EntrypointConfigError(Exception):
    """Stable CONFIG_INVALID; message is redacted (never contains secrets)."""

    def __init__(self, detail: str):
        self.code = "CONFIG_INVALID"
        super().__init__(redact("CONFIG_INVALID: %s" % detail))


def _validate_env(environ=None) -> dict:
    env = environ if environ is not None else os.environ

    upstream = env.get("UPSTREAM_URL", "").strip()
    if not upstream:
        raise EntrypointConfigError(
            "UPSTREAM_URL is not set; gateway.py's lifespan exits after 30 "
            "failed upstream connect attempts — the upstream must be "
            "configured explicitly (the isolated stack uses the in-container "
            "zero-tool stub %s)" % STUB_URL)
    parsed = urllib.parse.urlsplit(upstream)
    if parsed.scheme not in ("http", "https"):
        raise EntrypointConfigError(
            "UPSTREAM_URL scheme must be http or https (got %r)"
            % parsed.scheme[:20])
    if not parsed.netloc:
        raise EntrypointConfigError("UPSTREAM_URL has no host")
    if "@" in parsed.netloc:
        raise EntrypointConfigError(
            "UPSTREAM_URL must not carry userinfo (credentials never ride "
            "the URL)")
    if parsed.fragment:
        raise EntrypointConfigError("UPSTREAM_URL must not carry a fragment")

    role_tokens = env.get("ROLE_TOKENS", "").strip()
    if role_tokens:
        try:
            tokens = json.loads(role_tokens)
        except ValueError:
            raise EntrypointConfigError(
                "ROLE_TOKENS must be valid JSON (gateway.py json.loads it "
                "at import time)") from None
        if not isinstance(tokens, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and k and v
                for k, v in tokens.items()):
            raise EntrypointConfigError(
                "ROLE_TOKENS must be a JSON object of non-empty string "
                "role->token pairs")

    listen_host = env.get("LISTEN_HOST", "0.0.0.0").strip()
    if listen_host not in _VALID_LISTEN_HOSTS:
        raise EntrypointConfigError(
            "LISTEN_HOST must be 0.0.0.0/127.0.0.1/localhost (container-"
            "internal bind; got %r)" % listen_host)

    listen_port_s = env.get("LISTEN_PORT", "8083").strip()
    try:
        listen_port = int(listen_port_s)
    except ValueError:
        raise EntrypointConfigError(
            "LISTEN_PORT must be an integer (got %r)" % listen_port_s[:10]
        ) from None
    if not (0 < listen_port < 65536):
        raise EntrypointConfigError(
            "LISTEN_PORT out of range: %d" % listen_port)

    return {
        "upstream_url": upstream,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "use_stub": upstream == STUB_URL,
    }


def main() -> int:
    try:
        config = _validate_env()
    except EntrypointConfigError as exc:
        print(redact(str(exc)), file=sys.stderr, flush=True)
        return 1
    if config["use_stub"]:
        # In-container, loopback-only, zero-tool MCP SSE stub so the real
        # gateway lifespan can complete inside the isolated network.
        subprocess.Popen(
            [sys.executable, "-u", STUB_SCRIPT],
            stdout=sys.stderr, stderr=sys.stderr, start_new_session=True)
    os.execv(sys.executable,
             [sys.executable, "-u", GATEWAY_SCRIPT])
    return 0


if __name__ == "__main__":
    sys.exit(main())
