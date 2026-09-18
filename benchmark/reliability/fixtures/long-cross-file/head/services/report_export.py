"""Report export service: builds CSV reports and serves saved report files."""
from __future__ import annotations

import csv
import io
import os
from typing import Iterable, List


HEADER = ["order_id", "customer_id", "amount_cents", "status"]
EXPORT_DIR = os.environ.get("REPORT_EXPORT_DIR", "/srv/reports")


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
    paid = 0
    for o in orders:
        total += o.amount_cents
        count += 1
        if o.status == "paid":
            paid += 1
    return {"count": count, "total_cents": total, "paid": paid}


def export_report_file(orders: Iterable, filename: str) -> bytes:
    """Write the CSV next to previously saved reports and return the saved bytes.

    `filename` comes straight from the request so customers can name their
    exports; the file is looked up again after writing to return the exact
    bytes that will be served.
    """
    target = os.path.join(EXPORT_DIR, filename)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(build_csv(orders))
    with open(target, "rb") as fh:
        return fh.read()
