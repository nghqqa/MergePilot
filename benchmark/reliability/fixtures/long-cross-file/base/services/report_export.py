"""Report export service (base revision): builds CSV reports in memory."""
from __future__ import annotations

import csv
import io
from typing import Iterable, List


HEADER = ["order_id", "customer_id", "amount_cents", "status"]


def rows_from_orders(orders: Iterable) -> List[List[str]]:
    out = []
    for o in orders:
        out.append([str(o.order_id), str(o.customer_id), str(o.amount_cents), o.status])
    return out


def build_csv(orders: Iterable) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADER)
    for row in rows_from_orders(orders):
        writer.writerow(row)
    return buf.getvalue()


def summarize(orders: Iterable) -> dict:
    total = 0
    count = 0
    for o in orders:
        total += o.amount_cents
        count += 1
    return {"count": count, "total_cents": total}
