"""feature flag:架构 v3 流水线开关。默认关闭=旧串行链(bridge)照旧。

开启也不自动接线:v3 组件只在显式调用处生效;bridge 现网路径不含本包。
"""
from __future__ import annotations

import os

FLAG_ENV = "MERGEPILOT_REVIEW_V3"


def review_v3_enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(FLAG_ENV, "") == "1"
