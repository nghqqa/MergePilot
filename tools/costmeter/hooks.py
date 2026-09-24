"""预算接入点(M3.5 复核后准备):把 BudgetGuard 接到真实调用入口的工厂。

接入点盘点(2026-09-22 核对):
1. 桥派发边界(process):dispatch_budget_hook() —— 每次 v3/真实派发前消耗预估额度;
2. v3 调度执行器(SerialReviewExecutor.budget_hook):同上工厂产物直接传入;
3. worker agentloop(模型调用面):在 worker 侧,属 R5 镜像层,未接。

配置:MERGEPILOT_RUN_BUDGET_TOKENS(正整数)=单 run 预算上限。
未设置 → 返回 None(调用方跳过预算检查;**未配置不产生任何消费授权**,
也不会阻止工作——这是"预算未拍板"现状下的诚实语义,写入 ACCEPTANCE-COST)。
设置后 → fail-closed:BudgetExceeded 即拒绝。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Callable, Dict, Optional

BUDGET_ENV = "MERGEPILOT_RUN_BUDGET_TOKENS"


def make_dispatch_budget_hook(environ: Optional[Dict[str, str]] = None,
                              run_id: str = "dispatch",
                              ledger_dir: Optional[str] = None,
                             ) -> Optional[Callable[[], None]]:
    """环境驱动工厂:配置了预算 → 返回消耗型 hook(每次调用扣 1 单位预留并按需结算);
    未配置 → None。ledger_dir 给定时预算台账落盘(崩溃恢复)。"""
    env = os.environ if environ is None else environ
    raw = env.get(BUDGET_ENV, "")
    if not raw:
        return None
    limit = int(raw)
    if limit <= 0:
        raise ValueError("%s 必须为正整数,得到: %r" % (BUDGET_ENV, raw))

    def import_core():
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "costmeter", "core.py")
        spec = importlib.util.spec_from_file_location("costmeter_core_hook", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    core = import_core()
    ledger_path = (os.path.join(ledger_dir, "budget-%s.json" % run_id)
                   if ledger_dir else None)
    guard = core.BudgetGuard(run_id=run_id, limit=limit, ledger_path=ledger_path)

    def hook() -> None:
        # 每次调用消耗一个"次"单位(粗粒度派发计数预算);token 级结算随 R6 接入。
        rid = guard.reserve(1)
        guard.commit(rid, actual=1)

    hook.BudgetExceeded = core.BudgetExceeded  # 调用方按此捕获(独立加载副本时类身份不同)
    hook.guard_status = guard.status  # type: ignore[attr-defined]
    return hook
