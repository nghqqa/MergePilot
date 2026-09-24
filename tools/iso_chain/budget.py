# -*- coding: utf-8 -*-
"""iso_chain.budget — 真实模型调用的硬预算守卫(复用 costmeter.BudgetGuard)。

规则(本轮授权):
  - 每次调用前按「预估输入 + 单请求最大输出」预留;调用后按真实 usage 结算;
  - usage 缺失/不可靠 → 按最大输出保守结算,并标记 unreliable;
    unreliable 后不再发起新调用(输入计量不可靠时保守计账并停止新增);
  - 预算耗尽/不可靠 → reserve 抛 BudgetExceeded,调用方必须停止;
  - 重试沿用原预留(retry_of),不重复占额。
"""
from __future__ import annotations

import sys
import os

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from costmeter.core import BudgetGuard, BudgetExceeded  # noqa: E402


class ModelBudget:
    """模型调用预算门面。reserve→call→settle 三段式。"""

    def __init__(self, *, run_id: str, max_requests: int, max_total_tokens: int,
                 max_output_per_request: int, est_input_per_request: int = 4000,
                 ledger_path: str = None):
        self.max_requests = int(max_requests)
        self.max_output = int(max_output_per_request)
        self.est_input = int(est_input_per_request)
        self.requests = 0
        self.unreliable = False
        self.last_error = ""
        self.guard = BudgetGuard(run_id=run_id, limit=int(max_total_tokens),
                                 ledger_path=ledger_path)

    def reserve(self, est_input: int = None):
        """调用前预留。返回 reservation id;超限抛 BudgetExceeded。"""
        if self.requests >= self.max_requests:
            raise BudgetExceeded("request limit reached (%d)" % self.max_requests)
        if self.unreliable:
            raise BudgetExceeded("usage accounting unreliable; new calls stopped")
        est = int(est_input if est_input is not None else self.est_input) + self.max_output
        return self.guard.reserve(est)

    def settle(self, rid: str, usage: dict = None):
        """调用后结算。usage = {"prompt_tokens": int, "completion_tokens": int}。

        usage 缺失/不完整 → 保守按预留额结算并标记 unreliable。"""
        self.requests += 1
        if (not usage or not isinstance(usage.get("prompt_tokens"), int)
                or not isinstance(usage.get("completion_tokens"), int)):
            actual = None                      # 保守:按预留额结算
            self.unreliable = True
            self.last_error = "usage missing/incomplete; conservative accounting"
        else:
            actual = usage["prompt_tokens"] + usage["completion_tokens"]
        self.guard.commit(rid, actual=actual)

    def refund(self, rid: str):
        """调用根本未发出(如构造请求失败)→ 释放预留(全额退回)。"""
        self.guard.release(rid)

    @property
    def remaining(self) -> int:
        return self.guard._remaining

    def status(self) -> dict:
        return {"requests": self.requests, "max_requests": self.max_requests,
                "remaining_tokens": self.remaining, "unreliable": self.unreliable,
                "last_error": self.last_error}
