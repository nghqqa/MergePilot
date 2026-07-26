#!/usr/bin/env python3
"""
probe-tools.py — 以指定角色连接 Policy Gateway,返回该角色可见的工具名列表(干净 JSON)。
token 从 gateway 容器自己的 ROLE_TOKENS env 读取(不在命令行暴露)。
用法(docker exec policy-gw): python3 /tmp/probe-tools.py <role> [--call <tool> key=value ...]
"""
import asyncio
import json
import os
import sys
from mcp.client.sse import sse_client
from mcp import ClientSession

ROLE = sys.argv[1]
TOKEN = json.loads(os.environ["ROLE_TOKENS"])[ROLE]


async def main():
    async with sse_client(
        f"http://localhost:8083/{ROLE}/sse",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if len(sys.argv) > 2 and sys.argv[2] == "--call":
                tool = sys.argv[3]
                args = {}
                for kv in sys.argv[4:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        # 类型转换:bool/数字(否则 schema 要求 boolean/number 的字段会被
                        # SDK input validation 在策略检查之前拒掉,测不到策略行为)
                        if v.lower() == "true":
                            args[k] = True
                        elif v.lower() == "false":
                            args[k] = False
                        elif v.lstrip("-").isdigit():
                            args[k] = int(v)
                        else:
                            try:
                                args[k] = float(v)
                            except ValueError:
                                args[k] = v
                res = await s.call_tool(tool, args)
                # 输出结果文本(便于 grep POLICY_DENIED)
                try:
                    for c in res.content:
                        if hasattr(c, "text"):
                            print(c.text[:300])
                except Exception as e:
                    print(f"<probe call error: {e}>")
                if getattr(res, "is_error", False):
                    print("<is_error=true>")
            else:
                tools = await s.list_tools()
                print(json.dumps(sorted(t.name for t in tools.tools)))


asyncio.run(main())
