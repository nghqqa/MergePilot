"""MergePilot 成本计量脚手架(SCAFFOLD——备忘四.4/V0 硬门槛 4)。

**脚手架声明**:本包是成本计量与硬预算的逻辑骨架 + 本地收集器,
不是已完成的硬预算控制。硬预算在 M4 技术就绪前须完成端到端验证
(预留→重试→结算→超限→并发→usage 缺失),现状见 docs/productization/ACCEPTANCE.md 成本节。

事实基础(2026-09-22 核对):token 级 usage 目前只存在于 worker 的
OTel LLM spans(gen_ai.usage.*),经 OTLP 导出外部 collector;本地容器
仅有 span 名序列与审计日志。故本地能诚实计的是 span/调用计数;
token 数留 unavailable 标注,币值换算仅在显式提供价目表时进行。
"""
from .core import (  # noqa: F401
    BudgetExceeded,
    BudgetGuard,
    UsageRecord,
    summarize,
)
