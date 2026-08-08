#!/usr/bin/env python3
"""C2 github-mcp SSE->stdio bridge (minimal, mcp==1.28.1) + restricted test
branch-cleanup tool.

Proxy layer: official github-mcp-server (stdio) is the upstream; this bridge
serves SSE on 8082 and forwards list_tools/call_tool upstream.

Plus ONE restricted MCP tool — c2_delete_test_branch — used ONLY by the C2 test
harness for RUN_KEY-scoped fixture branch cleanup. It is NOT a general
delete_branch and is NOT in real-github-policy.yaml (the Policy Gateway DENIES
it for every role; only the harness calls the bridge directly).

Hard boundary:
  * PAT read from readonly secret-file into process env at startup; never in
    Config.Env/Args/logs/args/response. Only this process + the stdio child see it.
  * c2_delete_test_branch fail-closed validates: owner/repo fixed to the fixture,
    branch bound to the caller's RUN_KEY (feature/c2-src-<rk> or fix/<rk>-<hex12>),
    main/master/empty/traversal/encoding/control-chars/double-slash forbidden,
    no open PR may exist for the branch (PR must be closed first), branch must
    exist before delete and be absent after.
  * The internal HTTP DELETE uses the PAT from memory only; only 204/404 are
    success (404 = idempotent already_absent); 401/403/409/network/timeout all
    fail closed. The response contains only {deleted,already_absent,repo,branch,
    http_status} — never headers/body/secrets.
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import httpx
import uvicorn

UPSTREAM_CMD = os.environ.get("C2_GHMCP_CMD", "/usr/local/bin/github-mcp-server")
UPSTREAM_ARGS = os.environ.get("C2_GHMCP_ARGS", "stdio").split()
PAT_FILE = os.environ.get("C2_GHMCP_PAT_FILE", "/secrets/pat")
LISTEN_PORT = int(os.environ.get("C2_GHMCP_PORT", "8082"))

# Read PAT from secret-file into process runtime env (NOT Config.Env).
if not os.path.exists(PAT_FILE):
    print("FATAL: PAT secret-file not present at %s" % PAT_FILE, file=sys.stderr, flush=True)
    sys.exit(2)
with open(PAT_FILE, "r") as fh:
    _pat = fh.read().strip()
if len(_pat) < 20:
    print("FATAL: PAT secret-file too short (content not printed)", file=sys.stderr, flush=True)
    sys.exit(2)
os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = _pat
del _pat

_state: dict = {"session": None, "stack": None, "tool_count": 0, "ready": False}

# ── c2_delete_test_branch: restricted RUN_KEY-scoped fixture branch cleanup ──
FIXTURE_OWNER = "nghqqa"
FIXTURE_REPO = "MergePilot-e2e-fixture"
_RUN_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PROTECTED_BRANCHES = {"main", "master", "dev", "develop", "release", "prod", "gh-pages", "release/*"}
DELETE_TOOL = "c2_delete_test_branch"
DELETE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "branch", "run_key"],
    "properties": {
        "owner": {"type": "string"},
        "repo": {"type": "string"},
        "branch": {"type": "string"},
        "run_key": {"type": "string"},
    },
}


def _refused(reason, branch=None):
    return [TextContent(type="text", text=json.dumps(
        {"deleted": False, "refused": True, "reason": reason,
         "repo": FIXTURE_REPO, "branch": branch if isinstance(branch, str) else None}))]


def _validate_delete_args(owner, repo, branch, run_key):
    if owner != FIXTURE_OWNER:
        raise _Refused("owner_not_allowed")
    if repo != FIXTURE_REPO:
        raise _Refused("repo_not_allowed")
    if not isinstance(run_key, str) or not _RUN_KEY_RE.match(run_key):
        raise _Refused("bad_run_key")
    if not isinstance(branch, str) or not branch:
        raise _Refused("empty_branch")
    if branch in _PROTECTED_BRANCHES or branch.split("/")[0] in {"main", "master"}:
        raise _Refused("protected_branch")
    # control chars / %-encoding / path traversal / double slash
    if any(ord(c) < 32 or ord(c) == 127 for c in branch):
        raise _Refused("control_char")
    if "%" in branch or ".." in branch or "//" in branch or "\x00" in branch:
        raise _Refused("unsafe_chars")
    # branch MUST be bound to this RUN_KEY
    rk = re.escape(run_key)
    src_ok = re.match(r"^feature/c2-src-" + rk + r"$", branch)
    fix_ok = re.match(r"^fix/" + rk + r"-[0-9a-f]{12}$", branch)
    if not (src_ok or fix_ok):
        raise _Refused("branch_not_bound_to_run_key")


class _Refused(Exception):
    def __init__(self, reason):
        self.reason = reason


async def _upstream_branch_names(session):
    r = await session.call_tool("list_branches", {"owner": FIXTURE_OWNER, "repo": FIXTURE_REPO})
    txt = " ".join(c.text for c in (r.content or []) if hasattr(c, "text"))
    try:
        data = json.loads(txt)
    except Exception:
        raise _Refused("branch_read_schema")
    rows = data if isinstance(data, list) else data.get("branches", data.get("data", []))
    return [b.get("name") for b in rows if isinstance(b, dict)]


async def _upstream_open_pr_heads(session):
    r = await session.call_tool("list_pull_requests",
                                {"owner": FIXTURE_OWNER, "repo": FIXTURE_REPO, "state": "open", "perPage": 100})
    txt = " ".join(c.text for c in (r.content or []) if hasattr(c, "text"))
    try:
        data = json.loads(txt)
    except Exception:
        raise _Refused("pr_read_schema")
    rows = data if isinstance(data, list) else data.get("pullRequests", data.get("data", []))
    heads = []
    for p in rows:
        if isinstance(p, dict):
            h = p.get("head") or p.get("headRef")
            if isinstance(h, dict):
                heads.append(h.get("ref"))
            elif isinstance(h, str):
                heads.append(h)
    return [h for h in heads if h]


async def _git_refs_delete(branch):
    """DELETE /repos/<owner>/<repo>/git/refs/heads/<branch>. PAT from memory only."""
    pat = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not pat:
        raise _Refused("pat_unavailable")
    url = "https://api.github.com/repos/%s/%s/git/refs/heads/%s" % (
        FIXTURE_OWNER, FIXTURE_REPO, quote(branch, safe="/"))
    headers = {"Authorization": "Bearer " + pat,
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False, follow_redirects=False) as cli:
            resp = await cli.delete(url, headers=headers)
    except Exception:
        raise _Refused("network_or_timeout")
    return resp.status_code


async def _handle_delete_test_branch(args):
    owner = args.get("owner"); repo = args.get("repo")
    branch = args.get("branch"); run_key = args.get("run_key")
    try:
        _validate_delete_args(owner, repo, branch, run_key)
        session = _state["session"]
        if session is None:
            raise _Refused("upstream_not_ready")
        # PR must be closed first: no OPEN PR for this branch
        open_heads = await _upstream_open_pr_heads(session)
        if branch in open_heads:
            raise _Refused("open_pr_exists")
        # confirm branch exists before delete
        if branch not in await _upstream_branch_names(session):
            return [TextContent(type="text", text=json.dumps(
                {"deleted": False, "already_absent": True, "repo": FIXTURE_REPO,
                 "branch": branch, "http_status": 404}))]
        status = await _git_refs_delete(branch)
        if status == 204:
            pass
        elif status == 404:
            return [TextContent(type="text", text=json.dumps(
                {"deleted": False, "already_absent": True, "repo": FIXTURE_REPO,
                 "branch": branch, "http_status": 404}))]
        elif status in (401, 403):
            raise _Refused("forbidden_%d" % status)
        elif status == 409:
            raise _Refused("conflict_409")
        else:
            raise _Refused("unexpected_%d" % status)
        # confirm absent after delete
        still = branch in await _upstream_branch_names(session)
        return [TextContent(type="text", text=json.dumps(
            {"deleted": not still, "already_absent": False, "repo": FIXTURE_REPO,
             "branch": branch, "http_status": status, "verified_absent": not still}))]
    except _Refused as exc:
        return _refused(exc.reason, branch)


@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    stack = AsyncExitStack()
    await stack.__aenter__()
    try:
        params = StdioServerParameters(command=UPSTREAM_CMD, args=UPSTREAM_ARGS, env=dict(os.environ))
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools = await session.list_tools()
        _state["session"] = session
        _state["stack"] = stack
        _state["tool_count"] = len(tools.tools)
        _state["ready"] = True
        print("[ghmcp-bridge] upstream ready tool_count=%d" % _state["tool_count"],
              file=sys.stderr, flush=True)
        yield
    finally:
        _state["ready"] = False
        await stack.aclose()


server = Server("github-mcp-bridge-c2")


@server.list_tools()
async def _list_tools():  # type: ignore[no-untyped-def]
    s = _state["session"]
    r = await s.list_tools()
    tools = list(r.tools)
    tools.append(Tool(
        name=DELETE_TOOL,
        description="C2 TEST-ONLY: delete a fixture test branch bound to the caller RUN_KEY. "
                    "Not a general delete_branch; fail-closed scoped; not in Gateway policy.",
        inputSchema=DELETE_SCHEMA))
    return tools


@server.call_tool()
async def _call_tool(name, arguments):  # type: ignore[no-untyped-def]
    if name == DELETE_TOOL:
        return await _handle_delete_test_branch(arguments or {})
    s = _state["session"]
    r = await s.call_tool(name, arguments or {})
    return r.content


sse = SseServerTransport("/messages/")


async def _handle_sse(request):  # type: ignore[no-untyped-def]
    async with sse.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await server.run(r, w, server.create_initialization_options())


async def _health(request):  # type: ignore[no-untyped-def]
    return JSONResponse({"ok": _state["ready"], "tools": _state["tool_count"]},
                        status_code=200 if _state["ready"] else 503)


app = Starlette(
    routes=[
        Route("/sse", endpoint=_handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Route("/_health", _health),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="warning")
