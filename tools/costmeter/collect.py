"""本地用量收集器脚手架(SCAFFOLD):只读 span 计数,不触 token 外部源。

事实(2026-09-22 核对):本地 worker 容器内 /tmp/agentloop-spans.log 只有
span 名序列;token 级 usage 在 OTel spans 中且经 OTLP 导出外部 collector。
故本收集器输出的 UsageRecord 一律 tokens=None + missing=True + source="span-count",
汇总为调用次数面;token/币值面待 M4 前接通真实 usage 源后补齐——这是脚手架,
不是已完成的全量计量。
"""
from __future__ import annotations

import subprocess
from typing import Dict, List

from .core import UsageRecord

SPAN_LOG = "/tmp/agentloop-spans.log"


def _container_spans(container: str, timeout: int = 60):
    """读一个 worker 容器的 span 名序列;失败返回空表(fail-soft,缺失会进报告)。"""
    try:
        r = subprocess.run(["docker", "exec", container, "cat", SPAN_LOG],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None  # None=无法读取(容器不在等);[]=可读但为空
    if r.returncode != 0:
        return None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def collect_span_counts(run_id: str,
                        containers: Dict[str, str],
                        reader=_container_spans) -> List[UsageRecord]:
    """containers: {role: container_name}。产出逐 role 的调用计数记录。"""
    records: List[UsageRecord] = []
    for role, cname in containers.items():
        spans = reader(cname)
        if spans is None:
            records.append(UsageRecord(run_id=run_id, role=role, calls=0,
                                       missing=True, source="span-count"))
        else:
            records.append(UsageRecord(run_id=run_id, role=role, calls=len(spans),
                                       input_tokens=None, output_tokens=None,
                                       missing=True, source="span-count"))
    return records


def build_usage_report(records: List[UsageRecord], guard_status: dict) -> dict:
    """SCAFFOLD 报告体:调用计数 + 预算守卫状态 + 明确的不可用标注。"""
    from .core import summarize
    s = summarize(records)
    return {
        "report_version": 0,           # 0=脚手架:token 面未接通
        "token_source": "unavailable (OTel spans exported off-box; local=span names only)",
        "usage": s,
        "budget_guard": guard_status,
        "scaffold": True,
    }
