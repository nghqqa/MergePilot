"""证据采集与脱敏(本地;真实案例轮的证据打包复用此模块)。

redact():脱敏规则集中于此——GitHub token / Bearer / 密码 / DSN 凭证段 /
API key / OTel key 一律替换;采集的台账/日志/JSON 先过 redact 再落盘。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_PATTERNS = [
    (re.compile(r"(ghp_|gho_|github_pat_)[A-Za-z0-9_]{10,}"), "<redacted:gh-token>"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)Bearer\s+\S+"), r"\1<redacted>"),
    # 键名容忍 JSON 引号:"password": "hunter2"
    (re.compile(r"(?i)[\"']?(password|passwd|pwd)[\"']?(\s*[:=]\s*)[\"']?\S+"),
     r"\1\2<redacted>"),
    (re.compile(r"://[^:/\s]+:[^@/\s]+@"), "://<redacted>@"),  # DSN 凭证段
    (re.compile(r"(?i)[\"']?(api[_-]?key|secret|token)[\"']?(\s*[:=]\s*)[\"']?[A-Za-z0-9._-]{8,}"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)(x-arms-license-key['\"]?\s*[:=]\s*['\"]?)\S+"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    """文本脱敏:按集中规则替换凭证形态的片段。"""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_obj(obj: Any) -> Any:
    """递归脱敏 dict/list 的字符串值(键名也过一遍规则)。"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    return obj


def collect_evidence(run_store=None, hook_errors_limit: int = 20,
                     ledger_rows: Optional[List[Dict]] = None,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """汇总一轮案例的证据包(全部本地来源;外部行由调用方以参数注入)。
    输出前统一过 redact_obj。"""
    bundle: Dict[str, Any] = {
        "v3_runs": [],
        "hook_errors": [],
        "ledger_rows": ledger_rows or [],
        "extra": extra or {},
    }
    if run_store is not None:
        bundle["v3_runs"] = run_store.list_runs(limit=100)
        bundle["hook_errors"] = run_store.list_hook_errors(limit=hook_errors_limit)
    return redact_obj(bundle)


if __name__ == "__main__":
    demo = {"dsn": "postgresql://u:secret@h/db", "note": "password=abc123",
            "auth": "Authorization: Bearer ghp_abcdefghijklmnop", "safe": "ok"}
    print(json.dumps(redact_obj(demo), ensure_ascii=False, indent=1))
