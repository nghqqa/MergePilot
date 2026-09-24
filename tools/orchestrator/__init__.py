"""MergePilot 架构 v3:分级并行审查流水线(骨架,feature flag 默认关闭)。

设计:docs/productization/ARCHITECTURE-V3.md。
模块:risk(风险分级)/ stages(阶段状态与 outcome)/ scheduler(调度)/
aggregate(发现聚合)/ verify_finding(问题验证接口)/ console_contract(控制台契约)/
flag(开关,默认 False=旧串行链)。
"""
from .risk import (  # noqa: F401
    RISK_TRIVIAL, RISK_LITE, RISK_FULL,
    RiskRules, RiskGrade, grade_risk,
)
from .stages import (  # noqa: F401
    StageStatus, Outcome, StageRecord, RunStages, derive_outcome,
)
from .scheduler import (  # noqa: F401
    OrchestratorConfig, DispatchPlanner, PlanStep, SerialReviewExecutor,
    MAX_PR_CONCURRENCY, DEFAULT_REVIEWER_CONCURRENCY,
)
from .aggregate import (  # noqa: F401
    Finding, AggregatedFindings, aggregate_findings,
)
from .verify_finding import (  # noqa: F401
    VerifierInput, VerifierOutput, FindingVerifier,
)
from .console_contract import build_console_payload  # noqa: F401
from .flag import review_v3_enabled  # noqa: F401


def __getattr__(name):  # 延迟加载:adapter/runstore 依赖面较重,按需引入
    if name in ("adapter", "runstore"):
        import importlib
        return importlib.import_module("." + name, __package__)
    raise AttributeError(name)
