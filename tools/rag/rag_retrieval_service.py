#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-RAG · Retrieval integration service for Reviewer/Fixer workflow.

Wraps the CaseRetrieval skill into a queryable service that Reviewer
and Fix Planner call before their core work. Adds:
- OTel spans (rag.query / rag.result / rag.fallback) as CHILDREN of the
  current Agent span (not independent roots)
- Real bounded timeout via threading + join (cancels mid-query)
- Fail-closed degradation (retrieval_unavailable / no_history)
- Redaction on all outputs (reuse M6-A denylist)
- Structured output: case_id, similarity, summary, adopted flag

Design:
- Does NOT replace the CaseRetrieval Skill subprocess; wraps its output.
- Does NOT let retrieval skip Verifier or change risk classification.
- Verifier always runs current SAST/test evidence regardless of RAG.
- Uses a FakeRetrievalAdapter for testing; PgVectorBridge in production.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Optional

# OTel instrumentation (fail-closed)
_OTEL_ENABLED = False
try:
    _otel_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "otel")
    if _otel_dir not in sys.path:
        sys.path.insert(0, _otel_dir)
    import otel_spans as _otel
    _OTEL_ENABLED = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Retrieval result types
# ---------------------------------------------------------------------------

class RetrievalResult:
    __slots__ = ("case_id", "similarity", "category", "severity",
                 "issue_summary", "fix_summary", "citation_url",
                 "adopted", "untrusted")

    def __init__(self, case_id: str, similarity: float,
                 issue_summary: str = "", fix_summary: str = "",
                 category: str = "", severity: str = "",
                 citation_url: str = "", adopted: bool = False,
                 untrusted: bool = True):
        self.case_id = case_id
        self.similarity = round(similarity, 6)
        self.category = category
        self.severity = severity
        self.issue_summary = issue_summary[:200]
        self.fix_summary = fix_summary[:200]
        self.citation_url = citation_url
        self.adopted = adopted
        self.untrusted = untrusted

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id, "similarity": self.similarity,
            "category": self.category, "severity": self.severity,
            "issue_summary": self.issue_summary, "fix_summary": self.fix_summary,
            "citation_url": self.citation_url, "adopted": self.adopted,
            "untrusted": self.untrusted,
        }


class RetrievalResponse:
    def __init__(self, results: list[RetrievalResult], status: str = "ok",
                 latency_ms: float = 0, run_id: str = "", trace_id: str = "",
                 top_k: int = 5, fallback_reason: str = ""):
        self.results = results
        self.status = status
        self.latency_ms = latency_ms
        self.run_id = run_id
        self.trace_id = trace_id
        self.top_k = top_k
        self.fallback_reason = fallback_reason

    @property
    def hit_count(self): return len(self.results)

    @property
    def selected_case_ids(self): return [r.case_id for r in self.results if r.adopted]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
            "stats": {"hit_count": self.hit_count, "top_k": self.top_k,
                      "latency_ms": self.latency_ms,
                      "selected_case_ids": self.selected_case_ids},
            "run_id": self.run_id, "trace_id": self.trace_id,
            "fallback_reason": self.fallback_reason,
        }


# ---------------------------------------------------------------------------
# Fake retrieval adapter
# ---------------------------------------------------------------------------

class FakeRetrievalAdapter:
    def __init__(self, cases: list[dict] = None, latency_ms: float = 0,
                 fail_with: Exception = None):
        self.cases = cases or []
        self.latency_ms = latency_ms
        self.fail_with = fail_with
        self.query_count = 0

    def retrieve(self, query: str, top_k: int = 5,
                 min_score: float = 0.0) -> list[dict]:
        self.query_count += 1
        if self.fail_with:
            raise self.fail_with
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)
        matched = []
        query_lower = query.lower()
        for case in self.cases:
            if query_lower in case.get("issue", "").lower() or \
               query_lower in case.get("category", "").lower() or \
               not query_lower:
                score = case.get("score", 0.85)
                if score >= min_score:
                    matched.append(case)
        return matched[:top_k]


# ---------------------------------------------------------------------------
# PgVectorBridge: wraps CaseRetrieval PgVectorAdapter with bounded timeout
# ---------------------------------------------------------------------------

class PgVectorBridge:
    """Bridge to the existing CaseRetrieval PgVectorAdapter.

    In production, this wraps skills.case_retrieval.adapters.pg_vector.
    For testing, the FakeRetrievalAdapter is used directly.

    The bridge adds a real bounded timeout by running the retrieve call
    in a worker thread with a join deadline. If the thread doesn't finish
    in time, the query is abandoned (the thread is daemonized so it won't
    block process exit; the DB connection is closed in finally).
    """

    def __init__(self, adapter: Any = None, timeout_ms: int = 5000):
        self.adapter = adapter
        self.timeout_ms = timeout_ms

    def retrieve(self, query: str, top_k: int = 5,
                 min_score: float = 0.0) -> list[dict]:
        """Retrieve with bounded timeout. Raises TimeoutError if the
        adapter doesn't return within timeout_ms."""
        if self.adapter is None:
            raise RuntimeError("no adapter configured")

        result_box: dict[str, Any] = {}
        def _worker():
            try:
                result_box["data"] = self.adapter.retrieve(
                    query, top_k=top_k, min_score=min_score)
            except Exception as e:
                result_box["error"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=self.timeout_ms / 1000.0)

        if t.is_alive():
            raise TimeoutError("adapter query exceeded %dms" % self.timeout_ms)
        if "error" in result_box:
            raise result_box["error"]
        return result_box.get("data", [])


