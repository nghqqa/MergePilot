"""Notification service (base): formats messages only."""
from __future__ import annotations


def format_message(order_id: int, status: str) -> str:
    return "order %d is now %s" % (order_id, status)
