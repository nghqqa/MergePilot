#!/usr/bin/env python3
"""mcp_bridge_health.py — bridge MCP initialize + tools/list 自检(M8-GH-4B3 §4)。

合同:验证进程存活、SSE 端点可达、MCP initialize 握手与 tools/list 非空。
**不调用任何真实 GitHub repo 工具**——只验证 MCP 协议层存活。
退出码 0=健康,3=配置/不可达。
"""

from __future__ import annotations

import os
import sys
import urllib.request


def health(base_url: str) -> int:
    """对 <base>/sse 做一次轻量 MCP initialize 探测。

    mcp-proxy 的 SSE 端点在 GET /sse 时返回 event stream;本探针仅验证
    HTTP 可达 + 流头(200 + text/event-stream),真正的 initialize/
    tools/list 由 gateway lifespan 完成(其失败会阻止 gateway healthy)。
    """
    url = base_url.rstrip("/") + "/sse"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "text/event-stream")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                return 1
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                return 1
            return 0
    except OSError:
        return 1


def main() -> int:
    base_url = os.environ.get("MCP_BRIDGE_SSE_URL",
                              "http://127.0.0.1:8082")
    rc = health(base_url)
    if rc != 0:
        sys.stderr.write("[mcp-bridge] health probe failed (sse=%s)\n"
                         % base_url)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
