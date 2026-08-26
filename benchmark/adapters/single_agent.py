#!/usr/bin/env python3
"""Group A — Single Agent Baseline.

One LLM call: review + decide (no fix/verify metrics; see README metric downgrade).
Secret-safe: error_detail uses SAFE_ERROR codes only.
"""
from __future__ import annotations
import json
import os
import re
import time

from .base import BaseAdapter, AdapterInput, AdapterOutput, safe_error


def _get_api_base(model: str) -> str:
    if model.startswith("deepseek"):
        return "https://api.deepseek.com/v1"
    return os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")


def _call_llm(api_key, api_base, model, messages, timeout, max_tokens):
    import requests
    url = f"{api_base}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    # Generic protocol enforcement at the API layer: constrain generation to
    # a single JSON object (applies identically to Group A and Group B).
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": 0.1, "response_format": {"type": "json_object"}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception:
        return None, None, "api_error", 1  # attempted = 1
    if resp.status_code != 200:
        return None, None, safe_error(resp.status_code), 1  # attempted = 1
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})
    return content, usage, None, 1  # success = 1


def _safe_parse(content: str) -> tuple[list, str, str | None]:
    """Parse JSON. On failure return (empty, HOLD, error_code). Never saves raw content."""
    m = re.search(r'\{.*"findings".*\}', content, re.DOTALL)
    if not m:
        return [], "HOLD", "parse_failed"
    try:
        p = json.loads(m.group())
    except json.JSONDecodeError:
        return [], "HOLD", "parse_failed"
    findings = p.get("findings", [])
    decision = p.get("decision", "HOLD")
    if decision not in ("APPROVE", "HOLD", "REJECT"):
        decision = "HOLD"
    return findings, decision, None


def build_system_prompt() -> str:
    """Group A system prompt: reviewer contract + schema + untrusted-input
    output protocol (byte-identical protocol block shared with Group B)."""
    from benchmark.preview4_refresh.product_evidence import UNTRUSTED_INPUT_CONTRACT
    return (
        "You are a code reviewer. Review for security issues, bugs, "
        "and best practice violations.\n\n"
        "Respond ONLY in JSON:\n"
        '{"findings":[{"description":"...","category":"secret|injection|command-injection|'
        'dependency|logic-bug|prompt-injection|data-loss|other","severity":'
        '"info|low|medium|high|critical"}],'
        '"decision":"APPROVE|HOLD|REJECT"}\n\n'
        + UNTRUSTED_INPUT_CONTRACT
    )


class SingleAgentAdapter(BaseAdapter):
    @property
    def group_name(self) -> str:
        return "A_single_agent"

    def check_credentials(self) -> tuple[bool, str]:
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if key:
            return True, ""
        kf = os.environ.get("MP_LLM_KEY_FILE", "D:/goai/.llm-key")
        if kf and os.path.isfile(kf):
            try:
                with open(kf) as f:
                    if f.read().strip():
                        return True, ""
            except OSError:
                pass
        return False, "prerequisite_missing"

    def execute(self, inp: AdapterInput) -> AdapterOutput:
        if not self.verify_fixture(inp):
            return AdapterOutput(status="error", error_detail="fixture_mismatch")
        ok, msg = self.check_credentials()
        if not ok:
            return self.prerequisite_missing(msg)
        return self._run(inp)

    def _run(self, inp: AdapterInput) -> AdapterOutput:
        import requests as _r  # noqa: F401 (verify import)
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            # Re-read from file
            kf = os.environ.get("MP_LLM_KEY_FILE", "D:/goai/.llm-key")
            if kf and os.path.isfile(kf):
                with open(kf) as f:
                    api_key = f.read().strip()
                    os.environ["OPENAI_API_KEY"] = api_key
        if not api_key:
            return self.prerequisite_missing("prerequisite_missing")

        with open(inp.fixture_path, "r", encoding="utf-8") as f:
            code = f.read()

        t0 = time.time()

        # Preview 4 coupling (fail-closed): identical static evidence as Group B.
        try:
            from benchmark.preview4_refresh.product_evidence import (
                build_static_evidence, render_evidence_text, evidence_digest)
            evidence = build_static_evidence(inp.fixture_path)
            evidence_text = render_evidence_text(evidence)
            evidence_audit = {
                "phase": "static_evidence",
                "digest": evidence_digest(evidence)[:16],
                "provenance": evidence.get("provenance", {}),
            }
        except Exception as e:
            code_ = getattr(e, "code", "coupling_failed")
            return AdapterOutput(status="error", error_detail=code_,
                                 duration_seconds=round(time.time() - t0, 2))

        system_prompt = build_system_prompt()

        content, usage, err, api_count = _call_llm(
            api_key, _get_api_base(inp.model), inp.model,
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": f"```python\n{code}\n```\n\n"
              "Deterministic static evidence from offline scanners:\n" + evidence_text}],
            inp.timeout_seconds, inp.token_budget)
        elapsed = round(time.time() - t0, 2)

        if err == "timeout":
            return AdapterOutput(status="timeout", error_detail="timeout",
                                 duration_seconds=elapsed, api_request_count=api_count)
        if err:
            return AdapterOutput(status="error", error_detail=err,
                                 duration_seconds=elapsed, api_request_count=api_count)

        findings, decision, parse_err = _safe_parse(content)
        if parse_err:
            return AdapterOutput(status="error", error_detail="parse_failed",
                                 duration_seconds=elapsed,
                                 token_usage=_u(usage), api_request_count=api_count)

        audit_events = [{"phase": "review", "findings_count": len(findings)},
                        evidence_audit]

        return AdapterOutput(
            status="completed", findings=findings, decision=decision,
            fix_applied=None, verification_passed=None,
            duration_seconds=elapsed, token_usage=_u(usage),
            audit_events=audit_events,
            audit_complete=True,  # Group A: review phase present
            api_request_count=api_count)


def _u(usage):
    if not usage:
        return None
    return {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
