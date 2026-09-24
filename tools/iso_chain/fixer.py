# -*- coding: utf-8 -*-
"""iso_chain.fixer — 隔离 fixer:真实模型调用与补丁解析。

提示词只包含公开仓库代码(目标模块+测试文件)、finding 结论、公开组织
标准摘录与测试期望;不包含凭证/私有语料/环境变量。
输出解析:模型回复中提取统一 diff(```diff 围栏或裸 --- a/ 块)。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

FIXER_PROMPT_TEMPLATE = """You are a SECURITY FIXER. Produce a minimal unified git diff that remediates the confirmed finding.

## Confirmed finding
{finding_block}

## Current code (file to fix)
Path: {target_path}
```python
{target_code}
```

## Existing test file (will run against your patched code; boundary tests included)
Path: {test_path}
```python
{test_code}
```

## Applicable org standard (excerpt, public)
{standard_excerpt}

## Requirements
- Containment: resolve the user-controlled path against the base directory
  (os.path.realpath + os.path.commonpath or equivalent), reject escape with
  HTTP 400 (do not echo the server path); missing file -> HTTP 404.
- Keep the route signature compatible with existing callers.
- Also fix the test file only if its own fixture/payload is buggy (known
  depth off-by-one) so the tests genuinely exercise containment.
- Output ONLY a unified git diff (paths a/<path> and b/<path>), no prose.
"""


def build_fixer_prompt(*, target_path: str, target_code: str, test_path: str,
                       test_code: str, finding_block: str,
                       standard_excerpt: str) -> str:
    return FIXER_PROMPT_TEMPLATE.format(
        finding_block=finding_block, target_path=target_path,
        target_code=target_code, test_path=test_path, test_code=test_code,
        standard_excerpt=standard_excerpt)


DIFF_FENCE = re.compile(r"```(?:diff)?\s*\n(.*?)```", re.S)


def extract_diff(model_text: str) -> Optional[str]:
    """从模型回复提取统一 diff;提取不到 → None(调用方按失败处理)。"""
    if not model_text:
        return None
    for m in DIFF_FENCE.finditer(model_text):
        body = m.group(1)
        if "--- a/" in body or "--- a\t" in body:
            return body.strip() + "\n"
    if "--- a/" in model_text:
        i = model_text.index("--- a/")
        j = model_text.rfind("```", i)
        body = model_text[i:j if j > i else len(model_text)]
        return body.strip() + "\n"
    return None


def call_fixer(model_client, budget, prompt: str, *,
               model: str = "deepseek-flash",
               max_tokens: int = 8000) -> Dict[str, Any]:
    """经预算守卫调用 fixer 模型。返回 {ok, diff, model_text, usage, rid}。"""
    rid = budget.reserve(est_input=len(prompt) // 3)
    resp = model_client.chat(model, [{"role": "user", "content": prompt}],
                             timeout_s=120, max_tokens=max_tokens,
                             temperature=0.2)
    if not resp.get("ok"):
        # 失败响应同样保守结算(usage 若缺失会触发 unreliable)
        budget.settle(rid, resp.get("usage"))
        return {"ok": False, "detail": "model call failed: %s" %
                (resp.get("kind") or resp.get("detail", ""))[:120],
                "rid": rid}
    budget.settle(rid, resp.get("usage") or {
        "prompt_tokens": resp.get("usage", {}).get("prompt_tokens"),
        "completion_tokens": resp.get("usage", {}).get("completion_tokens")})
    text = resp.get("content") or ""
    diff = extract_diff(text)
    return {"ok": diff is not None, "diff": diff, "model_text": text,
            "usage": resp.get("usage"), "rid": rid,
            "detail": None if diff else "no unified diff in response"}
