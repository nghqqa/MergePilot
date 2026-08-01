"""Unit tests for the pgvector fixture seeder's safety guard.

A FakeCursor records every executed statement.  Each rejection case must raise
FixtureRejection AND the recorded statements must contain NO destructive SQL
(CREATE/DROP/ALTER/INSERT/DELETE/TRUNCATE/GRANT/REVOKE) -- proving the guard
refuses before any damage.
"""
from __future__ import annotations

import re

import pytest

from tests.m4e.fixtures.seed_pgvector_fixture import (
    ALLOWED_DBS,
    CONFIRM_VALUE,
    FIXTURE_DB,
    SCHEMA_VERSION,
    FixtureRejection,
    _check_guard,
)

_DESTRUCTIVE = re.compile(
    r"(?i)^\s*(CREATE|DROP|ALTER|INSERT|DELETE|TRUNCATE|GRANT|REVOKE)\b"
)


class FakeCursor:
    """Records execute() and returns canned results by SQL pattern."""

    def __init__(self, *, db=FIXTURE_DB, dbs=None, knowledge_exists=False,
                 marker_rows=None):
        self.db = db
        self.dbs = list(dbs) if dbs is not None else list(ALLOWED_DBS)
        self.knowledge_exists = knowledge_exists
        # None -> marker table absent; list of (run_id, schema_version)
        self.marker_rows = marker_rows
        self.executed = []
        self._last_params = None

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._last_params = params

    def fetchone(self):
        last = self.executed[-1]
        if "current_database" in last:
            return (self.db,)
        if "to_regclass" in last:
            name = (self._last_params or (None,))[0]
            if name in ("public.knowledge", "cr_fixture.knowledge"):
                return (name if self.knowledge_exists else None,)
            if name == "cr_fixture.fixture_marker":
                return (name if self.marker_rows is not None else None,)
            return (None,)
        return (None,)

    def fetchall(self):
        last = self.executed[-1]
        if "pg_database" in last:
            return [(d,) for d in self.dbs]
        if "cr_fixture.fixture_marker" in last:
            return list(self.marker_rows or [])
        return []

    def close(self):
        pass


def _set_confirm(monkeypatch, *, confirm=True, run_id=None):
    if confirm:
        monkeypatch.setenv("M4E_EPHEMERAL_CONFIRM", CONFIRM_VALUE)
    else:
        monkeypatch.delenv("M4E_EPHEMERAL_CONFIRM", raising=False)
    if run_id is not None:
        monkeypatch.setenv("M4E_FIXTURE_RUN_ID", run_id)
    else:
        monkeypatch.delenv("M4E_FIXTURE_RUN_ID", raising=False)


def _assert_no_destructive(cur):
    bad = [s for s in cur.executed if _DESTRUCTIVE.match(s)]
    assert not bad, "destructive SQL executed before guard rejection: %r" % (bad,)


@pytest.mark.parametrize(
    "label,cur,run_id",
    [
        ("missing_confirm", FakeCursor(), None),
        ("wrong_db", FakeCursor(db="mergepilot_audit"), None),
        ("shared_cluster", FakeCursor(dbs=["postgres", "mergepilot_m4e_fixture", "prod_db"]), None),
        ("tables_without_marker", FakeCursor(knowledge_exists=True), None),
        ("run_id_mismatch", FakeCursor(marker_rows=[("aaa", SCHEMA_VERSION)]), "bbb"),
        ("marker_but_no_run_id", FakeCursor(marker_rows=[("aaa", SCHEMA_VERSION)]), None),
        ("marker_old_version", FakeCursor(marker_rows=[("aaa", "0")]), "aaa"),
        ("marker_multi_row", FakeCursor(marker_rows=[("aaa", SCHEMA_VERSION), ("bbb", SCHEMA_VERSION)]), "aaa"),
    ],
)
def test_guard_rejects(monkeypatch, label, cur, run_id):
    _set_confirm(monkeypatch, confirm=(label != "missing_confirm"), run_id=run_id)
    with pytest.raises(FixtureRejection):
        _check_guard(cur, run_id)
    _assert_no_destructive(cur)


def test_guard_legit_first_run_allows(monkeypatch):
    _set_confirm(monkeypatch, run_id="newrun")
    cur = FakeCursor()  # fresh: no tables, no marker
    assert _check_guard(cur, "newrun") == "newrun"
    _assert_no_destructive(cur)


def test_guard_legit_rerun_matching_run_id_allows(monkeypatch):
    _set_confirm(monkeypatch, run_id="same")
    cur = FakeCursor(knowledge_exists=True, marker_rows=[("same", SCHEMA_VERSION)])
    assert _check_guard(cur, "same") == "same"
    _assert_no_destructive(cur)
