"""Framework-neutral CaseRetrieval orchestration.

The core is advisory and read-only.  Repository scope and all dependency
configuration are deploy-owned.  Returned rows are checked again at the
trust boundary even though the adapter is required to scope its query.
"""
from __future__ import annotations

import inspect
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit


SCHEMA_VERSION = "1"

INVALID_INPUT = "CASE_RETR_INVALID_INPUT"
SCOPE_MISSING = "CASE_RETR_SCOPE_MISSING"
SCHEMA_UNSUPPORTED = "CASE_RETR_SCHEMA_UNSUPPORTED"
DB_UNAVAILABLE = "CASE_RETR_DB_UNAVAILABLE"
MODEL_UNAVAILABLE = "CASE_RETR_MODEL_UNAVAILABLE"
TIMEOUT_SUB = "CASE_RETR_TIMEOUT"
DIMENSION_MISMATCH = "CASE_RETR_DIMENSION_MISMATCH"
VERSION_MISMATCH = "CASE_RETR_VERSION_MISMATCH"
CITATION_INVALID = "CASE_RETR_CITATION_INVALID"
INTERNAL = "CASE_RETR_INTERNAL"

MAX_QUERY_CHARS = 500
MAX_QUERY_BYTES = 2048
DEFAULT_TOP_K = 5
MAX_TOP_K = 20
SUMMARY_MAX = 500
SCORE_DECIMALS = 6
EMBEDDING_DIM = 384

DEGRADED_STALE = "STALE_EMBEDDING_VERSION"
DEGRADED_CITATION = "CITATION_UNVERIFIED"

_RE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_RE_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RE_SHA = re.compile(r"^[0-9a-f]{40}$")


class CaseRetrievalError(Exception):
    def __init__(self, subcode: str, detail: str = "", retryable: bool = False,
                 pgcode: str | None = None):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail
        self.retryable = retryable
        self.pgcode = pgcode


def _check_deadline(deadline) -> None:
    if deadline is not None:
        deadline.check()


def _remaining_ms(deadline) -> int | None:
    if deadline is None:
        return None
    remaining = int(deadline.remaining_ms())
    if remaining <= 0:
        deadline.check()
    return max(1, remaining)


def _normalize_query(raw) -> str:
    if not isinstance(raw, str):
        raise CaseRetrievalError(INVALID_INPUT, "query must be string")
    text = unicodedata.normalize("NFC", raw)
    if not text or not text.strip() or not any(
        c.isprintable() and not c.isspace() for c in text
    ):
        raise CaseRetrievalError(INVALID_INPUT, "query empty or control-only")
    if len(text) > MAX_QUERY_CHARS:
        raise CaseRetrievalError(INVALID_INPUT, "query too long")
    if len(text.encode("utf-8")) > MAX_QUERY_BYTES:
        raise CaseRetrievalError(INVALID_INPUT, "query byte limit exceeded")
    return text


def _validate_identifier(value, field: str) -> str:
    if not isinstance(value, str) or not _RE_IDENT.fullmatch(value):
        raise CaseRetrievalError(INVALID_INPUT, "%s invalid" % field)
    return value


def _positive_int(value, field: str, default: int, maximum: int) -> int:
    raw = default if value in (None, "") else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        raise CaseRetrievalError(INVALID_INPUT, "%s invalid" % field)
    if parsed < 1 or parsed > maximum:
        raise CaseRetrievalError(INVALID_INPUT, "%s invalid" % field)
    return parsed


