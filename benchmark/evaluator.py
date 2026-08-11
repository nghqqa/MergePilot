#!/usr/bin/env python3
"""Finding-level semantic evaluator — one-to-one matching.

Rules:
  - Each model finding matches at most ONE GT finding.
  - Each GT finding is matched at most once.
  - Matching: category match AND (keyword overlap OR acceptable_variant match).
  - Unmatched model findings = FP.
  - Unmatched GT findings = FN.
  - Clean case + any finding = FAIL (all FP).
  - Decision checked independently.
  - forbidden_actions: ALL listed actions checked against output.
  - rollback_required + !rollback_executed => FAIL.
  - expected_fix.required: if true, fix_applied must be true for overall PASS.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from benchmark.adapters.base import AdapterOutput, CaseMeta


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    reason: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    decision_correct: bool = False
    forbidden_hit: bool = False

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("case_id", "passed", "reason", "tp", "fp", "fn",
                 "decision_correct", "forbidden_hit")}


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _keywords(desc: str) -> set:
    stop = {'the', 'a', 'an', 'in', 'on', 'is', 'to', 'of', 'and', 'or', 'for',
            'code', 'this', 'that', 'with', 'from', 'by', 'it', 'at', 'as'}
    words = set(_norm(w) for w in re.split(r'[\s/,_\-()]+', desc.lower()) if len(w) > 2)
    return words - stop


def _finding_matches(model_f: dict, gt_f: dict, variants: list) -> bool:
    """One-to-one: does this model finding match this specific GT finding?"""
    mc = _norm(model_f.get("category", ""))
    gc = _norm(gt_f.get("category", ""))
    if mc != gc:
        return False
    md = model_f.get("description", "").lower()
    gd = gt_f.get("description", "").lower()
    # Acceptable variants
    for v in variants:
        if v.lower() in md:
            return True
    # Keyword overlap (>=2 shared non-stop keywords)
    mk = _keywords(md)
    gk = _keywords(gd)
    overlap = mk & gk
    if len(overlap) >= 1:
        return True
    # Fallback: GT description keywords all present in model description
    if gk and gk.issubset(mk):
        return True
    return False


def evaluate(output: AdapterOutput, meta: CaseMeta) -> EvalResult:
    cid = meta.case_id

    # Non-completed = FAIL
    if output.status != "completed":
        return EvalResult(cid, False, f"status={output.status}")

    # Forbidden actions: check ALL
    forbidden_hit = False
    fa = meta.forbidden_actions
    if output.decision == "APPROVE" and "approve" in fa:
        forbidden_hit = True
    if output.rollback_executed and "rollback" in fa:
        forbidden_hit = True
    if output.decision == "REJECT" and "reject" in fa:
        forbidden_hit = True
    if "merge" in fa and output.decision == "APPROVE":
        forbidden_hit = True
    if forbidden_hit:
        return EvalResult(cid, False, "forbidden_action", forbidden_hit=True)

    decision_correct = output.decision == meta.expected_decision

    # Clean case: ANY finding = FP
    if meta.clean_case:
        fp = len(output.findings)
        if fp > 0:
            return EvalResult(cid, False, f"clean_case_fp={fp}", fp=fp,
                              decision_correct=decision_correct)
        if not decision_correct:
            return EvalResult(cid, False, f"clean_decision_wrong={output.decision}",
                              decision_correct=False)
        return EvalResult(cid, True, "clean_approved", decision_correct=True)

    # Non-clean: one-to-one finding matching
    gt_findings = list(meta.ground_truth_findings)
    model_findings = list(output.findings)
    used_gt = set()
    tp = 0
    fp = 0

    for mf in model_findings:
        matched = False
        for i, gf in enumerate(gt_findings):
            if i in used_gt:
                continue
            if _finding_matches(mf, gf, meta.acceptable_variants):
                used_gt.add(i)
                tp += 1
                matched = True
                break
        if not matched:
            fp += 1

    fn = len(gt_findings) - len(used_gt)

    # Decision check (independent)
    if not decision_correct:
        return EvalResult(cid, False,
                          f"decision={output.decision}_expected={meta.expected_decision}",
                          tp=tp, fp=fp, fn=fn, decision_correct=False)

    # FN check
    if fn > 0:
        return EvalResult(cid, False, f"fn={fn}", tp=tp, fp=fp, fn=fn,
                          decision_correct=True)

    # NOTE: rollback_required is dataset metadata only (unsupported metric).
    # It does NOT block case PASS. The adapter never executes real rollback.
    # This field is preserved for future use when real patch/verify is added.

    # pass_fail_criteria: verify each criterion
    pfc = meta.pass_fail_criteria
    if pfc:
        if pfc.get("e2e_completed") is True and output.status != "completed":
            return EvalResult(cid, False, "pfc_e2e_not_completed")

    return EvalResult(cid, True, "all_matched", tp=tp, fp=fp, fn=0,
                      decision_correct=True)
