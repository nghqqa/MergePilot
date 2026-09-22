"""SQLiteTicketStore 测试:契约集(store_contract)+ SQLite 专属断言。

契约集可复用于未来 PostgreSQLTicketStore(设计契约确定后)——同一 mixin、
同一验收;实现方只需提供 make_store()。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# 构造包上下文,使 store_sqlite 的相对导入(from .approval)可用
_APPROVAL_DIR = Path(__file__).resolve().parents[2] / "tools" / "approval"
_pkg = types.ModuleType("approval_pkg")
_pkg.__path__ = [str(_APPROVAL_DIR)]
sys.modules["approval_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location("approval_pkg." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approval_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("approval", _APPROVAL_DIR / "approval.py")
_smod = _load("store_sqlite", _APPROVAL_DIR / "store_sqlite.py")

SQLiteTicketStore = _smod.SQLiteTicketStore
SqliteTicketStore = SQLiteTicketStore  # 旧名别名(兼容历史引用)

contract = _load("contract", Path(__file__).resolve().parent / "store_contract.py")
contract.PARAMS_HASH = core.canonical_hash({"method": "suggestion"})
contract.PATCH_FP = core.canonical_hash("patch-bytes")

NOW = "2026-09-22T12:00:00+00:00"
LATER = "2026-09-22T13:00:00+00:00"
APPROVER = "test-approver"


class SQLiteStoreContractTests(contract.StoreContractMixin):
    """契约集在 SQLite 实现上运行。"""

    Approver = APPROVER
    Now = NOW
    Later = LATER

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tickets.db")
        self.store = self.make_store()
        self.core = core

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_store(self):
        return SQLiteTicketStore(self.path)


class InterfaceConformanceTests(unittest.TestCase):
    """TicketStore 接口(P-1 后续演化:SQLite→PG 只换适配器,契约不变)。"""

    def test_sqlite_store_satisfies_ticketstore_protocol(self):
        from approval_pkg.store import TicketStore
        with tempfile.TemporaryDirectory() as t:
            store = SQLiteTicketStore(os.path.join(t, "x.db"))
            try:
                self.assertIsInstance(store, TicketStore)
            finally:
                store.close()

    def test_pg_store_connection_error_is_distinct(self):
        """要求⑦:连接失败抛 StorageUnavailable,不伪装成业务拒绝。"""
        from approval_pkg.pg_store import StorageUnavailable
        from approval_pkg.store import PostgreSQLTicketStore
        with self.assertRaises(StorageUnavailable):
            PostgreSQLTicketStore("postgresql://u:p@nonexistent-host/db")

    def test_pg_store_requires_dsn(self):
        from approval_pkg.store import PostgreSQLTicketStore
        with self.assertRaises(ValueError):
            PostgreSQLTicketStore("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
