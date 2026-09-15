"""Gateway-side classification of the DB release gate (finals D2, claim path).

Imports the real tools/policy-gateway/gateway.py (needs `mcp`; skipped on interpreters without it —
the conda env `goai` carries the image-pinned mcp==1.28.1). The DB is replaced by a fake connection
so this test is offline; the real-PG path is covered by test_migration_loop_pg.py."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GW_DIR = ROOT / "tools" / "policy-gateway"

pytest.importorskip("mcp", reason="gateway.py needs the mcp package (Python>=3.10; conda env goai)")
os.environ.setdefault("POLICY_FILE", str(GW_DIR / "policy.yaml"))
sys.path.insert(0, str(GW_DIR))
gateway = importlib.import_module("gateway")


class _Cursor:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.params = params
        if self.behaviour == "gate_refused":
            raise RuntimeError("DB_RELEASE_GATE_REFUSED: STALE_SUPERSEDED_BY_NEW_REVISION (bound_head=aaa, current_head=bbb)\nCONTEXT: PL/pgSQL")
        if self.behaviour == "db_error":
            raise RuntimeError("could not connect")

    def fetchone(self):
        if self.behaviour == "claimed":
            return ("11111111-2222-3333-4444-555555555555", '{"owner":"o","repo":"r","pullNumber":4}', "a" * 40, "main")
        return None


class _Conn:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.rolled_back = False

    def cursor(self):
        self.cur = _Cursor(self.behaviour)
        return self.cur

    def rollback(self):
        self.rolled_back = True


class TestClaimWrapperClassification(unittest.TestCase):

    def _with(self, behaviour):
        conn = _Conn(behaviour)
        gateway._get_l2_conn = lambda: conn
        return conn

    def test_gate_refusal_is_its_own_status_and_ticket_untouched(self):
        conn = self._with("gate_refused")
        status, claim = gateway.l2_claim_ticket("tkt", "merge", "o/r", 4, "0" * 64, "f" * 64)
        self.assertEqual(status, "GATE_REFUSED")
        self.assertIn("STALE_SUPERSEDED_BY_NEW_REVISION", claim["reason"])
        self.assertTrue(conn.rolled_back)
        self.assertEqual(conn.cur.params[5], "f" * 64)   # target data digest reaches SQL as the 6th argument

    def test_claimed_and_mismatch_and_db_error_unchanged(self):
        self._with("claimed")
        status, claim = gateway.l2_claim_ticket("tkt", "merge", "o/r", 4, "0" * 64)
        self.assertEqual(status, "CLAIMED")
        self.assertEqual(claim["expected_head_sha"], "a" * 40)
        self._with("mismatch")
        self.assertEqual(gateway.l2_claim_ticket("tkt", "merge", "o/r", 4, "0" * 64)[0], "MISMATCH")
        self._with("db_error")
        self.assertEqual(gateway.l2_claim_ticket("tkt", "merge", "o/r", 4, "0" * 64)[0], "DB_ERROR")

    def test_release_digest_never_changes_args_hash(self):
        base = {"owner": "o", "repo": "r", "pullNumber": 4, "commit_title": "t", "merge_method": "squash"}
        with_gw = dict(base, approval_ticket="tkt", release_data_digest="f" * 64)
        self.assertEqual(gateway.canonical_args_hash(base), gateway.canonical_args_hash(with_gw))


if __name__ == "__main__":
    unittest.main()
