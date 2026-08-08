#!/usr/bin/env python3
"""C2 github-mcp bridge MCP call (runs in m4f-runtime container).
Connects DIRECTLY to the github-mcp bridge SSE (no Policy Gateway, no auth —
the bridge holds the PAT internally). The harness uses this for: read main sha,
list branches, create source branch, push source file, create source PR, read
PR, list open PRs, close PR (update_pull_request), and the restricted
c2_delete_test_branch.

No PAT here — the bridge is the only GitHub caller. argv: tool, args_json."""
import asyncio, json, os, sys
from mcp import ClientSession
from mcp.client.sse import sse_client

BRIDGE = os.environ["C2_BRIDGE"]   # http://m5c2-gh:8082
TOOL = sys.argv[1]
ARGS = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}


async def main():
    try:
        async with sse_client(BRIDGE + "/sse") as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await asyncio.wait_for(s.call_tool(TOOL, ARGS), timeout=45)
                texts = [c.text for c in (res.content or []) if hasattr(c, "text")]
        print(json.dumps({"is_error": bool(res.isError), "content": " ".join(texts)[:4000]}))
    except Exception as e:
        print(json.dumps({"is_error": True, "exception": "%s: %s" % (type(e).__name__, str(e)[:200])}))


asyncio.run(main())
