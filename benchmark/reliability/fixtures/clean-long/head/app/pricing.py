"""Pricing rules.

Refactor: named constants, explicit validation helpers and docstrings.
No behaviour change (covered by tests/test_pricing.py).
"""
from __future__ import annotations

from typing import Dict, List

PRICE_TABLE_CENTS: Dict[str, int] = {"A": 1000, "B": 2500, "C": 4000}
MIN_PCT = 0
MAX_PCT = 100


def base_price(sku: str) -> int:
    """Return the list price in cents for a SKU (0 for unknown SKUs)."""
    return PRICE_TABLE_CENTS.get(sku, 0)


def validate_pct(pct: int) -> int:
    """Return pct unchanged or raise ValueError when outside 0..100."""
    if pct < MIN_PCT or pct > MAX_PCT:
        raise ValueError("pct out of range: %d" % pct)
    return pct


def apply_discount(price: int, pct: int) -> int:
    """Apply an integer percentage discount using floor arithmetic."""
    pct = validate_pct(pct)
    return price - price * pct // 100


def total(skus: List[str], pct: int = 0) -> int:
    """Sum discounted list prices for the given SKUs."""
    return sum(apply_discount(base_price(s), pct) for s in skus)
