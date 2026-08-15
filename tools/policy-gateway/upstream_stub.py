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
    async with sse.connect_sse(request.scope, request.receive,
                               request.scope["send"]):
        await server.run(sse.get_read_stream(), sse.get_write_stream(),
                         server.create_initialization_options())


app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ],
)


if __name__ == "__main__":
    print("[upstream-stub] isolated zero-tool MCP SSE stub on "
          "%s:%d (loopback only)" % (LISTEN_HOST, LISTEN_PORT),
          file=sys.stderr, flush=True)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning")
