"""ISOLATED_LIVE in-container upstream stub for the policy gateway (v3 Fix 3).

Why this exists: gateway.py's lifespan REQUIRES a reachable MCP SSE upstream
(it retries 30 times, then exits). The isolated stack has no github-mcp by
design (no GitHub, no production access). This stub is the honest in-stack
answer: a minimal MCP SSE server bound to CONTAINER LOOPBACK ONLY
(127.0.0.1:8084), serving ZERO tools.

Isolation properties (fail-closed by construction):
  - loopback-only listen: unreachable from the bridge network, never
    published, no host process, not a separate service, not a postgres twin;
  - ZERO tools: the gateway proxies nothing — list_tools is empty, and
    call_tool always raises, so no tool payload can ever transit;
  - no outbound connections of any kind;
  - exists solely so the gateway's real lifespan completes and its
    healthcheck (TCP to its own listen port) is meaningful.

Started by tools/gateway_entrypoint.py when UPSTREAM_URL points at the
in-container stub URL (http://127.0.0.1:8084/sse).
"""

from __future__ import annotations

import sys

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
import uvicorn

LISTEN_HOST = "127.0.0.1"   # container loopback ONLY — never the bridge
LISTEN_PORT = 8084

server = Server("mergepilot-isolated-upstream-stub")
sse = SseServerTransport("/messages/")


@server.list_tools()
async def list_tools():
    """ZERO tools: the isolated stack proxies nothing."""
    return []


@server.call_tool()
async def call_tool(name, arguments):
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
    print("[upstream-stub] isolated zero-tool MCP SSE stub on "
          "%s:%d (loopback only)" % (LISTEN_HOST, LISTEN_PORT),
          file=sys.stderr, flush=True)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning")
