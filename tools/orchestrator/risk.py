"""风险分级(纯逻辑,可解释,无 LLM 调度 Agent)。

规则(ARCHITECTURE-V3 §2):敏感路径命中 → 无条件 FULL(小 diff 不豁免);
行数=additions+deletions 合计;阈值与敏感路径集可配置;
输出带 reasons 逐条解释,随 run-manifest 落盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

RISK_TRIVIAL = "TRIVIAL"
RISK_LITE = "LITE"
RISK_FULL = "FULL"

DEFAULT_SENSITIVE_PATTERNS = ("auth", "crypto", "permission",
                              "credential", "migration")


@dataclass(frozen=True)
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class RiskRules:
    """可配置阈值(记录进 run-manifest.config)。"""
    trivial_max_lines: int = 50
    trivial_max_files: int = 5
    lite_max_lines: int = 500
    lite_max_files: int = 20
    sensitive_patterns: tuple = DEFAULT_SENSITIVE_PATTERNS
    # FULL 时启用的专业审查器(可按部署配置增减)
    specialist_reviewers: tuple = ("security",)
    generic_reviewer: str = "generic"

    def to_dict(self) -> Dict:
        return {"trivial_max_lines": self.trivial_max_lines,
                "trivial_max_files": self.trivial_max_files,
                "lite_max_lines": self.lite_max_lines,
                "lite_max_files": self.lite_max_files,
                "sensitive_patterns": list(self.sensitive_patterns),
                "specialist_reviewers": list(self.specialist_reviewers),
                "generic_reviewer": self.generic_reviewer}


@dataclass(frozen=True)
class RiskGrade:
    level: str
    reviewers: tuple
    human_review_required: bool
    reasons: tuple
    sensitive_hits: tuple
    lines_changed: int
    files_changed: int

    def to_dict(self) -> Dict:
        return {"level": self.level, "reviewers": list(self.reviewers),
                "human_review_required": self.human_review_required,
                "reasons": list(self.reasons),
                "sensitive_hits": list(self.sensitive_hits),
                "lines_changed": self.lines_changed,
                "files_changed": self.files_changed}


def _sensitive_hits(files: List[FileChange], patterns: tuple) -> List[str]:
    hits = []
    for f in files:
        low = f.path.lower()
        for pat in patterns:
            if pat in low:
                hits.append(f.path)
                break
    return hits


def grade_risk(files: List[FileChange], rules: RiskRules = RiskRules()) -> RiskGrade:
    """三档路由。敏感路径优先于规模;理由逐条记录(可解释)。"""
    lines = sum(f.additions + f.deletions for f in files)
    nfiles = len(files)
    hits = _sensitive_hits(files, rules.sensitive_patterns)
    reasons = []

    if hits:
        reasons.append("敏感路径命中: %s" % ", ".join(hits[:5]))
    if lines > rules.lite_max_lines:
        reasons.append("变更 %d 行 > %d" % (lines, rules.lite_max_lines))
    if nfiles > rules.lite_max_files:
        reasons.append("变更 %d 文件 > %d" % (nfiles, rules.lite_max_files))

    if hits or lines > rules.lite_max_lines or nfiles > rules.lite_max_files:
        reviewers = [rules.generic_reviewer] + list(rules.specialist_reviewers)
        return RiskGrade(RISK_FULL, tuple(reviewers), True,
                         tuple(reasons or ["达到 FULL 阈值"]),
                         tuple(hits), lines, nfiles)

    if lines >= rules.trivial_max_lines or nfiles >= rules.trivial_max_files:
        reasons.append("规模进入 LITE(%d 行 / %d 文件)" % (lines, nfiles))
        specialist = rules.specialist_reviewers[0] if rules.specialist_reviewers else "security"
        return RiskGrade(RISK_LITE,
                         (rules.generic_reviewer, specialist), False,
                         tuple(reasons), tuple(hits), lines, nfiles)

    reasons.append("小变更(%d 行 / %d 文件)且无敏感路径" % (lines, nfiles))
    return RiskGrade(RISK_TRIVIAL, (rules.generic_reviewer,), False,
                     tuple(reasons), tuple(hits), lines, nfiles)
