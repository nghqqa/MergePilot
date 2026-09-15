#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_digest.py — the ONE canonical data-digest algorithm shared by baseline registration
(data_baselines.data_digest) and the release executor (p_target_data_digest at claim time).

Trusted source rule: the digest presented at l2_claim_ticket() must be computed by the release
executor against the TARGET database with this exact algorithm, immediately before running the
migration. A digest copied from the verification report, or computed earlier, is not a fresh
observation of the target and must not be used.

Algorithm (deterministic, engine-independent within PostgreSQL):
    for each table in sorted(tables):
        h.update(b"table:<name>\\n")
        h.update(COPY (SELECT * FROM <name> ORDER BY 1) TO STDOUT)   # text format, UTF-8
    sha256 hex

Usage:
    python tools/dbverify/data_digest.py --dsn "host=... dbname=..." --tables customers,orders,payments,legacy_order_owner
    python tools/dbverify/data_digest.py --dsn ... --tables ... --expect <64hex>     # exit 3 on mismatch
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys

import psycopg2

DEFAULT_TABLES = ("customers", "orders", "payments", "legacy_order_owner")


def compute_data_digest(conn, tables=DEFAULT_TABLES) -> str:
    """sha256 over the canonical per-table dump of `tables` (sorted, ORDER BY 1)."""
    names = sorted(tables)
    for t in names:
        if not t.replace("_", "").isalnum():
            raise ValueError("unsafe table name: %r" % t)
    h = hashlib.sha256()
    with conn.cursor() as cur:
        for t in names:
            buf = io.StringIO()
            cur.copy_expert("COPY (SELECT * FROM %s ORDER BY 1) TO STDOUT" % t, buf)
            h.update(("table:%s\n" % t).encode("utf-8"))
            h.update(buf.getvalue().encode("utf-8"))
    return h.hexdigest()


def row_counts(conn, tables=DEFAULT_TABLES) -> dict:
    out = {}
    with conn.cursor() as cur:
        for t in sorted(tables):
            cur.execute("SELECT count(*) FROM %s" % t)
            out[t] = cur.fetchone()[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="canonical data digest of a PostgreSQL target")
    ap.add_argument("--dsn", required=True, help="libpq DSN (never put it in evidence or logs)")
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    ap.add_argument("--expect", default=None, help="expected digest (e.g. the bound baseline digest); exit 3 on mismatch")
    args = ap.parse_args()
    tables = tuple(t.strip() for t in args.tables.split(",") if t.strip())
    conn = psycopg2.connect(args.dsn)
    conn.set_session(readonly=True, autocommit=True)
    try:
        digest = compute_data_digest(conn, tables)
        counts = row_counts(conn, tables)
    finally:
        conn.close()
    print(digest)
    print("rows:", " ".join("%s=%d" % kv for kv in sorted(counts.items())), file=sys.stderr)
    if args.expect and args.expect != digest:
        print("MISMATCH: target data digest != expected (%s...) — stop the release" % args.expect[:16], file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
