"""Static safety contract for the forward-only migration."""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATION = os.path.join(
    ROOT,
    "skills",
    "case_retrieval",
    "migrations",
    "001_case_retrieval_scope.sql",
)


def sql_text():
    with open(MIGRATION, encoding="utf-8") as handle:
        return handle.read()


def executable_lines():
    return "\n".join(
        line for line in sql_text().splitlines() if not line.lstrip().startswith("--")
    )


def test_migration_file_exists():
    assert os.path.isfile(MIGRATION)


def test_migration_has_no_destructive_statement():
    sql = executable_lines()
    assert not re.search(r"(?im)^\s*(DROP|TRUNCATE|DELETE)\b", sql)


def test_migration_is_forward_only_and_idempotent():
    sql = executable_lines().upper()
    assert "ADD COLUMN IF NOT EXISTS REPO_SCOPE" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "IF NOT EXISTS" in sql


def test_migration_preserves_unscoped_legacy_rows():
    sql = executable_lines().upper()
    assert not re.search(r"(?im)^\s*UPDATE\s+PUBLIC\.KNOWLEDGE\b", sql)
    assert "DEFAULT" not in next(
        line for line in sql.splitlines() if "REPO_SCOPE" in line and "ADD COLUMN" in line
    )


def test_migration_contains_frozen_metadata_columns():
    sql = executable_lines().lower()
    for column in (
        "repo_scope",
        "source_pr_url",
        "source_commit_sha",
        "source_version",
        "embedding_model",
        "embedding_version",
        "adopted",
    ):
        assert "add column if not exists %s" % column in sql


def test_migration_database_name_is_deploy_neutral():
    sql = executable_lines()
    assert "mergepilot_audit" not in sql
    assert "current_database()" in sql


def test_migration_converges_role_attributes():
    sql = executable_lines().upper()
    for attribute in (
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert attribute in sql


def test_migration_revokes_target_writes_then_grants_select():
    sql = executable_lines().upper()
    assert "REVOKE ALL PRIVILEGES ON TABLE PUBLIC.KNOWLEDGE" in sql
    assert "GRANT SELECT ON TABLE PUBLIC.KNOWLEDGE" in sql
    assert "REVOKE CREATE ON SCHEMA PUBLIC" in sql


def test_migration_sets_readonly_limits_and_search_path():
    sql = executable_lines().lower()
    assert "default_transaction_read_only = on" in sql
    assert "statement_timeout = '10s'" in sql
    assert "lock_timeout = '5s'" in sql
    assert "search_path = public" in sql


def test_migration_contains_no_password_literal():
    assert "password" not in executable_lines().lower()
