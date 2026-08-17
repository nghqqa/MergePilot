"""ISOLATED_LIVE in-container upstream stub for the policy gateway (v3 Fix 3).

Why this exists: gateway.py's lifespan REQUIRES a reachable MCP SSE upstream
(it retries 30 times, then exits). The isolated stack has no github-mcp by
design (no GitHub, no production access). This stub is the honest in-stack
answer: a minimal MCP SSE server bound to CONTAINER LOOPBACK ONLY
(127.0.0.1:8084), serving ZERO tools.

Isolation properties (fail-closed by construction):
  - loopback-only listen: unreachable from the bridge network, never
    published, no host process, not a separate service, not a postgres twin;
  - ZERO tools by default: the gateway proxies nothing — list_tools is
    empty, and call_tool always raises, so no tool payload can ever transit;
  - no outbound connections of any kind;
  - exists solely so the gateway's real lifespan completes and its
    healthcheck (TCP to its own listen port) is meaningful.

Started by tools/gateway_entrypoint.py when UPSTREAM_URL points at the
in-container stub URL (http://127.0.0.1:8084/sse).

M8-A2-a opt-in PR fixture (isolated verification only):
  Setting MERGEPILOT_STUB_PR_FIXTURE=1 (exact string) makes the stub serve
  exactly ONE read-only tool — ``pull_request_read`` — answering the three
  read methods (get / get_diff / get_files) with a FIXED synthetic PR whose
  identity is consistent with the M4F_RUN / gateway_read_pr contracts.
  Everything else — unknown tools, unknown methods, write-ish methods,
  wrong owner/repo/PR numbers — fails closed exactly like the zero-tool
  default. The fixture never contacts GitHub or any network, carries no
  secrets, and is NOT production data; enabling it does NOT verify any
  producer contract or promote any verified field. Default (env unset or
  any other value): the original zero-tool behavior is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
import uvicorn

LISTEN_HOST = "127.0.0.1"   # container loopback ONLY — never the bridge
LISTEN_PORT = 8084

# ── M8-A2-a fixture identity (fixed, read-only, non-production) ─────────────
FIXTURE_ENV = "MERGEPILOT_STUB_PR_FIXTURE"
FIXTURE_OWNER = "mergepilot"
FIXTURE_REPO_NAME = "isolated-fixture"
FIXTURE_REPO = f"{FIXTURE_OWNER}/{FIXTURE_REPO_NAME}"
FIXTURE_PR_NUMBER = 9001
FIXTURE_BASE_SHA = "b4a1" * 10   # 40 lowercase hex, deterministic
FIXTURE_HEAD_SHA = "c9d2" * 10   # 40 lowercase hex, distinct from base
FIXTURE_HEAD_REF = "fix/m4f-isolated-fixture"
FIXTURE_BASE_REF = "main"
FIXTURE_TOOL = "pull_request_read"
FIXTURE_METHODS = ("get", "get_diff", "get_files")

# One changed file; the unified diff and the files list describe the SAME
# change so downstream consumers (build_skill_inputs/_safe_changed_files)
# see a self-consistent revision.
FIXTURE_FILE_PATH = "services/auth/session_timeout.py"
FIXTURE_DIFF = (
    "diff --git a/services/auth/session_timeout.py "
    "b/services/auth/session_timeout.py\n"
    "--- a/services/auth/session_timeout.py\n"
    "+++ b/services/auth/session_timeout.py\n"
    "@@ -1,3 +1,5 @@\n"
    " DEFAULT_TIMEOUT_SECONDS = 300\n"
    "+MAX_TIMEOUT_SECONDS = 3600\n"
    "+\n"
    " def clamp_timeout(requested):\n"
    "     return min(requested, DEFAULT_TIMEOUT_SECONDS)\n"
)
FIXTURE_FILES = [
    {
        "filename": FIXTURE_FILE_PATH,
        "status": "modified",
        "additions": 2,
        "deletions": 1,
    }
]

server = Server("mergepilot-isolated-upstream-stub")
sse = SseServerTransport("/messages/")


def _fixture_enabled() -> bool:
    """Opt-in gate: exactly '1'. Any other value keeps the zero-tool stub."""
    return os.environ.get(FIXTURE_ENV, "") == "1"


def _fixture_identity_matches(arguments: dict) -> None:
    """Fail-closed unless the request targets the exact fixture identity."""
    if not isinstance(arguments, dict):
        raise ValueError("fixture: arguments must be an object")
    owner = arguments.get("owner")
    repo = arguments.get("repo")
    number = arguments.get("pullNumber")
    if owner != FIXTURE_OWNER or repo != FIXTURE_REPO_NAME:
        raise ValueError(
            "fixture: repo does not match the fixed isolated fixture identity")
    if isinstance(number, bool) or not isinstance(number, int) \
            or number != FIXTURE_PR_NUMBER:
        raise ValueError(
            "fixture: pullNumber does not match the fixed fixture PR")


def _fixture_pr_json() -> str:
    """The fixed PR payload, shaped for gateway_read_pr's strict parser:
    head.sha/base.sha 40-hex, state str, merged explicit bool, number int
    echoed from the response, head.repo.full_name == requested repo."""
    return json.dumps(
        {
            "number": FIXTURE_PR_NUMBER,
            "state": "open",
            "merged": False,
            "head": {
                "sha": FIXTURE_HEAD_SHA,
                "ref": FIXTURE_HEAD_REF,
                "repo": {"full_name": FIXTURE_REPO},
            },
            "base": {
                "sha": FIXTURE_BASE_SHA,
                "ref": FIXTURE_BASE_REF,
            },
        },
        sort_keys=True,
    )


def _fixture_dispatch(name, arguments) -> str:
    """Serve exactly the three read methods; reject everything else."""
    if name != FIXTURE_TOOL:
        raise ValueError(
            "isolated upstream stub serves no tools (call_tool %r refused)"
            % name)
    _fixture_identity_matches(arguments)
    method = arguments.get("method")
    if method == "get":
        return _fixture_pr_json()
    if method == "get_diff":
        return FIXTURE_DIFF
    if method == "get_files":
        return json.dumps(FIXTURE_FILES, sort_keys=True)
    raise ValueError(
        "fixture: method %r not served (read-only get/get_diff/get_files "
        "only)" % method)


@server.list_tools()
async def list_tools():
    """ZERO tools by default; exactly one read-only fixture tool when the
    M8-A2-a opt-in env var is set to '1'."""
    if _fixture_enabled():
        return [
            Tool(
                name=FIXTURE_TOOL,
                description=(
                    "M8-A2-a isolated read-only PR fixture: fixed synthetic "
                    "PR identity, get/get_diff/get_files only, not "
                    "production data"),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": list(FIXTURE_METHODS),
                        },
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "pullNumber": {"type": "integer"},
                    },
                    "required": ["method", "owner", "repo", "pullNumber"],
                },
            )
        ]
    return []


@server.call_tool()
async def call_tool(name, arguments):
    if _fixture_enabled():
        # May still raise (fail-closed) for unknown tool/method/identity.
        # The MCP SDK 1.x call_tool contract requires a list of typed
        # content objects — a bare str fails Pydantic validation with
        # "Input should be a valid dictionary or instance of TextContent"
        # and the gateway proxies that validation error instead of the
        # fixture payload. Wrap the deterministic JSON string in exactly
        # one TextContent; _fixture_dispatch itself is unchanged.
        payload = _fixture_dispatch(name, arguments)
        return [TextContent(type="text", text=payload)]
    raise ValueError(
        "isolated upstream stub serves no tools (call_tool %r refused)"
        % name)


async def handle_sse(request):
    # Starlette carries the ASGI send callable on the Request object
    # (``request._send``), NOT in the scope: the previous direct scope
    # lookup of the send callable raised KeyError and turned every /sse
    # request into an HTTP 500 in the real 1-G container run. Fail closed
    # if the callable is absent — the error is a static string that never
    # echoes request data (headers, Authorization, tokens).
    send = getattr(request, "_send", None)
    if not callable(send):
        raise RuntimeError(
            "upstream stub: request carries no ASGI send callable "
            "(starlette Request._send missing); refusing to serve /sse")
    # mcp 1.28.1 (the version pinned in the image): connect_sse is an
    # async context manager that YIELDS the (read_stream, write_stream)
    # pair directly; this transport version has no per-stream accessor
    # methods (the 1-G retry 3 real run failed on exactly that).
    async with sse.connect_sse(request.scope, request.receive, send) \
            as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


class SSEEndpoint:
    """/sse as a RAW ASGI endpoint class (1-G stabilization sweep).

    Pinned Starlette semantics (read from the image): a Route endpoint
    that is a plain FUNCTION is wrapped as ``func(request) -> response``
    — its return value is awaited as the response, so an SSE handler
    that completes after the client disconnects returns None and the
    server logs ``TypeError: 'NoneType' object is not callable`` (the
    retry-5 observation). A CLASS endpoint follows the HTTPEndpoint
    calling convention — ``await Endpoint(scope, receive, send)``: the
    class is instantiated with the ASGI args and awaited via
    ``__await__``/``dispatch`` with NO return-value response handling.
    Constructing ``Request(scope, receive, send)`` in dispatch also
    guarantees ``request._send`` exists (pinned Starlette stores the
    send callable on the Request)."""

    def __init__(self, scope, receive, send):
        self.scope = scope
        self.receive = receive
        self.send = send

    def __await__(self):
        return self.dispatch().__await__()

    async def dispatch(self):
        await handle_sse(Request(self.scope, self.receive, self.send))


app = Starlette(
    routes=[
        Route("/sse", endpoint=SSEEndpoint),
        Mount("/messages/", app=sse.handle_post_message),
    ],
)


if __name__ == "__main__":
    mode = "PR-fixture (read-only, fixed identity)" if _fixture_enabled() \
        else "zero-tool"
    print("[upstream-stub] isolated MCP SSE stub on %s:%d (loopback only, "
          "%s)" % (LISTEN_HOST, LISTEN_PORT, mode),
          file=sys.stderr, flush=True)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning")
