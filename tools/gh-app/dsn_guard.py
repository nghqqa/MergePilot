#!/usr/bin/env python3
"""dsn_guard.py — PostgreSQL DSN connect_timeout 合同(M8-GH-2 §4)。

隔离 staging 实证(暂停 PG 时无 connect_timeout 会无限悬挂):gh-webhook 的
ingress DSN 必须携带受控 connect_timeout。本模块用 psycopg2 的**结构化**
parse_dsn/make_dsn 完成校验与构造——绝不做字符串拼接改写。

合同:
  * 缺少 connect_timeout → 结构化补默认值(DEFAULT_CONNECT_TIMEOUT=5s);
  * 已有 connect_timeout 必须为整数且在 [LOW, HIGH](1..30),否则
    DsnConfigError(fail-closed,启动即拒,不静默修正);
  * URI 形式(postgresql://...)与 keyword 形式(host=... password=...)均可;
    password 特殊字符经 parse/make 往返保真;
  * 错误只含稳定 code 与字段名——**永不回显 DSN 或密码**。
"""

from __future__ import annotations

DEFAULT_CONNECT_TIMEOUT = 5
CONNECT_TIMEOUT_LOW = 1
CONNECT_TIMEOUT_HIGH = 30


class DsnConfigError(Exception):
    """稳定码配置错误;message 不含 DSN/密码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(code + ((" (%s)" % detail) if detail else ""))


def ensure_connect_timeout(dsn: str, *, default: int = DEFAULT_CONNECT_TIMEOUT,
                           low: int = CONNECT_TIMEOUT_LOW,
                           high: int = CONNECT_TIMEOUT_HIGH) -> str:
    """校验并返回带受控 connect_timeout 的新 DSN(结构化,非拼接)。

    非 str / 解析失败 → DsnConfigError(DSN_INVALID);
    connect_timeout 非整数/越界 → DsnConfigError(CONNECT_TIMEOUT_INVALID);
    缺失 → 补 default(仍须在 [low, high] 内,否则编程错误同样拒绝)。
    """
    if not isinstance(dsn, str) or not dsn.strip():
        raise DsnConfigError("DSN_INVALID", "dsn must be a non-empty string")
    if not (low <= int(default) <= high):
        raise DsnConfigError("CONNECT_TIMEOUT_INVALID",
                             "default %r outside %d..%d" % (default, low,
                                                            high))
    import psycopg2.extensions as _ext
    try:
        params = _ext.parse_dsn(dsn)
    except Exception as exc:
        raise DsnConfigError(
            "DSN_INVALID", "parse failed: %s" % type(exc).__name__) from None
    raw = params.get("connect_timeout")
    if raw is None:
        params["connect_timeout"] = int(default)
    else:
        try:
            seconds = int(str(raw))
        except (TypeError, ValueError):
            raise DsnConfigError(
                "CONNECT_TIMEOUT_INVALID",
                "value is not an integer") from None
        if not (low <= seconds <= high):
            raise DsnConfigError(
                "CONNECT_TIMEOUT_INVALID",
                "value %d outside %d..%d" % (seconds, low, high))
        params["connect_timeout"] = seconds
    try:
        return _ext.make_dsn(**params)
    except Exception as exc:
        raise DsnConfigError(
            "DSN_INVALID", "rebuild failed: %s" % type(exc).__name__) from None


def validate_connect_timeout_present(dsn: str, **kwargs) -> None:
    """启动期 fail-closed 校验(非法值立即抛,缺失由 ensure 补默认)。"""
    ensure_connect_timeout(dsn, **kwargs)


__all__ = [
    "CONNECT_TIMEOUT_HIGH", "CONNECT_TIMEOUT_LOW",
    "DEFAULT_CONNECT_TIMEOUT", "DsnConfigError", "ensure_connect_timeout",
    "validate_connect_timeout_present",
]
