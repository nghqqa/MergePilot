#!/usr/bin/env python3
"""Stateful protocol-real GitHub MCP fixture for the M4-F Docker E2E."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    TextContent,
    TextResourceContents,
    Tool,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route


REPO = os.environ.get("FIXTURE_REPO", "example/project")
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
SOURCE_BRANCH = "fix/run-123-demo"
DIFF = (
    "diff --git a/src/user_service.py b/src/user_service.py\n"
    "--- a/src/user_service.py\n"
    "+++ b/src/user_service.py\n"
    "@@ -1 +1 @@\n"
    "-cur.execute('SELECT * FROM users WHERE id=' + user_id)\n"
    "+cur.execute('SELECT name FROM users WHERE id = %s', (user_id,))\n"
)
PATCH = (
    "@@ -1 +1 @@\n"
    "-cur.execute('SELECT * FROM users WHERE id=' + user_id)\n"
    "+cur.execute('SELECT name FROM users WHERE id = %s', (user_id,))\n"
)


branches: dict[str, str] = {"main": BASE_SHA, SOURCE_BRANCH: HEAD_SHA}
branch_files: dict[str, dict[str, str]] = {
    SOURCE_BRANCH: {
        "src/user_service.py": (
            "def load_user(cur, user_id):\n"
            "    cur.execute('SELECT name FROM users WHERE id = %s', (user_id,))\n"
            "    return cur.fetchone()\n"
        )
    }
}
commits: dict[str, dict[str, Any]] = {}
pulls: dict[int, dict[str, Any]] = {
    42: {
        "number": 42,
        "state": "open",
        "merged": False,
        "draft": False,
        "title": "fixture source PR",
        "body": "M4-F source",
        "head": {
            "sha": HEAD_SHA,
            "ref": SOURCE_BRANCH,
            "repo": {"full_name": REPO},
        },
        "base": {"sha": BASE_SHA, "ref": "main"},
        "html_url": "https://fixture.invalid/pull/42",
    }
}


def _json(value: Any) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(value, ensure_ascii=False, sort_keys=True),
            )
        ]
    )


def _pr_files(number: int) -> list[dict[str, Any]]:
    if number == 42:
        return [
            {
                "filename": "src/user_service.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": PATCH,
            }
        ]
    pr = pulls[number]
    head = pr["head"]["ref"]
    return [
        {"filename": path, "status": "modified", "additions": 1, "deletions": 0}
        for path in sorted(branch_files.get(head, {}))
    ]


TOOLS = [
    "pull_request_read",
    "list_branches",
    "list_pull_requests",
    "get_file_contents",
    "get_commit",
    "list_commits",
    "create_branch",
    "push_files",
    "create_pull_request",
]

server = Server("m4f-fake-github")


@server.list_tools()
async def list_tools():
    schema = {"type": "object", "additionalProperties": True}
    return [Tool(name=name, description=f"fixture {name}", inputSchema=schema) for name in TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None):
    args = dict(arguments or {})
    if name == "pull_request_read":
        number = int(args.get("pullNumber", 0))
        method = args.get("method")
        if number not in pulls:
            return CallToolResult(
                content=[TextContent(type="text", text="Not Found")], isError=True
            )
        if method == "get":
            return _json(pulls[number])
        if method == "get_diff":
            return CallToolResult(content=[TextContent(type="text", text=DIFF)])
        if method == "get_files":
            return _json(_pr_files(number))

    if name == "list_branches":
        return _json(
            [{"name": branch, "sha": sha} for branch, sha in sorted(branches.items())]
        )

    if name == "list_pull_requests":
        return _json([pulls[number] for number in sorted(pulls)])

    if name == "create_branch":
        branch = str(args["branch"])
        source = str(args.get("from_branch") or args.get("fromBranch") or "main")
        branches.setdefault(branch, branches.get(source, BASE_SHA))
        branch_files.setdefault(branch, {})
        return _json({"ref": f"refs/heads/{branch}", "sha": branches[branch]})

    if name == "push_files":
        branch = str(args["branch"])
        files = args.get("files") or []
        if branch not in branches or not isinstance(files, list):
            return CallToolResult(
                content=[TextContent(type="text", text="invalid branch/files")],
                isError=True,
            )
        contents: dict[str, str] = {}
        for item in files:
            contents[str(item["path"])] = str(item["content"])
        material = json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
        head_sha = hashlib.sha256(material).hexdigest()[:40]
        branches[branch] = head_sha
        branch_files[branch] = contents
        commits[head_sha] = {
            "sha": head_sha,
            "parent": BASE_SHA,
            "files": sorted(contents),
        }
        return _json({"sha": head_sha})

    if name == "list_commits":
        ref = str(args.get("sha") or "")
        head_sha = branches.get(ref, ref)
        commit = commits.get(head_sha)
        values = [{"sha": head_sha}]
        if commit:
            values.append({"sha": commit["parent"]})
        return _json(values)

    if name == "get_commit":
        sha = str(args.get("sha") or "")
        commit = commits.get(sha)
        if not commit:
            return _json({"sha": sha, "files": []})
        return _json(
            {
                "sha": sha,
                "files": [
                    {"filename": path, "status": "modified"}
                    for path in commit["files"]
                ],
            }
        )

    if name == "get_file_contents":
        path = str(args.get("path") or "")
        ref = str(args.get("ref") or "").removeprefix("refs/heads/")
        if ref.startswith("refs/pull/"):
            try:
                ref = pulls[int(ref.split("/")[2])]["head"]["ref"]
            except Exception:
                ref = ""
        content = branch_files.get(ref, {}).get(path)
        if content is None:
            return CallToolResult(
                content=[TextContent(type="text", text="Not Found")], isError=True
            )
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        return CallToolResult(
            content=[
                TextContent(type="text", text=f"SHA: {digest}"),
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=f"file:///fixture/{path}",
                        mimeType="text/plain",
                        text=content,
                    ),
                ),
            ]
        )

    if name == "create_pull_request":
        head = str(args["head"])
        number = max(pulls) + 1
        pulls[number] = {
            "number": number,
            "state": "open",
            "merged": False,
            "draft": bool(args.get("draft", False)),
            "title": str(args["title"]),
            "body": str(args.get("body") or ""),
            "head": {
                "sha": branches[head],
                "ref": head,
                "repo": {"full_name": REPO},
            },
            "base": {"sha": BASE_SHA, "ref": str(args["base"])},
            "html_url": f"https://fixture.invalid/pull/{number}",
        }
        return _json(pulls[number])

    return CallToolResult(
        content=[TextContent(type="text", text=f"unsupported fixture tool: {name}")],
        isError=True,
    )


sse = SseServerTransport(
    "/messages/",
    security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def handle_sse(request: Request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ]
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="warning")
