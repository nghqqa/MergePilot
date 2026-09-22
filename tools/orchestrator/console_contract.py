"""控制台字段契约(ARCHITECTURE-V3 §7):部分完成/降级必须可见,禁止单一绿色。"""
from __future__ import annotations

from typing import Dict, Optional


def build_console_payload(run_id: str, risk: Dict, stages_dict: Dict,
                          outcome: Dict, aggregates: Optional[Dict],
                          finding_validation: Optional[Dict],
                          fixer: Optional[Dict], patch_validation: Optional[Dict],
                          github_writeback: Optional[Dict],
                          rag: Optional[Dict], manifest: Optional[Dict]) -> Dict:
    """固定字段契约。coverage.complete=False 时顶层可见(不压缩成一个状态)。"""
    reviewers = [{"name": d.split("review:", 1)[-1], "status": r["status"],
                  "attempts": r["attempts"], "error": r["error"]}
                 for d, r in sorted(stages_dict.items()) if d.startswith("review:")]
    by_source: Dict[str, int] = {}
    if aggregates:
        for f in aggregates.get("findings", []):
            for s in f.get("sources", []):
                by_source[s["reviewer"]] = by_source.get(s["reviewer"], 0) + 1
    payload = {
        "run_id": run_id,
        "risk_level": risk.get("level"),
        "human_review_required": risk.get("human_review_required", False),
        "reviewers": reviewers,
        "findings": {"total": (aggregates or {}).get("total", 0),
                     "by_source": by_source,
                     "dropped_duplicates": (aggregates or {}).get("dropped_duplicates", 0)},
        "finding_validation": finding_validation or {"status": "NOT_APPLICABLE"},
        "outcome": outcome["outcome"],
        "review_complete": outcome["review_complete"],
        "coverage": {"complete": outcome["review_complete"],
                     "missing": outcome["coverage_missing"],
                     "critical_failures": outcome["critical_failures"],
                     "note": outcome["note"]},
        "degradations": [{"stage": d, "reason": (stages_dict.get(d) or {}).get("error", ""),
                          "status": (stages_dict.get(d) or {}).get("status")}
                         for d in outcome["coverage_missing"]],
        "fixer": fixer or {"status": "NOT_APPLICABLE"},
        "patch_validation": patch_validation or {"status": "NOT_APPLICABLE"},
        "github_writeback": github_writeback or {"status": "NOT_APPLICABLE"},
        "rag": rag or {"snapshot_id": None, "evidence_refs": []},
        "manifest": manifest or {},
    }
    return payload
