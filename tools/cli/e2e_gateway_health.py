"""M8-GH-4B3-W3B §4: Gateway MCP semantic health adapter.

Production-capable: verifies MCP initialize + tools/list against a
frozen read-only tool set. Injectable transport (fake for tests).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Optional

#: Frozen read-only tool contract. Authority: the deployed
#: github-mcp-server (digest-pinned in Dockerfile.mcp-bridge) live
#: toolset intersected with the fixture policy's tool_classes.read
#: (tools/policy-gateway/policy-e2e-fixture.yaml) — the gateway
#: exposes EXACTLY this set to the reviewer/verifier roles. The
#: previous placeholder names (get_pull_request,
#: get_pull_request_files, get_branch) never existed on the real
#: server (verified live: 44 tools, pull_request_read etc.) and a
#: manager role does not exist in the policy at all.
FROZEN_READ_ONLY_TOOLS = frozenset((
    "get_me",
    "get_commit",
    "get_file_contents",
    "get_label",
    "get_latest_release",
    "get_release_by_tag",
    "get_tag",
    "list_branches",
    "list_commits",
    "list_issues",
    "list_pull_requests",
    "list_releases",
    "list_repository_collaborators",
    "list_tags",
    "search_code",
    "search_commits",
    "search_issues",
    "search_pull_requests",
    "issue_read",
    "pull_request_read",
))


class GatewayHealthError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


def default_mcp_transport(method: str, url: str, *, headers: dict,
                          body: Optional[dict]) -> tuple:
    """SSE transport for MCP (injectable in tests)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers), raw
    except Exception as exc:
        raise GatewayHealthError(
            "GATEWAY_UPSTREAM_UNREACHABLE", type(exc).__name__) from None


def verify_gateway_mcp_health_required(*, upstream_url: str,
                                       required: frozenset,
                                       exact: bool = True,
                                       transport: Callable =
                                       default_mcp_transport) -> dict:
    """Generalized MCP semantic health (§6/§9): initialize + tools/list
    against a caller-supplied required set. exact=True enforces set
    equality (the Gateway frozen contract); exact=False enforces the
    required subset (the Bridge upstream contract — the four-tool
    filter is enforced at the Gateway, not at the bridge)."""
    status, headers, body = transport(
        "GET", upstream_url.rstrip("/"),
        headers={"Accept": "text/event-stream"}, body=None)
    if status != 200:
        raise GatewayHealthError("GATEWAY_INITIALIZE_FAILED",
                                 "HTTP %d" % status)
    status, headers, body = transport(
        "POST", upstream_url.rstrip("/") + "/messages",
        headers={"Content-Type": "application/json"},
        body={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    if status != 200:
        raise GatewayHealthError("GATEWAY_TOOLS_LIST_FAILED",
                                 "HTTP %d" % status)
    try:
        resp = json.loads(body) if isinstance(body, str) else body
        raw_tools = resp.get("result", {}).get("tools", [])
        tool_names = frozenset(t.get("name", "") for t in raw_tools
                               if isinstance(t, dict))
    except (ValueError, AttributeError):
        raise GatewayHealthError("GATEWAY_TOOLS_PARSE_ERROR",
                                 "invalid tools/list response") from None
    if not tool_names:
        raise GatewayHealthError("GATEWAY_ZERO_TOOLS",
                                 "no tools returned")
    missing = frozenset(required) - tool_names
    if missing:
        raise GatewayHealthError("GATEWAY_MISSING_TOOLS",
                                 "missing: %s" % sorted(missing))
    if exact:
        extra = tool_names - frozenset(required)
        if extra:
            raise GatewayHealthError(
                "GATEWAY_EXTRA_TOOLS",
                "extra or write tools: %s" % sorted(extra))
    return {"healthy": True, "tools": sorted(tool_names), "error": None}


def verify_gateway_mcp_health(*, upstream_url: str,
                               transport: Callable = default_mcp_transport
                               ) -> dict:
    """§4: MCP initialize + tools/list semantic health.

    Returns {"healthy": bool, "tools": [...], "error": str|None}.
    Fails on: zero tools, missing tools, extra tools, write tools,
    initialize failure, upstream failure, or non-semantic (Running/port)
    indicators."""
    # 1. MCP initialize (SSE POST)
    init_url = upstream_url.rstrip("/")
    try:
        status, headers, body = transport(
            "GET", init_url, headers={"Accept": "text/event-stream"},
            body=None)
    except GatewayHealthError:
        raise
    except Exception as exc:
        raise GatewayHealthError(
            "GATEWAY_UPSTREAM_UNREACHABLE", type(exc).__name__) from None

    if status != 200:
        raise GatewayHealthError("GATEWAY_INITIALIZE_FAILED",
                                 "HTTP %d" % status)

    # 2. tools/list (via SSE message)
    list_url = upstream_url.rstrip("/") + "/messages"
    try:
        status, headers, body = transport(
            "POST", list_url,
            headers={"Content-Type": "application/json"},
            body={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    except GatewayHealthError:
        raise
    except Exception as exc:
        raise GatewayHealthError(
            "GATEWAY_UPSTREAM_UNREACHABLE", type(exc).__name__) from None

    if status != 200:
        raise GatewayHealthError("GATEWAY_TOOLS_LIST_FAILED",
                                 "HTTP %d" % status)

    # Parse tools from response
    try:
        resp = json.loads(body) if isinstance(body, str) else body
        tools_result = resp.get("result", {})
        raw_tools = tools_result.get("tools", [])
        tool_names = frozenset(t.get("name", "") for t in raw_tools
                               if isinstance(t, dict))
    except (ValueError, AttributeError):
        raise GatewayHealthError("GATEWAY_TOOLS_PARSE_ERROR",
                                 "invalid tools/list response") from None

    # 3. Exact set comparison
    if not tool_names:
        raise GatewayHealthError("GATEWAY_ZERO_TOOLS", "no tools returned")

    missing = FROZEN_READ_ONLY_TOOLS - tool_names
    if missing:
        raise GatewayHealthError("GATEWAY_MISSING_TOOLS",
                                 "missing: %s" % sorted(missing))

    extra = tool_names - FROZEN_READ_ONLY_TOOLS
    if extra:
        raise GatewayHealthError(
            "GATEWAY_EXTRA_TOOLS",
            "extra or write tools: %s" % sorted(extra))

    return {"healthy": True, "tools": sorted(tool_names), "error": None}


def verify_gateway_mcp_health_safe(*, upstream_url: str,
                                   transport=default_mcp_transport
                                   ) -> dict:
    """Non-raising wrapper for status reporting."""
    try:
        return verify_gateway_mcp_health(
            upstream_url=upstream_url, transport=transport)
    except GatewayHealthError as exc:
        return {"healthy": False, "tools": [], "error": exc.code}


__all__ = [
    "FROZEN_READ_ONLY_TOOLS", "GatewayHealthError",
    "default_mcp_transport", "verify_gateway_mcp_health",
    "verify_gateway_mcp_health_safe",
    "verify_gateway_mcp_health_required",
]
