"""PG 可行性 spike(隔离 PostgreSQL 16,端口 55432,独立容器 mp-pg-contract-test)。

**定位**:技术可行性证据,非正式 schema(正式契约属设计窗口)。
验证 SQLiteTicketStore 的两个关键机制在 PG 上同形成立:
  1. partial UNIQUE INDEX 强制活动票唯一(跨连接);
  2. 前置状态守卫 UPDATE(rowcount CAS)先到先得。
通过后,PostgreSQLTicketStore(契约确定后实现)的语义可直接复用纯逻辑状态机。
环境:MERGEPILOT_PG_TEST_DSN 未设时跳过(不伪造 PG 验证)。
"""
from __future__ import annotations

import os
import unittest

DSN = os.environ.get(
    "MERGEPILOT_PG_TEST_DSN",
    "host=127.0.0.1 port=55432 user=mp_contract "
    "password=mp-contract-local-test dbname=mp_contract connect_timeout=5")

_SPIKE_SCHEMA = """
CREATE TABLE IF NOT EXISTS spike_tickets (
    ticket_id   TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    action      TEXT NOT NULL,
    finding_id  TEXT,
    status      TEXT NOT NULL CHECK (status IN
                ('PENDING','APPROVED','EXECUTING','USED','REJECTED',
                 'EXPIRED','FAILED','INVALIDATED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spike_active
    ON spike_tickets(run_id, action, finding_id)
    WHERE status IN ('PENDING','APPROVED','EXECUTING');
"""


def _connect():
    import psycopg2
    return psycopg2.connect(DSN)


@unittest.skipUnless(os.environ.get("MERGEPILOT_PG_TEST_DSN") or
                     os.environ.get("MERGEPILOT_PG_SPIKE") == "1",
                     "隔离 PG 未确认(MERGEPILOT_PG_TEST_DSN/SPIKE 未设置)")
class PgCasSpikeTests(unittest.TestCase):
    def setUp(self):
        self.conn = _connect()
        self.conn.autocommit = True
        cur = self.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS spike_tickets")
        cur.execute(_SPIKE_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_partial_unique_index_blocks_second_active_ticket(self):
        """跨连接:第二张活动票被部分唯一索引拒绝(与 SQLite 同形)。"""
        import psycopg2
        cur = self.conn.cursor()
        cur.execute("INSERT INTO spike_tickets VALUES ('t1','r1','generate_patch','F1','PENDING')")
        with self.assertRaises(psycopg2.IntegrityError):
            cur.execute("INSERT INTO spike_tickets VALUES ('t2','r1','generate_patch','F1','PENDING')")
            self.conn.rollback()

    def test_guarded_update_cas_first_wins(self):
        """跨连接 CAS:先到先得,后到 rowcount=0(不覆盖)。"""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO spike_tickets VALUES ('t1','r1','generate_patch','F1','PENDING')")
        cur.execute("UPDATE spike_tickets SET status='APPROVED' "
                    "WHERE ticket_id='t1' AND status='PENDING'")
        self.assertEqual(cur.rowcount, 1)
        cur.execute("UPDATE spike_tickets SET status='REJECTED' "
                    "WHERE ticket_id='t1' AND status='PENDING'")
        self.assertEqual(cur.rowcount, 0)   # 前置态不匹配 → 不覆盖
        cur.execute("SELECT status FROM spike_tickets WHERE ticket_id='t1'")
        self.assertEqual(cur.fetchone()[0], "APPROVED")

    def test_terminal_status_allows_new_attempt(self):
        """终态票不再占用活动唯一槽:允许新 attempt(与 SQLite/契约一致)。"""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO spike_tickets VALUES ('t1','r1','generate_patch','F1','REJECTED')")
        cur.execute("INSERT INTO spike_tickets VALUES ('t2','r1','generate_patch','F1','PENDING')")
        self.assertEqual(cur.rowcount, 1)   # 终态不冲突


if __name__ == "__main__":
    unittest.main(verbosity=2)
