"""授权闸门:集成脚本的双重保险。

is_authorized():仅当环境变量 MERGEPILOT_IT_AUTH=1 时为真。
assert_authorized():未授权即抛 AuthorizationRequired(dry-run 文化的一部分)。
环境变量本身不等于授权——它只是用户书面批复后在 shell 里的机器可读形式;
脚本注释必须指向 AUTH-DECISION-PACKAGE.md 的对应条目编号。
"""
from __future__ import annotations

import os

AUTH_ENV = "MERGEPILOT_IT_AUTH"


class AuthorizationRequired(PermissionError):
    """未获用户批复(或未设置机器可读确认)时执行被拒绝。"""


def is_authorized(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(AUTH_ENV, "") == "1"


def assert_authorized(package_item: str, environ=None) -> None:
    """package_item 形如 "R1"/"R4"——对应 AUTH-DECISION-PACKAGE.md 条目。"""
    if not is_authorized(environ):
        raise AuthorizationRequired(
            "授权前禁止执行: %s 未获批或未设置 %s=1。"
            "批复记录见 docs/productization/AUTH-DECISION-PACKAGE.md" % (package_item, AUTH_ENV))
