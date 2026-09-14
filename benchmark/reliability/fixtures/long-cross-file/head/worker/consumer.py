"""Queue consumer with retry bookkeeping."""
from __future__ import annotations

import json
from typing import Callable, Dict, List


class Consumer:
    def __init__(self, max_retries: int = 3) -> None:
        self._handlers: Dict[str, Callable[[dict], None]] = {}
        self.processed: List[str] = []
        self.failed: List[str] = []
        self._retries: Dict[str, int] = {}
        self.max_retries = max_retries

    def register(self, kind: str, fn: Callable[[dict], None]) -> None:
        self._handlers[kind] = fn

    def consume(self, raw: str) -> bool:
        msg = json.loads(raw)
        kind = msg.get("kind")
        msg_id = str(msg.get("id", ""))
        fn = self._handlers.get(kind)
        if fn is None:
            return False
        try:
            fn(msg.get("payload") or {})
        except Exception:
            n = self._retries.get(msg_id, 0) + 1
            self._retries[msg_id] = n
            if n >= self.max_retries:
                self.failed.append(msg_id)
            return False
        self.processed.append(kind)
        return True
