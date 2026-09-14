"""Pricing rules (base)."""
from __future__ import annotations

from typing import List


def base_price(sku: str) -> int:
    table = {"A": 1000, "B": 2500, "C": 4000}
    return table.get(sku, 0)


def apply_discount(price: int, pct: int) -> int:
    if pct < 0 or pct > 100:
        raise ValueError("pct out of range")
    return price - price * pct // 100


def total(skus: List[str], pct: int = 0) -> int:
    return sum(apply_discount(base_price(s), pct) for s in skus)
