"""Queue consumer (base revision)."""
from __future__ import annotations

import json
from typing import Callable, Dict, List


class Consumer:
    def __init__(self) -> None:
        self._handlers: Dict[str, Callable[[dict], None]] = {}
        self.processed: List[str] = []

    def register(self, kind: str, fn: Callable[[dict], None]) -> None:
        self._handlers[kind] = fn

    def consume(self, raw: str) -> bool:
        msg = json.loads(raw)
        kind = msg.get("kind")
        fn = self._handlers.get(kind)
        if fn is None:
            return False
        fn(msg.get("payload") or {})
        self.processed.append(kind)
        return True
