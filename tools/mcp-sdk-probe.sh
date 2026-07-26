#!/bin/bash
# mcp-sdk-probe.sh — 就地探 github-mcp 容器里 mcp SDK 的 API 签名(bridge 依赖 mcp-proxy,mcp 已装)。
set -uo pipefail
OUT=/mnt/d/goai/tools/mcp-sdk-probe.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }

log "=== mcp SDK 版本 ==="
docker exec github-mcp pip show mcp 2>&1 | grep -aiE "name|version|location" >> "$OUT" || true
log ""
log "=== 关键模块签名(Server / SseServerTransport / ClientSession / sse_client)==="
docker exec github-mcp python3 -c '
import inspect, mcp
print("mcp.__version__:", getattr(mcp,"__version__","?"))
try:
    from mcp.server import Server; print("Server init:", str(inspect.signature(Server.__init__))[:120])
except Exception as e: print("Server ERR:", e)
try:
    from mcp.server.sse import SseServerTransport; print("SseServerTransport:", str(inspect.signature(SseServerTransport.__init__))[:160])
    print("  connect_sse:", str(inspect.signature(SseServerTransport.connect_sse))[:200])
except Exception as e: print("SseServerTransport ERR:", e)
try:
    from mcp.client.sse import sse_client; print("sse_client:", str(inspect.signature(sse_client))[:160])
except Exception as e: print("sse_client ERR:", e)
try:
    from mcp import ClientSession; print("ClientSession init:", str(inspect.signature(ClientSession.__init__))[:120])
    print("  list_tools:", str(inspect.signature(ClientSession.list_tools))[:120])
    print("  call_tool:", str(inspect.signature(ClientSession.call_tool))[:160])
except Exception as e: print("ClientSession ERR:", e)
try:
    from mcp.server.models import InitializationOptions; print("InitializationOptions OK")
except Exception as e: print("InitOptions ERR:", e)
try:
    from mcp.server.fastmcp import FastMCP; print("FastMCP available (alt)")
except Exception as e: print("FastMCP ERR:", e)
' >> "$OUT" 2>&1 || true
log ""
log "=== server 模块里 list_tools/call_tool 装饰器怎么注册 ==="
docker exec github-mcp python3 -c '
from mcp.server import Server
m = [x for x in dir(Server) if x in ("list_tools","call_tool","list_prompts","list_resources","get_capabilities")]
print("Server attrs:", m)
import inspect
for x in ("list_tools","call_tool"):
    if hasattr(Server,x):
        print(x, ":", str(inspect.signature(getattr(Server,x)))[:140])
' >> "$OUT" 2>&1 || true
log ""
log "=== FastMCP 用法示例(最省事的 server 写法) ==="
docker exec github-mcp python3 -c '
from mcp.server.fastmcp import FastMCP
import inspect
print("FastMCP methods:", [x for x in dir(FastMCP) if "tool" in x.lower() or "sse" in x.lower() or "run" in x.lower()][:15])
print("sse:", hasattr(FastMCP, "run_sse_async") or hasattr(FastMCP,"sse_app"))
' >> "$OUT" 2>&1 || true
echo "done -> $OUT"
