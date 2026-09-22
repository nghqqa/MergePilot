"""发现聚合(ARCHITECTURE-V3 §5):确定性去重、排序、保留来源。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


@dataclass(frozen=True)
class Finding:
    finding_id: str
    source_reviewer: str
    category: str            # e.g. path-traversal / command-injection
    severity: str
    title: str
    path: str
    line: int = 0
    evidence: str = ""

    def dedupe_key(self) -> Tuple[str, str]:
        return (self.path, self.category)

    def title_tokens(self) -> frozenset:
        import re
        return frozenset(w for w in re.split(r"[\s_\-/,.;:()]+", self.title.lower()) if w)


@dataclass
class AggregatedFinding:
    key: Tuple[str, str]
    category: str
    severity: str
    title: str
    path: str
    line: int
    evidence: str = ""
    sources: List[Dict] = field(default_factory=list)   # 来源保留:[{reviewer, finding_id, severity}]

    def to_dict(self) -> Dict:
        return {"key": list(self.key), "category": self.category,
                "severity": self.severity, "title": self.title,
                "path": self.path, "line": self.line,
                "sources": self.sources, "source_count": len(self.sources)}


@dataclass
class AggregatedFindings:
    findings: List[AggregatedFinding] = field(default_factory=list)
    dropped_duplicates: int = 0

    def to_dict(self) -> Dict:
        return {"findings": [f.to_dict() for f in self.findings],
                "dropped_duplicates": self.dropped_duplicates,
                "total": len(self.findings)}


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def aggregate_findings(findings: List[Finding],
                       near_dup_threshold: float = 0.5) -> AggregatedFindings:
    """两级去重:精确 (path,category) 合并;近似同键 + 标题 Jaccard ≥ 阈值合并。
    severity 取最大;全部来源保留(sources)。"""
    buckets: Dict[Tuple[str, str], List[Finding]] = {}
    for f in findings:
        buckets.setdefault(f.dedupe_key(), []).append(f)

    out = AggregatedFindings()
    for key, group in buckets.items():
        group = sorted(group, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
        clusters: List[List[Finding]] = []
        for f in group:
            placed = False
            for cl in clusters:
                if _jaccard(f.title_tokens(), cl[0].title_tokens()) >= near_dup_threshold:
                    cl.append(f)
                    placed = True
                    break
            if not placed:
                clusters.append([f])
        for cl in clusters:
            head = cl[0]
            out.findings.append(AggregatedFinding(
                key=key, category=head.category,
                severity=head.severity, title=head.title,
                path=head.path, line=head.line, evidence=head.evidence,
                sources=[{"reviewer": f.source_reviewer, "finding_id": f.finding_id,
                          "severity": f.severity} for f in cl]))
            out.dropped_duplicates += len(cl) - 1

    out.findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                     -len(f.sources), f.path))
    return out
