#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 RAG Confirmatory Benchmark — HELD-OUT dataset (v3, pre-registered).

This dataset is SEPARATE from rag-bench-v2 and was NOT used during
development calibration. It is frozen BEFORE any quality thresholds are
checked against it.

Separation guarantees:
  - All case_ids use prefix 'kb-ho-' (v2 uses 'kb-')
  - All sample_ids use prefix 'ho-' (v2 uses 'bm-')
  - All issue text and queries are independently authored
  - No query text or issue text is reused from v2
  - Knowledge base uses same repo_scopes (for consistent scope filtering)
    but different issue descriptions

Dataset version: rag-bench-v3-heldout
Pre-registered: BEFORE running the confirmatory benchmark.

CRITICAL: Gold labels (gold_case_ids, expected_status) are stored in a
separate GOLD dict. Queries NEVER contain gold case_ids or evaluation
labels. Gold data is read ONLY by the evaluator.
"""
from __future__ import annotations

import hashlib
import json

DATASET_VERSION = "rag-bench-v3-heldout"
DETERMINISTIC_SEED = 99  # different seed from v2 (42)

# ── HELD-OUT Knowledge base (separate from v2) ─────────────────────────────
# Each case is independently authored with different wording from v2.

KNOWLEDGE_BASE_HELDOUT = [
    {"case_id": "kb-ho-sqli-01", "score": 0.94, "category": "sql_injection",
     "severity": "high",
     "issue": "Unsanitized user input concatenated into dynamic SQL statement execution",
     "fix": "Replace dynamic query construction with ORM or prepared statements",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/11"},
    {"case_id": "kb-ho-sqli-02", "score": 0.91, "category": "sql_injection",
     "severity": "high",
     "issue": "ORM raw query bypass with f-string formatting vulnerability database",
     "fix": "Use parameterized raw queries with explicit cursor binding",
     "source_pr_url": "https://github.com/test/repo-beta/pull/11"},
    {"case_id": "kb-ho-secret-01", "score": 0.93, "category": "hardcoded_secret",
     "severity": "critical",
     "issue": "Cloud provider access key identifier and secret pair in configuration file",
     "fix": "Use IAM role assumption instead of static key credentials",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/7"},
    {"case_id": "kb-ho-secret-02", "score": 0.90, "category": "hardcoded_secret",
     "severity": "critical",
     "issue": "Database connection DSN with plaintext password in version controlled config",
     "fix": "Inject database credentials via secret manager at runtime",
     "source_pr_url": "https://github.com/test/repo-delta/pull/5"},
    {"case_id": "kb-ho-cmdi-01", "score": 0.92, "category": "command_injection",
     "severity": "critical",
     "issue": "Untrusted data passed to eval builtin executing arbitrary Python expressions",
     "fix": "Replace eval with ast.literal_eval or dedicated parser",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/12"},
    {"case_id": "kb-ho-cmdi-02", "score": 0.88, "category": "command_injection",
     "severity": "high",
     "issue": "Popen invoked with shell=True and user-controlled string interpolation",
     "fix": "Pass argument list with shell=False to subprocess",
     "source_pr_url": "https://github.com/test/repo-beta/pull/12"},
    {"case_id": "kb-ho-pathtrav-01", "score": 0.89, "category": "path_traversal",
     "severity": "high",
     "issue": "Archive extraction without validating entry names allows zip slip directory escape",
     "fix": "Validate that extracted paths stay within target directory boundary",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/8"},
    {"case_id": "kb-ho-pathtrav-02", "score": 0.85, "category": "path_traversal",
     "severity": "medium",
     "issue": "Template engine allows including files outside web root via relative path",
     "fix": "Restrict template includes to sandboxed allowlist",
     "source_pr_url": "https://github.com/test/repo-delta/pull/6"},
    {"case_id": "kb-ho-depvuln-01", "score": 0.83, "category": "dependency_vulnerability",
     "severity": "high",
     "issue": "Pinned crypto library has remote code execution exploit in legacy version",
     "fix": "Bump to security patch release and verify compatibility",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/13"},
    {"case_id": "kb-ho-depvuln-02", "score": 0.80, "category": "dependency_vulnerability",
     "severity": "medium",
     "issue": "Web framework middleware with known ReDoS vulnerability regular expression",
     "fix": "Upgrade framework to version with patched regex engine",
     "source_pr_url": "https://github.com/test/repo-beta/pull/13"},
    {"case_id": "kb-ho-testfail-01", "score": 0.84, "category": "test_failure",
     "severity": "medium",
     "issue": "Unit test assumes deterministic ordering but set iteration is non-deterministic",
     "fix": "Convert set to sorted list before assertion comparison",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/9"},
    {"case_id": "kb-ho-testfail-02", "score": 0.81, "category": "test_failure",
     "severity": "low",
     "issue": "Test depends on external API response that changes shape between environments",
     "fix": "Mock the external API with contract-based fixture",
     "source_pr_url": "https://github.com/test/repo-delta/pull/7"},
    {"case_id": "kb-ho-configrisk-01", "score": 0.82, "category": "configuration_risk",
     "severity": "high",
     "issue": "Secret key set to default placeholder value in production deployment template",
     "fix": "Require explicit secret key via environment with no default fallback",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/14"},
    {"case_id": "kb-ho-configrisk-02", "score": 0.79, "category": "configuration_risk",
     "severity": "medium",
     "issue": "Rate limiting disabled in staging config leaking into production override",
     "fix": "Enforce rate limit minimums via config validation schema",
     "source_pr_url": "https://github.com/test/repo-beta/pull/14"},
    {"case_id": "kb-ho-promptinject-01", "score": 0.86, "category": "prompt_injection",
     "severity": "high",
     "issue": "Tool-using agent processes untrusted markdown with embedded system-level directives",
     "fix": "Strip formatting directives from untrusted input before LLM context",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/10"},
    {"case_id": "kb-ho-promptinject-02", "score": 0.84, "category": "prompt_injection",
     "severity": "medium",
     "issue": "Chatbot template concatenates user message directly into system prompt construction",
     "fix": "Use delimited message roles and never concatenate into system prompt",
     "source_pr_url": "https://github.com/test/repo-delta/pull/8"},
    {"case_id": "kb-ho-rollback-01", "score": 0.87, "category": "rollback_risk",
     "severity": "critical",
     "issue": "Bulk data migration renames critical column without backward-compatible aliasing",
     "fix": "Add deprecated alias column and dual-write during migration window",
     "source_pr_url": "https://github.com/test/repo-alpha/pull/15"},
    {"case_id": "kb-ho-rollback-02", "score": 0.85, "category": "rollback_risk",
     "severity": "high",
     "issue": "Infrastructure template destroys and recreates stateful resource on every deploy",
     "fix": "Use lifecycle rules to prevent destructive resource replacement",
     "source_pr_url": "https://github.com/test/repo-beta/pull/15"},
    {"case_id": "kb-ho-falsepos-01", "score": 0.78, "category": "false_positive_allowlist",
     "severity": "low",
     "issue": "Security scanner flags test fixture data that mimics credential patterns intentionally",
     "fix": "Add scanner suppression comment with documented justification",
     "source_pr_url": "https://github.com/test/repo-gamma/pull/11"},
    {"case_id": "kb-ho-clean-01", "score": 0.71, "category": "clean_no_issue",
     "severity": "low",
     "issue": "Well-structured module with proper error handling and comprehensive test coverage",
     "fix": "No remediation required — code follows best practices",
     "source_pr_url": "https://github.com/test/repo-delta/pull/9"},
]

# ── HELD-OUT N=24 benchmark samples ────────────────────────────────────────
# Cohort distribution:
#   positive_retrieval: 14
#   abstention:          5
#   fault_injection:     5
#   Total:              24 (≥20)

DATASET_HELDOUT = [
    # ── POSITIVE RETRIEVAL (14 samples) ──
    {"sample_id": "ho-001", "category_group": "sql_injection",
     "repo_scope": "repo-alpha",
     "reviewer_query": "dao/user_repository.py dynamic SQL statement concatenation user input",
     "fixer_query": "ensure_fix_pr dao/user_repository.py prepared statement ORM",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-sqli-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-002", "category_group": "sql_injection",
     "repo_scope": "repo-beta",
     "reviewer_query": "models/query_builder.py ORM raw query f-string formatting vulnerability",
     "fixer_query": "ensure_fix_pr models/query_builder.py parameterized cursor binding",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-sqli-02"],
     "expected_status": "ok"},
    {"sample_id": "ho-003", "category_group": "hardcoded_secret",
     "repo_scope": "repo-gamma",
     "reviewer_query": "deploy/iam_config.py cloud access key identifier secret pair static",
     "fixer_query": "ensure_fix_pr deploy/iam_config.py IAM role assumption",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-secret-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-004", "category_group": "hardcoded_secret",
     "repo_scope": "repo-delta",
     "reviewer_query": "settings/database.py connection DSN plaintext password version controlled",
     "fixer_query": "ensure_fix_pr settings/database.py secret manager runtime injection",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-secret-02"],
     "expected_status": "ok"},
    {"sample_id": "ho-005", "category_group": "command_injection",
     "repo_scope": "repo-alpha",
     "reviewer_query": "utils/expression.py eval builtin executing arbitrary Python expressions",
     "fixer_query": "ensure_fix_pr utils/expression.py ast.literal_eval parser",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-cmdi-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-006", "category_group": "command_injection",
     "repo_scope": "repo-beta",
     "reviewer_query": "tasks/executor.py Popen shell=True string interpolation user data",
     "fixer_query": "ensure_fix_pr tasks/executor.py shell=False argument list",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-cmdi-02"],
     "expected_status": "ok"},
    {"sample_id": "ho-007", "category_group": "path_traversal",
     "repo_scope": "repo-gamma",
     "reviewer_query": "archives/extractor.py zip entry names validation directory escape slip",
     "fixer_query": "ensure_fix_pr archives/extractor.py target directory boundary validation",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-pathtrav-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-008", "category_group": "path_traversal",
     "repo_scope": "repo-delta",
     "reviewer_query": "views/template_include.py relative path outside web root template engine",
     "fixer_query": "ensure_fix_pr views/template_include.py sandboxed allowlist",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-pathtrav-02"],
     "expected_status": "ok"},
    {"sample_id": "ho-009", "category_group": "dependency_vulnerability",
     "repo_scope": "repo-alpha",
     "reviewer_query": "requirements.txt crypto library remote code execution legacy version pinned",
     "fixer_query": "ensure_fix_pr requirements.txt security patch bump compatibility",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-depvuln-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-010", "category_group": "dependency_vulnerability",
     "repo_scope": "repo-beta",
     "reviewer_query": "package.json web framework middleware ReDoS regular expression vulnerability",
     "fixer_query": "ensure_fix_pr package.json upgrade patched regex engine",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-depvuln-02"],
     "expected_status": "ok"},
    {"sample_id": "ho-011", "category_group": "test_failure",
     "repo_scope": "repo-gamma",
     "reviewer_query": "tests/test_ordering.py set iteration non-deterministic assertion ordering",
     "fixer_query": "ensure_fix_pr tests/test_ordering.py sorted list comparison",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-testfail-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-012", "category_group": "configuration_risk",
     "repo_scope": "repo-alpha",
     "reviewer_query": "deploy/production.yaml secret key default placeholder value template",
     "fixer_query": "ensure_fix_pr deploy/production.yaml explicit environment no fallback",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-configrisk-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-013", "category_group": "prompt_injection",
     "repo_scope": "repo-gamma",
     "reviewer_query": "agents/tool_runner.py untrusted markdown embedded system-level directives",
     "fixer_query": "ensure_fix_pr agents/tool_runner.py strip formatting directives",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-promptinject-01"],
     "expected_status": "ok"},
    {"sample_id": "ho-014", "category_group": "rollback_risk",
     "repo_scope": "repo-alpha",
     "reviewer_query": "migrations/012_rename.py bulk data renames critical column no aliasing",
     "fixer_query": "ensure_fix_pr migrations/012_rename.py deprecated alias dual-write window",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-rollback-01"],
     "expected_status": "ok"},

    # ── ABSTENTION (5 samples) ──
    {"sample_id": "ho-015", "category_group": "clean",
     "repo_scope": "repo-delta",
     "reviewer_query": "lib/error_handler.py proper error handling comprehensive test coverage",
     "fixer_query": "ensure_fix_pr lib/error_handler.py documentation improvement",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},
    {"sample_id": "ho-016", "category_group": "no_history",
     "repo_scope": "repo-alpha",
     "reviewer_query": "feature/new_module.py initial implementation scaffold structure",
     "fixer_query": "ensure_fix_pr feature/new_module.py add type hints",
     "adapter_type": "none", "gold_case_ids": [],
     "expected_status": "no_history"},
    {"sample_id": "ho-017", "category_group": "empty_retrieval",
     "repo_scope": "repo-beta",
     "reviewer_query": "docs/architecture.md documentation structure readability formatting",
     "fixer_query": "ensure_fix_pr docs/architecture.md table of contents",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},
    {"sample_id": "ho-018", "category_group": "cross_repo_adversarial",
     "repo_scope": "repo-delta",
     "reviewer_query": "dao/user_repository.py dynamic SQL statement concatenation user input",
     "fixer_query": "ensure_fix_pr dao/user_repository.py prepared statement",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},
    {"sample_id": "ho-018b", "category_group": "empty_retrieval",
     "repo_scope": "repo-gamma",
     "reviewer_query": "ui/component_library.css stylesheet responsive grid layout design",
     "fixer_query": "ensure_fix_pr ui/component_library.css flexbox breakpoints",
     "adapter_type": "token_overlap", "gold_case_ids": [],
     "expected_status": "empty"},
    {"sample_id": "ho-019", "category_group": "false_positive_allowlist",
     "repo_scope": "repo-gamma",
     "reviewer_query": "tests/fixtures/credentials.py test fixture data mimics credential patterns intentionally",
     "fixer_query": "ensure_fix_pr tests/fixtures/credentials.py scanner suppression",
     "adapter_type": "token_overlap", "gold_case_ids": ["kb-ho-falsepos-01"],
     "expected_status": "ok"},

    # ── FAULT INJECTION (5 samples) ──
    {"sample_id": "ho-020", "category_group": "timeout",
     "repo_scope": "repo-alpha",
     "reviewer_query": "services/search_service.py full-text search index rebuild slow query",
     "fixer_query": "ensure_fix_pr services/search_service.py incremental index update",
     "adapter_type": "timeout", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
    {"sample_id": "ho-021", "category_group": "timeout",
     "repo_scope": "repo-beta",
     "reviewer_query": "jobs/batch_export.py large dataset streaming pagination memory overflow",
     "fixer_query": "ensure_fix_pr jobs/batch_export.py chunked streaming",
     "adapter_type": "timeout", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
    {"sample_id": "ho-022", "category_group": "adapter_unavailable",
     "repo_scope": "repo-gamma",
     "reviewer_query": "integrations/payment_gateway.py retry logic exponential backoff circuit breaker",
     "fixer_query": "ensure_fix_pr integrations/payment_gateway.py circuit breaker pattern",
     "adapter_type": "failing", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
    {"sample_id": "ho-023", "category_group": "adapter_unavailable",
     "repo_scope": "repo-delta",
     "reviewer_query": "cache/distributed_store.py Redis cluster failover consistency guarantee",
     "fixer_query": "ensure_fix_pr cache/distributed_store.py eventual consistency model",
     "adapter_type": "failing", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
    {"sample_id": "ho-024", "category_group": "malformed_result",
     "repo_scope": "repo-alpha",
     "reviewer_query": "billing/calculation.py tax rate precision rounding decimal places",
     "fixer_query": "ensure_fix_pr billing/calculation.py decimal precision handling",
     "adapter_type": "malformed", "gold_case_ids": [],
     "expected_status": "retrieval_unavailable"},
]

assert len(DATASET_HELDOUT) >= 20, "Held-out dataset must have >= 20 cases"

# Gold labels — evaluator-only access
GOLD_HELDOUT = {s["sample_id"]: {"gold_case_ids": s["gold_case_ids"],
                                   "expected_status": s["expected_status"]}
                 for s in DATASET_HELDOUT}


def dataset_heldout_sha256() -> str:
    """Deterministic SHA-256 of held-out dataset."""
    canonical = json.dumps(
        {"version": DATASET_VERSION, "samples": DATASET_HELDOUT,
         "knowledge_base": KNOWLEDGE_BASE_HELDOUT},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def heldout_cohort_counts() -> dict:
    counts = {"positive_retrieval": 0, "abstention": 0, "fault_injection": 0}
    for s in DATASET_HELDOUT:
        if s["adapter_type"] in ("timeout", "failing", "malformed"):
            counts["fault_injection"] += 1
        elif s["adapter_type"] == "none":
            counts["abstention"] += 1
        elif s["adapter_type"] == "token_overlap":
            if s["gold_case_ids"]:
                counts["positive_retrieval"] += 1
            else:
                counts["abstention"] += 1
    return counts


def verify_separation_from_v2() -> list[str]:
    """Verify no overlap with v2 dataset. Returns list of violations."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from dataset import DATASET as V2_DATASET, KNOWLEDGE_BASE as V2_KB

    violations = []
    v2_case_ids = {c["case_id"] for c in V2_KB}
    v2_sample_ids = {s["sample_id"] for s in V2_DATASET}
    v2_queries = set()
    for s in V2_DATASET:
        v2_queries.add(s["reviewer_query"])
        v2_queries.add(s["fixer_query"])

    # Check case_id overlap
    for c in KNOWLEDGE_BASE_HELDOUT:
        if c["case_id"] in v2_case_ids:
            violations.append(f"case_id overlap: {c['case_id']}")

    # Check sample_id overlap
    for s in DATASET_HELDOUT:
        if s["sample_id"] in v2_sample_ids:
            violations.append(f"sample_id overlap: {s['sample_id']}")

    # Check query overlap
    for s in DATASET_HELDOUT:
        if s["reviewer_query"] in v2_queries:
            violations.append(f"reviewer_query overlap: {s['sample_id']}")
        if s["fixer_query"] in v2_queries:
            violations.append(f"fixer_query overlap: {s['sample_id']}")

    return violations


