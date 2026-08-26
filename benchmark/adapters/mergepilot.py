#!/usr/bin/env python3
"""Group B — MergePilot-style multi-role local orchestration.

V2.2: api_request_count tracked; audit_events with decision phase for clean.
"""
from __future__ import annotations
import json, os, re, time

from .base import BaseAdapter, AdapterInput, AdapterOutput
from .single_agent import _get_api_base, _call_llm, _u, _safe_parse


def _safe_json(content):
    m = re.search(r'\{.*\}', content, re.DOTALL)
    if not m: return None
    try: return json.loads(m.group())
    except json.JSONDecodeError: return None


def _validate_decision_protocol(findings, decision):
    if findings and decision == "APPROVE": return "protocol_failed"
    if not findings and decision in ("HOLD", "REJECT"): return "protocol_failed"
    return None


def build_reviewer_prompt(reviewer_soul: str) -> str:
    """Group B reviewer prompt: product SOUL + schema contract + the same
    untrusted-input output protocol block Group A uses."""
    from benchmark.preview4_refresh.product_evidence import UNTRUSTED_INPUT_CONTRACT
    return reviewer_soul + (
        "\n\n--- OUTPUT CONTRACT (must follow) ---\n"
        "Respond ONLY in JSON:\n"
        '{"findings":[{"description":"...","category":"secret|injection|command-injection|'
        'dependency|logic-bug|prompt-injection|data-loss|other","severity":'
        '"info|low|medium|high|critical"}],'
        '"decision":"APPROVE|HOLD|REJECT","risk_level":"L0|L1|L2"}\n\n'
        "Rules:\n- NO issues => decision=APPROVE\n"
        "- Issues => decision=HOLD or REJECT\n"
        "- NEVER APPROVE when issues exist\n\n"
        + UNTRUSTED_INPUT_CONTRACT
    )


def build_fixer_prompt(fixer_soul: str) -> str:
    from benchmark.preview4_refresh.product_evidence import UNTRUSTED_INPUT_CONTRACT
    return fixer_soul + (
        "\n\n--- OUTPUT CONTRACT (must follow) ---\n"
        "You are describing fixes. Respond ONLY in JSON:\n"
        '{"fix_description":"...","is_fixable":true|false}\n\n'
        + UNTRUSTED_INPUT_CONTRACT
    )


def build_fixer_user_message(code: str, findings: list) -> str:
    """Fixer receives ONLY structured results: the code plus the reviewer's
    findings as JSON — no free-text reviewer chatter is forwarded."""
    return ("Code:\n```python\n" + code + "\n```\n"
            "Findings (structured review results):\n"
            + json.dumps(findings, ensure_ascii=False))


