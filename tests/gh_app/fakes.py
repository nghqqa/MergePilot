"""Shared fakes: scripted psycopg2-style connection for gh-app tests."""

from __future__ import annotations

from collections import deque


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = -1

    def execute(self, sql, params=None):
        self.rowcount = self._conn._on_execute(sql, params)

    def fetchone(self):
        return self._conn._next_fetchone()

    def fetchall(self):
        return self._conn._next_fetchall()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """Scripted connection.

    plan entries: {"match": <substr of sql>, "rowcount": int,
                   "fetchone": tuple|None, "fetchall": list|None}
    The first UNCONSUMED entry whose `match` appears in the executed SQL is
    consumed (rowcount applied, fetchone staged). Unmatched statements get
    rowcount=1 and no staged results.
    """

    def __init__(self, plan=None):
        self.plan = deque(plan or [])
        self.executed = []          # [(sql, params)]
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self._staged_fetchone = None
        self._staged_fetchall = None

    # -- scripting ------------------------------------------------------------

    def enqueue(self, match, rowcount=1, fetchone=None, fetchall=None):
        self.plan.append({"match": match, "rowcount": rowcount,
                          "fetchone": fetchone, "fetchall": fetchall})
        return self

    # -- psycopg2 surface ------------------------------------------------------

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _normalize(sql):
        return " ".join(str(sql).split())

    def _on_execute(self, sql, params):
        normalized = self._normalize(sql)
        self.executed.append((normalized, params))
        self._staged_fetchone = None
        self._staged_fetchall = None
        for entry in self.plan:
            if not entry.get("_used") and entry["match"] in normalized:
                entry["_used"] = True
                self._staged_fetchone = entry.get("fetchone")
                self._staged_fetchall = entry.get("fetchall")
                return entry["rowcount"]
        return 1

    def _next_fetchone(self):
        return self._staged_fetchone

    def _next_fetchall(self):
        return self._staged_fetchall or []

    # -- assertion helpers ------------------------------------------------------

    def sqls(self):
        return [sql for sql, _ in self.executed]

    def params_of(self, match):
        return [params for sql, params in self.executed if match in sql]