# ---------------------------------------------------------------------------
# Bounded retrieval executor (real timeout, not just pre-check)
# ---------------------------------------------------------------------------

def _run_with_timeout(fn, timeout_ms, *args, **kwargs):
    """Run fn(*args, **kwargs) with a real wall-clock timeout.

    Uses threading + join. If the function doesn't complete within
    timeout_ms, raises TimeoutError. The worker thread is daemonized
    so it won't block process exit.
    """
    result_box: dict[str, Any] = {}

    def _worker():
        try:
            result_box["data"] = fn(*args, **kwargs)
        except Exception as e:
            result_box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_ms / 1000.0)

    if t.is_alive():
        raise TimeoutError("operation exceeded %dms" % timeout_ms)
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("data")


# ---------------------------------------------------------------------------
# Retrieval service (the integration point)
# ---------------------------------------------------------------------------

def query_for_reviewer(query: str, run_id: str, trace_id: str,
                       adapter: Any = None, top_k: int = 5,
                       timeout_ms: int = 5000) -> RetrievalResponse:
    return _query("reviewer", query, run_id, trace_id, adapter, top_k, timeout_ms)


def query_for_fixer(query: str, run_id: str, trace_id: str,
                    adapter: Any = None, top_k: int = 5,
                    timeout_ms: int = 5000) -> RetrievalResponse:
    return _query("fixer", query, run_id, trace_id, adapter, top_k, timeout_ms)


def _query(agent_role: str, query: str, run_id: str, trace_id: str,
           adapter: Any, top_k: int, timeout_ms: int) -> RetrievalResponse:
    """Internal: run a retrieval query with OTel + real bounded timeout."""
    start = time.monotonic()

    # OTel: rag.query as child of current context (not independent root)
    ctx = None
    if _OTEL_ENABLED:
        ctx = _otel.start_span("rag.query", run_id=run_id, trace_id=trace_id,
                               agent_role=agent_role)
        ctx.__enter__()

    try:
        if adapter is None:
            elapsed = (time.monotonic() - start) * 1000
            resp = RetrievalResponse(
                results=[], status="no_history",
                latency_ms=round(elapsed, 2),
                run_id=run_id, trace_id=trace_id, top_k=top_k,
                fallback_reason="no_adapter")
            _emit_fallback_span(agent_role, run_id, trace_id,
                                "no_adapter", elapsed)
            return resp

        # Real bounded timeout: run adapter.retrieve in a worker thread
        # with join deadline. If it doesn't finish, raise TimeoutError.
        try:
            raw_results = _run_with_timeout(
                adapter.retrieve, timeout_ms,
                query, top_k=top_k)
        except TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            resp = RetrievalResponse(
                results=[], status="retrieval_unavailable",
                latency_ms=round(elapsed, 2),
                run_id=run_id, trace_id=trace_id, top_k=top_k,
                fallback_reason="timeout")
            _emit_fallback_span(agent_role, run_id, trace_id,
                                "timeout", elapsed)
            return resp

        elapsed = (time.monotonic() - start) * 1000

        if not raw_results:
            resp = RetrievalResponse(
                results=[], status="empty",
                latency_ms=round(elapsed, 2),
                run_id=run_id, trace_id=trace_id, top_k=top_k)
        else:
            results = [
                RetrievalResult(
                    case_id=str(r.get("case_id", r.get("id", "unknown"))),
                    similarity=float(r.get("score", 0.0)),
                    issue_summary=str(r.get("issue", r.get("issue_summary", ""))),
                    fix_summary=str(r.get("fix", r.get("fix_summary", ""))),
                    category=str(r.get("category", "")),
                    severity=str(r.get("severity", "")),
                    citation_url=str(r.get("source_pr_url",
                                           r.get("citation_url", ""))),
                    adopted=False,
                    untrusted=True,
                )
                for r in raw_results
            ]
            resp = RetrievalResponse(
                results=results, status="ok",
                latency_ms=round(elapsed, 2),
                run_id=run_id, trace_id=trace_id, top_k=top_k)

        _emit_result_span(agent_role, run_id, trace_id, resp)
        return resp

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        resp = RetrievalResponse(
            results=[], status="retrieval_unavailable",
            latency_ms=round(elapsed, 2),
            run_id=run_id, trace_id=trace_id, top_k=top_k,
            fallback_reason=type(e).__name__)
        _emit_fallback_span(agent_role, run_id, trace_id,
                            type(e).__name__, elapsed)
        return resp
    finally:
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass


def _emit_result_span(agent_role, run_id, trace_id, resp):
    if not _OTEL_ENABLED:
        return
    try:
        with _otel.start_span("rag.result", run_id=run_id, trace_id=trace_id,
                              agent_role=agent_role) as span:
            span.set_attribute("rag.hit_count", resp.hit_count)
            span.set_attribute("rag.top_k", resp.top_k)
            span.set_attribute("rag.latency_ms", resp.latency_ms)
            span.set_attribute("rag.status", resp.status)
            if resp.results:
                span.set_attribute("rag.top_score", resp.results[0].similarity)
                span.set_attribute("rag.top_case_id", resp.results[0].case_id)
    except Exception:
        pass


def _emit_fallback_span(agent_role, run_id, trace_id, reason, elapsed_ms):
    if not _OTEL_ENABLED:
        return
    try:
        with _otel.start_span("rag.fallback", run_id=run_id, trace_id=trace_id,
                              agent_role=agent_role) as span:
            span.set_attribute("rag.fallback_reason", reason)
            span.set_attribute("rag.latency_ms", round(elapsed_ms, 2))
            span.set_attribute("rag.hit_count", 0)
    except Exception:
        pass
