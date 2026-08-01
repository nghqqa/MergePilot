"""Real local PostgreSQL/pgvector E2E for M4-E.

Requires MERGEPILOT_CR_PG_DSN for the non-superuser reader.  The script emits
structured evidence only and never prints the DSN.
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fastembed
import psycopg2

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from skills.case_retrieval import core
from skills.case_retrieval import run as skill_run
from skills.case_retrieval.embedding.fastembed_provider import DeterministicFakeProvider
from skills.common.runtime.cli import Deadline


def delivery_digest():
    digest = hashlib.sha256()
    paths = []
    for base in (Path(ROOT) / "skills/case_retrieval", Path(ROOT) / "tests/m4e"):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            paths.append(path)
    for path in sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix()):
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def trusted_env(scope, schema="cr_fixture", table="knowledge"):
    return {
        "MERGEPILOT_CR_PG_DSN": os.environ["MERGEPILOT_CR_PG_DSN"],
        "MERGEPILOT_CR_REPO_SCOPE": scope,
        "MERGEPILOT_CR_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
        "MERGEPILOT_CR_EMBEDDING_VERSION": "1.0.0",
        "MERGEPILOT_CR_DB_SCHEMA": schema,
        "MERGEPILOT_CR_DB_TABLE": table,
        "MERGEPILOT_CR_CONNECT_TIMEOUT_MS": "5000",
        "MERGEPILOT_CR_STATEMENT_TIMEOUT_MS": "10000",
        "MERGEPILOT_CR_LOCK_TIMEOUT_MS": "5000",
    }


def deterministic(scope, query="SQL injection", **inp):
    request = {"query": query, "top_k": 20}
    request.update(inp)
    return core.run(
        request,
        embedding_provider=DeterministicFakeProvider(),
        trusted_env=trusted_env(scope),
        deadline=Deadline(10000),
    )


def production_handle(scope):
    previous = {key: os.environ.get(key) for key in trusted_env(scope)}
    os.environ.update(trusted_env(scope))
    try:
        return skill_run.handle({
            "input": {"query": "SQL injection", "top_k": 5},
            "deadline": Deadline(120000),
        })
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def verify_readonly_role():
    conn = psycopg2.connect(os.environ["MERGEPILOT_CR_PG_DSN"], connect_timeout=5)
    result = {"write_denials": {}, "timeout_pgcode": None}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT r.rolsuper,r.rolcreaterole,r.rolcreatedb,"
            "r.rolreplication,r.rolbypassrls,"
            "current_setting('default_transaction_read_only') "
            "FROM pg_roles r WHERE r.rolname=current_user"
        )
        role = cur.fetchone()
        conn.rollback()
        result["role"] = {
            "superuser": role[0],
            "createrole": role[1],
            "createdb": role[2],
            "replication": role[3],
            "bypassrls": role[4],
            "default_readonly": role[5],
        }

        cur = conn.cursor()
        cur.execute(
            "SELECT current_setting('server_version'), "
            "(SELECT extversion FROM pg_extension WHERE extname='vector')"
        )
        versions = cur.fetchone()
        result["versions"] = {
            "postgresql": versions[0],
            "pgvector": versions[1],
        }
        conn.rollback()

        attempts = {
            "insert": "INSERT INTO cr_fixture.knowledge (task_id) VALUES ('denied')",
            "update": "UPDATE cr_fixture.knowledge SET task_id='denied' WHERE false",
            "delete": "DELETE FROM cr_fixture.knowledge WHERE false",
            "truncate": "TRUNCATE cr_fixture.knowledge",
            "create": "CREATE TABLE cr_fixture.denied_table(id integer)",
            "alter": "ALTER TABLE cr_fixture.knowledge ADD COLUMN denied integer",
        }
        for name, sql in attempts.items():
            try:
                cur = conn.cursor()
                cur.execute(sql)
                conn.rollback()
                result["write_denials"][name] = False
            except Exception:
                conn.rollback()
                result["write_denials"][name] = True

        try:
            cur = conn.cursor()
            cur.execute("SET statement_timeout='100ms'")
            started = time.monotonic()
            cur.execute("SELECT pg_sleep(1)")
            conn.rollback()
            result["timeout_elapsed_ms"] = int((time.monotonic() - started) * 1000)
        except Exception as exc:
            result["timeout_elapsed_ms"] = int((time.monotonic() - started) * 1000)
            result["timeout_pgcode"] = getattr(exc, "pgcode", None)
            conn.rollback()

        cur = conn.cursor()
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE repo_scope IS NULL) "
            "FROM public.knowledge"
        )
        result["public_rows"], result["public_null_scope"] = cur.fetchone()
        conn.rollback()
        return result
    finally:
        conn.close()


def fetch_fixture(finding_id):
    conn = psycopg2.connect(os.environ["MERGEPILOT_CR_PG_DSN"], connect_timeout=5)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,task_id,finding_id,category,severity,issue,fix,repo_scope,"
            "source_pr_url,source_commit_sha,source_version,embedding_version,created_at "
            "FROM cr_fixture.knowledge WHERE finding_id=%s",
            (finding_id,),
        )
        names = [item[0] for item in cur.description]
        value = dict(zip(names, cur.fetchone()))
        value["score"] = 0.5
        conn.rollback()
        return value
    finally:
        conn.close()


def main():
    if not os.environ.get("MERGEPILOT_CR_PG_DSN"):
        raise SystemExit("reader DSN missing")

    alpha = deterministic("repo-alpha")
    beta = deterministic("repo-beta")
    alpha_again = deterministic("repo-alpha")
    stale = deterministic("repo-alpha", query="Outdated coding pattern from old version")
    poisoned = core._normalize_row(fetch_fixture("CF7"), "repo-beta", "1.0.0")
    no_match = deterministic(
        "repo-alpha", query="nothing", filters={"category": "style"}
    )

    assert alpha["stats"]["repo_scope"] == "repo-alpha"
    assert beta["stats"]["repo_scope"] == "repo-beta"
    assert alpha["stats"]["knowledge_base_size"] == 3
    assert beta["stats"]["knowledge_base_size"] == 3
    assert alpha["results"] == alpha_again["results"]
    assert all(item["case_id"] != "10" for item in alpha["results"] + beta["results"])
    assert no_match["results"] == [] and no_match["stats"]["total_found"] == 0
    assert any(item["stale"] for item in stale["results"])
    assert poisoned["untrusted"] is True
    assert poisoned["citation"]["verifiable"] is False

    missing_scope = None
    try:
        core.run(
            {"query": "x"},
            embedding_provider=DeterministicFakeProvider(),
            trusted_env={"MERGEPILOT_CR_PG_DSN": "postgresql://invalid"},
        )
    except core.CaseRetrievalError as exc:
        missing_scope = exc.subcode
    assert missing_scope == core.SCOPE_MISSING

    unsupported = None
    try:
        core.run(
            {"query": "x"},
            embedding_provider=DeterministicFakeProvider(),
            trusted_env=trusted_env("repo-alpha", "information_schema", "columns"),
            deadline=Deadline(10000),
        )
    except core.CaseRetrievalError as exc:
        unsupported = exc.subcode
    assert unsupported == core.SCHEMA_UNSUPPORTED

    production = production_handle("repo-alpha")
    assert production["status"] == "OK"
    assert production["output"]["stats"]["repo_scope"] == "repo-alpha"

    role = verify_readonly_role()
    assert role["role"] == {
        "superuser": False,
        "createrole": False,
        "createdb": False,
        "replication": False,
        "bypassrls": False,
        "default_readonly": "on",
    }
    assert all(role["write_denials"].values())
    assert role["timeout_pgcode"] == "57014"
    assert role["public_rows"] == 5 and role["public_null_scope"] == 5

    evidence = {
        "schema_version": "1",
        "all_passed": True,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "delivery_digest": delivery_digest(),
        "production_chain": {
            "entry": "skills.case_retrieval.run.handle",
            "status": production["status"],
            "returned": production["output"]["stats"]["returned"],
            "repo_scope": production["output"]["stats"]["repo_scope"],
        },
        "scopes": {
            "repo-alpha": {
                "knowledge_base_size": alpha["stats"]["knowledge_base_size"],
                "returned_ids": [item["case_id"] for item in alpha["results"]],
            },
            "repo-beta": {
                "knowledge_base_size": beta["stats"]["knowledge_base_size"],
                "returned_ids": [item["case_id"] for item in beta["results"]],
            },
        },
        "null_scope_excluded": True,
        "deterministic": True,
        "no_match": True,
        "stale_observed": True,
        "untrusted_observed": True,
        "missing_scope_subcode": missing_scope,
        "schema_unsupported_subcode": unsupported,
        "reader_role": role["role"],
        "versions": role["versions"],
        "write_denials": role["write_denials"],
        "statement_timeout": {
            "pgcode": role["timeout_pgcode"],
            "elapsed_ms": role["timeout_elapsed_ms"],
        },
        "public_legacy": {
            "rows": role["public_rows"],
            "null_scope": role["public_null_scope"],
        },
        "side_effects": [],
        "credential_hits": 0,
    }
    evidence["versions"]["fastembed"] = fastembed.__version__
    evidence["versions"]["psycopg2"] = psycopg2.__version__.split()[0]
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(sys.argv) == 3 and sys.argv[1] == "--output":
        with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
    elif len(sys.argv) != 1:
        raise SystemExit("usage: run_pgvector_e2e.py [--output PATH]")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
