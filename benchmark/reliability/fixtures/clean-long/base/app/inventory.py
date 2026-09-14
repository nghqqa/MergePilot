"""Inventory (base)."""
from __future__ import annotations

from typing import Dict


class Inventory:
    def __init__(self) -> None:
        self._stock: Dict[str, int] = {}

    def add(self, sku: str, qty: int) -> None:
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def take(self, sku: str, qty: int) -> bool:
        have = self._stock.get(sku, 0)
        if have < qty:
            return False
        self._stock[sku] = have - qty
        return True

    def level(self, sku: str) -> int:
        return self._stock.get(sku, 0)
