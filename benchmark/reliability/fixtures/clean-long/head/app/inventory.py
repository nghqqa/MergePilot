"""Inventory.

Refactor: explicit type aliases, docstrings and a read-only snapshot helper.
"""
from __future__ import annotations

from typing import Dict

StockLevels = Dict[str, int]


class Inventory:
    """In-memory stock levels keyed by SKU."""

    def __init__(self) -> None:
        self._stock: StockLevels = {}

    def add(self, sku: str, qty: int) -> None:
        """Increase the stock level of a SKU."""
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def take(self, sku: str, qty: int) -> bool:
        """Decrease stock if enough is available; return False otherwise."""
        have = self._stock.get(sku, 0)
        if have < qty:
            return False
        self._stock[sku] = have - qty
        return True

    def level(self, sku: str) -> int:
        """Current stock level (0 for unknown SKUs)."""
        return self._stock.get(sku, 0)

    def snapshot(self) -> StockLevels:
        """Copy of all stock levels."""
        return dict(self._stock)
