"""Production PostgreSQL adapter contract tests."""
from __future__ import annotations

import pytest

from skills.case_retrieval import core
from skills.case_retrieval.adapters.pg_vector import PgVectorAdapter, _REQUIRED_COLUMNS


class FakePgError(Exception):
    def __init__(self, pgcode=None):
        super().__init__("sensitive database detail")
        self.pgcode = pgcode


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.description = None

    def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise FakePgError(self.connection.fail_code)
        if "FROM pg_roles" in sql:
            self.rows = [self.connection.role_row]
        elif "information_schema.columns" in sql:
            self.rows = [(name,) for name in self.connection.columns]
        elif "WITH ranked AS" in sql:
            names = [
                "id", "task_id", "finding_id", "category", "severity",
                "issue", "fix", "file", "source", "repo_scope",
                "source_pr_url", "source_commit_sha", "source_version",
                "embedding_model", "embedding_version", "adopted",
                "created_at", "score", "total_found",
            ]
            self.description = [(name,) for name in names]
            self.rows = [tuple(row.get(name) for name in names) for row in self.connection.result_rows]
        elif "knowledge_base_size" in sql:
            self.rows = [self.connection.stats_row]
        else:
            self.rows = [("ok",)]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.connection.cursor_closes += 1


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.cursor_closes = 0
        self.rollbacks = 0
        self.commits = 0
        self.closed = False
        self.fail_on = None
        self.fail_code = None
        self.columns = set(_REQUIRED_COLUMNS)
        self.role_row = (
            False, False, False, False, False,
            "on", 10000, 5000, "public", True, False,
        )
        self.stats_row = (7, 3)
        self.result_rows = []

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True

    def set_session(self, **kwargs):
        self.session = kwargs


def config(schema="public"):
    return {
        "dsn": "postgresql://redacted",
        "schema": schema,
        "table": "knowledge",
        "connect_timeout_ms": 5000,
        "statement_timeout_ms": 10000,
        "lock_timeout_ms": 5000,
    }


def adapter_with(connection, schema="public"):
    adapter = PgVectorAdapter(config(schema))
    adapter._conn = connection
    return adapter


def test_adapter_parameterizes_scope_filters_vector_and_limit():
    conn = FakeConnection()
    conn.result_rows = [{
        "id": 1, "task_id": "t", "finding_id": "f", "category": "security",
        "severity": "high", "issue": "i", "fix": "f", "file": "x",
        "source": "test", "repo_scope": "repo-alpha",
        "source_pr_url": "https://example.test/pull/1",
        "source_commit_sha": None, "source_version": "v1",
        "embedding_model": "m", "embedding_version": "1.0.0",
        "adopted": False, "created_at": "2026-01-01T00:00:00+00:00",
        "score": 0.8, "total_found": 4,
    }]
    adapter = adapter_with(conn)
    out = adapter.retrieve(
        query_vec=[0.0] * 384,
        repo_scope="repo-alpha",
        top_k=3,
        min_score=0.2,
        filters={"category": "security", "severity": "high"},
        schema="public",
        table="knowledge",
    )
    assert out["total_found"] == 4
    assert len(out["rows"]) == 1
    sql, params = next(call for call in conn.calls if "WITH ranked AS" in call[0])
    assert "repo_scope = %s" in sql
    assert "repo-alpha" not in sql
    assert "repo-alpha" in params
    assert "security" in params and "high" in params
    assert "ORDER BY score DESC, created_at DESC" in sql
    # Tie-break mirrors core's string case_id order (COLLATE "C"), not the raw
    # integer id, so the database LIMIT window is a superset of core's top-k.
    assert 'COLLATE "C"' in sql
    assert "id ASC" not in sql


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_adapter_rejects_privileged_role(index):
    conn = FakeConnection()
    values = list(conn.role_row)
    values[index] = True
    conn.role_row = tuple(values)
    with pytest.raises(core.CaseRetrievalError) as raised:
        adapter_with(conn)._verify_role()
    assert raised.value.subcode == core.INTERNAL


@pytest.mark.parametrize(
    "index,value",
    [(5, "off"), (6, 0), (6, 10001), (7, 0), (7, 5001), (8, "evil"), (9, False), (10, True)],
)
def test_adapter_rejects_bad_readonly_timeout_path_or_privileges(index, value):
    conn = FakeConnection()
    values = list(conn.role_row)
    values[index] = value
    conn.role_row = tuple(values)
    with pytest.raises(core.CaseRetrievalError) as raised:
        adapter_with(conn)._verify_role()
    assert raised.value.subcode == core.INTERNAL


def test_adapter_requires_all_schema_columns():
    conn = FakeConnection()
    conn.columns.remove("repo_scope")
    with pytest.raises(core.CaseRetrievalError) as raised:
        adapter_with(conn)._verify_schema_capability()
    assert raised.value.subcode == core.SCHEMA_UNSUPPORTED


def test_adapter_stats_are_distinct_and_scoped():
    conn = FakeConnection()
    out = adapter_with(conn).stats("repo-alpha", "public", "knowledge")
    assert out == {"knowledge_base_size": 7, "trusted_available": 3}
    sql, params = next(call for call in conn.calls if "knowledge_base_size" in call[0])
    assert "repo_scope = %s" in sql
    assert params == ("repo-alpha",)
    assert "[^/@[:space:][:cntrl:]]+" in sql


def test_adapter_stats_failure_is_not_zero_success():
    conn = FakeConnection()
    conn.fail_on = "knowledge_base_size"
    with pytest.raises(core.CaseRetrievalError) as raised:
        adapter_with(conn).stats("repo-alpha", "public", "knowledge")
    assert raised.value.subcode == core.DB_UNAVAILABLE


def test_adapter_maps_query_cancel_to_timeout():
    conn = FakeConnection()
    conn.fail_on = "WITH ranked AS"
    conn.fail_code = "57014"
    with pytest.raises(core.CaseRetrievalError) as raised:
        adapter_with(conn).retrieve(
            query_vec=[0.0] * 384,
            repo_scope="repo-alpha",
            top_k=3,
            min_score=0,
            filters={},
            schema="public",
            table="knowledge",
        )
    assert raised.value.subcode == core.TIMEOUT_SUB
    assert raised.value.pgcode == "57014"


def test_adapter_rejects_unsafe_identifier():
    with pytest.raises(core.CaseRetrievalError):
        PgVectorAdapter._safe_ident("public;drop", "schema")


def test_adapter_close_is_idempotent():
    conn = FakeConnection()
    adapter = adapter_with(conn)
    adapter.close()
    adapter.close()
    assert conn.closed is True
    assert adapter._conn is None


def test_retrieve_sql_tie_break_mirrors_core_string_case_id():
    conn = FakeConnection()
    conn.result_rows = []
    adapter_with(conn).retrieve(
        query_vec=[0.0] * 384,
        repo_scope="repo-alpha",
        top_k=5,
        min_score=0,
        filters={},
        schema="public",
        table="knowledge",
    )
    sql = next(call[0] for call in conn.calls if "WITH ranked AS" in call[0])
    assert 'COLLATE "C"' in sql
    assert "id ASC" not in sql
    # Mirrors core._normalize_row case_id derivation: id -> finding_id -> task_id.
    assert "COALESCE(NULLIF(id::text" in sql
    assert "finding_id" in sql and "task_id" in sql
