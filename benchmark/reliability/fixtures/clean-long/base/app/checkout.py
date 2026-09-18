"""Checkout (base)."""
from __future__ import annotations

from typing import List

from app.inventory import Inventory
from app.pricing import total


def checkout(inv: Inventory, skus: List[str], pct: int = 0) -> dict:
    for s in skus:
        if not inv.take(s, 1):
            return {"ok": False, "error": "out_of_stock", "sku": s}
    return {"ok": True, "total_cents": total(skus, pct)}
