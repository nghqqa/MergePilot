"""Payment ledger — BASE revision (the code the PR starts from).

Known defect the Reviewer will flag: charge() appends unconditionally, so a
retried payment request for the same order charges the customer twice.
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

    def charge(self, order_id: int, payment_id: int, amount_cents: int) -> Charge:
        if amount_cents <= 0:
            raise LedgerError("amount_cents must be positive")
        c = Charge(order_id=order_id, payment_id=payment_id, amount_cents=amount_cents)
        self.charges.append(c)
        return c

    def total_for_order(self, order_id: int) -> int:
        return sum(c.amount_cents for c in self.charges if c.order_id == order_id)
