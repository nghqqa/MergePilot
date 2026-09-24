# -*- coding: utf-8 -*-
"""container_gateway — 经 docker exec 在 reviewer 容器内调用网关。

宿主无法路由 elemiso-net(实测),但 reviewer 容器可达网关
(elemiso-controller:8080)。本适配器与 ModelGatewayClient 同接口:
chat(model, messages, ...) -> {ok, usage?, content, ...}
凭据经 stdin 传入容器内子进程,不落入 argv/日志/输出。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional

CONTAINER = "elemiso-worker-reviewer"
INNER_READER = r'''import json, sys, urllib.request
req = json.load(sys.stdin)
body = json.dumps({"model": req["model"], "messages": req["messages"],
                   "max_tokens": req["max_tokens"], "temperature": req["temperature"]}).encode()
r = urllib.request.Request(req["url"], data=body, method="POST", headers={
    "Authorization": "Bearer " + req["key"], "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=req["timeout"]) as resp:
        d = json.load(resp)
    print(json.dumps({"ok": True, "content": d["choices"][0]["message"]["content"],
                      "usage": d.get("usage"), "model": d.get("model")}))
except Exception as e:
    print(json.dumps({"ok": False, "kind": type(e).__name__, "detail": str(e)[:150]}))
'''


class ContainerGatewayClient:
    """与 ModelGatewayClient 同接口;底层经 docker exec 在容器内发请求。"""

    def __init__(self, container: str, base_url: str, api_key: str):
        self.container = container
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key          # 仅存在于进程内存/stdin,不打印

    def chat(self, model: str, messages: List[Dict[str, str]], *,
             timeout_s: int = 120, max_tokens: int = 4000,
             temperature: float = 0.2) -> Dict[str, Any]:
        req = {"url": self.base_url + "/chat/completions", "key": self.api_key,
               "model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature,
               "timeout": min(timeout_s, 110)}
        proc = subprocess.run(
            ["docker", "exec", "-i", self.container, "python3", "-c", INNER_READER],
            input=json.dumps(req).encode(), capture_output=True, timeout=timeout_s + 20)
        try:
            out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        except Exception:
            return {"ok": False, "kind": "bridge", "detail":
                    (proc.stderr or b"").decode("utf-8", "replace")[:150]}
        if not out.get("ok"):
            return {"ok": False, "kind": out.get("kind", "upstream"),
                    "detail": out.get("detail", "")[:150]}
        return out
