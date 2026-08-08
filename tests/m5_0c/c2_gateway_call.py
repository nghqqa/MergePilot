#!/usr/bin/env python3
"""C2 Policy Gateway MCP call (runs in m4f-runtime container).
Connects to the real Policy Gateway SSE endpoint as <role>, calls one tool,
prints {is_error, content}. Used for: reading fixture main sha (list_branches as
fixer) + negative-gate probes (deny must return is_error + no upstream effect).
PAT never handled here — Gateway holds no PAT; github-mcp does."""
import asyncio, json, os, sys
from mcp import ClientSession
from mcp.client.sse import sse_client

GW = os.environ["C2_GATEWAY"]
ROLE = os.environ["C2_ROLE"]
TOK = os.environ["C2_TOKEN"]
TOOL = sys.argv[1]
ARGS = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}


async def main():
    url = "%s/%s/sse" % (GW, ROLE)
    try:
        async with sse_client(url, headers={"Authorization": "Bearer " + TOK}) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await asyncio.wait_for(s.call_tool(TOOL, ARGS), timeout=30)
                texts = [c.text for c in (res.content or []) if hasattr(c, "text")]
        print(json.dumps({"is_error": bool(res.isError), "content": " ".join(texts)[:3000]}))
    except Exception as e:
        print(json.dumps({"is_error": True, "exception": "%s: %s" % (type(e).__name__, str(e)[:200])}))


asyncio.run(main())
