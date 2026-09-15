"""Orders API — request handlers.

PR: add report export endpoint + pagination + cancellation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from services.report_export import export_report_file


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
        if amount_cents <= 0:
            raise ValueError("amount must be positive")
        o = Order(order_id, customer_id, amount_cents)
        self._orders[order_id] = o
        return o

    def get(self, order_id: int) -> Optional[Order]:
        return self._orders.get(order_id)

    def list_for_customer(self, customer_id: int, offset: int = 0, limit: int = 50) -> List[Order]:
        rows = [o for o in self._orders.values() if o.customer_id == customer_id]
        rows.sort(key=lambda o: o.order_id)
        return rows[offset: offset + limit]

    def mark_paid(self, order_id: int) -> Order:
        o = self._orders[order_id]
        if o.status == "cancelled":
            raise ValueError("cancelled orders cannot be paid")
        o.status = "paid"
        return o

    def cancel(self, order_id: int) -> Order:
        o = self._orders[order_id]
        if o.status == "paid":
            raise ValueError("paid orders cannot be cancelled")
        o.status = "cancelled"
        return o


def handle_create(store: OrderStore, payload: dict) -> dict:
    try:
        o = store.create(int(payload["order_id"]), int(payload["customer_id"]), int(payload["amount_cents"]))
    except (KeyError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "order": o.__dict__}


def handle_get(store: OrderStore, order_id: int) -> dict:
    o = store.get(order_id)
    if o is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "order": o.__dict__}


def handle_list(store: OrderStore, customer_id: int, offset: int = 0, limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    return {"ok": True, "orders": [o.__dict__ for o in store.list_for_customer(customer_id, offset, limit)]}


def handle_cancel(store: OrderStore, order_id: int) -> dict:
    try:
        o = store.cancel(order_id)
    except (KeyError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "order": o.__dict__}


def handle_export(store: OrderStore, customer_id: int, filename: str) -> dict:
    """Export the customer's orders as a downloadable report file."""
    orders = store.list_for_customer(customer_id, 0, 10000)
    content = export_report_file(orders, filename)
    return {"ok": True, "filename": filename, "bytes": len(content)}
