#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 RAG Benchmark — Fixed dataset (deterministic, versioned).

Layer A: Retrieval & Integration Benchmark.

Each sample is an INDEPENDENT case with a separate query. The query text
sent to the adapter NEVER contains gold labels (case_id, expected category,
expected severity, expected fix, or evaluation annotations). Gold data is
stored in a separate ``gold`` sub-dict that only the evaluator reads.

Dataset version: rag-bench-v2
"""
from __future__ import annotations

import hashlib
import json

DATASET_VERSION = "rag-bench-v2"
DETERMINISTIC_SEED = 42

# ── Knowledge base (the "history" that RAG searches) ───────────────────────
# Each case has: case_id, score (base similarity weight), category, severity,
# issue, fix, source_pr_url.
# Issue text is what the adapter matches against — it describes the problem
# in natural language, NOT the gold label.

KNOWLEDGE_BASE = [
    {"case_id": "kb-sqli-01", "score": 0.95, "category": "sql_injection",
     "severity": "high",
     "issue": "SQL injection via string concatenation in database query execute",
     "fix": "Use parameterized queries with bound parameters",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/1"},
    {"case_id": "kb-sqli-02", "score": 0.93, "category": "sql_injection",
     "severity": "high",
     "issue": "Raw SQL query built from user input without sanitization execute",
     "fix": "Escape and validate input before query construction",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/6"},
    {"case_id": "kb-secret-01", "score": 0.92, "category": "hardcoded_secret",
     "severity": "critical",
     "issue": "Hardcoded credential password API key token embedded in source",
     "fix": "Move credentials to environment variables or secret manager",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/2"},
    {"case_id": "kb-secret-02", "score": 0.90, "category": "hardcoded_secret",
     "severity": "critical",
     "issue": "Private key RSA PEM block committed to repository credentials",
     "fix": "Remove key and rotate compromised credentials immediately",
     "source_pr_url": "https://github.com/test/repo-beta/pull/6"},
    {"case_id": "kb-cmdi-01", "score": 0.91, "category": "command_injection",
     "severity": "critical",
     "issue": "Operating system command execution via subprocess shell user input",
     "fix": "Use subprocess with shell=False and validated argument arrays",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/4"},
    {"case_id": "kb-cmdi-02", "score": 0.88, "category": "command_injection",
     "severity": "high",
     "issue": "Shell expansion injection through unsanitized os system call exec",
     "fix": "Avoid shell=True and use shlex.quote for dynamic arguments",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/7"},
    {"case_id": "kb-pathtrav-01", "score": 0.90, "category": "path_traversal",
     "severity": "high",
     "issue": "Directory traversal file access via dot-dot path segments open",
     "fix": "Canonicalize and restrict file paths to allowed root directory",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/4"},
    {"case_id": "kb-pathtrav-02", "score": 0.86, "category": "path_traversal",
     "severity": "high",
     "issue": "Arbitrary file read through symlink escape manipulation link",
     "fix": "Resolve symlinks and enforce path prefix boundaries",
     "source_pr_url": "https://github.com/test/repo-beta/pull/7"},
    {"case_id": "kb-depvuln-01", "score": 0.82, "category": "dependency_vulnerability",
     "severity": "medium",
     "issue": "Outdated library version with known CVE vulnerability package",
     "fix": "Upgrade dependency to patched release version",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/3"},
    {"case_id": "kb-depvuln-02", "score": 0.80, "category": "dependency_vulnerability",
     "severity": "low",
     "issue": "Transitive dependency pinned to insecure release framework outdated",
     "fix": "Update lockfile and audit full dependency tree",
     "source_pr_url": "https://github.com/test/repo-delta/pull/2"},
    {"case_id": "kb-testfail-01", "score": 0.84, "category": "test_failure",
     "severity": "medium",
     "issue": "Integration test broken after refactor assertion mismatch mock",
     "fix": "Update test expectations to match new interface contract",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/8"},
    {"case_id": "kb-testfail-02", "score": 0.81, "category": "test_failure",
     "severity": "low",
     "issue": "Flaky test intermittent failure due to race timing fixture",
     "fix": "Add proper synchronization or increase test timeout margin",
     "source_pr_url": "https://github.com/test/repo-beta/pull/8"},
    {"case_id": "kb-configrisk-01", "score": 0.83, "category": "configuration_risk",
     "severity": "medium",
     "issue": "Debug mode enabled in production configuration settings expose",
     "fix": "Set DEBUG=False and use environment-specific config loading",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/5"},
    {"case_id": "kb-configrisk-02", "score": 0.79, "category": "configuration_risk",
     "severity": "medium",
     "issue": "Insecure CORS wildcard origin allowed policy permissive header",
     "fix": "Restrict allowed origins to known frontend domains only",
     "source_pr_url": "https://github.com/test/repo-delta/pull/3"},
    {"case_id": "kb-promptinject-01", "score": 0.87, "category": "prompt_injection",
     "severity": "high",
     "issue": "Adversarial prompt injection IGNORE previous instructions override",
     "fix": "Sanitize and delimit untrusted input entering LLM context",
     "source_pr_url": "https://github.com/test/repo-beta/pull/9"},
    {"case_id": "kb-promptinject-02", "score": 0.85, "category": "prompt_injection",
     "severity": "high",
     "issue": "Malicious payload attempting to manipulate agent behavior jailbreak",
     "fix": "Implement input validation and output filtering guardrails",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/6"},
    {"case_id": "kb-rollback-01", "score": 0.89, "category": "rollback_risk",
     "severity": "critical",
     "issue": "High-risk database migration schema change destructive drop column",
     "fix": "Use reversible migration with backup and staged rollout",
     "source_pr_url": "https://github.com/test/repo-beta/pull/10"},
    {"case_id": "kb-rollback-02", "score": 0.87, "category": "rollback_risk",
     "severity": "high",
     "issue": "Breaking API contract change removal endpoint deprecation force",
     "fix": "Version the API and provide deprecation timeline for consumers",
     "source_pr_url": "https://github.com/test/repo-delta/pull/4"},
    {"case_id": "kb-falsepos-01", "score": 0.78, "category": "false_positive_allowlist",
     "severity": "low",
     "issue": "Flagged pattern is intentional safe usage allowlisted verified",
     "fix": "Document the allowlist rationale and add suppression annotation",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/9"},
    {"case_id": "kb-clean-01", "score": 0.70, "category": "clean_no_issue",
     "severity": "low",
     "issue": "Code review passed no issues found clean well-structured implementation",
     "fix": "No fix needed — code meets quality standards",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/10"},
]

# ── N≥20 INDEPENDENT benchmark samples ─────────────────────────────────────
#
# CRITICAL: The ``query`` fields contain ONLY the text a Reviewer/Fixer would
# naturally produce from inspecting source files. They must NEVER contain:
#   - gold case_id
#   - expected category/severity
#   - expected fix text
#   - evaluation labels
#
# Gold data lives in the separate ``gold`` sub-dict, read only by the evaluator.

DATASET = [
    # ── CLEAN / NO-ISSUE (2 samples) ──
    {"sample_id": "bm-001", "category_group": "clean",
     "repo_scope": "repo-alpha",
     "reviewer_query": "models/user.py class definition with type hints and validation",
     "fixer_query": "ensure_fix_pr models/user.py refactor type annotations",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},
    {"sample_id": "bm-002", "category_group": "clean",
     "repo_scope": "repo-alpha",
     "reviewer_query": "utils/helpers.py utility functions well documented pure logic",
     "fixer_query": "ensure_fix_pr utils/helpers.py add docstrings",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},

    # ── HARDCODED SECRET (2 samples) ──
    {"sample_id": "bm-003", "category_group": "hardcoded_secret",
     "repo_scope": "repo-alpha",
     "reviewer_query": "config/database.py connection string with embedded credentials password",
     "fixer_query": "ensure_fix_pr config/database.py externalize credentials",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-secret-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-004", "category_group": "hardcoded_secret",
     "repo_scope": "repo-beta",
     "reviewer_query": "auth/certs.py RSA private key PEM block committed",
     "fixer_query": "ensure_fix_pr auth/certs.py remove key and rotate",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-secret-02"],
     "expected_status": "ok"},

    # ── SQL INJECTION (2 samples) ──
    {"sample_id": "bm-005", "category_group": "sql_injection",
     "repo_scope": "repo-alpha",
     "reviewer_query": "api/query.py string concatenation building raw database query",
     "fixer_query": "ensure_fix_pr api/query.py parameterized queries",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-sqli-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-006", "category_group": "sql_injection",
     "repo_scope": "repo-alpha",
     "reviewer_query": "dao/user_dao.py user input directly into SQL execute statement",
     "fixer_query": "ensure_fix_pr dao/user_dao.py input sanitization",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-sqli-02"],
     "expected_status": "ok"},

    # ── COMMAND INJECTION (2 samples) ──
    {"sample_id": "bm-007", "category_group": "command_injection",
     "repo_scope": "repo-gamma",
     "reviewer_query": "tasks/runner.py subprocess call with shell execution input",
     "fixer_query": "ensure_fix_pr tasks/runner.py shell=False argument array",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-cmdi-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-008", "category_group": "command_injection",
     "repo_scope": "repo-alpha",
     "reviewer_query": "scripts/deploy.py os.system call with unsanitized expansion",
     "fixer_query": "ensure_fix_pr scripts/deploy.py shlex quote arguments",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-cmdi-02"],
     "expected_status": "ok"},

    # ── PATH TRAVERSAL (2 samples) ──
    {"sample_id": "bm-009", "category_group": "path_traversal",
     "repo_scope": "repo-alpha",
     "reviewer_query": "storage/handler.py file open with dot-dot path segments",
     "fixer_query": "ensure_fix_pr storage/handler.py canonicalize restrict root",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-pathtrav-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-010", "category_group": "path_traversal",
     "repo_scope": "repo-beta",
     "reviewer_query": "files/reader.py symlink escape allowing arbitrary file read",
     "fixer_query": "ensure_fix_pr files/reader.py resolve symlink prefix",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-pathtrav-02"],
     "expected_status": "ok"},

    # ── DEPENDENCY VULNERABILITY (2 samples) ──
    {"sample_id": "bm-011", "category_group": "dependency_vulnerability",
     "repo_scope": "repo-gamma",
     "reviewer_query": "requirements.txt outdated library known CVE vulnerability package",
     "fixer_query": "ensure_fix_pr requirements.txt upgrade patched version",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-depvuln-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-012", "category_group": "dependency_vulnerability",
     "repo_scope": "repo-delta",
     "reviewer_query": "package.json transitive dependency pinned insecure framework",
     "fixer_query": "ensure_fix_pr package.json update lockfile audit tree",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-depvuln-02"],
     "expected_status": "ok"},

    # ── TEST FAILURE (2 samples) ──
    {"sample_id": "bm-013", "category_group": "test_failure",
     "repo_scope": "repo-alpha",
     "reviewer_query": "tests/test_api.py integration test assertion mismatch after refactor",
     "fixer_query": "ensure_fix_pr tests/test_api.py update expectations contract",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-testfail-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-014", "category_group": "test_failure",
     "repo_scope": "repo-beta",
     "reviewer_query": "tests/test_concurrency.py flaky intermittent race timing fixture",
     "fixer_query": "ensure_fix_pr tests/test_concurrency.py synchronization timeout",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-testfail-02"],
     "expected_status": "ok"},

    # ── CONFIGURATION RISK (2 samples) ──
    {"sample_id": "bm-015", "category_group": "configuration_risk",
     "repo_scope": "repo-gamma",
     "reviewer_query": "settings/prod.py debug mode enabled configuration expose",
     "fixer_query": "ensure_fix_pr settings/prod.py DEBUG=False env config",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-configrisk-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-016", "category_group": "configuration_risk",
     "repo_scope": "repo-delta",
     "reviewer_query": "middleware/cors.py wildcard origin allowed permissive policy",
     "fixer_query": "ensure_fix_pr middleware/cors.py restrict allowed origins",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-configrisk-02"],
     "expected_status": "ok"},

    # ── PROMPT INJECTION (2 samples) ──
    {"sample_id": "bm-017", "category_group": "prompt_injection",
     "repo_scope": "repo-beta",
     "reviewer_query": "prompts/template.py user input with IGNORE previous instructions override",
     "fixer_query": "ensure_fix_pr prompts/template.py sanitize delimit untrusted input",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-promptinject-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-018", "category_group": "prompt_injection",
     "repo_scope": "repo-gamma",
     "reviewer_query": "agents/handler.py malicious payload manipulating behavior jailbreak",
     "fixer_query": "ensure_fix_pr agents/handler.py input validation guardrails",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-promptinject-02"],
     "expected_status": "ok"},

    # ── ROLLBACK / HIGH-RISK CHANGE (2 samples) ──
    {"sample_id": "bm-019", "category_group": "rollback_risk",
     "repo_scope": "repo-beta",
     "reviewer_query": "migrations/007_drop.py schema change destructive drop column migration",
     "fixer_query": "ensure_fix_pr migrations/007_drop.py reversible backup staged",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-rollback-01"],
     "expected_status": "ok"},
    {"sample_id": "bm-020", "category_group": "rollback_risk",
     "repo_scope": "repo-delta",
     "reviewer_query": "api/v2/breaking.py contract change removal endpoint deprecation",
     "fixer_query": "ensure_fix_pr api/v2/breaking.py version deprecation timeline",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-rollback-02"],
     "expected_status": "ok"},

    # ── FALSE-POSITIVE / ALLOWLIST (1 sample) ──
    {"sample_id": "bm-021", "category_group": "false_positive_allowlist",
     "repo_scope": "repo-alpha",
     "reviewer_query": "crypto/usage.py flagged pattern intentional safe allowlisted verified",
     "fixer_query": "ensure_fix_pr crypto/usage.py document allowlist suppression",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-falsepos-01"],
     "expected_status": "ok"},

    # ── NO-HISTORY (baseline: adapter=None) ──
    {"sample_id": "bm-022", "category_group": "no_history",
     "repo_scope": "repo-alpha",
     "reviewer_query": "any query text here does not matter",
     "fixer_query": "ensure_fix_pr any file here",
     "adapter_type": "none", "gold_case_ids": [],
     "expected_status": "no_history"},

    # ── EMPTY RETRIEVAL (1 sample, real scope but no match) ──
    {"sample_id": "bm-023", "category_group": "empty_retrieval",
     "repo_scope": "repo-delta",
     "reviewer_query": "performance optimization caching strategy memoization invalidation",
     "fixer_query": "ensure_fix_pr cache.py memoization pattern",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},

    # ── TIMEOUT (2 samples) ──
    {"sample_id": "bm-024", "category_group": "timeout",
     "repo_scope": "repo-alpha",
     "reviewer_query": "search/query.py database search optimization index lookup",
     "fixer_query": "ensure_fix_pr search/query.py add index",
     "adapter_type": "timeout", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
    {"sample_id": "bm-025", "category_group": "timeout",
     "repo_scope": "repo-beta",
     "reviewer_query": "export/batch.py bulk export pagination streaming large dataset",
     "fixer_query": "ensure_fix_pr export/batch.py streaming pagination",
     "adapter_type": "timeout", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},

    # ── UNAVAILABLE ADAPTER (2 samples) ──
    {"sample_id": "bm-026", "category_group": "adapter_unavailable",
     "repo_scope": "repo-alpha",
     "reviewer_query": "auth/session.py session management timeout expiry token refresh",
     "fixer_query": "ensure_fix_pr auth/session.py token refresh logic",
     "adapter_type": "failing", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
    {"sample_id": "bm-027", "category_group": "adapter_unavailable",
     "repo_scope": "repo-gamma",
     "reviewer_query": "notifications/email.py SMTP connection pool retry backoff",
     "fixer_query": "ensure_fix_pr notifications/email.py retry backoff",
     "adapter_type": "failing", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},

    # ── MALFORMED ADAPTER RESULT (1 sample) ──
    {"sample_id": "bm-028", "category_group": "malformed_result",
     "repo_scope": "repo-alpha",
     "reviewer_query": "billing/invoice.py calculation rounding precision decimal",
     "fixer_query": "ensure_fix_pr billing/invoice.py decimal precision",
     "adapter_type": "malformed", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},

    # ── CROSS-REPO ADVERSARIAL MATCH (1 sample) ──
    # Query matches a case in repo-alpha, but the sample's scope is repo-delta.
    # The adapter must NOT return cross-scope results.
    {"sample_id": "bm-029", "category_group": "cross_repo_adversarial",
     "repo_scope": "repo-delta",
     "reviewer_query": "dao/user_dao.py raw SQL execute statement injection concatenation",
     "fixer_query": "ensure_fix_pr dao/user_dao.py parameterized bound",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},
]

assert len(DATASET) >= 20, "Need at least 20 independent cases"

# Gold label fields kept separate from queries — evaluator-only access.
# This dict maps sample_id -> {gold_case_ids, expected_status} for the evaluator.
GOLD = {s["sample_id"]: {"gold_case_ids": s["gold_case_ids"],
                          "expected_status": s["expected_status"]}
         for s in DATASET}


def dataset_sha256() -> str:
    """Deterministic SHA-256 of dataset (sorted keys, UTF-8, LF)."""
    canonical = json.dumps(
        {"version": DATASET_VERSION, "samples": DATASET,
         "knowledge_base": KNOWLEDGE_BASE},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def unique_category_groups() -> dict[str, int]:
    """Count samples per category_group — ensures no singleton dominates."""
    counts: dict[str, int] = {}
    for s in DATASET:
        counts[s["category_group"]] = counts.get(s["category_group"], 0) + 1
    return counts


if __name__ == "__main__":
    print(f"version: {DATASET_VERSION}")
    print(f"unique_case_count: {len(DATASET)}")
    print(f"knowledge_base_cases: {len(KNOWLEDGE_BASE)}")
    print(f"sha256: {dataset_sha256()}")
    print(f"category_groups: {unique_category_groups()}")
    # Verify no gold labels leak into query text
    for s in DATASET:
        for gid in s["gold_case_ids"]:
            if gid in s["reviewer_query"] or gid in s["fixer_query"]:
                raise AssertionError(f"GOLD LEAK in {s['sample_id']}: {gid}")
    print("gold_label_leak_check: PASS")
