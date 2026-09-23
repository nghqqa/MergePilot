# -*- coding: utf-8 -*-
"""model_gateway.client — OpenAI-compatible 网关客户端 + 错误分类。

只依赖标准库(urllib)。接口面 = 现链真实使用的两个端点:
  GET  {base}/models            目录探测(health/model-list 等价能力检查)
  POST {base}/chat/completions  最小非敏感请求(隔离 smoke 用)
错误分类与桥的发布五分类同纪律:结果不明(transport/timeout)绝不当失败重试
处理,由调用方决定;429/5xx 可重试;401=凭证配置;其余 4xx=permanent。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

UrlopenFn = Callable[..., Any]

# 错误类别(与 tools/gh-bridge 发布分类口径对齐的模型调用版):
#   auth        401——key 配置问题,重试无意义
#   rate_limited 429——限频,可退避重试
#   retryable   5xx / 403(限频文案)——可退避重试
#   permanent   其余 4xx(400/403 非限频/404/422)——参数/权限问题
#   timeout     本地超时——结果未知(上游可能已计费/已生成)
#   transport   连接层失败——结果未知
#   unknown     无法归类(保守按未知)
TIMEOUT_KIND = "timeout"
TRANSPORT_KIND = "transport"


def classify_model_error(http: Optional[int], detail: str = "") -> str:
    """(http 状态码, 细节) → 错误类别。纯函数。"""
    if http == 401:
        return "auth"
    if http == 429:
        return "rate_limited"
    if http is not None and 500 <= http <= 599:
        return "retryable"
    if http == 403:
        return "rate_limited" if "rate limit" in str(detail).lower() else "permanent"
    if http is not None and 400 <= http <= 499:
        return "permanent"
    return "unknown"


def _mk_request(url: str, key: Optional[str], body: Optional[Dict[str, Any]] = None) -> Any:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, method="POST" if body else "GET",
                                  headers=headers)


class ModelGatewayClient:
    """最小 OpenAI-compatible 客户端。urlopen_fn 可注入(测试离线跑)。"""

    def __init__(self, base_url: str, api_key: Optional[str],
                 urlopen_fn: Optional[UrlopenFn] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._urlopen = urlopen_fn or urllib.request.urlopen

    @property
    def urlopen_fn(self) -> UrlopenFn:
        """底层传输函数(测试离线注入用;smoke 借它派生坏 key 探测器)。"""
        return self._urlopen

    def list_models(self, timeout_s: float = 10.0) -> Dict[str, Any]:
        """GET /models → {ok, http, models[], kind?, detail?}。"""
        url = self.base_url + "/models"
        t0 = time.time()
        try:
            with self._urlopen(_mk_request(url, self.api_key), timeout=timeout_s) as r:
                d = json.loads(r.read().decode("utf-8"))
            models = sorted(str(m.get("id")) for m in (d.get("data") or []) if m.get("id"))
            return {"ok": True, "http": getattr(r, "status", 200), "models": models,
                    "latency_ms": int((time.time() - t0) * 1000)}
        except urllib.error.HTTPError as e:
            return {"ok": False, "http": e.code, "models": [],
                    "kind": classify_model_error(e.code, str(e.reason)),
                    "detail": str(e.reason)[:160],
                    "latency_ms": int((time.time() - t0) * 1000)}
        except socket.timeout as e:
            return {"ok": False, "http": None, "models": [], "kind": TIMEOUT_KIND,
                    "detail": type(e).__name__, "latency_ms": int((time.time() - t0) * 1000)}
        except Exception as e:   # URLError/ConnectionError/...
            return {"ok": False, "http": None, "models": [], "kind": TRANSPORT_KIND,
                    "detail": type(e).__name__ + ": " + str(e)[:140],
                    "latency_ms": int((time.time() - t0) * 1000)}

    def chat(self, model: str, messages: List[Dict[str, str]],
             timeout_s: float = 60.0, max_tokens: Optional[int] = None,
             temperature: Optional[float] = None) -> Dict[str, Any]:
        """POST /chat/completions(单次,无内部重试——重试策略归调用方)。

        返回 {ok, http, model, content, finish_reason, usage, latency_ms}
        或 {ok:False, http, kind, detail, latency_ms}。"""
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        url = self.base_url + "/chat/completions"
        t0 = time.time()
        try:
            with self._urlopen(_mk_request(url, self.api_key, body), timeout=timeout_s) as r:
                d = json.loads(r.read().decode("utf-8"))
            choice = ((d.get("choices") or [{}])[0])
            msg = choice.get("message") or {}
            usage = d.get("usage") or {}
            return {"ok": True, "http": getattr(r, "status", 200),
                    "model": d.get("model"), "content": msg.get("content"),
                    "finish_reason": choice.get("finish_reason"),
                    "usage": usage, "raw_keys": sorted(d.keys()),
                    "latency_ms": int((time.time() - t0) * 1000)}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = str(e.reason)[:160]
            return {"ok": False, "http": e.code,
                    "kind": classify_model_error(e.code, detail + " " + str(e.reason)),
                    "detail": detail, "latency_ms": int((time.time() - t0) * 1000)}
        except socket.timeout as e:
            return {"ok": False, "http": None, "kind": TIMEOUT_KIND,
                    "detail": type(e).__name__, "latency_ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            return {"ok": False, "http": None, "kind": TRANSPORT_KIND,
                    "detail": type(e).__name__ + ": " + str(e)[:140],
                    "latency_ms": int((time.time() - t0) * 1000)}
