"""Orders API — request handlers (base revision)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Order:
    order_id: int
    customer_id: int
    amount_cents: int
    status: str = "new"


class OrderStore:
    def __init__(self) -> None:
        self._orders: Dict[int, Order] = {}

    def create(self, order_id: int, customer_id: int, amount_cents: int) -> Order:
        if order_id in self._orders:
            raise ValueError("duplicate order")
        o = Order(order_id, customer_id, amount_cents)
        self._orders[order_id] = o
        return o

    def get(self, order_id: int) -> Optional[Order]:
        return self._orders.get(order_id)

    def list_for_customer(self, customer_id: int) -> List[Order]:
        return [o for o in self._orders.values() if o.customer_id == customer_id]

    def mark_paid(self, order_id: int) -> Order:
        o = self._orders[order_id]
        o.status = "paid"
        return o


def handle_create(store: OrderStore, payload: dict) -> dict:
    o = store.create(int(payload["order_id"]), int(payload["customer_id"]), int(payload["amount_cents"]))
    return {"ok": True, "order": o.__dict__}


def handle_get(store: OrderStore, order_id: int) -> dict:
    o = store.get(order_id)
    if o is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "order": o.__dict__}


def handle_list(store: OrderStore, customer_id: int) -> dict:
    return {"ok": True, "orders": [o.__dict__ for o in store.list_for_customer(customer_id)]}
