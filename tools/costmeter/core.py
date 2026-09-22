"""预算守卫与用量汇总(纯逻辑,零 IO;持久化由调用方决定)。

预算单位默认 tokens(不用货币:价目未定,不伪造币值;提供价目表时才换算)。
红线:预算上限由调用方显式传入——本模块不设默认额度,不构成任何真实消费授权。

语义(对应提示词七.C 的 M4 验证清单):
- reserve:调用前检查并预留;余额不足 → BudgetExceeded;
- 重试:同一次调用的重试沿用原预留(retry_of),不重复占用额度;
- commit:调用结束后按实际结算;实际未知(usage 缺失)→ 按预留额消耗并记 gap;
- release:调用失败且无副作用 → 全额退还;
- 超限:任何 reserve 在剩余额度不足时一律拒绝(fail-closed);
- 并发:同进程内锁保护,同一预留 id 不可重复创建(不会重复使用同一额度);
- 崩溃:load() 恢复台账,过期预留(deadline 前)在下次操作时回收(stale reclaim)。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PRICE_TABLE_HINT = "价格表 {model: {'input_per_1k': x, 'output_per_1k': y}};缺失模型的换算返回 None,绝不估 价。"


class BudgetExceeded(Exception):
    """剩余额度不足以覆盖请求的预留。"""


@dataclass
class UsageRecord:
    """一条用量事实。tokens 未知时 input/output 为 None 并置 missing=True。"""

    run_id: str
    role: str
    calls: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    missing: bool = False
    source: str = "unknown"   # span | report | guard | manual
    model: Optional[str] = None


@dataclass
class _Reservation:
    rid: str
    amount: int
    created_ts: float
    state: str = "held"   # held | settled | released


@dataclass
class BudgetGuard:
    """单 run 预算守卫。limit 必须显式传入(>0);持久化路径可选。"""

    run_id: str
    limit: int
    unit: str = "tokens"
    ledger_path: Optional[str] = None
    stale_after_s: float = 3600.0   # 崩溃残留预留的回收时限
    _remaining: int = field(default=-1, init=False)
    _reservations: Dict[str, _Reservation] = field(default_factory=dict, init=False)
    _gaps: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self):
        if self.limit <= 0:
            raise ValueError("limit 必须显式传入且 >0(不设默认额度)")
        self._remaining = self.limit
        if self.ledger_path:
            self._load()

    # ── 持久化(write-through;崩溃后 load 恢复) ─────────────────────────
    def _dump(self):
        if not self.ledger_path:
            return
        import json
        body = {"run_id": self.run_id, "limit": self.limit, "unit": self.unit,
                "remaining": self._remaining, "gaps": self._gaps,
                "reservations": [
                    {"rid": r.rid, "amount": r.amount, "created_ts": r.created_ts,
                     "state": r.state} for r in self._reservations.values()]}
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)

    def _load(self):
        import json
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                body = json.load(f)
        except Exception:
            return  # 无台账=全新
        self._remaining = int(body.get("remaining", self.limit))
        self._gaps = int(body.get("gaps", 0))
        for r in body.get("reservations", []):
            self._reservations[r["rid"]] = _Reservation(
                r["rid"], int(r["amount"]), float(r["created_ts"]), r["state"])

    def _reclaim_stale(self, now: float):
        for rid in [rid for rid, r in self._reservations.items()
                    if r.state == "held" and now - r.created_ts > self.stale_after_s]:
            r = self._reservations[rid]
            r.state = "released"
            self._remaining += r.amount  # 崩溃残留:持有者已不在,退回额度

    # ── 核心语义 ────────────────────────────────────────────────────────
    def reserve(self, amount: int, retry_of: Optional[str] = None,
                now: Optional[float] = None) -> str:
        """调用前预留。amount>0;retry_of 指定原预留 id 时沿用之(重试不重复占额)。"""
        if amount <= 0:
            raise ValueError("reserve amount 必须 >0")
        now = time.time() if now is None else now
        with self._lock:
            self._reclaim_stale(now)
            if retry_of is not None:
                r = self._reservations.get(retry_of)
                if r is None or r.state != "held":
                    raise ValueError("retry_of 预留不存在或已终结:%s" % retry_of)
                return retry_of  # 沿用,不新扣
            if amount > self._remaining:
                raise BudgetExceeded(
                    "预算不足(%s):需 %d,余 %d / 上限 %d"
                    % (self.unit, amount, self._remaining, self.limit))
            rid = uuid.uuid4().hex[:16]
            self._reservations[rid] = _Reservation(rid, amount, now, "held")
            self._remaining -= amount
            self._dump()
            return rid

    def commit(self, rid: str, actual: Optional[int] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        """结算。actual=None(usage 缺失)→ 按预留额消耗并记 gap(保守,不退款)。"""
        with self._lock:
            r = self._reservations.get(rid)
            if r is None or r.state != "held":
                raise ValueError("结算目标不是活动预留:%s" % rid)
            if actual is None:
                self._gaps += 1          # 缺失显式记账,留待对账
                held = r.amount
            elif actual <= r.amount:
                self._remaining += r.amount - actual   # 多退
                held = actual
            else:
                over = actual - r.amount
                if over > self._remaining:
                    self._remaining += r.amount
                    r.state = "settled"
                    self._dump()
                    raise BudgetExceeded(
                        "实际用量超预留且超出余额:需补 %d,余 %d" % (over, self._remaining))
                self._remaining -= over                # 少补
                held = actual
            r.state = "settled"
            self._dump()
            return {"consumed": held, "remaining": self._remaining,
                    "usage_missing": actual is None, "gaps": self._gaps}

    def release(self, rid: str) -> int:
        """释放未使用预留(调用失败且无副作用)。返回退还额度。"""
        with self._lock:
            r = self._reservations.get(rid)
            if r is None or r.state != "held":
                raise ValueError("释放目标不是活动预留:%s" % rid)
            r.state = "released"
            self._remaining += r.amount
            self._dump()
            return r.amount

    def status(self) -> Dict[str, Any]:
        with self._lock:
            held = sum(r.amount for r in self._reservations.values() if r.state == "held")
            return {"run_id": self.run_id, "unit": self.unit, "limit": self.limit,
                    "remaining": self._remaining, "held": held,
                    "gaps": self._gaps,
                    "active_reservations": sum(1 for r in self._reservations.values()
                                               if r.state == "held")}


# ── 汇总与换算 ──────────────────────────────────────────────────────────────
def summarize(records: List[UsageRecord],
              prices: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
    """按 run/role 汇总。无价目表或模型缺价 → cost=None(不伪造币值)。"""
    out: Dict[str, Any] = {"runs": {}, "missing_usage_records": 0}
    for r in records:
        run = out["runs"].setdefault(r.run_id, {"calls": 0, "roles": {}})
        run["calls"] += r.calls
        role = run["roles"].setdefault(r.role, {"calls": 0, "input_tokens": 0,
                                                "output_tokens": 0, "missing": 0})
        role["calls"] += r.calls
        if r.missing or r.input_tokens is None:
            out["missing_usage_records"] += 1
            role["missing"] += r.calls
        else:
            role["input_tokens"] += r.input_tokens
            role["output_tokens"] += r.output_tokens
    if prices:
        out["cost"] = _cost_by_run(records, prices)
    return out


def _cost_by_run(records, prices):
    import collections
    costs = collections.defaultdict(float)
    unknown = set()
    for r in records:
        if r.missing or r.input_tokens is None:
            continue
        p = prices.get(r.model or "")
        if not p:
            unknown.add(r.model or "?")
            continue
        costs[r.run_id] += (r.input_tokens * p.get("input_per_1k", 0)
                            + r.output_tokens * p.get("output_per_1k", 0)) / 1000.0
    result = {k: round(v, 6) for k, v in costs.items()}
    if unknown:
        result["unknown_models"] = sorted(unknown)
    return result
