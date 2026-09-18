"""Payment ledger — FIX ATTEMPT 1 (plausible but wrong).

The Fixer de-duplicates by payment_id. That closes the "same payment id
retried" hole but not the one the Reviewer actually flagged: a gateway retry
arrives with a NEW payment id for the same order and is still charged again.
The acceptance test test_second_payment_request_for_same_order_is_idempotent
keeps failing, so the Verifier must send the task back.
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
    _seen_payment_ids: Dict[int, Charge] = field(default_factory=dict)

    def charge(self, order_id: int, payment_id: int, amount_cents: int) -> Charge:
        if amount_cents <= 0:
            raise LedgerError("amount_cents must be positive")
        if payment_id in self._seen_payment_ids:
            return self._seen_payment_ids[payment_id]
        c = Charge(order_id=order_id, payment_id=payment_id, amount_cents=amount_cents)
        self.charges.append(c)
        self._seen_payment_ids[payment_id] = c
        return c

    def total_for_order(self, order_id: int) -> int:
        return sum(c.amount_cents for c in self.charges if c.order_id == order_id)