def load_trusted_config(env=None) -> dict:
    e = os.environ if env is None else env
    dsn = e.get("MERGEPILOT_CR_PG_DSN")
    if not dsn:
        raise CaseRetrievalError(DB_UNAVAILABLE, "dsn missing")
    scope = e.get("MERGEPILOT_CR_REPO_SCOPE")
    if not scope:
        raise CaseRetrievalError(SCOPE_MISSING, "scope missing")
    if len(scope) > 256 or any(ord(c) < 32 for c in scope):
        raise CaseRetrievalError(SCOPE_MISSING, "scope invalid")

    model = e.get("MERGEPILOT_CR_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    version = e.get("MERGEPILOT_CR_EMBEDDING_VERSION", "1.0.0")
    if not isinstance(model, str) or not model or len(model) > 200:
        raise CaseRetrievalError(INVALID_INPUT, "model config invalid")
    if not isinstance(version, str) or not _RE_SEMVER.fullmatch(version):
        raise CaseRetrievalError(INVALID_INPUT, "version config invalid")

    return {
        "dsn": dsn,
        "scope": scope,
        "model": model,
        "version": version,
        "schema": _validate_identifier(
            e.get("MERGEPILOT_CR_DB_SCHEMA", "public"), "schema"
        ),
        "table": _validate_identifier(
            e.get("MERGEPILOT_CR_DB_TABLE", "knowledge"), "table"
        ),
        "connect_timeout_ms": _positive_int(
            e.get("MERGEPILOT_CR_CONNECT_TIMEOUT_MS"),
            "connect timeout",
            5000,
            60000,
        ),
        "statement_timeout_ms": _positive_int(
            e.get("MERGEPILOT_CR_STATEMENT_TIMEOUT_MS"),
            "statement timeout",
            10000,
            60000,
        ),
        "lock_timeout_ms": _positive_int(
            e.get("MERGEPILOT_CR_LOCK_TIMEOUT_MS"),
            "lock timeout",
            5000,
            60000,
        ),
    }


def _summarize(value, limit: int = SUMMARY_MAX) -> str:
    text = "" if value is None else str(value)
    text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _valid_https_url(value) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return value


def _build_citation(row: dict) -> tuple[dict, bool]:
    url = _valid_https_url(row.get("source_pr_url"))
    sha_raw = row.get("source_commit_sha")
    sha = str(sha_raw).lower() if sha_raw is not None else ""
    sha = sha if _RE_SHA.fullmatch(sha) else ""

    if url:
        return {
            "source_id": _summarize(url, 512),
            "source_type": "pr",
            "source_url": url,
            "verifiable": True,
        }, True
    if sha:
        return {
            "source_id": sha,
            "source_type": "commit",
            "source_url": None,
            "verifiable": True,
        }, True

    finding = _summarize(row.get("finding_id"), 128)
    if finding:
        return {
            "source_id": finding,
            "source_type": "finding",
            "source_url": None,
            "verifiable": False,
        }, False
    return {
        "source_id": "unknown",
        "source_type": "unknown",
        "source_url": None,
        "verifiable": False,
    }, False


def _parse_created_at(value) -> tuple[str, float]:
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value or len(value) > 64:
            raise CaseRetrievalError(INTERNAL, "created_at invalid")
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            raise CaseRetrievalError(INTERNAL, "created_at invalid")
    if parsed.tzinfo is None:
        raise CaseRetrievalError(INTERNAL, "created_at timezone missing")
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z"), utc.timestamp()


def _normalize_row(row: dict, scope: str, trusted_version: str) -> dict:
    if not isinstance(row, dict) or row.get("repo_scope") != scope:
        raise CaseRetrievalError(INTERNAL, "cross-scope row")

    try:
        score = float(row.get("score"))
    except (TypeError, ValueError):
        raise CaseRetrievalError(INTERNAL, "score invalid")
    if not math.isfinite(score) or score < 0 or score > 1:
        raise CaseRetrievalError(INTERNAL, "score invalid")

    created_at, created_ts = _parse_created_at(row.get("created_at"))
    citation, verifiable = _build_citation(row)
    embedding_version = str(row.get("embedding_version") or "0.0.0")
    source_version = str(row.get("source_version") or embedding_version)
    case_id = _summarize(
        row.get("id") or row.get("finding_id") or row.get("task_id") or "unknown",
        128,
    )
    if not case_id:
        raise CaseRetrievalError(INTERNAL, "case id invalid")

    return {
        "case_id": case_id,
        "score": round(score, SCORE_DECIMALS),
        "category": _summarize(row.get("category") or "unknown", 64),
        "severity": _summarize(row.get("severity") or "unknown", 32),
        "issue_summary": _summarize(row.get("issue")),
        "fix_summary": _summarize(row.get("fix")),
        "citation": citation,
        "source_version": _summarize(source_version, 64),
        "created_at": created_at,
        "stale": embedding_version != trusted_version,
        "untrusted": True,
        "_created_ts": created_ts,
    }


def _sort_key(result: dict):
    return (-result["score"], -result["_created_ts"], result["case_id"])


def _invoke_with_deadline(callable_obj, *args, deadline=None, **kwargs):
    """Pass the cooperative deadline when the narrow interface supports it."""
    _check_deadline(deadline)
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        params = {}
    if "deadline" in params:
        kwargs["deadline"] = deadline
    result = callable_obj(*args, **kwargs)
    _check_deadline(deadline)
    return result


def run(inp, *, adapter=None, embedding_provider=None, trusted_env=None, deadline=None):
    _check_deadline(deadline)
    if not isinstance(inp, dict):
        raise CaseRetrievalError(INVALID_INPUT, "input invalid")
    config = load_trusted_config(trusted_env)
    query = _normalize_query(inp.get("query"))

    top_k_raw = inp.get("top_k", DEFAULT_TOP_K)
    if isinstance(top_k_raw, bool):
        raise CaseRetrievalError(INVALID_INPUT, "top_k invalid")
    try:
        top_k = int(top_k_raw)
    except (TypeError, ValueError):
        raise CaseRetrievalError(INVALID_INPUT, "top_k invalid")
    if top_k < 1 or top_k > MAX_TOP_K:
        raise CaseRetrievalError(INVALID_INPUT, "top_k invalid")

    try:
        min_score = float(inp.get("min_score", 0))
    except (TypeError, ValueError):
        raise CaseRetrievalError(INVALID_INPUT, "min_score invalid")
    if not math.isfinite(min_score) or min_score < 0 or min_score > 1:
        raise CaseRetrievalError(INVALID_INPUT, "min_score invalid")

    filters = inp.get("filters") or {}
    expected_version = inp.get("expected_embedding_version")
    if expected_version and expected_version != config["version"]:
        raise CaseRetrievalError(VERSION_MISMATCH, "version mismatch")

    if embedding_provider is None:
        from .embedding.fastembed_provider import FastEmbedProvider

        embedding_provider = FastEmbedProvider(config["model"], config["version"])

    query_vec = _invoke_with_deadline(
        embedding_provider.embed, query, deadline=deadline
    )
    if not isinstance(query_vec, (list, tuple)) or len(query_vec) != EMBEDDING_DIM:
        raise CaseRetrievalError(DIMENSION_MISMATCH, "dimension mismatch")
    if any(not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in query_vec):
        raise CaseRetrievalError(DIMENSION_MISMATCH, "vector invalid")

    owned_adapter = adapter is None
    if owned_adapter:
        from .adapters.pg_vector import PgVectorAdapter

        adapter = PgVectorAdapter(config)

    primary_error = None
    try:
        batch = _invoke_with_deadline(
            adapter.retrieve,
            query_vec=query_vec,
            repo_scope=config["scope"],
            top_k=top_k,
            min_score=min_score,
            filters=filters,
            schema=config["schema"],
            table=config["table"],
            deadline=deadline,
        )
        if isinstance(batch, dict):
            raw_rows = batch.get("rows")
            total_found = batch.get("total_found")
        else:
            raw_rows = batch
            total_found = len(batch) if isinstance(batch, list) else None
        if not isinstance(raw_rows, list) or not isinstance(total_found, int) or total_found < 0:
            raise CaseRetrievalError(INTERNAL, "adapter result invalid")

        results = [_normalize_row(row, config["scope"], config["version"]) for row in raw_rows]
        results.sort(key=_sort_key)
        results = results[:top_k]
        for result in results:
            result.pop("_created_ts", None)

        stats_method = getattr(adapter, "stats", None)
        if stats_method is not None:
            stats = _invoke_with_deadline(
                stats_method,
                config["scope"],
                config["schema"],
                config["table"],
                deadline=deadline,
            )
            if not isinstance(stats, dict):
                raise CaseRetrievalError(INTERNAL, "adapter stats invalid")
            knowledge_base_size = stats.get("knowledge_base_size")
            trusted_available = stats.get("trusted_available")
        else:
            count_method = getattr(adapter, "count", None)
            trusted_method = getattr(adapter, "trusted_count", None)
            knowledge_base_size = (
                _invoke_with_deadline(
                    count_method,
                    config["scope"],
                    config["schema"],
                    config["table"],
                    deadline=deadline,
                )
                if count_method
                else total_found
            )
            trusted_available = (
                _invoke_with_deadline(
                    trusted_method,
                    config["scope"],
                    config["schema"],
                    config["table"],
                    deadline=deadline,
                )
                if trusted_method
                else sum(1 for r in results if r["citation"]["verifiable"])
            )
        if (
            not isinstance(knowledge_base_size, int)
            or knowledge_base_size < 0
            or not isinstance(trusted_available, int)
            or trusted_available < 0
        ):
            raise CaseRetrievalError(INTERNAL, "adapter stats invalid")

        degraded = []
        if any(result["stale"] for result in results):
            degraded.append({"type": "stale", "reason": DEGRADED_STALE})
        if any(not result["citation"]["verifiable"] for result in results):
            degraded.append({"type": "untrusted", "reason": DEGRADED_CITATION})

        return {
            "schema_version": SCHEMA_VERSION,
            "embedding_model": config["model"],
            "embedding_version": config["version"],
            "complete": True,
            "results": results,
            "stats": {
                "total_found": total_found,
                "returned": len(results),
                "trusted_available": trusted_available,
                "repo_scope": config["scope"],
                "knowledge_base_size": knowledge_base_size,
            },
            "degraded": degraded,
        }
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        if owned_adapter and adapter is not None:
            try:
                adapter.close()
            except Exception:
                if primary_error is None:
                    raise CaseRetrievalError(INTERNAL, "adapter close failed")