# ── PRE-REGISTERED QUALITY THRESHOLDS ──────────────────────────────────────
# Frozen BEFORE any execution against this dataset.
# These are confirmatory thresholds — the development calibration results
# must NOT be used to set these values. They represent minimum acceptable
# quality for a confirmatory pass.

PRE_REGISTERED_THRESHOLDS = {
    "min_hit_at_1": 0.70,          # ≥70% of positive_retrieval cases
    "min_hit_at_3": 0.80,          # ≥80%
    "min_mrr": 0.75,               # ≥0.75
    "min_top1_category_match_rate": 0.70,
    "min_top1_severity_match_rate": 0.70,
    "min_abstention_accuracy": 0.60,
    "max_scope_leak_count": 0,     # must be exactly 0
    "min_timeout_semantics_correct_rate": 1.0,
    "min_adapter_unavailable_fail_closed_rate": 1.0,
    "min_malformed_result_fail_closed_rate": 1.0,
    "min_fault_fallback_accuracy": 1.0,
    "require_deterministic_replay": True,
    "max_gold_label_leaks": 0,
    "max_secret_leaks": 0,
    "max_worker_thread_delta": 0,
    "max_temp_dir_residue": 0,
}


if __name__ == "__main__":
    print(f"version: {DATASET_VERSION}")
    print(f"seed: {DETERMINISTIC_SEED}")
    print(f"unique_case_count: {len(DATASET_HELDOUT)}")
    print(f"knowledge_base_cases: {len(KNOWLEDGE_BASE_HELDOUT)}")
    print(f"sha256: {dataset_heldout_sha256()}")
    print(f"cohorts: {heldout_cohort_counts()}")
    violations = verify_separation_from_v2()
    print(f"v2 separation violations: {len(violations)}")
    for v in violations:
        print(f"  VIOLATION: {v}")
    # Gold leak check
    for s in DATASET_HELDOUT:
        for gid in s["gold_case_ids"]:
            assert gid not in s["reviewer_query"], f"LEAK {s['sample_id']}"
            assert gid not in s["fixer_query"], f"LEAK {s['sample_id']}"
    print("gold_label_leak_check: PASS")
    print()
    print("=== PRE-REGISTERED THRESHOLDS (frozen before execution) ===")
    for k, v in PRE_REGISTERED_THRESHOLDS.items():
        print(f"  {k}: {v}")