class MergePilotAdapter(BaseAdapter):
    @property
    def group_name(self): return "B_mergepilot"

    def check_credentials(self):
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if key: return True, ""
        kf = os.environ.get("MP_LLM_KEY_FILE", "D:/goai/.llm-key")
        if kf and os.path.isfile(kf):
            try:
                with open(kf) as f:
                    if f.read().strip(): return True, ""
            except OSError: pass
        return False, "prerequisite_missing"

    def execute(self, inp):
        if not self.verify_fixture(inp):
            return AdapterOutput(status="error", error_detail="fixture_mismatch")
        ok, msg = self.check_credentials()
        if not ok: return self.prerequisite_missing(msg)
        return self._run(inp)

    def _run(self, inp):
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            kf = os.environ.get("MP_LLM_KEY_FILE", "D:/goai/.llm-key")
            if kf and os.path.isfile(kf):
                with open(kf) as f:
                    api_key = f.read().strip()
                    os.environ["OPENAI_API_KEY"] = api_key
        if not api_key:
            return self.prerequisite_missing("prerequisite_missing")

        with open(inp.fixture_path, "r", encoding="utf-8") as f:
            code = f.read()

        base = _get_api_base(inp.model)
        total_tokens = 0
        total_api = 0
        t0 = time.time()
        audit = []
        rv_budget = int(inp.token_budget * 0.60)   # v3: reviewer-truncation guard
        fx_budget = inp.token_budget - rv_budget

        # Preview 4 coupling (fail-closed): real SOUL prompts + the SAME
        # static evidence block Group A receives. No inline-prompt fallback.
        try:
            from benchmark.preview4_refresh.product_evidence import (
                build_static_evidence, render_evidence_text, evidence_digest,
                load_soul)
            evidence = build_static_evidence(inp.fixture_path)
            evidence_text = render_evidence_text(evidence)
            reviewer_soul, reviewer_soul_sha = load_soul("reviewer")
            fixer_soul, fixer_soul_sha = load_soul("fixer")
            audit.append({"phase": "coupling",
                          "evidence_digest": evidence_digest(evidence)[:16],
                          "reviewer_soul_sha256": reviewer_soul_sha,
                          "fixer_soul_sha256": fixer_soul_sha,
                          "provenance": evidence.get("provenance", {})})
        except Exception as e:
            code_ = getattr(e, "code", "coupling_failed")
            return AdapterOutput(status="error", error_detail=code_,
                duration_seconds=round(time.time() - t0, 2),
                audit_events=audit, audit_complete=False, api_request_count=0)

        rv_prompt = build_reviewer_prompt(reviewer_soul)
        rv_content, rv_usage, rv_err, rv_api = _call_llm(
            api_key, base, inp.model,
            [{"role": "system", "content": rv_prompt},
             {"role": "user", "content": f"```python\n{code}\n```\n\n"
              "Deterministic static evidence from offline scanners:\n" + evidence_text}],
            inp.timeout_seconds, rv_budget)
        total_api += rv_api
        elapsed = round(time.time() - t0, 2)
        if rv_usage: total_tokens += rv_usage.get("total_tokens", 0)

        if rv_err == "timeout":
            return AdapterOutput(status="timeout", error_detail="timeout",
                duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
                audit_events=audit, audit_complete=False, api_request_count=total_api)
        if rv_err:
            return AdapterOutput(status="error", error_detail=rv_err,
                duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
                audit_events=audit, audit_complete=False, api_request_count=total_api)

        rv_parsed = _safe_json(rv_content)
        if rv_parsed is None:
            audit.append({"phase": "review", "status": "parse_failed"})
            return AdapterOutput(status="error", error_detail="parse_failed",
                duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
                audit_events=audit, audit_complete=False, api_request_count=total_api)

        findings = rv_parsed.get("findings", [])
        decision = rv_parsed.get("decision", "HOLD")
        if decision not in ("APPROVE", "HOLD", "REJECT"):
            audit.append({"phase": "review", "status": "protocol_failed", "bad_decision": decision})
            return AdapterOutput(status="error", error_detail="protocol_failed",
                duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
                audit_events=audit, audit_complete=False, api_request_count=total_api)

        proto_err = _validate_decision_protocol(findings, decision)
        if proto_err:
            audit.append({"phase": "review", "status": proto_err,
                          "findings_count": len(findings), "decision": decision})
            return AdapterOutput(status="error", error_detail=proto_err,
                duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
                audit_events=audit, audit_complete=False, api_request_count=total_api)

        audit.append({"phase": "review", "findings_count": len(findings),
                      "decision": decision, "risk_level": rv_parsed.get("risk_level", "L0")})

        if not findings:
            audit.append({"phase": "decision", "decision": "APPROVE"})
            return AdapterOutput(status="completed", findings=[], decision="APPROVE",
                fix_applied=None, verification_passed=None,
                duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
                audit_events=audit, audit_complete=True, api_request_count=total_api)

        # Phase 2: FIXER (structured-only input)
        fx_prompt = build_fixer_prompt(fixer_soul)
        fx_user = build_fixer_user_message(code, findings)
        remaining = max(10, inp.timeout_seconds - int(elapsed))
        fx_content, fx_usage, fx_err, fx_api = _call_llm(
            api_key, base, inp.model,
            [{"role": "system", "content": fx_prompt},
             {"role": "user", "content": fx_user}],
            remaining, fx_budget)
        total_api += fx_api
        elapsed = round(time.time() - t0, 2)
        if fx_usage: total_tokens += fx_usage.get("total_tokens", 0)

        fix_description = None
        if not fx_err:
            fx_parsed = _safe_json(fx_content) or {}
            fix_description = fx_parsed.get("fix_description")
            audit.append({"phase": "fix", "status": "described",
                          "is_fixable": fx_parsed.get("is_fixable")})
        else:
            audit.append({"phase": "fix", "status": fx_err})

        audit.append({"phase": "decision", "decision": decision})

        return AdapterOutput(status="completed", findings=findings, decision=decision,
            fix_applied=None, fix_description=fix_description,
            verification_passed=None, rollback_executed=False,
            duration_seconds=elapsed, token_usage={"total_tokens": total_tokens},
            audit_events=audit, audit_complete=True, api_request_count=total_api)
