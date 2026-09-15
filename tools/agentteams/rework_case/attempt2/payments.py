"""Payment ledger — FIX ATTEMPT 2 (after rework).

Idempotency is keyed by order_id: an order is charged at most once; any later
charge request for that order (same or new payment id) returns the existing
charge unchanged. This is the invariant the Reviewer's finding asked for and
the acceptance test enforces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


class LedgerError(ValueError):
    """Business-rule violation."""


@dataclass
class Charge:
    order_id: int
    payment_id: int
    amount_cents: int


@dataclass
class PaymentLedger:
    charges: List[Charge] = field(default_factory=list)
    _charge_by_order: Dict[int, Charge] = field(default_factory=dict)

    def charge(self, order_id: int, payment_id: int, amount_cents: int) -> Charge:
        if amount_cents <= 0:
            raise LedgerError("amount_cents must be positive")
        existing = self._charge_by_order.get(order_id)
        if existing is not None:
            return existing
        c = Charge(order_id=order_id, payment_id=payment_id, amount_cents=amount_cents)
        self.charges.append(c)
        self._charge_by_order[order_id] = c
        return c

    def total_for_order(self, order_id: int) -> int:
        return sum(c.amount_cents for c in self.charges if c.order_id == order_id)
