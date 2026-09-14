"""Order service as changed by the PR under review.

The code change that accompanies the migration: every new order must carry a
customer_id, and at most one payment is accepted per order (a second payment
for the same order is treated as an idempotent replay, not a new payment).

These rules are what the PR's unit tests verify. They hold for the code in
isolation — the tests never touch historical rows, which is exactly why a
green test run says nothing about whether the migration can be applied to the
production baseline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


class OrderError(ValueError):
    """Business-rule violation."""


@dataclass
class Order:
    order_id: int
    customer_id: int
    amount_cents: int
    status: str = "new"


@dataclass
class Payment:
    payment_id: int
    order_id: int
    amount_cents: int


@dataclass
class OrderService:
    orders: Dict[int, Order] = field(default_factory=dict)
    payments_by_order: Dict[int, Payment] = field(default_factory=dict)

    def create_order(self, order_id: int, customer_id: Optional[int], amount_cents: int) -> Order:
        if customer_id is None:
            raise OrderError("customer_id is required")
        if amount_cents <= 0:
            raise OrderError("amount_cents must be positive")
        if order_id in self.orders:
            raise OrderError("duplicate order_id %d" % order_id)
        order = Order(order_id=order_id, customer_id=customer_id, amount_cents=amount_cents)
        self.orders[order_id] = order
        return order

    def record_payment(self, payment_id: int, order_id: int, amount_cents: int) -> Payment:
        order = self.orders.get(order_id)
        if order is None:
            raise OrderError("unknown order_id %d" % order_id)
        existing = self.payments_by_order.get(order_id)
        if existing is not None:
            # one payment per order: a replay returns the recorded payment unchanged
            return existing
        if amount_cents != order.amount_cents:
            raise OrderError("payment amount %d != order amount %d" % (amount_cents, order.amount_cents))
        payment = Payment(payment_id=payment_id, order_id=order_id, amount_cents=amount_cents)
        self.payments_by_order[order_id] = payment
        order.status = "paid"
        return payment
