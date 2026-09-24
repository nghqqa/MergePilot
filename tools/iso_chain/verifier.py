# -*- coding: utf-8 -*-
"""iso_chain.verifier — 独立 verifier(CL-05)。

独立性边界(结构强制):
  * build_verifier_input 只接收 目标代码/finding/补丁/真实采集的测试结果——
    **不接受 fixer 的推理、自述或成功声明**(fixer_reasoning 参数不存在);
  * verifier 使用独立工作区(workspace 参数)与独立模型会话;
  * 判定 = 模型意见(证据之一) + 真实采集的测试结果(决定性);
    测试结果由沙箱采集器传入,不由 fixer 提供。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

VERIFIER_PROMPT_TEMPLATE = """You are an INDEPENDENT VERIFIER for a security fix.
You did not write the patch and you have no access to the fixer's reasoning.

## Finding (confirmed by an independent reviewer)
{finding_block}

## Target code AFTER applying the patch
```python
{patched_code}
```

## Patch (unified diff)
```diff
{patch_diff}
```

## Collected test results (executed by the test harness, not by the fixer)
{test_results_block}

## Your job
1. Independently assess whether the patch fully remediates the finding
   (path containment per the org standard: resolved path must stay inside
   the base directory; reject with 400, missing -> 404).
2. Check boundary handling: symlink escape, absolute path, encoding,
   traversal depth, url-encoded traversal if applicable.
3. Judge whether the collected test results are sufficient and passing.

Reply with EXACTLY this format:
VERDICT: VERIFIED | REJECTED
REASON: <one paragraph>
GAPS: <comma-separated gap names, or "none">
"""


def build_verifier_input(*, finding: Dict[str, Any], patched_code: str,
                         patch_diff: str, test_results: Dict[str, Any],
                         repo: str, pr: int, head_sha: str) -> Dict[str, Any]:
    """组装 verifier 输入。参数表刻意不含 fixer_reasoning。"""
    finding_block = "\n".join(
        "- %s: %s" % (k, v) for k, v in finding.items())
    test_lines = []
    for t in test_results.get("cases", []):
        test_lines.append("%s -> %s" % (t.get("name"), t.get("result")))
    test_block = "\n".join(test_lines) or "(no cases)"
    if test_results.get("all_passed") is not True:
        test_block += "\nNOTE: not all collected tests passed."
    prompt = VERIFIER_PROMPT_TEMPLATE.format(
        finding_block=finding_block, patched_code=patched_code,
        patch_diff=patch_diff, test_results_block=test_block)
    return {"prompt": prompt,
            "bindings": {"repo": repo, "pr": pr, "head_sha": head_sha},
            "test_results": test_results}


def parse_verdict(model_text: str) -> Dict[str, Any]:
    """解析 verifier 输出。判定=模型 VERDICT **且** 测试全过,两者缺一不可。"""
    verdict, reason, gaps = None, "", ""
    for line in (model_text or "").splitlines():
        s = line.strip()
        if s.startswith("VERDICT:") and verdict is None:
            v = s.split(":", 1)[1].strip().upper()
            verdict = v if v in ("VERIFIED", "REJECTED") else None
        elif s.startswith("REASON:"):
            reason = s.split(":", 1)[1].strip()
        elif s.startswith("GAPS:"):
            gaps = s.split(":", 1)[1].strip()
    return {"verdict": verdict, "reason": reason, "gaps": gaps}


def finalize(model_verdict: Dict[str, Any], test_results: Dict[str, Any],
             apply_check: Dict[str, Any]) -> Dict[str, Any]:
    """最终判定:模型意见仅是证据之一;真实采集结果决定性。"""
    if not apply_check.get("ok"):
        return {"final": "APPLY_FAILED", "detail": apply_check.get("detail", "")}
    tests_ok = test_results.get("all_passed") is True
    model_ok = model_verdict.get("verdict") == "VERIFIED"
    if tests_ok and model_ok:
        final = "VERIFIED"
    elif tests_ok and not model_ok:
        final = "VERIFIED_WITH_MODEL_OBJECTION"   # 测试决定性,但模型异议留痕
    else:
        final = "NOT_VERIFIED"
    return {"final": final, "model_verdict": model_verdict.get("verdict"),
            "model_reason": model_verdict.get("reason"),
            "model_gaps": model_verdict.get("gaps"),
            "tests_all_passed": tests_ok}
