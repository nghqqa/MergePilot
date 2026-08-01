"""PostgreSQL/pgvector read-only adapter for CaseRetrieval."""
from __future__ import annotations

import math
import re

from ..core import (
    CaseRetrievalError,
    DB_UNAVAILABLE,
    INTERNAL,
    SCHEMA_UNSUPPORTED,
    TIMEOUT_SUB,
)


_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_REQUIRED_COLUMNS = {
    "id",
    "category",
    "severity",
    "issue",
    "fix",
    "embedding",
    "created_at",
    "repo_scope",
    "source_pr_url",
    "source_commit_sha",
    "source_version",
    "embedding_version",
}


class PgVectorAdapter:
    def __init__(self, config):
        self.config = dict(config)
        self._conn = None

    @staticmethod
    def _safe_ident(value, field):
        if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
            raise CaseRetrievalError(INTERNAL, "%s invalid" % field)
        return value

    @staticmethod
    def _remaining_ms(deadline, configured):
        if deadline is None:
            return configured
        deadline.check()
        remaining = max(1, int(deadline.remaining_ms()))
        return min(configured, remaining)

    @staticmethod
    def _is_timeout_error(exc):
        return getattr(exc, "pgcode", None) == "57014"

    def _map_db_error(self, exc, default="database operation failed"):
        if isinstance(exc, CaseRetrievalError):
            raise exc
        if self._is_timeout_error(exc):
            raise CaseRetrievalError(TIMEOUT_SUB, "database timeout")
        raise CaseRetrievalError(DB_UNAVAILABLE, default)

    def _connect(self, deadline=None):
        if self._conn is not None:
            return self._conn
        try:
            import psycopg2
        except ImportError:
            raise CaseRetrievalError(DB_UNAVAILABLE, "driver unavailable")

        timeout_ms = self._remaining_ms(
            deadline, self.config.get("connect_timeout_ms", 5000)
        )
        connect_timeout = max(1, int(math.ceil(timeout_ms / 1000.0)))
        try:
            conn = psycopg2.connect(
                self.config["dsn"], connect_timeout=connect_timeout
            )
            conn.set_session(readonly=True, autocommit=False)
            self._conn = conn
            self._configure_session(deadline)
            self._verify_role()
            self._verify_schema_capability()
            return conn
        except Exception as exc:
            try:
                if self._conn is not None:
                    self._conn.close()
            finally:
                self._conn = None
            self._map_db_error(exc, "connection rejected")

    def _configure_session(self, deadline=None):
        schema = self._safe_ident(self.config["schema"], "schema")
        statement_ms = self._remaining_ms(
            deadline, self.config.get("statement_timeout_ms", 10000)
        )
        lock_ms = min(self.config.get("lock_timeout_ms", 5000), statement_ms)
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false), "
                "set_config('lock_timeout', %s, false), "
                "set_config('search_path', %s, false)",
                ("%dms" % statement_ms, "%dms" % lock_ms, schema),
            )
            self._conn.commit()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._map_db_error(exc, "session configuration failed")
        finally:
            cur.close()

    def _verify_role(self):
        relation = "%s.%s" % (
            self._safe_ident(self.config["schema"], "schema"),
            self._safe_ident(self.config["table"], "table"),
        )
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, "
                "r.rolreplication, r.rolbypassrls, "
                "current_setting('transaction_read_only'), "
                "(extract(epoch FROM current_setting('statement_timeout')::interval) * 1000)::bigint, "
                "(extract(epoch FROM current_setting('lock_timeout')::interval) * 1000)::bigint, "
                "current_setting('search_path'), "
                "has_table_privilege(current_user, %s, 'SELECT'), "
                "has_table_privilege(current_user, %s, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES') "
                "FROM pg_roles r WHERE r.rolname = current_user",
                (relation, relation),
            )
            row = cur.fetchone()
            if row is None:
                raise CaseRetrievalError(INTERNAL, "role missing")
            expected_statement = self.config.get("statement_timeout_ms", 10000)
            expected_lock = min(
                self.config.get("lock_timeout_ms", 5000), expected_statement
            )
            if any(row[index] for index in range(5)):
                raise CaseRetrievalError(INTERNAL, "privileged role rejected")
            if row[5] != "on":
                raise CaseRetrievalError(INTERNAL, "read-only missing")
            if row[6] <= 0 or row[6] > expected_statement:
                raise CaseRetrievalError(INTERNAL, "statement timeout invalid")
            if row[7] <= 0 or row[7] > expected_lock:
                raise CaseRetrievalError(INTERNAL, "lock timeout invalid")
            if row[8] != self.config["schema"]:
                raise CaseRetrievalError(INTERNAL, "search path invalid")
            if row[9] is not True or row[10] is not False:
                raise CaseRetrievalError(INTERNAL, "table privileges invalid")
            self._conn.rollback()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._map_db_error(exc, "role verification failed")
        finally:
            cur.close()

    def _verify_schema_capability(self):
        schema = self._safe_ident(self.config["schema"], "schema")
        table = self._safe_ident(self.config["table"], "table")
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            columns = {row[0] for row in cur.fetchall()}
            if not _REQUIRED_COLUMNS.issubset(columns):
                raise CaseRetrievalError(
                    SCHEMA_UNSUPPORTED, "required columns missing"
                )
            self._conn.rollback()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._map_db_error(exc, "schema verification failed")
        finally:
            cur.close()

    def _prepare_transaction(self, deadline=None):
        conn = self._connect(deadline)
        statement_ms = self._remaining_ms(
            deadline, self.config.get("statement_timeout_ms", 10000)
        )
        lock_ms = min(self.config.get("lock_timeout_ms", 5000), statement_ms)
        cur = conn.cursor()
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)",
                ("%dms" % statement_ms, "%dms" % lock_ms),
            )
        finally:
            cur.close()
        return conn

    @staticmethod
    def _vector_literal(query_vec):
        values = []
        for value in query_vec:
            number = float(value)
            if not math.isfinite(number):
                raise CaseRetrievalError(INTERNAL, "vector invalid")
            values.append(format(number, ".9g"))
        return "[" + ",".join(values) + "]"

    def retrieve(
        self,
        *,
        query_vec,
        repo_scope,
        top_k,
        min_score,
        filters,
        schema,
        table,
        deadline=None,
    ):
        conn = self._prepare_transaction(deadline)
        schema = self._safe_ident(schema, "schema")
        table = self._safe_ident(table, "table")
        vec_str = self._vector_literal(query_vec)

        conditions = ["repo_scope = %s"]
        params = [repo_scope]
        if filters.get("category"):
            conditions.append("category = %s")
            params.append(filters["category"])
        if filters.get("severity"):
            conditions.append("severity = %s")
            params.append(filters["severity"])
        where = " AND ".join(conditions)
        fetch_k = min(top_k * 3, 60)

        sql = (
            "WITH ranked AS ("
            " SELECT id, task_id, finding_id, category, severity, issue, fix, file, source,"
            " repo_scope, source_pr_url, source_commit_sha, source_version,"
            " embedding_model, embedding_version,"
            " adopted, created_at, round((1 - (embedding OPERATOR(public.<=>) %s::public.vector))::numeric, 6) AS score"
            " FROM %s.%s WHERE %s"
            "), filtered AS (SELECT * FROM ranked WHERE score >= %s)"
            " SELECT filtered.*, count(*) OVER() AS total_found"
            " FROM filtered ORDER BY score DESC, created_at DESC, "
            "(COALESCE(NULLIF(id::text, ''), NULLIF(finding_id, ''), "
            "NULLIF(task_id, ''), 'unknown') COLLATE \"C\") ASC LIMIT %s"
        ) % ("%s", schema, table, where, "%s", "%s")
        query_params = [vec_str] + params + [min_score, fetch_k]

        cur = conn.cursor()
        try:
            cur.execute(sql, query_params)
            rows = cur.fetchall()
            names = [desc[0] for desc in cur.description]
            mapped = [dict(zip(names, row)) for row in rows]
            total_found = int(mapped[0].pop("total_found")) if mapped else 0
            for row in mapped[1:]:
                row.pop("total_found", None)
            conn.rollback()
            return {"rows": mapped, "total_found": total_found}
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self._map_db_error(exc, "retrieval failed")
        finally:
            cur.close()

    def stats(self, repo_scope, schema, table, deadline=None):
        conn = self._prepare_transaction(deadline)
        schema = self._safe_ident(schema, "schema")
        table = self._safe_ident(table, "table")
        sql = (
            "SELECT count(*)::bigint AS knowledge_base_size, "
            "count(*) FILTER (WHERE "
            " (source_pr_url ~ '^https://[^/@[:space:][:cntrl:]]+(/[^[:space:][:cntrl:]]*)?$') OR "
            " (lower(source_commit_sha) ~ '^[0-9a-f]{40}$')"
            ")::bigint AS trusted_available "
            "FROM %s.%s WHERE repo_scope = %%s"
        ) % (schema, table)
        cur = conn.cursor()
        try:
            cur.execute(sql, (repo_scope,))
            row = cur.fetchone()
            conn.rollback()
            if row is None:
                raise CaseRetrievalError(DB_UNAVAILABLE, "stats missing")
            return {
                "knowledge_base_size": int(row[0]),
                "trusted_available": int(row[1]),
            }
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self._map_db_error(exc, "stats failed")
        finally:
            cur.close()

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
