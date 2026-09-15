"""Checkout.

Refactor: result helpers and docstrings; behaviour unchanged.
"""
from __future__ import annotations

from typing import List

from app.inventory import Inventory
from app.pricing import total


def _failure(error: str, **extra) -> dict:
    out = {"ok": False, "error": error}
    out.update(extra)
    return out


def _success(total_cents: int) -> dict:
    return {"ok": True, "total_cents": total_cents}


def checkout(inv: Inventory, skus: List[str], pct: int = 0) -> dict:
    """Reserve one unit per SKU and return the discounted total."""
    for s in skus:
        if not inv.take(s, 1):
            return _failure("out_of_stock", sku=s)
    return _success(total(skus, pct))
