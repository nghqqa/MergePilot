# -*- coding: utf-8 -*-
"""model_gateway.config — 模型调用配置面(2026-09-23 切换准备)。

现状(复核确认):
  worker 内 active_model.json {provider_id: agentteams-gateway, model: deepseek-chat}
  → higress 网关 http://elemiso-controller:8080/v1 (OpenAI-compatible)
  → 上游 https://api.deepseek.com (AGENTTEAMS_OPENAI_BASE_URL)。
  上游 /models 现只提供 **deepseek-flash / deepseek-v4-pro**;deepseek-chat
  已不在上游目录(CASE1 之后的上游变更)。

原则:
  * 代码默认值 = 当前已验证配置(deepseek-chat 经 agentteams-gateway),
    不把新模型设为生产默认;切换只经环境变量/部署配置。
  * API key 永不进代码/仓库:配置面只接受"key 变量名"(MERGEPILOT_MODEL_API_KEY_ENV),
    值由运行环境注入;smoke 在 worker 容器内取网关 key,从不打印。
  * timeout/max_attempts/temperature/max_tokens 显式可配;temperature/max_tokens
    默认不发送(与现链一致:agentloop 不外露 generation params)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 当前已验证配置(2026-09-23 CASE1 基线)——切走前永远不改这三行默认值
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "http://elemiso-controller:8080/v1"
DEFAULT_API_KEY_ENV = "AGENTTEAMS_GATEWAY_API_KEY"

MODEL_ENV = "MERGEPILOT_MODEL"
BASE_URL_ENV = "MERGEPILOT_PROVIDER_BASE_URL"
API_KEY_ENV_NAME_ENV = "MERGEPILOT_MODEL_API_KEY_ENV"   # 值:存 key 的环境变量名
TIMEOUT_ENV = "MERGEPILOT_MODEL_TIMEOUT_S"
MAX_ATTEMPTS_ENV = "MERGEPILOT_MODEL_MAX_ATTEMPTS"
TEMPERATURE_ENV = "MERGEPILOT_MODEL_TEMPERATURE"
MAX_TOKENS_ENV = "MERGEPILOT_MODEL_MAX_TOKENS"


@dataclass(frozen=True)
class ModelCallConfig:
    """单次模型调用的完整配置;load_model_config(env) 是唯一构造入口。"""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_s: float = 60.0
    max_attempts: int = 3
    temperature: Optional[float] = None   # None = 不发送(provider 默认)
    max_tokens: Optional[int] = None      # None = 不发送
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_manifest_dict(self) -> Dict[str, Any]:
        """入 run-manifest 的形态(无秘密;key 只出现变量名)。"""
        return {"primary": self.model,
                "provider_base_url": self.base_url,
                "api_key_env": self.api_key_env,
                "timeout_s": self.timeout_s,
                "max_attempts": self.max_attempts,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens}


def load_model_config(environ: Optional[Dict[str, str]] = None) -> ModelCallConfig:
    """从环境读配置;未设置的项落到当前已验证默认值(不静默换模型)。"""
    env = os.environ if environ is None else environ

    def _get(name: str) -> str:
        v = (env.get(name) or "").strip()
        return v

    def _float(name: str, default):
        v = _get(name)
        if not v:
            return default
        try:
            return float(v)
        except ValueError:
            raise ValueError("%s must be a number, got %r" % (name, v))

    def _int(name: str, default):
        v = _get(name)
        if not v:
            return default
        try:
            return int(v)
        except ValueError:
            raise ValueError("%s must be an integer, got %r" % (name, v))

    timeout_s = _float(TIMEOUT_ENV, 60.0)
    if timeout_s <= 0:
        raise ValueError("%s must be > 0" % TIMEOUT_ENV)
    max_attempts = _int(MAX_ATTEMPTS_ENV, 3)
    if max_attempts < 1:
        raise ValueError("%s must be >= 1" % MAX_ATTEMPTS_ENV)
    temperature = _float(TEMPERATURE_ENV, None)   # type: ignore[arg-type]
    if temperature is not None and not 0.0 <= temperature <= 2.0:
        raise ValueError("%s must be within [0, 2]" % TEMPERATURE_ENV)
    max_tokens = _int(MAX_TOKENS_ENV, None)       # type: ignore[arg-type]
    if max_tokens is not None and max_tokens < 1:
        raise ValueError("%s must be >= 1" % MAX_TOKENS_ENV)
    return ModelCallConfig(
        model=_get(MODEL_ENV) or DEFAULT_MODEL,
        base_url=_get(BASE_URL_ENV) or DEFAULT_BASE_URL,
        api_key_env=_get(API_KEY_ENV_NAME_ENV) or DEFAULT_API_KEY_ENV,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        temperature=temperature,
        max_tokens=max_tokens,
    )
