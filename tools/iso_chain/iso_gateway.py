# -*- coding: utf-8 -*-
"""iso_gateway — 隔离网关适配器(一次性容器;不经共享 reviewer)。

宿主无法路由 elemiso-net(实测)。本适配器把每次模型调用放进**一次性
python:3.11-slim 容器**(挂 elemiso-net,用后即毁)内执行 HTTP 请求——
不触碰共享 reviewer/leader 容器与文件。

安全: 网关 key 经 stdin 传入一次性容器内子进程,不落入 argv/日志/输出;
响应只回传 content/usage/finish_reason,不含 key。

已证实参数(官方 api-docs.deepseek.com /guides/thinking_mode):
  deepseek-flash(V4.1-Flash) thinking 默认开启(effort=high);
  关闭 = 请求体 {"thinking": {"type": "disabled"}}。本适配器默认带上。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional

NET = "elemiso-net"
GW = "http://elemiso-controller:8080/v1"
IMAGE = "python:3.11-slim"

INNER = r'''import json, sys, urllib.request
req = json.load(sys.stdin)
body = {"model": req["model"], "messages": req["messages"],
        "max_tokens": req["max_tokens"], "temperature": req["temperature"]}
if req.get("thinking_disabled"):
    body["thinking"] = {"type": "disabled"}
data = json.dumps(body).encode("utf-8")
r = urllib.request.Request(req["url"], data=data, method="POST", headers={
    "Authorization": "Bearer " + req["key"], "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=req["timeout"]) as resp:
        d = json.load(resp)
    ch = (d.get("choices") or [{}])[0]
    out = {"ok": True, "content": ch.get("message", {}).get("content"),
           "finish_reason": ch.get("finish_reason"),
           "usage": d.get("usage"), "model": d.get("model"),
           "reasoning_present": bool((ch.get("message") or {}).get("reasoning_content"))}
    print(json.dumps(out))
except urllib.error.HTTPError as e:
    print(json.dumps({"ok": False, "kind": "http_%d" % e.code,
                      "detail": e.read().decode("utf-8", "replace")[:200]}))
except Exception as e:
    print(json.dumps({"ok": False, "kind": type(e).__name__,
                      "detail": str(e)[:150]}))
'''


class IsoGatewayClient:
    """与 ModelGatewayClient 同接口;每次调用一个一次性容器。"""

    def __init__(self, base_url: str = GW, api_key: str = None,
                 network: str = NET, image: str = IMAGE,
                 disable_thinking: bool = True):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.network = network
        self.image = image
        self.disable_thinking = disable_thinking

    def chat(self, model: str, messages: List[Dict[str, str]], *,
             timeout_s: int = 110, max_tokens: int = 8000,
             temperature: float = 0.2) -> Dict[str, Any]:
        req = {"url": self.base_url + "/chat/completions", "key": self.api_key,
               "model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature,
               "thinking_disabled": self.disable_thinking,
               "timeout": min(timeout_s, 100)}
        proc = subprocess.run(
            ["docker", "run", "--rm", "-i", "--network", self.network,
             self.image, "python", "-c", INNER],
            input=json.dumps(req).encode(), capture_output=True,
            timeout=timeout_s + 30)
        try:
            out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        except Exception:
            return {"ok": False, "kind": "bridge",
                    "detail": (proc.stderr or b"").decode("utf-8", "replace")[:150]}
        if not out.get("ok"):
            return {"ok": False, "kind": out.get("kind", "upstream"),
                    "detail": out.get("detail", "")[:150],
                    "finish_reason": out.get("finish_reason")}
        return out
