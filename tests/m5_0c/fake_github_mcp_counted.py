#!/usr/bin/env python3
"""Minimal COUNTING fake GitHub MCP (SSE) for the M5-0C real-Gateway runtime
test.

Isolated upstream for the Policy Gateway. Implements the tool names the M5-0C
policy references (read + fix classes) and counts every CallTool by name, so the
test can DIRECTLY prove "DENY => upstream not called" (counter unchanged) rather
than inferring it from audit alone.

No real GitHub, no PAT, no network egress. Stub TextContent results only — the
policy test cares about the Gateway's ALLOW/DENY decision and whether the
upstream was reached, not the payload shape (which is covered by M4-F's
schema-validated e2e against the full fake_github_mcp.py).
"""
from __future__ import annotations

import json
import os

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

LISTEN_HOST = os.environ.get("M5C_FAKE_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("M5C_FAKE_PORT", "8082"))

# Tool names the policy references (read + fix classes). merge_pull_request is
# intentionally present so the test can prove it is DENIED for every role (it is
# in no policy class) and therefore never reaches this upstream.
TOOLS = [
    Tool(name="pull_request_read", description="stub", inputSchema={"type": "object"}),
    Tool(name="list_branches", description="stub", inputSchema={"type": "object"}),
    Tool(name="list_pull_requests", description="stub", inputSchema={"type": "object"}),
    Tool(name="get_file_contents", description="stub", inputSchema={"type": "object"}),
    Tool(name="get_commit", description="stub", inputSchema={"type": "object"}),
    Tool(name="list_commits", description="stub", inputSchema={"type": "object"}),
    Tool(name="create_branch", description="stub", inputSchema={"type": "object"}),
    Tool(name="push_files", description="stub", inputSchema={"type": "object"}),
    Tool(name="create_pull_request", description="stub", inputSchema={"type": "object"}),
    Tool(name="merge_pull_request", description="stub", inputSchema={"type": "object"}),
]

_counts: dict[str, int] = {t.name: 0 for t in TOOLS}
_total = {"calls": 0}

server = Server("m5-0c-counting-fake-mcp")


@server.list_tools()
async def _list_tools():  # type: ignore[no-untyped-def]
    return TOOLS


@server.call_tool()
async def _call_tool(name: str, args: dict):  # type: ignore[no-untyped-def]
    _total["calls"] += 1
    _counts[name] = _counts.get(name, 0) + 1
    # Echo the tool + args so the client can confirm the upstream actually ran.
    return [TextContent(type="text", text=json.dumps({"tool": name, "args": args}))]


sse = SseServerTransport("/messages/")


async def _handle_sse(request):  # type: ignore[no-untyped-def]
    async with sse.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await server.run(r, w, server.create_initialization_options())


async def _count(_request):  # type: ignore[no-untyped-def]
    return JSONResponse({"total": _total["calls"], "by_tool": dict(_counts)})


app = Starlette(
    routes=[
        Route("/sse", endpoint=_handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Route("/_count", _count),
    ]
)


if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning")
